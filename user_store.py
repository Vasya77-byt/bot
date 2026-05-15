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
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

# Сколько дней даём референту за каждую первую оплату приглашённого
# (используется ТОЛЬКО когда оплата не пересекает порог тира — иначе
# вместо +15 выдаётся tier reward, см. award_referral_bonus)
REFERRAL_BONUS_DAYS = 15
# Тариф, который активируется референту-Free при первой оплате его приглашённого
REFERRAL_BONUS_TARIFF_FOR_FREE = "start"

# Ранг тарифов для сравнения lifetime vs monthly.
# effective_tariff() выбирает максимальный из активного monthly и lifetime.
TARIFF_RANK: Dict[str, int] = {"free": 0, "start": 1, "pro": 2, "business": 3}

# Сколько дней даёт каждый tier-бонус (для tier'ов с временной наградой).
# Gold/Diamond выдают lifetime — для них значение здесь не используется.
TIER_BONUS_DAYS: Dict[str, int] = {"bronze": 30, "silver": 90}
# Тариф, на который активируется free-референт при tier-награде с днями.
# (Gold/Diamond сами по себе апгрейдят до Pro/Business lifetime.)
TIER_BONUS_TARIFF_FOR_FREE: Dict[str, str] = {"bronze": "pro", "silver": "pro"}
# Lifetime-тариф, выдаваемый при достижении tier'а.
TIER_LIFETIME: Dict[str, str] = {"gold": "pro", "diamond": "business"}

logger = logging.getLogger("financial-architect")

STORAGE_FILE = os.getenv("USERS_FILE", "users.json")

# Общий дневной лимит проверок (любого типа — Quick preview или Full отчёт).
# Quick (краткая, ~1₽) и Full (полный, ~10-20₽) на UI-уровне разделены —
# юзер сначала видит short-карточку, потом полный отчёт по кнопке.
# Но СЧЁТЧИК один: каждая проверка декрементирует одну единицу.
TARIFF_LIMITS: Dict[str, Optional[int]] = {
    "free": 3,
    "start": 20,
    "pro": 40,
    "business": 80,
}

# Bulk-проверка (Block C, Step C1): отдельный счётчик ИНН в день.
# Не отъедает Full-квоту — пользователь может делать обычные проверки
# параллельно с bulk-загрузками. Per-batch cap отдельно — см. BULK_MAX_PER_REQUEST.
TARIFF_BULK_LIMITS: Dict[str, Optional[int]] = {
    "free": 0,
    "start": 0,
    "pro": 30,
    "business": 100,
}

# Кошелёк (Wallet): цены платных действий с баланса юзера.
# Хранится в КОПЕЙКАХ — чтобы избежать float-погрешностей.
# Юзер пополняет произвольной суммой, тратит на действия по этим ценам.
# Доступ из main.py / subscription.py через try_spend(action).
def calc_topup_bonus_percent(amount_rub: int) -> int:
    """Возвращает процент бонуса для суммы пополнения.
    Применяется по убыванию порога — первый матчинг побеждает."""
    for threshold, bonus_pct in TOPUP_BONUS_THRESHOLDS_RUB:
        if amount_rub >= threshold:
            return bonus_pct
    return 0


def calc_topup_credits(amount_rub: int) -> tuple:
    """По сумме оплаты возвращает (base_kopeks, bonus_kopeks).
    base — возвратная часть, bonus — невозвратный бонус.
    """
    base_kopeks = amount_rub * 100
    bonus_pct = calc_topup_bonus_percent(amount_rub)
    bonus_kopeks = base_kopeks * bonus_pct // 100
    return base_kopeks, bonus_kopeks


PAID_ACTION_PRICES_KOPEKS: Dict[str, int] = {
    "quick_check":  500,   # 5₽ за краткую проверку
    "full_check":  1500,   # 15₽ за полный отчёт
    "ai_analysis": 3000,   # 30₽ за AI-анализ
    "bulk_inn":     800,   # 8₽ за один ИНН в bulk
}

