"""
Telegram-бот для проверки контрагентов по ИНН.

Функционал:
  - Проверка компании по ИНН (Free: краткий / Pro: полный отчёт)
  - Предложение и Запрос счёта
  - Сравнение двух компаний
  - ИИ-анализ, Выписка ЕГРЮЛ
  - Система подписок (Free / Pro / Business / Admin)
  - Скрытая админ-авторизация (секретная команда + логин/пароль/кодовое слово)
  - Промокоды (3 полных проверки)
  - Профиль с лимитами
  - Поддержка
  - Пользовательское соглашение и Политика конфиденциальности
"""

import asyncio
import html as html_mod
import logging
import re
import sys
import time
import hmac
import hashlib

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BufferedInputFile,
)

from config import (
    TELEGRAM_BOT_TOKEN, DADATA_API_KEY, GIGACHAT_CREDENTIALS,
    ADMIN_COMMAND, ADMIN_LOGIN, ADMIN_PASSWORD, ADMIN_SECRET, ADMIN_IDS,
    SUPPORT_CHAT_ID, MAX_AUTH_ATTEMPTS, AUTH_BAN_MINUTES,
    PLAN_LIMITS, RATE_LIMIT_MESSAGES, RATE_LIMIT_PERIOD,
)
from validators import validate_inn
from dadata_client import fetch_company_data, extract_company_fields, DaDataError, search_company_by_name
from open_sources import generate_links, fetch_zsk_data, fetch_rusprofile_data
from zchb_client import fetch_company_card, fetch_court_cases, fetch_fssp
from itsoft_client import fetch_finance_history
from report_formatter import (
    format_report, format_report_free,
    format_proposal, format_invoice, format_comparison,
    format_changes, format_bulk_report, format_affiliated, format_contracts,
    format_courts_detail, format_fns_detail,
)
from states import (
    ProposalFlow, InvoiceFlow, CompareFlow,
    AdminAuthFlow, PromoFlow, SupportFlow,
)
from proposal_counter import next_proposal, get_stats
from invoice_counter import next_invoice, get_invoice_stats
from ai_client import generate_recommendation
from fns_client import get_egrul_pdf
from sanctions_client import check_sanctions
from cbrf_client import check_bank_refusals
from apifns_client import full_apifns_check, fetch_changes, extract_changes_data
from legal import get_user_agreement, get_privacy_policy
import cache
import database as db
import subscription as sub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

dp = Dispatcher(storage=MemoryStorage())

# ─────────────────────────────────────────────────
# Хелперы: safe delete, long message, rate limiter
# ─────────────────────────────────────────────────

async def _safe_delete(msg: Message) -> None:
    """Удаляет сообщение, игнорируя ошибки (бот не может удалять чужие в группах)."""
    try:
        await msg.delete()
    except Exception:
        pass


async def _send_long_message(
    target: Message,
    text: str,
    reply_markup=None,
    **kwargs,
) -> None:
    """Отправляет длинный текст, разбивая на куски ≤4000 символов по \\n."""
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await target.answer(text, reply_markup=reply_markup, **kwargs)
        return
    # Разбиваем по строкам
    lines = text.split("\n")
    chunk = ""
    chunks: list[str] = []
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_LEN:
            if chunk:
                chunks.append(chunk)
            chunk = line
        else:
            chunk = chunk + "\n" + line if chunk else line
    if chunk:
        chunks.append(chunk)
    # Отправляем
    for i, part in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await target.answer(
            part,
            reply_markup=reply_markup if is_last else None,
            **kwargs,
        )


# ── Rate limiter (in-memory) ──
_rate_limits: dict[int, list[float]] = {}


def _check_rate_limit(user_id: int) -> bool:
    """Возвращает True если пользователь превысил лимит."""
    now = time.time()
    window = RATE_LIMIT_PERIOD
    max_msgs = RATE_LIMIT_MESSAGES

    timestamps = _rate_limits.get(user_id, [])
    # Чистим старые
    timestamps = [t for t in timestamps if now - t < window]
    timestamps.append(now)
    _rate_limits[user_id] = timestamps

    # Чистим словарь от неактивных пользователей периодически
    if len(_rate_limits) > 10000:
        cutoff = now - window * 2
        stale = [uid for uid, ts in _rate_limits.items()
                 if not ts or ts[-1] < cutoff]
        for uid in stale:
            del _rate_limits[uid]

    return len(timestamps) > max_msgs


# ─────────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────────

