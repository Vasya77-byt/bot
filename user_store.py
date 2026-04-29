"""Хранение профилей пользователей.

Каждый пользователь имеет:
- Тариф (free / start / pro / business)
- Счётчик проверок сегодня
- Дата последнего сброса счётчика
- Общее количество проверок
- Подписка: дата окончания, токен карты для рекуррентных платежей, флаг автопродления
"""

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

# Сколько дней даём референту за каждую первую оплату приглашённого
REFERRAL_BONUS_DAYS = 15
# Тариф, который активируется референту-Free при первой оплате его приглашённого
REFERRAL_BONUS_TARIFF_FOR_FREE = "start"

logger = logging.getLogger("financial-architect")

STORAGE_FILE = os.getenv("USERS_FILE", "users.json")

# Лимиты проверок по тарифам (в день)
TARIFF_LIMITS: Dict[str, Optional[int]] = {
    "free": 3,
    "start": 50,
    "pro": 300,
    "business": None,  # безлимит
}

# Лимиты подписок на мониторинг ИНН (одновременно отслеживаемых)
TARIFF_MONITORING_LIMITS: Dict[str, Optional[int]] = {
    "free": 0,         # на free мониторинг недоступен
    "start": 5,
    "pro": 50,
    "business": None,  # безлимит
}