# Прогрессивные бонусы при пополнении (по сумме платежа).
# Порог в рублях → бонус в процентах. Бонус НЕВОЗВРАТЕН по оферте,
# база — возвращаемая часть. Применяется по убыванию порога:
# первый матчинг → этот бонус.
TOPUP_BONUS_THRESHOLDS_RUB: list = [
    (5000, 20),  # от 5000₽ → +20%
    (3000, 15),  # от 3000₽ → +15%
    (1000, 10),  # от 1000₽ → +10%
]

# Минимальная и максимальная сумма пополнения (защита от error'ов).
TOPUP_MIN_RUB = 100
TOPUP_MAX_RUB = 10000


# Лимиты подписок на мониторинг ИНН (одновременно отслеживаемых)
TARIFF_MONITORING_LIMITS: Dict[str, Optional[int]] = {
    "free": 0,         # на free мониторинг недоступен
    "start": 5,
    "pro": 50,
    "business": None,  # безлимит
}

# Цены тарифов в рублях (месячная подписка)
TARIFF_PRICES: Dict[str, int] = {
    "start": 500,
    "pro": 990,
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
    checks_today: int = 0        # общий счётчик проверок за сегодня
    checks_date: str = ""        # ISO дата последнего сброса: "2024-01-15"
    checks_total: int = 0
    # Кошелёк (Wallet) — баланс в копейках. Юзер пополняет, тратит
    # на платные действия. Не сбрасывается с течением времени.
    # См. PAID_ACTION_PRICES_KOPEKS для расценок.
    balance_kopeks: int = 0
    # Сколько копеек из текущего баланса — невозвратный бонус (за объём
    # при пополнении). Возврат разрешён только в пределах
    # (balance_kopeks - balance_bonus_kopeks).
    balance_bonus_kopeks: int = 0
    # Bulk-проверка (Step C1): отдельный счётчик ИНН в день для bulk-загрузок.
    bulk_today: int = 0          # ИНН, обработанных через bulk-upload сегодня
    # Подписка
    tariff_expires_at: str = ""      # ISO datetime в UTC, пусто для free
    subscription_operation_id: str = ""  # operationId подписки в Точке
                                         # для charge_subscription / cancel
    yookassa_payment_method_id: str = ""  # id сохранённой карты в ЮKassa
                                          # для рекуррентных списаний
    card_token: str = ""             # legacy: остаётся для совместимости
                                     # с существующими users.json; новые
                                     # подписки используют subscription_operation_id
                                     # или yookassa_payment_method_id
    auto_renew: bool = True          # автопродление
    renewal_failures: int = 0        # счётчик подряд неудачных списаний
    last_payment_id: str = ""        # id последней операции
    # Напоминания об истечении подписки — чтобы не отправлять одно и то же
    # уведомление дважды за день. Хранит ISO-дату последней отправки.
    last_expiry_reminder_date: str = ""
    # Флаг, что юзер уже получил уведомление о переходе на Free (один раз)
    expired_notice_sent: bool = False
    # Флаг, что Free-юзеру уже показали upsell на тарифы после первой
    # полной проверки (D1) — один раз навсегда, без повторов.
    first_full_upsell_shown: bool = False
    email: str = ""                  # email для чека
    phone: str = ""                  # телефон в формате +79991234567
    full_name: str = ""              # ФИО клиента (опц., из профиля)
    accepted_offer_at: str = ""      # ISO datetime принятия оферты
    # Партнёрская программа
    referral_code: str = ""              # личный код вида "ref_<8 hex>"
    referrer_id: Optional[int] = None    # кто пригласил этого пользователя
    referral_source: str = ""            # UTM-source из ref_<code>_<source>
    referral_bonus_granted: bool = False # бонус референту уже выдан (one-shot)
    invitee_bonus_granted: bool = False  # +15 дней приглашённому уже выданы (one-shot)
    referrals_count: int = 0             # сколько привлёк (включая Free)
    referrals_paid_count: int = 0        # сколько привлечённых оплатили
    referral_bonus_days_total: int = 0   # сколько дней получил суммарно
    registered_at: str = ""              # ISO datetime первой регистрации
    # Фаза 2 партнёрской программы: tier-награды и lifetime
    lifetime_tariff: str = ""            # "pro"/"business"/"" — если выдан
                                         # tier'ом Gold/Diamond, тариф навсегда
    tier_rewards_granted: List[str] = field(default_factory=list)
                                         # ключи tier'ов с уже выданной
                                         # наградой ("bronze","silver","gold",
                                         # "diamond") — для идемпотентности
    revshare_enabled: bool = False       # Diamond-флаг (механика выплат — TODO)

    def reset_if_new_day(self) -> None:
        today = date.today().isoformat()
        if self.checks_date != today:
            self.checks_today = 0
            self.bulk_today = 0
            self.checks_date = today

    def _monthly_active(self) -> bool:
        """Активна ли месячная (платная) подписка прямо сейчас (без учёта
        lifetime). Внутренний метод; внешние коды должны использовать
        is_subscription_active()."""
        if self.tariff == "free":
            return False
        if not self.tariff_expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.tariff_expires_at)
        except ValueError:
            return False
        return expires > datetime.now(timezone.utc)

    def is_subscription_active(self) -> bool:
        """Активна ли любая подписка (monthly или lifetime)."""
        if self.lifetime_tariff:
            return True
        return self._monthly_active()

    def effective_tariff(self) -> str:
        """Тариф с учётом истечения подписки и lifetime.
        Возвращает максимальный из активного monthly и lifetime."""
        monthly = self.tariff if self._monthly_active() else "free"
        lifetime = self.lifetime_tariff or "free"
        return monthly if TARIFF_RANK.get(monthly, 0) >= TARIFF_RANK.get(lifetime, 0) else lifetime

    def can_check(self) -> bool:
        """Можно ли сделать ещё одну проверку (любого типа — Quick или Full)."""
        self.reset_if_new_day()
        limit = TARIFF_LIMITS.get(self.effective_tariff())
        if limit is None:
            return True
        return self.checks_today < limit

    def remaining_checks(self) -> Optional[int]:
        """Остаток проверок на сегодня; None — безлимит."""
        self.reset_if_new_day()
        limit = TARIFF_LIMITS.get(self.effective_tariff())
        if limit is None:
            return None
        return max(0, limit - self.checks_today)

    def daily_limit(self) -> Optional[int]:
        """Дневной лимит по effective-тарифу."""
        return TARIFF_LIMITS.get(self.effective_tariff())

    def increment(self) -> None:
        """Инкремент общего счётчика проверок."""
        self.reset_if_new_day()
        self.checks_today += 1
        self.checks_total += 1

    # ── Кошелёк (Wallet) ──

    @property
    def balance_rub(self) -> float:
        """Баланс в рублях (для отображения)."""
        return self.balance_kopeks / 100

    def can_afford(self, action: str) -> bool:
        """Хватает ли баланса на действие. Неизвестное действие → False."""
        price = PAID_ACTION_PRICES_KOPEKS.get(action)
        if price is None:
            return False
        return self.balance_kopeks >= price

    def try_spend(self, action: str) -> bool:
        """Атомарно списывает с баланса стоимость действия.

        Возвращает True если списано, False если денег не хватает или
        action неизвестен. При успешном списании баланс уменьшается,
        бонусная часть тратится В ПЕРВУЮ ОЧЕРЕДЬ (так юзер не теряет
        возвратные деньги до конца).
        """
        price = PAID_ACTION_PRICES_KOPEKS.get(action)
        if price is None or self.balance_kopeks < price:
            return False
        # Сначала тратим бонус (невозвратный), потом возвратный остаток
        if self.balance_bonus_kopeks >= price:
            self.balance_bonus_kopeks -= price
        else:
            # Бонус кончается частично — остаток списываем с базы
            self.balance_bonus_kopeks = 0
        self.balance_kopeks -= price
        return True

    def add_balance(self, base_kopeks: int, bonus_kopeks: int = 0) -> None:
        """Пополняет баланс. base_kopeks — возвратная часть (что юзер
        реально заплатил), bonus_kopeks — невозвратный бонус за объём."""
        if base_kopeks < 0 or bonus_kopeks < 0:
            return
        self.balance_kopeks += base_kopeks + bonus_kopeks
        self.balance_bonus_kopeks += bonus_kopeks

    def can_bulk(self, count: int = 1) -> bool:
        """Можно ли обработать ещё `count` ИНН в bulk сегодня."""
        self.reset_if_new_day()
        limit = TARIFF_BULK_LIMITS.get(self.effective_tariff())
        if limit is None:
            return True
        return self.bulk_today + count <= limit

    def remaining_bulk(self) -> Optional[int]:
        """Остаток ИНН для bulk на сегодня; None — безлимит, 0 — недоступен."""
        self.reset_if_new_day()
        limit = TARIFF_BULK_LIMITS.get(self.effective_tariff())
        if limit is None:
            return None
        return max(0, limit - self.bulk_today)

    def increment_bulk(self, count: int = 1) -> None:
        self.reset_if_new_day()
        self.bulk_today += count
        self.checks_total += count

    def has_completed_onboarding(self) -> bool:
        """Прошёл ли клиент обязательные шаги: оферта принята + телефон."""
        return bool(self.accepted_offer_at) and bool(self.phone)


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
        """Инкремент общего счётчика проверок (Quick или Full — всё равно)."""
        profile = self.get(user_id)
        profile.increment()
        self.save_profile(profile)
        return profile

    def try_spend(self, user_id: int, action: str) -> bool:
        """Атомарное списание с баланса юзера. True если успешно.

        Тонкость: persist'ит профиль ТОЛЬКО при успешном списании.
        Если try_spend в UserProfile вернул False — изменений не было.
        """
        profile = self.get(user_id)
        if profile.try_spend(action):
            self.save_profile(profile)
            return True
        return False

    def add_balance(
        self, user_id: int, base_kopeks: int, bonus_kopeks: int = 0,
    ) -> UserProfile:
        """Зачисляет на баланс. Используется webhook'ом после оплаты
        пополнения. base — возвратная часть, bonus — невозвратный
        бонус за объём (см. TOPUP_BONUS_THRESHOLDS_RUB)."""
        profile = self.get(user_id)
        profile.add_balance(base_kopeks, bonus_kopeks)
        self.save_profile(profile)
        return profile

    def increment_bulk(self, user_id: int, count: int = 1) -> UserProfile:
        """Инкремент счётчика bulk-проверок на N ИНН."""
        profile = self.get(user_id)
        profile.increment_bulk(count)
        self.save_profile(profile)
        return profile

    def mark_first_full_upsell_shown(self, user_id: int) -> UserProfile:
        """One-shot: помечает, что upsell после первого Full-отчёта
        уже показан (D1). Идемпотентно — повторный вызов безопасен."""
        profile = self.get(user_id)
        if not profile.first_full_upsell_shown:
            profile.first_full_upsell_shown = True
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
        subscription_operation_id: str = "",
        yookassa_payment_method_id: str = "",
        payment_id: str = "",
    ) -> UserProfile:
        """Активирует (или продлевает) подписку на тариф на N дней.
        Если подписка ещё активна — срок прибавляется к текущему, иначе от now().

        - card_token: legacy-поле, заполняется только если приходит явно
          (старый код или внешний клиент). Новый Tochka-flow его не
          использует — для списаний нужен subscription_operation_id.
        - subscription_operation_id: id подписки в Точке для последующих
          charge_subscription. Не перезаписывается пустой строкой —
          можно безопасно вызывать activate_subscription без аргумента
          при продлении.
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
        if subscription_operation_id:
            profile.subscription_operation_id = subscription_operation_id
        if yookassa_payment_method_id:
            profile.yookassa_payment_method_id = yookassa_payment_method_id
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

    def set_referrer_by_code(
        self, invited_user_id: int, code: str, source: str = "",
    ) -> bool:
        """Привязывает приглашённого к референту по его коду.
        Опционально сохраняет UTM-источник (instagram/email/telegram_chat/…).

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
        if source:
            # Ограничим длину/чарсет — защита от мусора в JSON
            invited.referral_source = "".join(
                c for c in source[:32] if c.isalnum() or c in "_-"
            )
        if not invited.registered_at:
            from datetime import datetime, timezone
            invited.registered_at = datetime.now(timezone.utc).isoformat()
        self.save_profile(invited)

        # Инкрементируем счётчик у референта
        referrer.referrals_count += 1
        self.save_profile(referrer)
        return True

    def list_invitees(self, referrer_id: int) -> list[UserProfile]:
        """Возвращает список профилей всех приглашённых данным юзером.
        Не делает анонимизацию — это задача UI-слоя."""
        result: list[UserProfile] = []
        for raw in self._data.values():
            if raw.get("referrer_id") == referrer_id:
                result.append(self._profile_from_raw(raw))
        # Сортируем по дате регистрации (новые наверху). Пустые даты — в конец.
        result.sort(
            key=lambda p: p.registered_at or "0",
            reverse=True,
        )
        return result

    def award_referral_bonus(
        self, invited_user_id: int, days: int = REFERRAL_BONUS_DAYS,
    ) -> Optional[UserProfile]:
        """Выдаёт референту награду за первую оплату приглашённого.
        Идемпотентно: повторный вызов на том же invited не начислит.

        Логика:
        - Считаем, какой tier станет текущим ПОСЛЕ инкремента
          referrals_paid_count и не было ли он уже награждён.
        - Если новый tier есть → выдаём ТОЛЬКО tier-награду (+15 не идёт):
            * bronze/silver → +30/+90 дней (free-референту → Pro)
            * gold/diamond  → lifetime Pro/Business
            * diamond также включает revshare_enabled
          tier помечается в tier_rewards_granted.
        - Иначе → +days дней текущего тарифа (free → start, как раньше).

        В обоих ветках на референте обновляется referrals_paid_count и
        referral_bonus_days_total (для lifetime — символический +days,
        чтобы статистика «дней получил» осталась монотонной).

        Возвращает обновлённый профиль референта, либо None, если
        бонуса не положено (нет реферера / уже выдан / приглашённый
        неизвестен).
        """
        from referral_tiers import TIERS

        invited = self.get(invited_user_id)
        if invited.referrer_id is None:
            return None
        if invited.referral_bonus_granted:
            return None

        referrer = self._raw_profile(invited.referrer_id)
        if referrer is None:
            return None

        paid_before = referrer.referrals_paid_count
        paid_after = paid_before + 1

        # Самый высокий tier, чей порог пересечён ЭТОЙ оплатой и который
        # ещё не награждали. Защита от перепрыгивания (если по какой-то
        # причине paid_count подскочил сразу на несколько — берём только
        # верхний; промежуточные считаются уже неактуальными).
        new_tier = None
        for tier in TIERS:
            if tier.key == "none":
                continue
            if tier.key in referrer.tier_rewards_granted:
                continue
            if paid_before < tier.threshold <= paid_after:
                new_tier = tier  # пересечён в этой оплате
        if new_tier is None:
            # Запасной вариант: tier уже пройден ранее, но не выдавался
            # (например, миграция со старых users.json). Выдаём
            # максимальный незаявленный tier до текущего уровня.
            for tier in TIERS:
                if tier.key == "none":
                    continue
                if tier.key in referrer.tier_rewards_granted:
                    continue
                if paid_after >= tier.threshold:
                    new_tier = tier

        bonus_days_for_stats = days  # сколько прибавим в referral_bonus_days_total

        if new_tier is not None:
            # Tier перекрывает +15: выдаём только tier-награду.
            self._grant_tier_reward(referrer.user_id, new_tier)
            # Обновим в памяти, т.к. _grant_tier_reward уже сохранил.
            refreshed = self._raw_profile(referrer.user_id)
            if refreshed is None:
                return None
            # Для bronze/silver учтём фактические дни tier'а в статистике;
            # для lifetime просто +days как «весомый» вклад в счётчик.
            bonus_days_for_stats = TIER_BONUS_DAYS.get(new_tier.key, days)
        else:
            # Tier не пересечён — обычный +days бонус (как Фаза 1).
            if referrer.tariff == "free" or not referrer.is_subscription_active():
                self.activate_subscription(
                    referrer.user_id,
                    REFERRAL_BONUS_TARIFF_FOR_FREE,
                    days=days,
                )
                self.disable_auto_renew(referrer.user_id)
            else:
                self.activate_subscription(
                    referrer.user_id, referrer.tariff, days=days,
                )
            refreshed = self._raw_profile(referrer.user_id)
            if refreshed is None:
                return None

        refreshed.referrals_paid_count = paid_after
        refreshed.referral_bonus_days_total += bonus_days_for_stats
        self.save_profile(refreshed)

        # Помечаем приглашённого, чтобы не выдать повторно
        invited = self.get(invited_user_id)
        invited.referral_bonus_granted = True
        self.save_profile(invited)

        return refreshed

    def _grant_tier_reward(self, user_id: int, tier) -> None:
        """Применяет конкретную tier-награду к профилю и помечает её
        выданной. Не возвращает ничего — вызывающий код потом перечитает
        профиль через _raw_profile.

        Для bronze/silver: целевой тариф = max(текущий, Pro по рангу).
        То есть Free/Start → апгрейд до Pro на N дней; Pro/Business —
        продление своего же тарифа. Так выполняется обещание
        «+30/+90 дней Pro» из спецификации, но юзеры на Business не
        деградируют.
        """
        key = tier.key
        if key in ("bronze", "silver"):
            days = TIER_BONUS_DAYS[key]
            min_tariff = TIER_BONUS_TARIFF_FOR_FREE[key]  # "pro"
            profile = self._raw_profile(user_id)
            if profile is None:
                return
            current = profile.tariff if profile.is_subscription_active() else "free"
            cur_rank = TARIFF_RANK.get(current, 0)
            min_rank = TARIFF_RANK.get(min_tariff, 0)
            target = current if cur_rank >= min_rank else min_tariff
            self.activate_subscription(user_id, target, days=days)
            # Если у Free-юзера нет карты — отключаем auto_renew (как и
            # в обычной фазе-1 ветке).
            if not profile.is_subscription_active() and not profile.card_token \
                    and not profile.subscription_operation_id \
                    and not profile.yookassa_payment_method_id:
                self.disable_auto_renew(user_id)
        elif key in ("gold", "diamond"):
            lifetime_tariff = TIER_LIFETIME[key]
            profile = self._raw_profile(user_id)
            if profile is None:
                return
            # Lifetime повышаем только вверх (Diamond > Gold > free).
            new_rank = TARIFF_RANK.get(lifetime_tariff, 0)
            cur_rank = TARIFF_RANK.get(profile.lifetime_tariff or "free", 0)
            if new_rank > cur_rank:
                profile.lifetime_tariff = lifetime_tariff
            if key == "diamond":
                profile.revshare_enabled = True
            self.save_profile(profile)
        else:
            return  # неизвестный tier — игнорируем

        # Помечаем выдачу, перечитав свежий профиль (activate_subscription
        # мог перезаписать).
        refreshed = self._raw_profile(user_id)
        if refreshed is None:
            return
        if key not in refreshed.tier_rewards_granted:
            refreshed.tier_rewards_granted.append(key)
        self.save_profile(refreshed)

    def award_invitee_bonus(
        self, invited_user_id: int, days: int = REFERRAL_BONUS_DAYS,
    ) -> Optional[UserProfile]:
        """Выдаёт +N бонусных дней САМОМУ приглашённому за его первую
        оплату (это и есть «15 бесплатных дней» из приветственного
        сообщения). Идемпотентно через invitee_bonus_granted.

        Возвращает обновлённый профиль приглашённого, либо None, если:
        - у пользователя нет referrer_id (он не приходил по реф.ссылке);
        - бонус уже выдан этому пользователю;
        - тариф free (на free не имеет смысла продлевать).

        Логика: продлеваем текущий тариф приглашённого на N дней.
        activate_subscription сам прибавит к текущему expires.
        """
        invited = self.get(invited_user_id)
        if invited.referrer_id is None or invited.invitee_bonus_granted:
            return None
        # Если приглашённый ещё на free — нечего продлевать. Этот случай
        # маловероятен: метод вызывается из webhook'а успешной оплаты,
        # тариф к этому моменту уже активирован. Но защита не повредит.
        if invited.tariff == "free":
            return None

        self.activate_subscription(invited_user_id, invited.tariff, days=days)
        refreshed = self._raw_profile(invited_user_id)
        if refreshed is None:
            return None
        refreshed.invitee_bonus_granted = True
        self.save_profile(refreshed)
        return refreshed

    def _raw_profile(self, user_id: int) -> Optional[UserProfile]:
        """Возвращает профиль, не создавая его если нет."""
        raw = self._data.get(str(user_id))
        return self._profile_from_raw(raw) if raw else None

    # ── Откат бонусов при возврате платежа (chargeback / refund) ─────

    def revoke_referral_bonus(
        self, invited_user_id: int, days: int = REFERRAL_BONUS_DAYS,
    ) -> Optional[UserProfile]:
        """Откатывает реф-бонус у реферера при возврате платежа
        приглашённого. Идемпотентен — если бонус не был выдан,
        возвращает None.

        Что делает:
        - декремент `referrals_paid_count` (не ниже 0)
        - уменьшение `referral_bonus_days_total` на days (не ниже 0)
        - сброс `referral_bonus_granted = False` на приглашённом
          (повторная оплата того же юзера снова даст бонус)

        Что НЕ делает (намеренно, sticky):
        - не отнимает уже-выданный срок подписки (нет истории
          по конкретным начислениям, точный rollback невозможен)
        - не отзывает lifetime_tariff и не вычищает
          tier_rewards_granted: gold/diamond — навсегда, иначе
          подорвём доверие к программе

        Возвращает обновлённый профиль реферера, либо None если
        бонус не был выдан / нет реферера / нет такого приглашённого.
        """
        invited = self._raw_profile(invited_user_id)
        if invited is None:
            return None
        if not invited.referral_bonus_granted:
            return None
        if invited.referrer_id is None:
            return None

        referrer = self._raw_profile(invited.referrer_id)
        if referrer is None:
            return None

        referrer.referrals_paid_count = max(0, referrer.referrals_paid_count - 1)
        referrer.referral_bonus_days_total = max(
            0, referrer.referral_bonus_days_total - days,
        )
        self.save_profile(referrer)

        invited.referral_bonus_granted = False
        self.save_profile(invited)
        return referrer

    def revoke_invitee_bonus(
        self, invited_user_id: int,
    ) -> Optional[UserProfile]:
        """Откатывает бонус приглашённого (бесплатные дни, которые ему
        зачисляли за оплату по реф-ссылке). Идемпотентен.

        Сбрасывает `invitee_bonus_granted = False` — при повторной
        оплате (после refund'а) бонус снова можно выдать.
        Срок подписки не уменьшаем (см. revoke_referral_bonus).
        """
        invited = self._raw_profile(invited_user_id)
        if invited is None or not invited.invitee_bonus_granted:
            return None
        invited.invitee_bonus_granted = False
        self.save_profile(invited)
        return invited
