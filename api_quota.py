"""Глобальный учёт расхода платных API на уровне бота.

Зачем: 100к₽ бюджета на DaData/ФНС/ЗЧБ — нужно гарантировать, что
один greedy-юзер или сбойный цикл не сожрёт всю квоту за день.
Дополнительно к per-user лимитам (Quick/Full в user_store), здесь
учитывается **глобальный** расход бота по каждому апстриму:

- DaData fetch:  лимит из DADATA_DAILY_LIMIT  (по умолчанию 200/день)
- DaData suggest: DADATA_SUGGEST_DAILY_LIMIT (200/день)
- ФНС fetch:      FNS_DAILY_LIMIT             (80/день — fns-card дорогой)
- ЗЧБ:            ZCHB_DAILY_LIMIT            (200/день)
- СБИС:           SBIS_DAILY_LIMIT            (100/день)
- GigaChat:       GIGACHAT_DAILY_LIMIT        (300/день)

Если апстрим достиг 95% дневного лимита — raises ApiQuotaExhausted,
клиенты ловят и возвращают None; CompanyService.fetch продолжает
работать с тем, что осталось (graceful degradation). На 80% — WARNING
в лог + Sentry breadcrumb.

Хранилище: JSON-файл в CACHE_DIR/api_quota.json с записью
{api: {date_iso: count}}; устаревшие даты подтираются при чтении.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("financial-architect")


class ApiQuotaExhausted(Exception):
    """Бросается, когда апстрим достиг 95% дневной квоты.
    Клиенты должны ловить и возвращать None — fetch продолжается
    с другими источниками (graceful degradation)."""


# Имена API → env-переменная с дневным лимитом.
# Лимиты подобраны так, что 100к₽ бюджета хватает на ~30 дней при
# среднем расходе. Перенастраиваются через env без пересборки.
_DEFAULT_LIMITS: Dict[str, int] = {
    "dadata_fetch":   200,
    "dadata_suggest": 200,
    "fns":             80,   # fns-card стоит x10 — экономим
    "zchb":           200,
    "sbis":           100,
    "gigachat":       300,
    "zakupki":        100,   # Block E: данные госзакупок (опц., через ENV)
}

# Порог «приближение к лимиту» — для алертов, без блокировки.
_NEAR_THRESHOLD = 0.80
# Порог auto-degradation — после него raise ApiQuotaExhausted.
_EXHAUSTED_THRESHOLD = 0.95


def _env_limit(api: str) -> Optional[int]:
    """Читает дневной лимит из env (ENV: <API>_DAILY_LIMIT в верхнем
    регистре). Если переменная не задана — берём дефолт. Если задана
    как пустая или 0 — безлимит (None)."""
    env_name = f"{api.upper()}_DAILY_LIMIT"
    raw = os.getenv(env_name, "")
    if raw == "":
        return _DEFAULT_LIMITS.get(api)
    try:
        n = int(raw)
        return n if n > 0 else None
    except ValueError:
        logger.warning(
            "ApiQuota: неверный формат %s=%r, использую default", env_name, raw,
        )
        return _DEFAULT_LIMITS.get(api)


class ApiQuota:
    """Хранилище расхода API. Один экземпляр на процесс (создаётся
    в main.py / company_service)."""

    def __init__(self, filepath: Optional[str] = None) -> None:
        if filepath is None:
            base = os.getenv("CACHE_DIR", ".cache")
            self.path = Path(base) / "api_quota.json"
        else:
            self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Структура: {"api_name": {"YYYY-MM-DD": count}}
        self._data: Dict[str, Dict[str, int]] = {}
        # Для одноразового WARNING'а при пересечении 80% — чтобы не
        # спамить логи на каждом инкременте.
        self._warned_today: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = {
                    k: dict(v) for k, v in raw.items() if isinstance(v, dict)
                }
        except Exception as exc:
            logger.warning("ApiQuota: не смог загрузить %s: %s", self.path, exc)

    def _save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
        except Exception as exc:
            logger.error("ApiQuota: не смог сохранить %s: %s", self.path, exc)

    def _today(self) -> str:
        return date.today().isoformat()

    def usage_today(self, api: str) -> int:
        """Сколько вызовов сделано к API сегодня (UTC-day по локали)."""
        return self._data.get(api, {}).get(self._today(), 0)

    def daily_limit(self, api: str) -> Optional[int]:
        """Дневной лимит API (None — безлимит, по env-конфигу)."""
        return _env_limit(api)

    def remaining(self, api: str) -> Optional[int]:
        limit = self.daily_limit(api)
        if limit is None:
            return None
        return max(0, limit - self.usage_today(api))

    def is_near_limit(self, api: str) -> bool:
        """80% и выше — пора алертить, но ещё не блокировать."""
        limit = self.daily_limit(api)
        if limit is None:
            return False
        return self.usage_today(api) >= int(limit * _NEAR_THRESHOLD)

    def is_exhausted(self, api: str) -> bool:
        """95% и выше — auto-degradation, raise ApiQuotaExhausted."""
        limit = self.daily_limit(api)
        if limit is None:
            return False
        return self.usage_today(api) >= int(limit * _EXHAUSTED_THRESHOLD)

    def check(self, api: str) -> None:
        """Поднимает ApiQuotaExhausted, если API в auto-degradation.

        Вызывается клиентами ПЕРЕД сетевым запросом (после miss кэша).
        """
        if self.is_exhausted(api):
            raise ApiQuotaExhausted(
                f"{api}: исчерпано {self.usage_today(api)} из {self.daily_limit(api)} "
                f"({_EXHAUSTED_THRESHOLD * 100:.0f}% дневного лимита)",
            )

    def record(self, api: str, count: int = 1) -> None:
        """Учёт N сетевых вызовов к API. Сохраняет в JSON.

        Дополнительно: на пересечении 80% порога — один WARNING
        в лог в течение дня (anti-spam).
        """
        if count <= 0:
            return
        today = self._today()
        bucket = self._data.setdefault(api, {})
        # Подчищаем старые даты (всё, что не сегодня) — чтобы файл не
        # рос бесконечно. Простая стратегия: храним только сегодня.
        for k in list(bucket.keys()):
            if k != today:
                del bucket[k]
        bucket[today] = bucket.get(today, 0) + count
        self._save()

        # Anti-spam алерт на пересечение 80%
        warn_key = f"{api}:{today}"
        if warn_key not in self._warned_today and self.is_near_limit(api):
            self._warned_today.add(warn_key)
            logger.warning(
                "ApiQuota: %s достиг 80%% дневного лимита (%d/%d). "
                "Скоро auto-degradation на 95%%.",
                api, self.usage_today(api), self.daily_limit(api),
            )

    def snapshot(self) -> Dict[str, dict]:
        """Снимок состояния всех известных API для админ-отчёта.

        Возвращает {api: {used, limit, remaining, percent}} —
        включая API без расходов сегодня (limit виден всегда)."""
        result: Dict[str, dict] = {}
        all_apis = set(_DEFAULT_LIMITS.keys()) | set(self._data.keys())
        for api in sorted(all_apis):
            used = self.usage_today(api)
            limit = self.daily_limit(api)
            if limit is None:
                pct = 0.0
                remaining: Optional[int] = None
            else:
                pct = round(100 * used / limit, 1) if limit > 0 else 0.0
                remaining = max(0, limit - used)
            result[api] = {
                "used": used,
                "limit": limit,
                "remaining": remaining,
                "percent": pct,
                "near": self.is_near_limit(api),
                "exhausted": self.is_exhausted(api),
            }
        return result


# Singleton-инстанс на процесс. Создаётся лениво.
_singleton: Optional[ApiQuota] = None


def get_quota() -> ApiQuota:
    """Получить shared singleton. Используется клиентами в decorator-
    style: from api_quota import get_quota; get_quota().record('dadata_fetch').
    """
    global _singleton
    if _singleton is None:
        _singleton = ApiQuota()
    return _singleton


def reset_singleton() -> None:
    """Сбросить singleton — нужно для тестов, где CACHE_DIR меняется
    через monkeypatch и старая инстанция указывает на старый файл."""
    global _singleton
    _singleton = None