# Цены тарифов в рублях (месячная подписка)
TARIFF_PRICES: Dict[str, int] = {
    "start": 490,
    "pro": 1290,
    "business": 2490,
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
    # Подписка
    tariff_expires_at: str = ""      # ISO datetime в UTC, пусто для free
    card_token: str = ""             # токен сохранённой карты от Точки
    auto_renew: bool = True          # автопродление
    renewal_failures: int = 0        # счётчик подряд неудачных списаний
    last_payment_id: str = ""        # id последней операции
    email: str = ""                  # email для чека
    # Партнёрская программа
    referral_code: str = ""              # личный код вида "ref_<8 hex>"
    referrer_id: Optional[int] = None    # кто пригласил этого пользователя
    referral_bonus_granted: bool = False # бонус референту уже выдан (one-shot)
    referrals_count: int = 0             # сколько привлёк (включая Free)
    referrals_paid_count: int = 0        # сколько привлечённых оплатили
    referral_bonus_days_total: int = 0   # сколько дней получил суммарно

    def reset_if_new_day(self) -> None:
        today = date.today().isoformat()
        if self.checks_date != today:
            self.checks_today = 0
            self.checks_date = today

    def is_subscription_active(self) -> bool:
        """Активна ли платная подписка прямо сейчас."""
        if self.tariff == "free":
            return False
        if not self.tariff_expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.tariff_expires_at)
        except ValueError:
            return False
        return expires > datetime.now(timezone.utc)

    def effective_tariff(self) -> str:
        """Тариф с учётом истечения подписки.
        Если подписка на платный тариф истекла — возвращаем free."""
        if self.tariff == "free":
            return "free"
        if self.is_subscription_active():
            return self.tariff
        return "free"

    def can_check(self) -> bool:
        self.reset_if_new_day()
        limit = TARIFF_LIMITS.get(self.effective_tariff())
        if limit is None:
            return True  # безлимит
        return self.checks_today < limit

    def remaining_checks(self) -> Optional[int]:
        self.reset_if_new_day()
        limit = TARIFF_LIMITS.get(self.effective_tariff())
        if limit is None:
            return None  # безлимит
        return max(0, limit - self.checks_today)

    def daily_limit(self) -> Optional[int]:
        return TARIFF_LIMITS.get(self.effective_tariff())

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

    def _profile_from_raw(self, raw: dict) -> UserProfile:
        """Создаёт UserProfile из словаря, игнорируя неизвестные поля
        (для обратной совместимости со старыми users.json)."""
        known = {f for f in UserProfile.__dataclass_fields__}
        clean = {k: v for k, v in raw.items() if k in known}
        return UserProfile(**clean)

    def get(self, user_id: int) -> UserProfile:
        key = str(user_id)
        if key not in self._data:
            profile = UserProfile(
                user_id=user_id,
                referral_code=self._generate_referral_code(),
            )
            self._data[key] = asdict(profile)
            self._save()
            return profile

        profile = self._profile_from_raw(self._data[key])
        # Бэкфилл: у старых пользователей без кода — генерируем при первом get
        if not profile.referral_code:
            profile.referral_code = self._generate_referral_code()
            self._data[key] = asdict(profile)
            self._save()
        return profile

    def _generate_referral_code(self) -> str:
        """Генерирует уникальный реферальный код. Защита от коллизий —
        проверка по уже выданным."""
        existing = {
            raw.get("referral_code")
            for raw in self._data.values()
            if raw.get("referral_code")
        }
        for _ in range(20):
            code = f"ref_{uuid.uuid4().hex[:8]}"
            if code not in existing:
                return code
        # На практике 20 итераций uuid4 дают вероятность коллизии ~10^-50.
        # Если попали сюда — что-то сломано, кидаем явно.
        raise RuntimeError("Failed to generate unique referral code")

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

    def activate_subscription(
        self,
        user_id: int,
        tariff: str,
        days: int = 30,
        card_token: str = "",
        payment_id: str = "",
    ) -> UserProfile:
        """Активирует (или продлевает) подписку на тариф на N дней.
        Если подписка ещё активна — срок прибавляется к текущему, иначе от now().
        """
        profile = self.get(user_id)
        now = datetime.now(timezone.utc)
        if profile.is_subscription_active() and profile.tariff == tariff:
            try:
                base = datetime.fromisoformat(profile.tariff_expires_at)
            except ValueError:
                base = now
        else:
            base = now
        new_expires = base + timedelta(days=days)
        profile.tariff = tariff
        profile.tariff_expires_at = new_expires.isoformat()
        if card_token:
            profile.card_token = card_token
        if payment_id:
            profile.last_payment_id = payment_id
        profile.renewal_failures = 0
        profile.auto_renew = True
        self.save_profile(profile)
        return profile

    def disable_auto_renew(self, user_id: int) -> UserProfile:
        profile = self.get(user_id)
        profile.auto_renew = False
        self.save_profile(profile)
        return profile

    def enable_auto_renew(self, user_id: int) -> UserProfile:
        profile = self.get(user_id)
        profile.auto_renew = True
        self.save_profile(profile)
        return profile

    def set_email(self, user_id: int, email: str) -> UserProfile:
        profile = self.get(user_id)
        profile.email = email
        self.save_profile(profile)
        return profile

    def record_renewal_failure(self, user_id: int) -> UserProfile:
        profile = self.get(user_id)
        profile.renewal_failures += 1
        # После 3 неудач подряд — выключаем автопродление
        if profile.renewal_failures >= 3:
            profile.auto_renew = False
        self.save_profile(profile)
        return profile

    def iter_profiles(self):
        """Итератор по всем профилям (для планировщика)."""
        for raw in self._data.values():
            yield self._profile_from_raw(raw)

    # ── Партнёрская программа ─────────────────────────────────────────

    def find_by_referral_code(self, code: str) -> Optional[UserProfile]:
        """Поиск пользователя по его реферальному коду."""
        if not code:
            return None
        for raw in self._data.values():
            if raw.get("referral_code") == code:
                return self._profile_from_raw(raw)
        return None

    def set_referrer_by_code(self, invited_user_id: int, code: str) -> bool:
        """Привязывает приглашённого к референту по его коду.
        Возвращает True, если привязка прошла; False — если нельзя
        (само-реферал, нет такого кода, уже есть реферер)."""
        invited = self.get(invited_user_id)
        if invited.referrer_id is not None:
            return False  # уже привязан
        referrer = self.find_by_referral_code(code)
        if referrer is None:
            return False  # неизвестный код
        if referrer.user_id == invited_user_id:
            return False  # само-реферал

        invited.referrer_id = referrer.user_id
        self.save_profile(invited)

        # Инкрементируем счётчик у референта
        referrer.referrals_count += 1
        self.save_profile(referrer)
        return True

    def award_referral_bonus(
        self, invited_user_id: int, days: int = REFERRAL_BONUS_DAYS,
    ) -> Optional[UserProfile]:
        """Выдаёт референту бонусные дни тарифа за первую оплату
        приглашённого. Идемпотентно: если бонус уже выдан для этого
        приглашённого, повторно не начислит.

        Возвращает обновлённый профиль референта, либо None, если
        бонуса не положено (нет реферера / уже выдан / приглашённый
        неизвестен).

        Логика дней:
        - Если у референта free и подписка не активна — активируем
          start на N дней без auto_renew (карты у него нет).
        - Если у референта активна платная подписка — продлеваем на
          тот же тариф на N дней. activate_subscription знает, что
          add'ить к текущему expires.
        """
        invited = self.get(invited_user_id)
        if invited.referrer_id is None:
            return None
        if invited.referral_bonus_granted:
            return None

        referrer = self._raw_profile(invited.referrer_id)
        if referrer is None:
            return None

        if referrer.tariff == "free" or not referrer.is_subscription_active():
            # Free-референт получает start на 15 дней. activate_subscription
            # выставит auto_renew=True; тут же гасим, т.к. карты нет.
            self.activate_subscription(
                referrer.user_id,
                REFERRAL_BONUS_TARIFF_FOR_FREE,
                days=days,
            )
            self.disable_auto_renew(referrer.user_id)
        else:
            # Активный платник — продлеваем текущий тариф
            self.activate_subscription(referrer.user_id, referrer.tariff, days=days)

        # Финальные обновления статистики. После activate_subscription
        # запись референта точно есть, но проверяем явно для устойчивости
        # к гипотетическому race с удалением профиля.
        refreshed = self._raw_profile(referrer.user_id)
        if refreshed is None:
            return None
        refreshed.referrals_paid_count += 1
        refreshed.referral_bonus_days_total += days
        self.save_profile(refreshed)

        # Помечаем приглашённого, чтобы не выдать повторно
        invited = self.get(invited_user_id)
        invited.referral_bonus_granted = True
        self.save_profile(invited)

        return refreshed

    def _raw_profile(self, user_id: int) -> Optional[UserProfile]:
        """Возвращает профиль, не создавая его если нет."""
        raw = self._data.get(str(user_id))
        return self._profile_from_raw(raw) if raw else None
