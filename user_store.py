"""Хранение профилей пользователей.

Каждый пользователь имеет:
- Тариф (free / start / pro / business)
- Счётчик проверок сегодня
- Дата последнего сброса счётчика
- Общее количество проверок
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, Optional

logger = logging.getLogger("financial-architect")

STORAGE_FILE = os.getenv("USERS_FILE", "users.json")

# Лимиты проверок по тарифам (в день)
TARIFF_LIMITS: Dict[str, Optional[int]] = {
    "free": 3,
    "start": 50,
    "pro": 300,
    "business": None,  # безлимит
}

# Отображаемые названия тарифов
TARIFF_LABELS = {
    "free": "🆓 Free",
    "start": "⭐️ Start",
    "pro": "💎 Pro",
    "business": "🏆 Business",
}

# Возможности по тарифам
TARIFF_FEATURES = {
    "free": {
        "📋 Краткий отчёт": True,
        "📄 Полный отчёт": False,
        "🏛 ЕГРЮЛ": False,
        "⚖️ Суды / ФССП": False,
        "🛑 Стоп-листы": False,
        "🤖 ИИ-анализ": False,
        "🔗 Связи": False,
        "📜 История": False,
        "👁 Мониторинг": False,
        "🔌 API доступ": False,
        "📦 Массовые проверки": False,
        "📑 PDF / 1С экспорт": False,
    },
    "start": {
        "📋 Краткий отчёт": True,
        "📄 Полный отчёт": True,
        "🏛 ЕГРЮЛ": True,
        "⚖️ Суды / ФССП": True,
        "🛑 Стоп-листы": True,
        "🤖 ИИ-анализ": False,
        "🔗 Связи": False,
        "📜 История": False,
        "👁 Мониторинг": False,
        "🔌 API доступ": False,
        "📦 Массовые проверки": False,
        "📑 PDF / 1С экспорт": False,
    },
    "pro": {
        "📋 Краткий отчёт": True,
        "📄 Полный отчёт": True,
        "🏛 ЕГРЮЛ": True,
        "⚖️ Суды / ФССП": True,
        "🛑 Стоп-листы": True,
        "🤖 ИИ-анализ": True,
        "🔗 Связи": True,
        "📜 История": True,
        "👁 Мониторинг": True,
        "🔌 API доступ": False,
        "📦 Массовые проверки": False,
        "📑 PDF / 1С экспорт": False,
    },
    "business": {
        "📋 Краткий отчёт": True,
        "📄 Полный отчёт": True,
        "🏛 ЕГРЮЛ": True,
        "⚖️ Суды / ФССП": True,
        "🛑 Стоп-листы": True,
        "🤖 ИИ-анализ": True,
        "🔗 Связи": True,
        "📜 История": True,
        "👁 Мониторинг": True,
        "🔌 API доступ": True,
        "📦 Массовые проверки": True,
        "📑 PDF / 1С экспорт": True,
    },
}


@dataclass
class UserProfile:
    user_id: int
    tariff: str = "free"
    checks_today: int = 0
    checks_date: str = ""        # ISO дата последнего сброса: "2024-01-15"
    checks_total: int = 0

    def reset_if_new_day(self) -> None:
        today = date.today().isoformat()
        if self.checks_date != today:
            self.checks_today = 0
            self.checks_date = today

    def can_check(self) -> bool:
        self.reset_if_new_day()
        limit = TARIFF_LIMITS.get(self.tariff)
        if limit is None:
            return True  # безлимит
        return self.checks_today < limit

    def remaining_checks(self) -> Optional[int]:
        self.reset_if_new_day()
        limit = TARIFF_LIMITS.get(self.tariff)
        if limit is None:
            return None  # безлимит
        return max(0, limit - self.checks_today)

    def daily_limit(self) -> Optional[int]:
        return TARIFF_LIMITS.get(self.tariff)

    def increment(self) -> None:
        self.reset_if_new_day()
        self.checks_today += 1
        self.checks_total += 1


class UserStore:
    """Хранилище профилей пользователей (JSON-файл)."""

    def __init__(self, filepath: str = STORAGE_FILE) -> None:
        self.filepath = filepath
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as exc:
                logger.warning("UserStore: failed to load %s: %s", self.filepath, exc)
                self._data = {}

    def _save(self) -> None:
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("UserStore: failed to save %s: %s", self.filepath, exc)

    def get(self, user_id: int) -> UserProfile:
        key = str(user_id)
        if key not in self._data:
            profile = UserProfile(user_id=user_id)
            self._data[key] = asdict(profile)
            self._save()
            return profile
        return UserProfile(**self._data[key])

    def save_profile(self, profile: UserProfile) -> None:
        self._data[str(profile.user_id)] = asdict(profile)
        self._save()

    def increment_checks(self, user_id: int) -> UserProfile:
        profile = self.get(user_id)
        profile.increment()
        self.save_profile(profile)
        return profile

    def set_tariff(self, user_id: int, tariff: str) -> UserProfile:
        profile = self.get(user_id)
        profile.tariff = tariff
        self.save_profile(profile)
        return profile
