"""
База данных пользователей и промокодов — SQLite.

Таблицы:
  users — пользователи с планами и лимитами
  promo_codes — промокоды для рекламы
  promo_activations — активации промокодов пользователями
  support_messages — обращения в поддержку
  auth_attempts — попытки авторизации (для защиты от брутфорса)

Все публичные функции — синхронные.
Для вызова из async-кода используйте обёртку `run_sync()`.
"""

import asyncio
import sqlite3
import logging
import secrets
from datetime import datetime, date, timedelta
from functools import partial
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "users.db"


def _ensure_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


async def run_sync(func, *args, **kwargs):
    """Запускает синхронную DB-функцию в thread executor (не блокирует event loop)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def init_db() -> None:
    """Создаёт таблицы, если их нет."""
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                plan TEXT DEFAULT 'free',
                is_admin INTEGER DEFAULT 0,
                checks_today INTEGER DEFAULT 0,
                checks_total INTEGER DEFAULT 0,
                last_check_date TEXT,
                granted_by INTEGER,
                plan_expires TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                checks_per_use INTEGER DEFAULT 3,
                max_activations INTEGER DEFAULT 1,
                activations_count INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT
            );

            CREATE TABLE IF NOT EXISTS promo_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                checks_remaining INTEGER DEFAULT 3,
                activated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, user_id)
            );

            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                message TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_resolved INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS auth_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                success INTEGER DEFAULT 0,
                attempt_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                inn TEXT NOT NULL,
                company_name TEXT,
                last_data_hash TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, inn)
            );
        """)
        # Миграция: добавляем docs_access если ещё нет
        try:
            conn.execute("ALTER TABLE users ADD COLUMN docs_access INTEGER DEFAULT 0")
            conn.commit()
        except Exception:
            pass  # Колонка уже существует
        conn.commit()
        logger.info("Database initialized: %s", _DB_PATH)
    finally:
        conn.close()


# ─────────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────────

def get_user(user_id: int) -> dict[str, Any] | None:
    """Получить пользователя по ID."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ensure_user(user_id: int, username: str | None = None,
                first_name: str | None = None) -> dict[str, Any]:
    """Создаёт пользователя если нет, возвращает данные."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row:
            if username or first_name:
                conn.execute(
                    "UPDATE users SET username = COALESCE(?, username), "
                    "first_name = COALESCE(?, first_name), "
                    "updated_at = ? WHERE user_id = ?",
                    (username, first_name, _now(), user_id),
                )
                conn.commit()
            return dict(conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone())

        conn.execute(
            "INSERT INTO users (user_id, username, first_name, plan, "
            "is_admin, checks_today, checks_total, last_check_date, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'free', 0, 0, 0, ?, ?, ?)",
            (user_id, username, first_name, _today(), _now(), _now()),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone())
    finally:
        conn.close()


def increment_check(user_id: int) -> dict[str, Any]:
    """Увеличивает счётчик проверок. Сбрасывает daily если новый день. Атомарная операция."""
    conn = _connect()
    try:
        ensure_user(user_id)
        today = _today()
        # Атомарный UPDATE — без race condition
        conn.execute(
            "UPDATE users SET "
            "checks_today = CASE WHEN last_check_date = ? THEN checks_today + 1 ELSE 1 END, "
            "checks_total = checks_total + 1, "
            "last_check_date = ?, "
            "updated_at = ? "
            "WHERE user_id = ?",
            (today, today, _now(), user_id),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone())
    finally:
        conn.close()


def get_checks_today(user_id: int) -> int:
    """Возвращает кол-во проверок сегодня."""
    user = get_user(user_id)
    if not user:
        return 0
    if user["last_check_date"] != _today():
        return 0
    return user["checks_today"]


def set_plan(user_id: int, plan: str, granted_by: int | None = None,
             expires: str | None = None) -> None:
    """Устанавливает план пользователю."""
    conn = _connect()
    try:
        ensure_user(user_id)
        is_admin = 1 if plan == "admin" else 0
        conn.execute(
            "UPDATE users SET plan = ?, is_admin = ?, granted_by = ?, "
            "plan_expires = ?, updated_at = ? WHERE user_id = ?",
            (plan, is_admin, granted_by, expires, _now(), user_id),
        )
        conn.commit()
        logger.info("Plan %s set for user %s (by %s, expires %s)",
                     plan, user_id, granted_by, expires)
    finally:
        conn.close()


