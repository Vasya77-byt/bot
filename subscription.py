"""Логика подписок: создание платежа, обработка успеха, автопродление."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from payments_store import PaymentsStore
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
    ) -> None:
        self.tochka = tochka
        self.users = users
        self.payments = payments
        self.redirect_url = redirect_url
        self.fail_redirect_url = fail_redirect_url

    async def create_initial_payment(
        self, user_id: int, tariff: str
    ) -> tuple[str, str]:
        """Создаёт ссылку на оплату для первой покупки тарифа.
        Возвращает (payment_link, operation_id)."""
        if tariff not in TARIFF_PRICES:
            raise ValueError(f"Unknown tariff: {tariff}")
        amount = float(TARIFF_PRICES[tariff])
        profile = self.users.get(user_id)

        result = await self.tochka.create_payment(
            amount=amount,
            purpose=f"Подписка на тариф {tariff} (месяц)",
            user_id=user_id,
            tariff=tariff,
            redirect_url=self.redirect_url,
            fail_redirect_url=self.fail_redirect_url,
            email=profile.email,
            save_card=True,
        )

        # orderId у нас в purpose/meta не виден — восстановим формат sub_{uid}_{tariff}_{rand}
        # TochkaClient.create_payment генерит его внутри, но не возвращает.
        # Для истории пишем operation_id как order_id (Tochka возвращает свой id).
        self.payments.record_created(
            operation_id=result.operation_id,
            order_id=result.operation_id,
            user_id=user_id,
            tariff=tariff,
            amount=amount,
            kind="initial",
        )
        return result.payment_link, result.operation_id

    def handle_webhook_paid(
        self, *, operation_id: str, order_id: str, card_token: str, amount: float
    ) -> Optional[UserProfile]:
        """Обрабатывает уведомление об успешной оплате от Точки.

        Находит запись платежа, определяет user_id+tariff, активирует подписку.
        Возвращает обновлённый профиль или None, если платёж не найден.
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
                return None
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
            return self.users.get(rec.user_id)

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

        # Если у пользователя есть реферер и это первая оплата —
        # выдаём референту бонусные дни. Метод идемпотентен.
        referrer = self.users.award_referral_bonus(rec.user_id)
        if referrer is not None:
            logger.info(
                "Referral bonus granted: referrer=%s days_total=%s",
                referrer.user_id, referrer.referral_bonus_days_total,
            )

        return profile

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

    async def poll_pending_payments(
        self,
        *,
        older_than_seconds: int = 300,
        max_age_seconds: int = 24 * 60 * 60,
    ) -> list[tuple[str, str]]:
        """Опрашивает Точку по всем платежам в статусе 'created'.

        Это safety-net на случай, когда webhook не дошёл до нашего сервера
        (сетевой обрыв, прокси, кратковременное падение). Без этого
        пользователь оплачивает, но подписка не активируется до ручного
        вмешательства.

        - older_than_seconds: не опрашиваем платежи моложе N секунд —
          Точка не успела создать операцию.
        - max_age_seconds: платежи старше суток считаем брошенными,
          не опрашиваем (клиент уже не ждёт).

        Возвращает список (operation_id, action), где action ∈
        {"activated", "failed", "still_pending", "error"}.
        """
        pending = self.payments.iter_pending(
            older_than_seconds=older_than_seconds,
            max_age_seconds=max_age_seconds,
        )
        results: list[tuple[str, str]] = []
        for rec in pending:
            try:
                data = await self.tochka.get_operation_status(rec.operation_id)
            except Exception as exc:
                logger.warning("Poll status failed for %s: %s",
                               rec.operation_id, exc)
                results.append((rec.operation_id, "error"))
                continue

            status = (data.get("status") or "").lower()
            card_token = data.get("cardToken") or data.get("savedCardToken") or ""
            amount = float(data.get("amount") or rec.amount)

            if status in ("paid", "approved", "confirmed", "completed"):
                # Активируем как при webhook'е — handle_webhook_paid
                # идемпотентен (не активирует уже paid повторно).
                self.handle_webhook_paid(
                    operation_id=rec.operation_id,
                    order_id=rec.order_id,
                    card_token=card_token,
                    amount=amount,
                )
                results.append((rec.operation_id, "activated"))
                logger.info("Poller: activated subscription for op=%s",
                            rec.operation_id)
            elif status in ("failed", "declined", "cancelled", "rejected"):
                self.handle_webhook_failed(
                    operation_id=rec.operation_id,
                    error=str(data.get("errorMessage", "")),
                )
                results.append((rec.operation_id, "failed"))
            else:
                # Точка ещё думает — оставляем в created
                results.append((rec.operation_id, "still_pending"))
        return results
