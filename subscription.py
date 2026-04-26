"""Логика подписок: создание платежа, обработка успеха, автопродление."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from payments_store import PaymentsStore
from referral_store import (
    REFERRAL_BONUS_DAYS,
    REFERRAL_DISCOUNT_PCT,
    ReferralStore,
)
from tochka_client import TochkaClient, parse_order_id
from user_store import TARIFF_PRICES, UserProfile, UserStore

logger = logging.getLogger("financial-architect")

# За сколько дней до истечения пытаться продлить
RENEWAL_LEAD_DAYS = 1


class SubscriptionService:
    def __init__(
        self,
        tochka: TochkaClient,
        users: UserStore,
        payments: PaymentsStore,
        *,
        redirect_url: str,
        fail_redirect_url: str,
        referrals: Optional[ReferralStore] = None,
    ) -> None:
        self.tochka = tochka
        self.users = users
        self.payments = payments
        self.redirect_url = redirect_url
        self.fail_redirect_url = fail_redirect_url
        self.referrals = referrals

    def _has_referral_discount(self, user_id: int) -> bool:
        """Имеет ли пользователь право на скидку 15% по реферальной программе.
        Условие: пришёл по реф.ссылке и ещё не покупал подписку."""
        if not self.referrals:
            return False
        if not self.referrals.has_active_referrer(user_id):
            return False
        profile = self.users.get(user_id)
        # Скидка только для тех, кто ещё ни разу не платил
        if profile.tariff_expires_at:
            return False
        return True

    async def create_initial_payment(
        self, user_id: int, tariff: str
    ) -> tuple[str, str, float, bool]:
        """Создаёт ссылку на оплату для первой покупки тарифа.
        Возвращает (payment_link, operation_id, amount, discount_applied)."""
        if tariff not in TARIFF_PRICES:
            raise ValueError(f"Unknown tariff: {tariff}")
        base_amount = float(TARIFF_PRICES[tariff])
        profile = self.users.get(user_id)

        discount_applied = self._has_referral_discount(user_id)
        if discount_applied:
            amount = round(base_amount * (1 - REFERRAL_DISCOUNT_PCT / 100), 2)
            purpose = (
                f"Подписка на тариф {tariff} (месяц), скидка {REFERRAL_DISCOUNT_PCT}%"
            )
        else:
            amount = base_amount
            purpose = f"Подписка на тариф {tariff} (месяц)"

        result = await self.tochka.create_payment(
            amount=amount,
            purpose=purpose,
            user_id=user_id,
            tariff=tariff,
            redirect_url=self.redirect_url,
            fail_redirect_url=self.fail_redirect_url,
            email=profile.email,
            save_card=True,
        )

        self.payments.record_created(
            operation_id=result.operation_id,
            order_id=result.order_id,
            user_id=user_id,
            tariff=tariff,
            amount=amount,
            kind="initial",
        )
        return result.payment_link, result.operation_id, amount, discount_applied

    def handle_webhook_paid(
        self, *, operation_id: str, order_id: str, card_token: str, amount: float
    ) -> tuple[Optional[UserProfile], Optional[int]]:
        """Обрабатывает уведомление об успешной оплате от Точки.

        Находит запись платежа, определяет user_id+tariff, активирует подписку.
        Возвращает (обновлённый профиль, id реферера которому выдан бонус) либо
        (None, None), если платёж не найден.
        """
        # Сначала ищем по operation_id
        rec = self.payments.find_by_operation(operation_id)
        if not rec:
            # Пробуем по order_id (если в первый раз видим operation_id)
            rec = self.payments.find_by_order(order_id)
        if not rec:
            # Пытаемся восстановить из orderId формата sub_{uid}_{tariff}_...
            parsed = parse_order_id(order_id)
            if not parsed:
                logger.error("Unknown payment: op=%s order=%s", operation_id, order_id)
                return None, None
            user_id, tariff = parsed
            rec = self.payments.record_created(
                operation_id=operation_id,
                order_id=order_id,
                user_id=user_id,
                tariff=tariff,
                amount=amount,
                kind="initial",
            )

        if rec.status == "paid":
            logger.info("Payment %s already processed", operation_id)
            return self.users.get(rec.user_id), None

        self.payments.mark_paid(operation_id)
        profile = self.users.activate_subscription(
            user_id=rec.user_id,
            tariff=rec.tariff,
            days=30,
            card_token=card_token,
            payment_id=operation_id,
        )
        logger.info(
            "Subscription activated: user=%s tariff=%s expires=%s",
            profile.user_id,
            profile.tariff,
            profile.tariff_expires_at,
        )

        # Реферальный бонус — только при первой («initial») оплате приглашённого
        rewarded_referrer: Optional[int] = None
        if rec.kind == "initial" and self.referrals:
            referrer_id = self.referrals.mark_converted(rec.user_id)
            if referrer_id:
                self.users.add_referral_bonus_days(
                    user_id=referrer_id, days=REFERRAL_BONUS_DAYS
                )
                rewarded_referrer = referrer_id
                logger.info(
                    "Referral bonus: +%s days to user %s for referral %s",
                    REFERRAL_BONUS_DAYS, referrer_id, rec.user_id,
                )
        return profile, rewarded_referrer

    def handle_webhook_failed(
        self, *, operation_id: str, error: str = ""
    ) -> None:
        self.payments.mark_failed(operation_id, error=error)
        logger.info("Payment %s marked failed: %s", operation_id, error)

    async def try_renew(self, profile: UserProfile) -> tuple[bool, str]:
        """Пытается рекуррентно списать подписку.
        Возвращает (успех, сообщение).
        """
        if profile.tariff == "free" or not profile.auto_renew:
            return False, "auto_renew disabled"
        if not profile.card_token:
            return False, "no saved card"
        if profile.tariff not in TARIFF_PRICES:
            return False, f"unknown tariff {profile.tariff}"

        amount = float(TARIFF_PRICES[profile.tariff])
        result = await self.tochka.charge_recurring(
            amount=amount,
            purpose=f"Автопродление тарифа {profile.tariff}",
            card_token=profile.card_token,
            user_id=profile.user_id,
            tariff=profile.tariff,
            email=profile.email,
        )

        self.payments.record_created(
            operation_id=result.operation_id,
            order_id=result.operation_id,
            user_id=profile.user_id,
            tariff=profile.tariff,
            amount=amount,
            kind="recurring",
        )

        if result.status == "approved":
            self.payments.mark_paid(result.operation_id)
            self.users.activate_subscription(
                user_id=profile.user_id,
                tariff=profile.tariff,
                days=30,
                payment_id=result.operation_id,
            )
            return True, "renewed"

        if result.status == "pending":
            # Ждём webhook — подписка продлится при подтверждении
            return True, "pending"

        # declined
        self.payments.mark_failed(result.operation_id, error=result.error_message)
        self.users.record_renewal_failure(profile.user_id)
        return False, result.error_message or "declined"

    def expiring_soon(self, days: int = RENEWAL_LEAD_DAYS) -> list[UserProfile]:
        """Возвращает профили с истекающими подписками (для автопродления)."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days)
        result = []
        for profile in self.users.iter_profiles():
            if profile.tariff == "free" or not profile.auto_renew:
                continue
            if not profile.tariff_expires_at:
                continue
            try:
                expires = datetime.fromisoformat(profile.tariff_expires_at)
            except ValueError:
                continue
            if now <= expires <= cutoff:
                result.append(profile)
        return result