def set_admin(user_id: int) -> None:
    """Делает пользователя админом с полным доступом."""
    conn = _connect()
    try:
        ensure_user(user_id)
        conn.execute(
            "UPDATE users SET plan = 'admin', is_admin = 1, "
            "updated_at = ? WHERE user_id = ?",
            (_now(), user_id),
        )
        conn.commit()
        logger.info("Admin granted to user %s", user_id)
    finally:
        conn.close()


def revoke_plan(user_id: int) -> None:
    """Сбрасывает план на free."""
    set_plan(user_id, "free")


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь админом."""
    user = get_user(user_id)
    return bool(user and user.get("is_admin"))


def get_user_plan(user_id: int) -> str:
    """Возвращает план пользователя. Если истёк — сбрасывает на free."""
    user = get_user(user_id)
    if not user:
        return "free"
    plan = user.get("plan", "free")

    # Проверяем срок действия
    expires = user.get("plan_expires")
    if expires and plan not in ("free", "admin"):
        try:
            exp_date = datetime.fromisoformat(expires).date()
            if exp_date < date.today():
                revoke_plan(user_id)
                return "free"
        except (ValueError, TypeError):
            pass
    return plan


def get_all_users(limit: int = 100) -> list[dict[str, Any]]:
    """Все пользователи (для админ-панели)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY checks_total DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_count() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
    finally:
        conn.close()


def get_active_today_count() -> int:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE last_check_date = ?",
            (_today(),),
        ).fetchone()["cnt"]
    finally:
        conn.close()


def get_total_checks() -> int:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(checks_total), 0) as cnt FROM users"
        ).fetchone()["cnt"]
    finally:
        conn.close()


def get_plan_stats() -> dict[str, int]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT plan, COUNT(*) as cnt FROM users GROUP BY plan"
        ).fetchall()
        return {r["plan"]: r["cnt"] for r in rows}
    finally:
        conn.close()


# ─────────────────────────────────────────────────
# PROMO CODES
# ─────────────────────────────────────────────────

def generate_promo_codes(count: int = 20, checks_per_use: int = 3,
                         created_by: int | None = None,
                         expires_days: int = 90) -> list[str]:
    """Генерирует пакет промокодов. Возвращает список кодов."""
    conn = _connect()
    codes: list[str] = []
    expires = (datetime.now() + timedelta(days=expires_days)).isoformat()
    try:
        for _ in range(count):
            code = f"FRIDAY-{secrets.token_hex(3).upper()}"
            # Убедимся что код уникален
            while conn.execute(
                "SELECT 1 FROM promo_codes WHERE code = ?", (code,)
            ).fetchone():
                code = f"FRIDAY-{secrets.token_hex(3).upper()}"

            conn.execute(
                "INSERT INTO promo_codes (code, checks_per_use, max_activations, "
                "activations_count, created_by, created_at, expires_at) "
                "VALUES (?, ?, 1, 0, ?, ?, ?)",
                (code, checks_per_use, created_by, _now(), expires),
            )
            codes.append(code)
        conn.commit()
        logger.info("Generated %d promo codes", count)
        return codes
    finally:
        conn.close()


def activate_promo(code: str, user_id: int) -> dict[str, Any]:
    """
    Активирует промокод для пользователя.

    Возвращает dict:
      success: True/False
      error: текст ошибки (если False)
      checks: кол-во начисленных проверок (если True)
    """
    conn = _connect()
    try:
        # Проверяем код
        promo = conn.execute(
            "SELECT * FROM promo_codes WHERE code = ?", (code.upper().strip(),)
        ).fetchone()
        if not promo:
            return {"success": False, "error": "Промокод не найден"}

        promo = dict(promo)

        # Проверяем срок
        if promo.get("expires_at"):
            try:
                exp = datetime.fromisoformat(promo["expires_at"])
                if exp < datetime.now():
                    return {"success": False, "error": "Промокод истёк"}
            except (ValueError, TypeError):
                pass

        # Проверяем лимит активаций
        if promo["activations_count"] >= promo["max_activations"]:
            return {"success": False, "error": "Промокод уже использован"}

        # Проверяем не активировал ли уже этот пользователь
        existing = conn.execute(
            "SELECT * FROM promo_activations WHERE code = ? AND user_id = ?",
            (promo["code"], user_id),
        ).fetchone()
        if existing:
            remaining = dict(existing)["checks_remaining"]
            if remaining > 0:
                return {"success": False,
                        "error": f"Промокод уже активирован. Осталось {remaining} проверок"}
            else:
                return {"success": False, "error": "Вы уже использовали этот промокод"}

        # Активируем!
        checks = promo["checks_per_use"]
        conn.execute(
            "INSERT INTO promo_activations (code, user_id, checks_remaining, activated_at) "
            "VALUES (?, ?, ?, ?)",
            (promo["code"], user_id, checks, _now()),
        )
        conn.execute(
            "UPDATE promo_codes SET activations_count = activations_count + 1 WHERE code = ?",
            (promo["code"],),
        )
        conn.commit()
        logger.info("Promo %s activated by user %s (%d checks)",
                     promo["code"], user_id, checks)
        return {"success": True, "checks": checks}
    finally:
        conn.close()