def _main_kb(show_docs: bool = False) -> ReplyKeyboardMarkup:
    """Главная клавиатура. Предложение/Счёт — только для пользователей с доступом."""
    rows = [
        [KeyboardButton(text="📋 Проверка компании"), KeyboardButton(text="⚖️ Сравнить")],
    ]
    if show_docs:
        rows.append([
            KeyboardButton(text="📝 Предложение"),
            KeyboardButton(text="🧾 Запрос счета"),
        ])
    rows.append([KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💎 Тарифы")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)

# Дефолтная клавиатура без предложений/счетов
MAIN_KB = _main_kb(show_docs=False)


def _report_keyboard(inn: str, full: bool = True, entity_type: str = "ul", show_docs: bool = False) -> InlineKeyboardMarkup:
    """Inline-кнопки под отчётом. show_docs=True для админов и пользователей с доступом к предложениям/счетам."""
    buttons = []
    # Предложение/Счёт — только для пользователей с доступом (админ или назначенные)
    if show_docs:
        buttons.append([
            InlineKeyboardButton(text="📝 Предложение", callback_data=f"proposal:{inn}"),
            InlineKeyboardButton(text="🧾 Запрос счёта", callback_data=f"invoice:{inn}"),
        ])
    if full:
        buttons.append([
            InlineKeyboardButton(text="⚖️ Суды", callback_data=f"courts:{inn}"),
            InlineKeyboardButton(text="🏦 ФНС", callback_data=f"fnscheck:{inn}"),
        ])
        egrul_label = "📄 ЕГРИП" if entity_type == "ip" else "📄 ЕГРЮЛ"
        buttons.append([
            InlineKeyboardButton(text="🤖 ИИ-анализ", callback_data=f"ai:{inn}"),
            InlineKeyboardButton(text=egrul_label, callback_data=f"egrul:{inn}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="📜 История", callback_data=f"history:{inn}"),
            InlineKeyboardButton(text="🔗 Связи", callback_data=f"affiliated:{inn}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="📋 Госзакупки", callback_data=f"contracts:{inn}"),
            InlineKeyboardButton(text="📥 1С", callback_data=f"export1c:{inn}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="📄 PDF", callback_data=f"pdf:{inn}"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh:{inn}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────────
# Полная проверка компании (кэш + все источники)
# ─────────────────────────────────────────────────

async def _full_check(inn: str) -> dict:
    """Запрашивает все источники параллельно. Кэш 30 мин. Retry x3 для критических."""
    cached = cache.get(f"check:{inn}")
    if cached is not None:
        logger.info("Cache HIT for %s", inn)
        return cached

    # ── Retry-обёртка: повторяет запрос до 3 раз при ошибке ──
    async def _retry(coro_fn, *args, retries=3, **kwargs):
        for attempt in range(retries):
            try:
                return await coro_fn(*args, **kwargs)
            except Exception as e:
                if attempt < retries - 1:
                    logger.info("Retry %d/%d for %s: %s", attempt + 1, retries, coro_fn.__name__, e)
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise

    raw_data = await fetch_company_data(inn)
    fields = extract_company_fields(raw_data)
    ogrn = fields.get("ogrn")
    company_name = fields.get("name", "")

    # Параллельно запрашиваем ВСЕ источники (критические с retry)
    zchb_task = asyncio.create_task(_retry(fetch_company_card, inn))       # ЗЧБ API (основной, retry x3)
    zsk_task = asyncio.create_task(fetch_zsk_data(inn, ogrn))              # ЗЧБ scraping (фоллбэк, без retry)
    rp_task = asyncio.create_task(fetch_rusprofile_data(inn))              # Rusprofile (фоллбэк)
    fin_task = asyncio.create_task(fetch_finance_history(inn))             # ITSoft
    fns_task = asyncio.create_task(_retry(full_apifns_check, inn))         # API-FNS (retry x3)
    sanctions_task = asyncio.create_task(check_sanctions(inn, company_name))
    cbrf_task = asyncio.create_task(check_bank_refusals(inn))
    fssp_task = asyncio.create_task(fetch_fssp(inn))

    results = await asyncio.gather(
        zchb_task, zsk_task, rp_task, fin_task, fns_task,
        sanctions_task, cbrf_task, fssp_task,
        return_exceptions=True,
    )

    def _safe(val, default_factory=dict):
        if isinstance(val, BaseException):
            logger.warning("Source error (graceful skip): %s", val)
            return default_factory()
        return val

    zchb_data = _safe(results[0])
    zsk_data = _safe(results[1])
    rp_data = _safe(results[2])
    fin_history = _safe(results[3])
    fns_data = _safe(results[4])
    sanctions_data = _safe(results[5])
    cbrf_data = _safe(results[6])
    fssp_data = _safe(results[7])

    # Обогащаем zchb_data данными ФССП из ЗЧБ API
    if fssp_data:
        zchb_data["fssp_count"] = fssp_data.get("total", fssp_data.get("count", 0))
        zchb_data["fssp_sum"] = fssp_data.get("sum", 0)
        zchb_data["fssp_details"] = fssp_data.get("items", [])

    # ── Обогащаем fields данными из ЗЧБ API (точные, из реестров) ──
    if zchb_data.get("capital") is not None:
        fields["capital_value"] = zchb_data["capital"]  # Точный капитал из ЕГРЮЛ
    if zchb_data.get("address"):
        fields["full_address"] = zchb_data["address"]   # Полный адрес
    if zchb_data.get("employee_count") is not None:
        fields["employee_count"] = zchb_data["employee_count"]
    if zchb_data.get("msp_category"):
        fields["msp_category"] = zchb_data["msp_category"]

    links = generate_links(inn, ogrn)
    if zchb_data.get("url"):
        links["zachestnyibiznes"] = zchb_data["url"]

    result = {
        "fields": fields,
        "zchb_data": zchb_data,  # ЗЧБ API — основной источник
        "zsk_data": zsk_data,    # ЗЧБ scraping — фоллбэк
        "rp_data": rp_data,
        "fin_history": fin_history,
        "fns_data": fns_data,
        "sanctions_data": sanctions_data,
        "cbrf_data": cbrf_data,
        "links": links,
    }
    cache.put(f"check:{inn}", result, ttl=1800)
    return result


# ─────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────

def _find_inn(text: str) -> str | None:
    m = re.search(r"\b(\d{12})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{10})\b", text)
    return m.group(1) if m else None


async def _is_admin(user_id: int) -> bool:
    """Проверяет админ из базы или из config."""
    return (await db.a_is_admin(user_id)) or user_id in ADMIN_IDS


async def _register_user(message: Message) -> None:
    """Регистрирует пользователя при первом обращении."""
    await db.a_ensure_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )


# ═══════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════

# ─── /start ───

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await _register_user(message)
    name = message.from_user.first_name or "друг"
    has_docs = await db.a_has_docs_access(message.from_user.id)
    kb = _main_kb(show_docs=has_docs)
    await message.answer(
        f"Привет, {name}! 👋\n\n"
        "Я проверяю компании по ИНН из открытых источников.\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Отправьте <b>ИНН</b> (10 или 12 цифр) — получите отчёт\n"
        "• Отправьте <b>название</b> компании — найду ИНН\n"
        "• <code>сравнить</code> — сравнить две компании\n\n"
        "<b>Полезные команды:</b>\n"
        "/profile — ваш профиль и лимиты\n"
        "/tariffs — тарифные планы\n"
        "/promo — активировать промокод\n"
        "/support — связаться с поддержкой\n\n"
        "Используйте кнопки меню ⬇️",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


# ─── /profile ───

@dp.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    await _register_user(message)
    text = await sub.get_profile_text(message.from_user.id)
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(F.text == "👤 Профиль")
async def menu_profile(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer("Сначала завершите текущий диалог или нажмите /cancel.")
        return
    await _register_user(message)
    text = await sub.get_profile_text(message.from_user.id)
    await message.answer(text, parse_mode=ParseMode.HTML)


# ─── /tariffs ───

@dp.message(Command("tariffs"))
async def cmd_tariffs(message: Message) -> None:
    await _register_user(message)
    text = sub.get_tariffs_text()
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(F.text == "💎 Тарифы")
async def menu_tariffs(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer("Сначала завершите текущий диалог или нажмите /cancel.")
        return
    await _register_user(message)
    text = sub.get_tariffs_text()
    await message.answer(text, parse_mode=ParseMode.HTML)


# ─── /promo ───

@dp.message(Command("promo"))
async def cmd_promo(message: Message, state: FSMContext) -> None:
    await _register_user(message)
    await state.set_state(PromoFlow.waiting_code)
    await message.answer(
        "🎁 Введите промокод:",
        parse_mode=ParseMode.HTML,
    )


@dp.message(PromoFlow.waiting_code)
async def promo_enter_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    await state.clear()

    if not code:
        await message.answer("❌ Промокод не может быть пустым.")
        return

    result = await db.a_activate_promo(code, message.from_user.id)
    if result["success"]:
        remaining = await db.a_get_promo_checks_remaining(message.from_user.id)
        await message.answer(
            f"✅ Промокод активирован!\n\n"
            f"🎁 Вам начислено <b>{result['checks']}</b> полных проверок.\n"
            f"Всего промо-проверок: <b>{remaining}</b>\n\n"
            "Отправьте ИНН для проверки.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(f"❌ {result['error']}")


# ─── /support ───

@dp.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await _register_user(message)
    await state.set_state(SupportFlow.waiting_message)
    await message.answer(
        "📩 <b>Поддержка</b>\n\n"
        "Опишите ваш вопрос или проблему одним сообщением.\n"
        "Мы ответим как можно скорее.\n\n"
        "/cancel — отменить",
        parse_mode=ParseMode.HTML,
    )


@dp.message(SupportFlow.waiting_message)
async def support_receive(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await state.clear()

    if not text:
        await message.answer("❌ Сообщение не может быть пустым.")
        return

    msg_id = await db.a_save_support_message(
        user_id=message.from_user.id,
        username=message.from_user.username,
        message=text,
    )
    await message.answer(
        f"✅ Обращение #{msg_id} отправлено!\n"
        "Мы свяжемся с вами в ближайшее время.",
    )

    # Пересылаем админу
    if SUPPORT_CHAT_ID:
        try:
            bot: Bot = message.bot
            username = f"@{message.from_user.username}" if message.from_user.username else "нет"
            await bot.send_message(
                chat_id=int(SUPPORT_CHAT_ID),
                text=(
                    f"📩 <b>Обращение #{msg_id}</b>\n"
                    f"От: {html_mod.escape(message.from_user.first_name or '')} ({html_mod.escape(username)})\n"
                    f"ID: <code>{message.from_user.id}</code>\n\n"
                    f"{html_mod.escape(text)}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Failed to forward support message: %s", e)


# ─── /agreement и /privacy ───

@dp.message(Command("agreement"))
async def cmd_agreement(message: Message) -> None:
    await _register_user(message)
    text = get_user_agreement()
    await _send_long_message(message, text, parse_mode=ParseMode.HTML)


@dp.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await _register_user(message)
    text = get_privacy_policy()
    await _send_long_message(message, text, parse_mode=ParseMode.HTML)


# ─── /admin_help (скрытый) ───

@dp.message(Command("admin_help"))
async def cmd_admin_help(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return
    await message.answer(
        "🔧 <b>Админ-панель</b>\n\n"
        "📊 <b>Мониторинг:</b>\n"
        "/status — статус API, ключи, сервер\n"
        "/apistats — лимиты API-FNS\n"
        "/users — список пользователей\n"
        "/stats — статистика предложений\n"
        "/invoices — статистика счетов\n\n"
        "👥 <b>Управление:</b>\n"
        "/grant <code>USER_ID PLAN [DAYS]</code> — выдать тариф\n"
        "/revoke <code>USER_ID</code> — отозвать тариф\n"
        "/grant_docs <code>USER_ID</code> — открыть предложения/счета\n"
        "/revoke_docs <code>USER_ID</code> — закрыть предложения/счета\n"
        "/gen_promos <code>N</code> — сгенерировать промокоды\n\n"
        "🔧 <b>Система:</b>\n"
        "/refresh_cache — очистить кэш\n"
        "/force_recheck <code>ИНН</code> — принудительная проверка\n\n"
        "Планы: free, start, pro, business, admin",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("refresh_cache"))
async def cmd_refresh_cache(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return
    cache.clear()
    await message.answer("✅ Кэш полностью очищен.")


@dp.message(Command("force_recheck"))
async def cmd_force_recheck(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /force_recheck <code>ИНН</code>", parse_mode=ParseMode.HTML)
        return
    inn = parts[1].strip()
    cache.delete(f"check:{inn}")
    await message.answer(f"✅ Кэш для ИНН <code>{inn}</code> очищен. Следующий запрос будет свежим.", parse_mode=ParseMode.HTML)


# ─── /stats и /invoices ───

@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    s = get_stats()
    await message.answer(
        f"📊 <b>Статистика предложений</b>\n\n"
        f"Всего выдано: <b>{s['total']}</b>\n"
        f"Сегодня ({s['today_date']}): <b>{s['today']}</b>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("invoices"))
async def cmd_invoices(message: Message) -> None:
    s = get_invoice_stats()
    await message.answer(
        f"🧾 <b>Статистика запросов счетов</b>\n\n"
        f"Всего выдано: <b>{s['total']}</b>\n"
        f"Сегодня ({s['today_date']}): <b>{s['today']}</b>",
        parse_mode=ParseMode.HTML,
    )


# ─── /cancel ───

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current:
        await state.clear()
        await message.answer(
            "Диалог отменён. Выберите действие в меню ⬇️",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer("Нечего отменять.")


# ═══════════════════════════════════════════════
# СКРЫТАЯ АДМИН-АВТОРИЗАЦИЯ
# ═══════════════════════════════════════════════

@dp.message(F.text.func(lambda t: t and t.strip() == ADMIN_COMMAND))
async def admin_auth_start(message: Message, state: FSMContext) -> None:
    """Секретная команда авторизации. Не показывается в меню."""
    if not ADMIN_LOGIN or not ADMIN_PASSWORD or not ADMIN_SECRET:
        return  # Не настроено — молча игнорируем

    user_id = message.from_user.id

    # Анти-брутфорс
    failed = await db.a_get_failed_attempts_count(user_id, AUTH_BAN_MINUTES)
    if failed >= MAX_AUTH_ATTEMPTS:
        logger.warning("Auth blocked for user %s (too many attempts)", user_id)
        return  # Молча игнорируем — не показываем что команда существует

    # Если уже админ — молча подтверждаем
    if await _is_admin(user_id):
        await message.answer("⭐ Вы уже авторизованы как администратор.")
        await _safe_delete(message)
        return

    await state.set_state(AdminAuthFlow.waiting_login)
    await _safe_delete(message)  # Удаляем команду из чата
    await message.answer("🔐 Логин:")


@dp.message(AdminAuthFlow.waiting_login)
async def admin_auth_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    await _safe_delete(message)  # Удаляем логин из чата

    if not hmac.compare_digest(login, ADMIN_LOGIN):
        await db.a_log_auth_attempt(message.from_user.id, message.from_user.username, False)
        await state.clear()
        # Молча — не говорим что не так
        return

    await state.update_data(login_ok=True)
    await state.set_state(AdminAuthFlow.waiting_pass)
    await message.answer("🔑 Пароль:")


@dp.message(AdminAuthFlow.waiting_pass)
async def admin_auth_pass(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    await _safe_delete(message)  # Удаляем пароль из чата

    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        await db.a_log_auth_attempt(message.from_user.id, message.from_user.username, False)
        await state.clear()
        return  # Молча

    await state.update_data(pass_ok=True)
    await state.set_state(AdminAuthFlow.waiting_secret)
    await message.answer("🗝️ Секретное слово:")


@dp.message(AdminAuthFlow.waiting_secret)
async def admin_auth_secret(message: Message, state: FSMContext) -> None:
    secret = (message.text or "").strip()
    await _safe_delete(message)  # Удаляем секретное слово из чата

    data = await state.get_data()
    await state.clear()

    if not data.get("login_ok") or not data.get("pass_ok"):
        await db.a_log_auth_attempt(message.from_user.id, message.from_user.username, False)
        return

    if not hmac.compare_digest(secret, ADMIN_SECRET):
        await db.a_log_auth_attempt(message.from_user.id, message.from_user.username, False)
        return  # Молча

    # ✅ Все 3 фактора верны — делаем админом!
    user_id = message.from_user.id
    await db.a_set_admin(user_id)
    await db.a_log_auth_attempt(user_id, message.from_user.username, True)
    logger.info("Admin auth SUCCESS for user %s (@%s)",
                user_id, message.from_user.username)

    await message.answer(
        "⭐ <b>Авторизация успешна!</b>\n\n"
        "Вы получили полный доступ ко всем функциям.\n\n"
        "<b>Админ-команды:</b>\n"
        "/grant <code>USER_ID PLAN</code> — выдать план\n"
        "/grant_docs <code>USER_ID</code> — открыть предложения/счета\n"
        "/revoke_docs <code>USER_ID</code> — закрыть предложения/счета\n"
        "/revoke <code>USER_ID</code> — отозвать план\n"
        "/users — список пользователей\n"
        "/status — статус API ключей и сервера\n"
        "/apistats — статистика API\n"
        "/support_list — обращения\n"
        "/gen_promos <code>N</code> — сгенерировать промокоды\n"
        "/promos — список промокодов",
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════
# АДМИН-КОМАНДЫ (только для авторизованных)
# ═══════════════════════════════════════════════

@dp.message(Command("grant"))
async def admin_grant(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Использование: /grant <code>USER_ID PLAN [DAYS]</code>\n"
            "Планы: pro, business, admin\n"
            "Пример: /grant 123456789 pro 30",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ USER_ID должен быть числом.")
        return

    plan = parts[2].lower()
    if plan not in ("pro", "business", "admin", "free"):
        await message.answer("❌ Доступные планы: free, pro, business, admin")
        return

    expires = None
    if plan in ("pro", "business") and len(parts) >= 4:
        try:
            days = int(parts[3])
            from datetime import datetime, timedelta
            expires = (datetime.now() + timedelta(days=days)).isoformat()
        except ValueError:
            pass

    await db.a_set_plan(target_id, plan, granted_by=message.from_user.id, expires=expires)

    exp_text = f" (до {expires[:10]})" if expires else " (бессрочно)"
    await message.answer(
        f"✅ Пользователю <code>{target_id}</code> установлен план "
        f"<b>{plan}</b>{exp_text}",
        parse_mode=ParseMode.HTML,
    )

    # Уведомляем пользователя
    try:
        plan_names = {"pro": "💎 Pro", "business": "🏆 Business", "admin": "⭐ Admin", "free": "🆓 Free"}
        await message.bot.send_message(
            chat_id=target_id,
            text=f"🎉 Вам активирован тариф <b>{plan_names.get(plan, plan)}</b>{exp_text}\n\n"
                 "Введите /profile чтобы увидеть детали.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


@dp.message(Command("grant_docs"))
async def admin_grant_docs(message: Message) -> None:
    """Открывает доступ к предложениям/счетам для пользователя."""
    if not await _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /grant_docs <code>USER_ID</code>", parse_mode=ParseMode.HTML)
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ USER_ID должен быть числом.")
        return
    await db.a_set_docs_access(target_id, True)
    await message.answer(f"✅ Пользователю <code>{target_id}</code> открыт доступ к предложениям/счетам.", parse_mode=ParseMode.HTML)
    try:
        await message.bot.send_message(target_id, "📝 Вам открыт доступ к созданию предложений и счетов!")
    except Exception:
        pass


@dp.message(Command("revoke_docs"))
async def admin_revoke_docs(message: Message) -> None:
    """Закрывает доступ к предложениям/счетам для пользователя."""
    if not await _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /revoke_docs <code>USER_ID</code>", parse_mode=ParseMode.HTML)
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ USER_ID должен быть числом.")
        return
    await db.a_set_docs_access(target_id, False)
    await message.answer(f"✅ Доступ к предложениям/счетам для <code>{target_id}</code> закрыт.", parse_mode=ParseMode.HTML)


@dp.message(Command("revoke"))
async def admin_revoke(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /revoke <code>USER_ID</code>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ USER_ID должен быть числом.")
        return

    await db.a_revoke_plan(target_id)
    await message.answer(f"✅ План пользователя <code>{target_id}</code> сброшен на Free.",
                         parse_mode=ParseMode.HTML)


@dp.message(Command("users"))
async def admin_users(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return

    total = await db.a_get_user_count()
    active_today = await db.a_get_active_today_count()
    total_checks = await db.a_get_total_checks()
    plan_stats = await db.a_get_plan_stats()

    lines = [
        "👥 <b>Пользователи</b>",
        "",
        f"Всего: <b>{total}</b>",
        f"Активных сегодня: <b>{active_today}</b>",
        f"Всего проверок: <b>{total_checks}</b>",
        "",
        "<b>По планам:</b>",
    ]
    for plan, cnt in sorted(plan_stats.items()):
        icon = {"free": "🆓", "pro": "💎", "business": "🏆", "admin": "⭐"}.get(plan, "📋")
        lines.append(f"  {icon} {plan}: {cnt}")

    # Топ-5 пользователей
    users = await db.a_get_all_users(5)
    if users:
        lines.append("")
        lines.append("<b>Топ-5 по проверкам:</b>")
        for u in users:
            uname = f"@{u['username']}" if u.get("username") else f"ID:{u['user_id']}"
            lines.append(f"  {uname} — {u['checks_total']} проверок ({u['plan']})")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("admin_help"))
async def admin_help(message: Message) -> None:
    """Полный список скрытых админ-команд."""
    if not await _is_admin(message.from_user.id):
        return
    await message.answer(
        "🔧 <b>Админ-панель</b>\n\n"
        "─── <b>Мониторинг</b> ───\n"
        "/status — статус API ключей и сервера\n"
        "/apistats — лимиты API-FNS (использовано/осталось)\n"
        "/users — список пользователей\n\n"
        "─── <b>Управление</b> ───\n"
        "/grant <code>USER_ID PLAN [DAYS]</code> — выдать тариф\n"
        "/revoke <code>USER_ID</code> — отозвать тариф\n"
        "/grant_docs <code>USER_ID</code> — открыть предложения/счета\n"
        "/revoke_docs <code>USER_ID</code> — закрыть предложения/счета\n"
        "/gen_promos <code>N</code> — сгенерировать промокоды\n\n"
        "─── <b>Диагностика</b> ───\n"
        "/force_check <code>ИНН</code> — проверка без кэша\n"
        "/clear_cache — очистить весь кэш\n\n"
        "─── <b>Контент</b> ───\n"
        "/stats — статистика предложений\n"
        "/invoices — статистика счетов\n"
        "/support_list — обращения в поддержку\n\n"
        "─── <b>Авторизация</b> ───\n"
        f"/f_access — скрытая команда входа",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("force_check"))
async def admin_force_check(message: Message) -> None:
    """Принудительная проверка ИНН без кэша."""
    if not await _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /force_check <code>ИНН</code>", parse_mode=ParseMode.HTML)
        return
    inn = parts[1].strip()
    cache.delete(f"check:{inn}")
    await message.answer(f"🔄 Кэш для {inn} очищен. Отправьте ИНН для свежей проверки.")


@dp.message(Command("clear_cache"))
async def admin_clear_cache(message: Message) -> None:
    """Очистить весь кэш."""
    if not await _is_admin(message.from_user.id):
        return
    cache.clear()
    await message.answer("🗑 Кэш очищен.")


@dp.message(Command("status"))
async def admin_status(message: Message) -> None:
    """Статус API ключей, лимитов и сервера."""
    if not await _is_admin(message.from_user.id):
        return
    await message.answer("⏳ Собираю информацию...")

    lines = ["🖥 <b>Статус системы</b>", ""]

    # ── Сервер ──
    lines.append("─── <b>Сервер</b> ───")
    lines.append("IP: <code>5.42.113.218</code>")
    lines.append("Запущен: 11.03.2026")
    lines.append("Оплата: ~до 11.04.2026 (1 мес)")
    lines.append("")

    # ── API ключи ──
    lines.append("─── <b>API ключи</b> ───")

    # 1. DaData
    from config import DADATA_API_KEY, APIFNS_KEY, ZCHB_API_KEY, OPENSANCTIONS_API_KEY, GIGACHAT_CREDENTIALS
    dadata_ok = bool(DADATA_API_KEY)
    lines.append(f"{'✅' if dadata_ok else '❌'} <b>DaData</b>: {'настроен' if dadata_ok else 'НЕ НАСТРОЕН'}")
    if dadata_ok:
        lines.append(f"   Ключ: <code>...{DADATA_API_KEY[-8:]}</code>")
        lines.append("   Срок: бессрочно (баланс)")

    # 2. API-FNS
    fns_ok = bool(APIFNS_KEY)
    lines.append(f"{'✅' if fns_ok else '❌'} <b>API-FNS</b>: {'настроен' if fns_ok else 'НЕ НАСТРОЕН'}")
    if fns_ok:
        lines.append(f"   Ключ: <code>...{APIFNS_KEY[-8:]}</code>")
        # Запрашиваем статистику
        try:
            from apifns_client import fetch_stat
            stat = await fetch_stat()
            if stat:
                date_end = stat.get("ДатаОконч", "н/д")
                lines.append(f"   Срок: до <b>{date_end}</b>")
                methods = stat.get("Методы", {})
                for method, info in methods.items():
                    limit = info.get("Лимит", "?")
                    used = info.get("Истрачено", "0")
                    lines.append(f"   {method}: {used}/{limit}")
        except Exception:
            lines.append("   Статистика: ошибка запроса")

    # 3. ЗЧБ
    zchb_ok = bool(ZCHB_API_KEY)
    lines.append(f"{'✅' if zchb_ok else '❌'} <b>ЗЧБ</b>: {'настроен' if zchb_ok else 'НЕ НАСТРОЕН'}")
    if zchb_ok:
        lines.append(f"   Ключ: <code>...{ZCHB_API_KEY[-8:]}</code>")
        lines.append("   Срок: по тарифу ЗЧБ")

    # 4. GigaChat
    gc_ok = bool(GIGACHAT_CREDENTIALS)
    lines.append(f"{'✅' if gc_ok else '❌'} <b>GigaChat</b>: {'настроен' if gc_ok else 'НЕ НАСТРОЕН'}")

    # 5. OpenSanctions
    os_ok = bool(OPENSANCTIONS_API_KEY)
    lines.append(f"{'✅' if os_ok else '❌'} <b>OpenSanctions</b>: {'настроен' if os_ok else 'НЕ НАСТРОЕН'}")

    lines.append("")
    lines.append("─── <b>Итого</b> ───")
    total = sum([dadata_ok, fns_ok, zchb_ok, gc_ok, os_ok])
    lines.append(f"Активных API: <b>{total}/5</b>")

    # Пользователи
    users_count = await db.a_get_users_count() if hasattr(db, 'a_get_users_count') else "н/д"
    lines.append(f"Пользователей: <b>{users_count}</b>")

    await _send_long_message(message, "\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("admin_help"))
async def admin_help(message: Message) -> None:
    """Полный список скрытых админ-команд."""
    if not await _is_admin(message.from_user.id):
        return
    await message.answer(
        "🔧 <b>Админ-команды</b>\n\n"
        "<b>Мониторинг:</b>\n"
        "/status — статус API, ключей, сервера\n"
        "/apistats — лимиты API-FNS (использовано/осталось)\n"
        "/users — список пользователей\n"
        "/support_list — обращения в поддержку\n\n"
        "<b>Управление доступом:</b>\n"
        "/grant <code>USER_ID PLAN [DAYS]</code> — выдать тариф\n"
        "  планы: free, start, pro, business, admin\n"
        "/revoke <code>USER_ID</code> — отозвать тариф\n"
        "/grant_docs <code>USER_ID</code> — открыть предложения/счета\n"
        "/revoke_docs <code>USER_ID</code> — закрыть предложения/счета\n\n"
        "<b>Промокоды:</b>\n"
        "/gen_promos <code>N</code> — сгенерировать N промокодов\n\n"
        "<b>Статистика:</b>\n"
        "/stats — статистика предложений\n"
        "/invoices — статистика счетов\n\n"
        "<b>Диагностика:</b>\n"
        "/force_recheck <code>INN</code> — проверка ИНН без кэша\n"
        "/refresh_cache — очистка всего кэша\n",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("force_recheck"))
async def admin_force_recheck(message: Message) -> None:
    """Принудительная проверка ИНН без кэша."""
    if not await _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /force_recheck <code>INN</code>", parse_mode=ParseMode.HTML)
        return
    inn = parts[1].strip()
    cache.delete(f"check:{inn}")
    await message.answer(f"🔄 Кэш для <code>{inn}</code> очищен. Отправьте ИНН для проверки.", parse_mode=ParseMode.HTML)


@dp.message(Command("refresh_cache"))
async def admin_refresh_cache(message: Message) -> None:
    """Очистка всего кэша."""
    if not await _is_admin(message.from_user.id):
        return
    cache.clear()
    await message.answer("✅ Весь кэш очищен.")


@dp.message(Command("apistats"))
async def admin_apistats(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return

    from apifns_client import fetch_stat
    stat = await fetch_stat()

    if not stat:
        await message.answer("❌ Не удалось получить статистику API-FNS.")
        return

    lines = ["📊 <b>Статистика API-FNS</b>", ""]

    if isinstance(stat, dict):
        for key, val in stat.items():
            if isinstance(val, dict):
                used = val.get("used", val.get("Использовано", "?"))
                total = val.get("total", val.get("Всего", "?"))
                lines.append(f"  {key}: {used}/{total}")
            else:
                lines.append(f"  {key}: {val}")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("support_list"))
async def admin_support_list(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return

    msgs = await db.a_get_support_messages(resolved=False, limit=10)
    if not msgs:
        await message.answer("✅ Нет нерешённых обращений.")
        return

    lines = ["📩 <b>Обращения в поддержку</b>", ""]
    for m in msgs:
        uname = f"@{m['username']}" if m.get("username") else f"ID:{m['user_id']}"
        short_msg = m["message"][:100] + ("..." if len(m["message"]) > 100 else "")
        lines.append(
            f"#{m['id']} от {uname}\n"
            f"  {short_msg}\n"
            f"  📅 {m['created_at'][:16]}\n"
        )

    lines.append("Чтобы закрыть: /resolve <code>ID</code>")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("resolve"))
async def admin_resolve(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /resolve <code>ID</code>", parse_mode=ParseMode.HTML)
        return
    try:
        msg_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return
    await db.a_resolve_support_message(msg_id)
    await message.answer(f"✅ Обращение #{msg_id} закрыто.")


@dp.message(Command("gen_promos"))
async def admin_gen_promos(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    count = 20
    if len(parts) >= 2:
        try:
            count = min(int(parts[1]), 100)
        except ValueError:
            pass

    codes = await db.a_generate_promo_codes(
        count=count,
        checks_per_use=3,
        created_by=message.from_user.id,
    )

    lines = [f"🎁 <b>Сгенерировано {len(codes)} промокодов</b>", ""]
    lines.append("Каждый даёт 3 полных проверки:")
    lines.append("")
    for code in codes:
        lines.append(f"<code>{code}</code>")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("promos"))
async def admin_promos(message: Message) -> None:
    if not await _is_admin(message.from_user.id):
        return

    promos = await db.a_get_all_promo_codes()
    if not promos:
        await message.answer("Нет промокодов. Создайте: /gen_promos 20")
        return

    lines = [f"🎁 <b>Промокоды</b> ({len(promos)} шт.)", ""]
    for p in promos:
        status = "✅" if p["activations_count"] < p["max_activations"] else "❌ использован"
        lines.append(f"<code>{p['code']}</code> — {status} ({p['activations_count']}/{p['max_activations']})")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════
# КНОПКИ МЕНЮ
# ═══════════════════════════════════════════════

@dp.message(F.text == "📋 Проверка компании")
async def menu_check(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer("Сначала завершите текущий диалог или нажмите /cancel.")
        return
    await message.answer("Введите ИНН компании (10 цифр) или ИП (12 цифр):")


@dp.message(F.text == "📝 Предложение")
async def menu_proposal(message: Message, state: FSMContext) -> None:
    if not await db.a_has_docs_access(message.from_user.id):
        await message.answer("❌ Функция недоступна. Обратитесь к администратору.")
        return
    if await state.get_state():
        await message.answer("Сначала завершите текущий диалог или нажмите /cancel.")
        return
    await state.set_state(ProposalFlow.waiting_inn)
    await message.answer("Введите ИНН компании (10 или 12 цифр):")


@dp.message(F.text == "🧾 Запрос счета")
async def menu_invoice(message: Message, state: FSMContext) -> None:
    if not await db.a_has_docs_access(message.from_user.id):
        await message.answer("❌ Функция недоступна. Обратитесь к администратору.")
        return
    if await state.get_state():
        await message.answer("Сначала завершите текущий диалог или нажмите /cancel.")
        return
    await state.set_state(InvoiceFlow.waiting_inn)
    await message.answer("Введите ИНН компании (10 или 12 цифр):")


@dp.message(F.text == "⚖️ Сравнить")
async def menu_compare(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer("Сначала завершите текущий диалог или нажмите /cancel.")
        return
    await state.set_state(CompareFlow.waiting_inn1)
    await message.answer("⚖️ <b>Сравнение компаний</b>\n\nВведите ИНН <b>первой</b> компании:",
                         parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════
# ПРЕДЛОЖЕНИЕ — FSM
# ═══════════════════════════════════════════════

@dp.message(F.text.regexp(r"(?i)^предложени[еяe]"))
async def proposal_start(message: Message, state: FSMContext) -> None:
    inn = _find_inn(message.text or "")
    if inn:
        is_valid, err = validate_inn(inn)
        if not is_valid:
            await message.answer(f"❌ {err}")
            return
        await state.update_data(inn=inn)
        await state.set_state(ProposalFlow.waiting_purpose)
        await message.answer(
            f"ИНН <code>{inn}</code> принят.\n\nВопрос 1️⃣ — <b>Какое назначение платежа?</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await state.set_state(ProposalFlow.waiting_inn)
        await message.answer("Введите ИНН компании (10 или 12 цифр):")


@dp.message(ProposalFlow.waiting_inn)
async def proposal_get_inn(message: Message, state: FSMContext) -> None:
    inn = (message.text or "").strip()
    is_valid, err = validate_inn(inn)
    if not is_valid:
        await message.answer(f"❌ {err}\n\nВведите ИНН ещё раз:")
        return
    await state.update_data(inn=inn)
    await state.set_state(ProposalFlow.waiting_purpose)
    await message.answer("Вопрос 1️⃣ — <b>Какое назначение платежа?</b>", parse_mode=ParseMode.HTML)


@dp.message(ProposalFlow.waiting_purpose)
async def proposal_get_purpose(message: Message, state: FSMContext) -> None:
    await state.update_data(purpose=message.text or "")
    await state.set_state(ProposalFlow.waiting_price)
    await message.answer("Вопрос 2️⃣ — <b>Цена дисконта?</b>", parse_mode=ParseMode.HTML)


@dp.message(ProposalFlow.waiting_price)
async def proposal_get_price(message: Message, state: FSMContext) -> None:
    await state.update_data(price=message.text or "")
    await state.set_state(ProposalFlow.waiting_term)
    await message.answer("Вопрос 3️⃣ — <b>Срок отгрузки?</b>", parse_mode=ParseMode.HTML)


@dp.message(ProposalFlow.waiting_term)
async def proposal_get_term(message: Message, state: FSMContext) -> None:
    await state.update_data(term=message.text or "")
    await state.set_state(ProposalFlow.waiting_client)
    await message.answer("Вопрос 4️⃣ — <b>Кому предлагаем?</b>", parse_mode=ParseMode.HTML)


@dp.message(ProposalFlow.waiting_client)
async def proposal_get_client(message: Message, state: FSMContext) -> None:
    await state.update_data(client=message.text or "")
    data = await state.get_data()
    await state.clear()

    inn = data["inn"]
    processing_msg = await message.answer("⏳ Проверяю компанию, формирую предложение...")

    try:
        check = await _full_check(inn)
        counter = next_proposal()
        report = format_proposal(
            number=counter["number"],
            fields=check["fields"],
            purpose=data.get("purpose", ""),
            price=data.get("price", ""),
            term=data.get("term", ""),
            client=data.get("client", ""),
            zchb_data=check.get("zchb_data"),
            zsk_data=check["zsk_data"],
            rp_data=check["rp_data"],
            links=check["links"],
        )
        await _safe_delete(processing_msg)
        await _send_long_message(message, report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await message.answer(f"📋 Предложение №{counter['number']} создано. Сегодня: {counter['today']}")
    except DaDataError as e:
        await _safe_delete(processing_msg)
        await message.answer(f"⚠️ {e}")
    except Exception as e:
        await _safe_delete(processing_msg)
        await message.answer("❌ Ошибка. Попробуйте позже.")
        logger.exception("Proposal error INN %s: %s", inn, e)


# ═══════════════════════════════════════════════
# ЗАПРОС СЧЕТА — FSM
# ═══════════════════════════════════════════════

@dp.message(F.text.regexp(r"(?i)^запрос\s*счет"))
async def invoice_start(message: Message, state: FSMContext) -> None:
    inn = _find_inn(message.text or "")
    if inn:
        is_valid, err = validate_inn(inn)
        if not is_valid:
            await message.answer(f"❌ {err}")
            return
        await state.update_data(inn=inn)
        await state.set_state(InvoiceFlow.waiting_purpose)
        await message.answer(
            f"ИНН <code>{inn}</code> принят.\n\nВопрос 1️⃣ — <b>Какое назначение платежа?</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await state.set_state(InvoiceFlow.waiting_inn)
        await message.answer("Введите ИНН компании (10 или 12 цифр):")


@dp.message(InvoiceFlow.waiting_inn)
async def invoice_get_inn(message: Message, state: FSMContext) -> None:
    inn = (message.text or "").strip()
    is_valid, err = validate_inn(inn)
    if not is_valid:
        await message.answer(f"❌ {err}\n\nВведите ИНН ещё раз:")
        return
    await state.update_data(inn=inn)
    await state.set_state(InvoiceFlow.waiting_purpose)
    await message.answer("Вопрос 1️⃣ — <b>Какое назначение платежа?</b>", parse_mode=ParseMode.HTML)


@dp.message(InvoiceFlow.waiting_purpose)
async def invoice_get_purpose(message: Message, state: FSMContext) -> None:
    await state.update_data(purpose=message.text or "")
    await state.set_state(InvoiceFlow.waiting_target)
    await message.answer("Вопрос 2️⃣ — <b>На кого выставляем? (ИНН или название)</b>",
                         parse_mode=ParseMode.HTML)


@dp.message(InvoiceFlow.waiting_target)
async def invoice_get_target(message: Message, state: FSMContext) -> None:
    await state.update_data(target=message.text or "")
    await state.set_state(InvoiceFlow.waiting_from_whom)
    await message.answer("Вопрос 3️⃣ — <b>У кого запрашиваем?</b>", parse_mode=ParseMode.HTML)


@dp.message(InvoiceFlow.waiting_from_whom)
async def invoice_get_from_whom(message: Message, state: FSMContext) -> None:
    await state.update_data(from_whom=message.text or "")
    await state.set_state(InvoiceFlow.waiting_amount)
    await message.answer("Вопрос 4️⃣ — <b>Сумма?</b>", parse_mode=ParseMode.HTML)


@dp.message(InvoiceFlow.waiting_amount)
async def invoice_get_amount(message: Message, state: FSMContext) -> None:
    await state.update_data(amount=message.text or "")
    await state.set_state(InvoiceFlow.waiting_issuer)
    await message.answer("Вопрос 5️⃣ — <b>От кого выставляем?</b>", parse_mode=ParseMode.HTML)


@dp.message(InvoiceFlow.waiting_issuer)
async def invoice_get_issuer(message: Message, state: FSMContext) -> None:
    await state.update_data(issuer=message.text or "")
    data = await state.get_data()
    await state.clear()

    target_inn = data.get("target", "") or data["inn"]
    target_name = None
    try:
        raw_data = await fetch_company_data(target_inn)
        fields = extract_company_fields(raw_data)
        target_name = fields.get("name")
    except Exception:
        pass

    counter = next_invoice()
    report = format_invoice(
        number=counter["number"],
        from_whom=data.get("from_whom", ""),
        purpose=data.get("purpose", ""),
        target_inn=target_inn,
        amount=data.get("amount", ""),
        issuer=data.get("issuer", ""),
        target_name=target_name,
    )
    await _send_long_message(message, report, parse_mode=ParseMode.HTML)
    await message.answer(f"🧾 Запрос счёта №{counter['number']} создан. Сегодня: {counter['today']}")


# ═══════════════════════════════════════════════
# СРАВНЕНИЕ — FSM
# ═══════════════════════════════════════════════

@dp.message(F.text.regexp(r"(?i)^сравни"))
async def compare_start(message: Message, state: FSMContext) -> None:
    await state.set_state(CompareFlow.waiting_inn1)
    await message.answer(
        "⚖️ <b>Сравнение компаний</b>\n\nВведите ИНН <b>первой</b> компании:",
        parse_mode=ParseMode.HTML,
    )


@dp.message(CompareFlow.waiting_inn1)
async def compare_get_inn1(message: Message, state: FSMContext) -> None:
    inn = (message.text or "").strip()
    is_valid, err = validate_inn(inn)
    if not is_valid:
        await message.answer(f"❌ {err}\n\nВведите ИНН первой компании:")
        return
    await state.update_data(inn1=inn)
    await state.set_state(CompareFlow.waiting_inn2)
    await message.answer(
        f"✅ Первая: <code>{inn}</code>\n\nТеперь введите ИНН <b>второй</b> компании:",
        parse_mode=ParseMode.HTML,
    )


@dp.message(CompareFlow.waiting_inn2)
async def compare_get_inn2(message: Message, state: FSMContext) -> None:
    inn2 = (message.text or "").strip()
    is_valid, err = validate_inn(inn2)
    if not is_valid:
        await message.answer(f"❌ {err}\n\nВведите ИНН второй компании:")
        return

    data = await state.get_data()
    inn1 = data["inn1"]
    await state.clear()

    if inn1 == inn2:
        await message.answer("❌ ИНН одинаковые.")
        return

    processing_msg = await message.answer("⏳ Проверяю обе компании...")
    try:
        check1, check2 = await asyncio.gather(_full_check(inn1), _full_check(inn2))
        report = format_comparison(check1, check2)
        await _safe_delete(processing_msg)
        await _send_long_message(message, report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except DaDataError as e:
        await _safe_delete(processing_msg)
        await message.answer(f"⚠️ {e}")
    except Exception as e:
        await _safe_delete(processing_msg)
        await message.answer("❌ Ошибка сравнения. Попробуйте позже.")
        logger.exception("Compare error %s vs %s: %s", inn1, inn2, e)


# ═══════════════════════════════════════════════
# INLINE-КНОПКИ
# ═══════════════════════════════════════════════

def _extract_inn_from_callback(callback: CallbackQuery) -> str | None:
    """Извлекает и валидирует ИНН из callback_data."""
    inn = callback.data.split(":", 1)[1]
    is_valid, _ = validate_inn(inn)
    if not is_valid:
        return None
    return inn


@dp.callback_query(F.data.startswith("proposal:"))
async def cb_proposal(callback: CallbackQuery, state: FSMContext) -> None:
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    await state.update_data(inn=inn)
    await state.set_state(ProposalFlow.waiting_purpose)
    await callback.message.answer(
        f"📝 Предложение для ИНН <code>{inn}</code>\n\n"
        "Вопрос 1️⃣ — <b>Какое назначение платежа?</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("invoice:"))
async def cb_invoice(callback: CallbackQuery, state: FSMContext) -> None:
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    await state.update_data(inn=inn)
    await state.set_state(InvoiceFlow.waiting_purpose)
    await callback.message.answer(
        f"🧾 Запрос счёта для ИНН <code>{inn}</code>\n\n"
        "Вопрос 1️⃣ — <b>Какое назначение платежа?</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("refresh:"))
async def cb_refresh(callback: CallbackQuery) -> None:
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    user_id = callback.from_user.id
    await callback.answer("⏳ Обновляю...")

    cache.delete(f"check:{inn}")

    try:
        check = await _full_check(inn)
        access = await sub.check_access(user_id)
        entity_type = check["fields"].get("entity_type", "ul")
        if access.get("full_report"):
            report = format_report(
                check["fields"],
                links=check["links"],
                zchb_data=check.get("zchb_data"),
                zsk_data=check["zsk_data"],
                rp_data=check["rp_data"],
                fin_history=check["fin_history"],
                fns_data=check.get("fns_data"),
                sanctions_data=check.get("sanctions_data"),
                cbrf_data=check.get("cbrf_data"),
            )
        else:
            report = format_report_free(
                check["fields"],
                zchb_data=check.get("zchb_data"),
                zsk_data=check["zsk_data"],
                rp_data=check["rp_data"],
                fns_data=check.get("fns_data"),
                sanctions_data=check.get("sanctions_data"),
                cbrf_data=check.get("cbrf_data"),
            )
        # Если отчёт длинный — удаляем старое и отправляем новое
        if len(report) > 4000:
            await _safe_delete(callback.message)
            await _send_long_message(
                callback.message,
                report,
                parse_mode=ParseMode.HTML,
                reply_markup=_report_keyboard(inn, full=access.get("full_report", False), entity_type=entity_type, show_docs=await db.a_has_docs_access(user_id)),
                disable_web_page_preview=True,
            )
        else:
            await callback.message.edit_text(
                report,
                parse_mode=ParseMode.HTML,
                reply_markup=_report_keyboard(inn, full=access.get("full_report", False), entity_type=entity_type, show_docs=await db.a_has_docs_access(user_id)),
                disable_web_page_preview=True,
            )
    except DaDataError as e:
        await callback.message.answer(f"⚠️ {e}")
    except Exception as e:
        await callback.message.answer("❌ Ошибка обновления.")
        logger.exception("Refresh error INN %s: %s", inn, e)


@dp.callback_query(F.data.startswith("courts:"))
async def cb_courts(callback: CallbackQuery) -> None:
    """Детальная информация по судам и ФССП."""
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    access = await sub.check_access(callback.from_user.id)
    if not access.get("full_report"):
        await callback.answer("❌ Доступно в тарифе Pro", show_alert=True)
        return
    await callback.answer("⏳ Загружаю...")
    check = cache.get(f"check:{inn}")
    if not check:
        check = await _full_check(inn)
    # Запрашиваем детальные дела из ЗЧБ API
    court_cases = await fetch_court_cases(inn)
    text = format_courts_detail(
        check.get("zsk_data"),
        zchb_data=check.get("zchb_data"),
        court_cases=court_cases,
    )
    await _send_long_message(callback.message, text, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("fnscheck:"))
async def cb_fns_check(callback: CallbackQuery) -> None:
    """Детальная информация ФНС + блокировки + отказы ЦБ."""
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    access = await sub.check_access(callback.from_user.id)
    if not access.get("full_report"):
        await callback.answer("❌ Доступно в тарифе Pro", show_alert=True)
        return
    await callback.answer("⏳ Загружаю...")
    check = cache.get(f"check:{inn}")
    if not check:
        check = await _full_check(inn)
    text = format_fns_detail(check.get("fns_data"), check.get("cbrf_data"))
    await _send_long_message(callback.message, text, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("pdf:"))
async def cb_export_pdf(callback: CallbackQuery) -> None:
    """Экспорт полного отчёта в PDF."""
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    access = await sub.check_access(callback.from_user.id)
    if not access.get("full_report"):
        await callback.answer("❌ Доступно в тарифе Pro", show_alert=True)
        return
    await callback.answer("⏳ Генерирую PDF...")
    try:
        check = cache.get(f"check:{inn}")
        if not check:
            check = await _full_check(inn)
        from export_pdf import generate_report_pdf
        pdf_bytes = generate_report_pdf(
            check["fields"],
            zsk_data=check.get("zsk_data"),
            fns_data=check.get("fns_data"),
            fin_history=check.get("fin_history"),
            sanctions_data=check.get("sanctions_data"),
            cbrf_data=check.get("cbrf_data"),
        )
        company_name = check["fields"].get("name", "Компания")
        # Очистка имени файла
        safe_name = "".join(c for c in company_name if c.isalnum() or c in " -_")[:40]
        filename = f"Отчёт_{safe_name}_{inn}.pdf"
        doc = BufferedInputFile(pdf_bytes, filename=filename)
        await callback.message.answer_document(doc)
    except Exception as e:
        await callback.message.answer("❌ Ошибка генерации PDF.")
        logger.exception("PDF export error INN %s: %s", inn, e)


@dp.callback_query(F.data.startswith("ai:"))
async def cb_ai_recommendation(callback: CallbackQuery) -> None:
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    user_id = callback.from_user.id

    # Проверяем доступ к ИИ
    access = await sub.check_access(user_id)
    if not access.get("ai_analysis"):
        await callback.answer("❌ ИИ-анализ доступен в тарифе Pro", show_alert=True)
        return

    await callback.answer("🤖 Анализирую...")

    try:
        check = await _full_check(inn)
        name = check["fields"].get("name", "компанию")

        recommendation = await generate_recommendation(
            fields=check["fields"],
            zchb_data=check.get("zchb_data"),
            zsk_data=check["zsk_data"],
            rp_data=check["rp_data"],
            fin_history=check["fin_history"],
            fns_data=check.get("fns_data"),
            api_key=GIGACHAT_CREDENTIALS or None,
        )

        header = "🤖 <b>ИИ-анализ</b>" if GIGACHAT_CREDENTIALS else "📊 <b>Авто-анализ</b>"
        await _send_long_message(
            callback.message,
            f"{header}\nКомпания: <b>{name}</b>\n\n{recommendation}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await callback.message.answer("❌ Ошибка ИИ-анализа.")
        logger.exception("AI error INN %s: %s", inn, e)


@dp.callback_query(F.data.startswith("egrul:"))
async def cb_egrul_pdf(callback: CallbackQuery) -> None:
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return
    user_id = callback.from_user.id

    # Проверяем доступ
    access = await sub.check_access(user_id)
    if not access.get("egrul"):
        await callback.answer("❌ Выписка доступна в тарифе Pro", show_alert=True)
        return

    is_ip = len(inn) == 12
    label = "ЕГРИП" if is_ip else "ЕГРЮЛ"
    await callback.answer(f"📄 Скачиваю выписку {label}...")

    try:
        pdf_bytes = await get_egrul_pdf(inn)
        if pdf_bytes:
            doc = BufferedInputFile(pdf_bytes, filename=f"{label}_{inn}.pdf")
            await callback.message.answer_document(
                doc,
                caption=f"📄 Выписка {label} для ИНН <code>{inn}</code>\nИсточник: egrul.nalog.ru",
                parse_mode=ParseMode.HTML,
            )
        else:
            await callback.message.answer(f"❌ Не удалось скачать выписку {label}.")
    except Exception as e:
        await callback.message.answer("❌ Ошибка загрузки выписки.")
        logger.exception("EGRUL PDF error INN %s: %s", inn, e)


# ── Новые callback-хендлеры ──

@dp.callback_query(F.data.startswith("history:"))
async def cb_history(callback: CallbackQuery) -> None:
    """История изменений компании (API-FNS changes)."""
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return

    access = await sub.check_access(callback.from_user.id)
    if not access.get("full_report"):
        await callback.answer("❌ История доступна в тарифе Pro", show_alert=True)
        return

    await callback.answer("📜 Загружаю историю...")

    try:
        raw = await fetch_changes(inn)
        if not raw:
            await callback.message.answer("ℹ️ История изменений пока недоступна для данной компании.")
            return
        changes = extract_changes_data(raw)
        check = await _full_check(inn)
        company_name = check["fields"].get("name", "Компания")
        report = format_changes(changes, company_name)
        await _send_long_message(callback.message, report, parse_mode=ParseMode.HTML)
    except Exception as e:
        await callback.message.answer("❌ Ошибка загрузки истории.")
        logger.exception("History error INN %s: %s", inn, e)


@dp.callback_query(F.data.startswith("affiliated:"))
async def cb_affiliated(callback: CallbackQuery) -> None:
    """Связанные компании через DaData findAffiliated."""
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return

    access = await sub.check_access(callback.from_user.id)
    if not access.get("full_report"):
        await callback.answer("❌ Связи доступны в тарифе Pro", show_alert=True)
        return

    await callback.answer("🔗 Ищу связанные компании...")

    try:
        from affiliated_client import fetch_affiliated
        check = cache.get(f"check:{inn}")
        if not check:
            check = await _full_check(inn)
        companies = await fetch_affiliated(
            inn,
            fields=check.get("fields"),
            zchb_data=check.get("zchb_data"),
        )
        if not companies:
            await callback.message.answer(
                "ℹ️ Связанные компании не найдены.\n\n"
                "<i>Поиск ведётся по ФИО руководителя и учредителей.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        report = format_affiliated(companies)
        await _send_long_message(callback.message, report, parse_mode=ParseMode.HTML)
    except Exception as e:
        await callback.message.answer("❌ Ошибка поиска связей.")
        logger.exception("Affiliated error INN %s: %s", inn, e)


@dp.callback_query(F.data.startswith("contracts:"))
async def cb_contracts(callback: CallbackQuery) -> None:
    """Госзакупки через clearspending API."""
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return

    access = await sub.check_access(callback.from_user.id)
    if not access.get("full_report"):
        await callback.answer("❌ Госзакупки доступны в тарифе Pro", show_alert=True)
        return

    await callback.answer("📋 Загружаю госзакупки...")

    try:
        from goscontract_client import fetch_contracts
        data = await fetch_contracts(inn)
        if not data:
            await callback.message.answer("ℹ️ Госзакупки не найдены для данной компании.")
            return
        report = format_contracts(data)
        await _send_long_message(callback.message, report, parse_mode=ParseMode.HTML)
    except Exception as e:
        await callback.message.answer("❌ Ошибка загрузки госзакупок.")
        logger.exception("Contracts error INN %s: %s", inn, e)


@dp.callback_query(F.data.startswith("export1c:"))
async def cb_export_1c(callback: CallbackQuery) -> None:
    """Выгрузка реквизитов для 1С (XML)."""
    inn = _extract_inn_from_callback(callback)
    if not inn:
        await callback.answer("❌ Некорректный ИНН", show_alert=True)
        return

    access = await sub.check_access(callback.from_user.id)
    if not access.get("full_report"):
        await callback.answer("❌ Выгрузка 1С доступна в тарифе Pro", show_alert=True)
        return

    await callback.answer("📥 Формирую файл для 1С...")

    try:
        from export_1c import generate_1c_xml
        check = await _full_check(inn)
        xml_bytes = generate_1c_xml(check["fields"])
        doc = BufferedInputFile(xml_bytes, filename=f"1C_контрагент_{inn}.xml")
        await callback.message.answer_document(
            doc,
            caption=f"📥 Реквизиты для 1С — ИНН <code>{inn}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await callback.message.answer("❌ Ошибка формирования файла 1С.")
        logger.exception("Export 1C error INN %s: %s", inn, e)


# ═══════════════════════════════════════════════
# ОСНОВНОЙ ОБРАБОТЧИК — ПРОВЕРКА ПО ИНН
# ═══════════════════════════════════════════════

async def _bulk_check(inns: list[str], message: Message, access: dict) -> None:
    """Массовая проверка до 10 ИНН."""
    user_id = message.from_user.id
    processing_msg = await message.answer(f"⏳ Массовая проверка {len(inns)} ИНН...")

    results: list[dict] = []
    for inn in inns:
        try:
            check = await _full_check(inn)
            results.append({"inn": inn, "fields": check["fields"], "ok": True})
            # Списываем проверку за каждый ИНН
            await sub.after_check(user_id, access)
        except Exception as e:
            results.append({"inn": inn, "ok": False, "error": str(e)})

    report = format_bulk_report(results)
    await _safe_delete(processing_msg)
    await _send_long_message(message, report, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ── Команды мониторинга ──

@dp.message(Command("watch"))
async def cmd_watch(message: Message) -> None:
    """Добавить компанию в мониторинг: /watch ИНН"""
    await _register_user(message)
    user_id = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /watch <code>ИНН</code>", parse_mode=ParseMode.HTML)
        return

    inn = parts[1].strip()
    is_valid, err = validate_inn(inn)
    if not is_valid:
        await message.answer(f"❌ {err}")
        return

    plan = await db.a_get_user_plan(user_id)
    limits = {"free": 0, "promo": 0, "pro": 5, "business": 20, "admin": 999}
    max_watches = limits.get(plan, 0)
    current = await db.a_get_user_watchlist(user_id)

    if len(current) >= max_watches:
        if max_watches == 0:
            await message.answer("❌ Мониторинг доступен в тарифе Pro.\n/tariffs — посмотреть тарифы")
        else:
            await message.answer(f"❌ Лимит мониторинга: {max_watches} компаний для вашего тарифа.")
        return

    # Получаем название компании
    try:
        check = await _full_check(inn)
        company_name = check["fields"].get("name", "Компания")
        data_hash = hashlib.md5(str(check["fields"]).encode()).hexdigest()
    except Exception:
        company_name = inn
        data_hash = ""

    success = await db.a_add_watch(user_id, inn, company_name, data_hash)
    if success:
        await message.answer(
            f"✅ <b>{company_name}</b> добавлена в мониторинг.\n"
            f"Вы будете получать уведомления при изменениях.\n\n"
            f"/watchlist — ваш список мониторинга",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer("ℹ️ Эта компания уже в вашем мониторинге.")


@dp.message(Command("unwatch"))
async def cmd_unwatch(message: Message) -> None:
    """Убрать компанию из мониторинга: /unwatch ИНН"""
    user_id = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unwatch <code>ИНН</code>", parse_mode=ParseMode.HTML)
        return

    inn = parts[1].strip()
    await db.a_remove_watch(user_id, inn)
    await message.answer(f"✅ ИНН <code>{inn}</code> удалён из мониторинга.", parse_mode=ParseMode.HTML)


@dp.message(Command("watchlist"))
async def cmd_watchlist(message: Message) -> None:
    """Список мониторинга."""
    await _register_user(message)
    watches = await db.a_get_user_watchlist(message.from_user.id)
    if not watches:
        await message.answer(
            "📭 Ваш мониторинг пуст.\n\n"
            "Добавьте: /watch <code>ИНН</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["👁 <b>Ваш мониторинг</b>", ""]
    for w in watches:
        lines.append(f"• <b>{w['company_name']}</b> — <code>{w['inn']}</code>")
        lines.append(f"  Добавлено: {w['created_at'][:10]}")
    lines.append("")
    lines.append("Убрать: /unwatch <code>ИНН</code>")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(F.text)
async def handle_inn(message: Message, state: FSMContext) -> None:
    # В группах канальные посты не имеют from_user
    if not message.from_user:
        return

    # Если идёт диалог FSM — не мешаем
    if await state.get_state():
        return

    text = (message.text or "").strip()
    is_group = message.chat.type in ("group", "supergroup")

    # В группах: ТОЛЬКО чистый ИНН
    if is_group:
        if not re.fullmatch(r"\d{10}|\d{12}", text):
            return

    # ── Rate limiter ──
    user_id = message.from_user.id
    if _check_rate_limit(user_id) and not await _is_admin(user_id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return

    # ── Массовая проверка (несколько ИНН через запятую/пробел/перенос) ──
    all_inns = re.findall(r"\b(\d{10}|\d{12})\b", text)
    if len(all_inns) > 1:
        # Массовая проверка — только для Pro+
        await _register_user(message)
        access = await sub.check_access(user_id)
        plan = access.get("plan", "free")
        if plan not in ("pro", "business", "admin"):
            await message.answer(
                "❌ Массовая проверка доступна в тарифе Pro.\n/tariffs — посмотреть тарифы",
                parse_mode=ParseMode.HTML,
            )
            return

        unique_inns = list(dict.fromkeys(all_inns))[:10]  # max 10, unique
        # Валидируем каждый
        valid_inns = []
        for inn in unique_inns:
            is_valid, _ = validate_inn(inn)
            if is_valid:
                valid_inns.append(inn)
        if not valid_inns:
            await message.answer("❌ Не найдены корректные ИНН.")
            return

        await _bulk_check(valid_inns, message, access)
        return

    is_valid, error = validate_inn(text)
    if not is_valid:
        if is_group:
            return
        # ── Поиск по названию компании ──
        # Если текст содержит буквы (не числа) и длина ≥ 3 — ищем через DaData
        if len(text) >= 3 and re.search(r"[а-яА-ЯёЁa-zA-Z]", text):
            results = await search_company_by_name(text, count=5)
            if results:
                lines = ["🔍 <b>Найдены компании:</b>\n"]
                for r in results:
                    status_icon = "🟢" if r["status"] == "ACTIVE" else "🔴"
                    lines.append(
                        f"{status_icon} <b>{html_mod.escape(r['name'])}</b>\n"
                        f"   ИНН: <code>{r['inn']}</code> ({r['entity']})\n"
                        f"   {html_mod.escape(r['address'])}\n"
                    )
                lines.append("Отправьте ИНН из списка для полной проверки.")
                await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
                return
        await message.answer(
            f"❌ {error}\n\n"
            "Отправьте <b>ИНН</b> (10 или 12 цифр) или <b>название</b> компании ⬇️",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KB,
        )
        return

    # ── Регистрируем пользователя ──
    await _register_user(message)

    # ── Проверяем лимиты ──
    access = await sub.check_access(user_id)
    if not access["allowed"]:
        await message.answer(access["message"], parse_mode=ParseMode.HTML)
        return

    processing_msg = await message.answer("⏳ Проверяю компанию по всем источникам...")

    try:
        check = await _full_check(text)
        entity_type = check["fields"].get("entity_type", "ul")

        # Формируем отчёт в зависимости от плана
        if access["full_report"]:
            report = format_report(
                check["fields"],
                links=check["links"],
                zchb_data=check.get("zchb_data"),
                zsk_data=check["zsk_data"],
                rp_data=check["rp_data"],
                fin_history=check["fin_history"],
                fns_data=check.get("fns_data"),
                sanctions_data=check.get("sanctions_data"),
                cbrf_data=check.get("cbrf_data"),
            )
        else:
            report = format_report_free(
                check["fields"],
                zchb_data=check.get("zchb_data"),
                zsk_data=check["zsk_data"],
                rp_data=check["rp_data"],
                fns_data=check.get("fns_data"),
                sanctions_data=check.get("sanctions_data"),
                cbrf_data=check.get("cbrf_data"),
            )

        # Списываем проверку и получаем текст статуса
        status_text = await sub.after_check(user_id, access)

        await _safe_delete(processing_msg)
        await _send_long_message(
            message,
            report,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_report_keyboard(text, full=access["full_report"], entity_type=entity_type, show_docs=await db.a_has_docs_access(user_id)),
        )
        # Отдельное сообщение со статусом
        await message.answer(status_text, parse_mode=ParseMode.HTML)

    except DaDataError as e:
        await _safe_delete(processing_msg)
        await message.answer(f"⚠️ {e}")
        logger.warning("DaData error INN %s: %s", text, e)
    except Exception as e:
        await _safe_delete(processing_msg)
        await message.answer("❌ Ошибка. Попробуйте позже.")
        logger.exception("Unexpected error INN %s: %s", text, e)


# ═══════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════

async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан в .env")
        sys.exit(1)
    if not DADATA_API_KEY:
        logger.error("DADATA_API_KEY не задан в .env")
        sys.exit(1)

    # Инициализируем базу данных
    await db.run_sync(db.init_db)
    logger.info("Database ready")

    # Генерируем стартовые промокоды если их нет
    existing_promos = await db.a_get_all_promo_codes()
    if not existing_promos:
        codes = await db.a_generate_promo_codes(count=20, checks_per_use=3)
        logger.info("Generated %d initial promo codes", len(codes))
        for code in codes:
            logger.info("  PROMO: %s", code)

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Команды бота (только публичные — админские НЕ показываем)
    await bot.set_my_commands([
        BotCommand(command="start", description="Начало работы"),
        BotCommand(command="profile", description="Мой профиль и лимиты"),
        BotCommand(command="tariffs", description="Тарифные планы"),
        BotCommand(command="promo", description="Активировать промокод"),
        BotCommand(command="watch", description="Мониторинг компании"),
        BotCommand(command="watchlist", description="Список мониторинга"),
        BotCommand(command="support", description="Связаться с поддержкой"),
        BotCommand(command="stats", description="Статистика предложений"),
        BotCommand(command="invoices", description="Статистика счетов"),
        BotCommand(command="agreement", description="Пользовательское соглашение"),
        BotCommand(command="privacy", description="Политика конфиденциальности"),
        BotCommand(command="cancel", description="Отменить диалог"),
    ])

    # Запускаем фоновый мониторинг
    from monitor import start_monitoring
    asyncio.create_task(start_monitoring(bot))
    logger.info("Background monitoring started")

    logger.info("Бот запущен. Ожидаю ИНН...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