def get_promo_checks_remaining(user_id: int) -> int:
    """Сколько промо-проверок осталось у пользователя (суммарно по всем кодам)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(checks_remaining), 0) as total "
            "FROM promo_activations WHERE user_id = ? AND checks_remaining > 0",
            (user_id,),
        ).fetchone()
        return row["total"]
    finally:
        conn.close()


def use_promo_check(user_id: int) -> bool:
    """
    Списывает 1 промо-проверку (с самой старой активации).
    Возвращает True если списано, False если нет промо-проверок.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, checks_remaining FROM promo_activations "
            "WHERE user_id = ? AND checks_remaining > 0 "
            "ORDER BY activated_at ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return False

        conn.execute(
            "UPDATE promo_activations SET checks_remaining = checks_remaining - 1 "
            "WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_all_promo_codes() -> list[dict[str, Any]]:
    """Все промокоды (для админ-панели)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM promo_codes ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────────────
# SUPPORT
# ─────────────────────────────────────────────────

def save_support_message(user_id: int, username: str | None,
                         message: str) -> int:
    """Сохраняет обращение в поддержку. Возвращает ID."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO support_messages (user_id, username, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, message, _now()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_support_messages(resolved: bool = False,
                         limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM support_messages WHERE is_resolved = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (1 if resolved else 0, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_support_message(msg_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE support_messages SET is_resolved = 1 WHERE id = ?",
            (msg_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────
# AUTH ATTEMPTS (anti-bruteforce)
# ─────────────────────────────────────────────────

def log_auth_attempt(user_id: int, username: str | None,
                     success: bool) -> None:
    """Логирует попытку авторизации."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO auth_attempts (user_id, username, success, attempt_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, 1 if success else 0, _now()),
        )
        conn.commit()
        if not success:
            logger.warning("Failed auth attempt by user %s (@%s)",
                           user_id, username)
    finally:
        conn.close()


def get_failed_attempts_count(user_id: int, minutes: int = 30) -> int:
    """Кол-во неудачных попыток за последние N минут."""
    conn = _connect()
    since = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM auth_attempts "
            "WHERE user_id = ? AND success = 0 AND attempt_at > ?",
            (user_id, since),
        ).fetchone()
        return row["cnt"]
    finally:
        conn.close()


# ─────────────────────────────────────────────────
# WATCHLIST (мониторинг)
# ─────────────────────────────────────────────────

def add_watch(user_id: int, inn: str, company_name: str,
              data_hash: str = "") -> bool:
    """Добавляет компанию в мониторинг. Возвращает True если добавлена."""
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM watchlist WHERE user_id = ? AND inn = ?",
            (user_id, inn),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO watchlist (user_id, inn, company_name, last_data_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, inn, company_name, data_hash, _now()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_watch(user_id: int, inn: str) -> None:
    """Удаляет компанию из мониторинга."""
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND inn = ?",
            (user_id, inn),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_watchlist(user_id: int) -> list[dict[str, Any]]:
    """Возвращает мониторинг пользователя."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_watches() -> list[dict[str, Any]]:
    """Все записи мониторинга (для фоновой проверки)."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM watchlist").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_watch_hash(watch_id: int, new_hash: str) -> None:
    """Обновляет хэш данных для записи мониторинга."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE watchlist SET last_data_hash = ? WHERE id = ?",
            (new_hash, watch_id),
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat()


def _today() -> str:
    return date.today().isoformat()


# ─────────────────────────────────────────────────
# ASYNC-ОБЁРТКИ (запуск в executor, не блокируют event loop)
# ─────────────────────────────────────────────────

async def a_get_user(user_id: int):
    return await run_sync(get_user, user_id)

async def a_ensure_user(user_id: int, username=None, first_name=None):
    return await run_sync(ensure_user, user_id, username, first_name)

async def a_increment_check(user_id: int):
    return await run_sync(increment_check, user_id)

async def a_get_checks_today(user_id: int):
    return await run_sync(get_checks_today, user_id)

async def a_set_plan(user_id: int, plan: str, granted_by=None, expires=None):
    return await run_sync(set_plan, user_id, plan, granted_by, expires)

async def a_set_admin(user_id: int):
    return await run_sync(set_admin, user_id)

async def a_revoke_plan(user_id: int):
    return await run_sync(revoke_plan, user_id)

async def a_is_admin(user_id: int):
    return await run_sync(is_admin, user_id)

async def a_get_user_plan(user_id: int):
    return await run_sync(get_user_plan, user_id)

async def a_get_all_users(limit=100):
    return await run_sync(get_all_users, limit)

async def a_get_user_count():
    return await run_sync(get_user_count)

async def a_get_active_today_count():
    return await run_sync(get_active_today_count)

async def a_get_total_checks():
    return await run_sync(get_total_checks)

async def a_get_plan_stats():
    return await run_sync(get_plan_stats)

async def a_generate_promo_codes(count=20, checks_per_use=3, created_by=None, expires_days=90):
    return await run_sync(generate_promo_codes, count, checks_per_use, created_by, expires_days)

async def a_activate_promo(code: str, user_id: int):
    return await run_sync(activate_promo, code, user_id)

async def a_get_promo_checks_remaining(user_id: int):
    return await run_sync(get_promo_checks_remaining, user_id)

async def a_use_promo_check(user_id: int):
    return await run_sync(use_promo_check, user_id)

async def a_get_all_promo_codes():
    return await run_sync(get_all_promo_codes)

async def a_save_support_message(user_id: int, username, message: str):
    return await run_sync(save_support_message, user_id, username, message)

async def a_get_support_messages(resolved=False, limit=20):
    return await run_sync(get_support_messages, resolved, limit)

async def a_resolve_support_message(msg_id: int):
    return await run_sync(resolve_support_message, msg_id)

async def a_log_auth_attempt(user_id: int, username, success: bool):
    return await run_sync(log_auth_attempt, user_id, username, success)

async def a_get_failed_attempts_count(user_id: int, minutes=30):
    return await run_sync(get_failed_attempts_count, user_id, minutes)

async def a_add_watch(user_id: int, inn: str, company_name: str, data_hash=""):
    return await run_sync(add_watch, user_id, inn, company_name, data_hash)

async def a_remove_watch(user_id: int, inn: str):
    return await run_sync(remove_watch, user_id, inn)

async def a_get_user_watchlist(user_id: int):
    return await run_sync(get_user_watchlist, user_id)

async def a_get_all_watches():
    return await run_sync(get_all_watches)

async def a_update_watch_hash(watch_id: int, new_hash: str):
    return await run_sync(update_watch_hash, watch_id, new_hash)


# ── Доступ к документам (предложения/счета) ──

def set_docs_access(user_id: int, access: bool) -> None:
    """Устанавливает право на создание предложений/счетов."""
    conn = _connect()
    try:
        conn.execute("UPDATE users SET docs_access = ? WHERE user_id = ?", (1 if access else 0, user_id))
        conn.commit()
    finally:
        conn.close()

def has_docs_access(user_id: int) -> bool:
    """Проверяет право на создание предложений/счетов."""
    conn = _connect()
    try:
        row = conn.execute("SELECT docs_access, is_admin FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return False
        return bool(row["docs_access"]) or bool(row["is_admin"])
    finally:
        conn.close()

async def a_set_docs_access(user_id: int, access: bool):
    return await run_sync(set_docs_access, user_id, access)

async def a_has_docs_access(user_id: int) -> bool:
    return await run_sync(has_docs_access, user_id)


def get_users_count() -> int:
    """Количество пользователей в базе."""
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()

async def a_get_users_count() -> int:
    return await run_sync(get_users_count)
