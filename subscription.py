"""Логика подписок: создание подписки в Точке, обработка успеха,
автопродление через Charge Subscription."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from payments_store import PaymentsStore
from tochka_client import TochkaClient, parse_payment_link_id
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
        tax_system_code: str = "",
    ) -> None:
        self.tochka = tochka
        self.users = users
        self.payments = payments
        self.redirect_url = redirect_url
        self.fail_redirect_url = fail_redirect_url
        self.tax_system_code = tax_system_code

    async def create_initial_payment(
        self, user_id: int, tariff: str
    ) -> tuple[str, str]:
        """Создаёт подписку (recurring=true) в Точке. Возвращает
        (payment_link, subscription_operation_id).

        Точка возвращает operationId подписки — он используется для
        последующих списаний через charge_subscription и для отмены.
        """
        if tariff not in TARIFF_PRICES:
            raise ValueError(f"Unknown tariff: {tariff}")
        amount = float(TARIFF_PRICES[tariff])
        profile = self.users.get(user_id)

        result = await self.tochka.create_subscription(
            amount=amount,
            purpose=f"Подписка на тариф {tariff} (месяц)",
            user_id=user_id,
            tariff=tariff,
            redirect_url=self.redirect_url,
            fail_redirect_url=self.fail_redirect_url,
            email=profile.email,
            tax_system_code=self.tax_system_code,
        )

        # Запись платежа: operation_id = subscription operationId,
        # order_id = paymentLinkId, который Точка пришлёт в webhook'е.
        # Точный paymentLinkId генерируется внутри create_subscription;
        # мы его не возвращаем наружу, но он восстановится из webhook'а
        # через find_by_operation либо через parse_payment_link_id.
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
        self, *,
        operation_id: str,
        order_id: str,
        card_token: str = "",  # игнорируется — у Точки cardToken не отдаётся
        amount: float,
    ) -> Optional[UserProfile]:
        """Обрабатывает acquiringInternetPayment с APPROVED/AUTHORIZED.

        Находит запись платежа, активирует подписку, привязывает
        operationId подписки к профилю пользователя.
        """
        # Сначала ищем по operation_id (для первичного платежа это id
        # подписки; для продления — id зарегистрированный из ответа charge)
        rec = self.payments.find_by_operation(operation_id)
        if not rec:
            rec = self.payments.find_by_order(order_id)
        if not rec:
            # Webhook пришёл первым: восстанавливаем запись из
            # paymentLinkId формата sub_{uid}_{tariff}_{rand}
            parsed = parse_payment_link_id(order_id)
            if not parsed:
                logger.error(
                    "Unknown payment: op=%s order=%s",
                    operation_id, order_id,
                )
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
        # Привязываем operationId подписки к профилю — для последующих
        # charge_subscription. Это происходит ТОЛЬКО для initial-платежа;
        # рекуррентные списания не пересохраняют id (он тот же).
        subscription_op_id = ""
        if rec.kind == "initial":
            subscription_op_id = operation_id

        profile = self.users.activate_subscription(
            user_id=rec.user_id,
            tariff=rec.tariff,
            days=30,
            subscription_operation_id=subscription_op_id,
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
        self, *, operation_id: str, error: str = "",
    ) -> None:
        """В реальной интеграции с Точкой webhook про failed не приходит
        вовсе — failed-логика идёт через payment_poller, который сам
        вызывает этот метод после get_subscription_status. Сохраняем
        для совместимости и тестов."""
        self.payments.mark_failed(operation_id, error=error)
        logger.info("Payment %s marked failed: %s", operation_id, error)

    async def try_renew(self, profile: UserProfile) -> tuple[bool, str]:
        """Списывает с привязанной к подписке карты через Точку."""
        if profile.tariff == "free" or not profile.auto_renew:
            return False, "auto_renew disabled"
        if not profile.subscription_operation_id:
            return False, "no subscription"
        if profile.tariff not in TARIFF_PRICES:
            return False, f"unknown tariff {profile.tariff}"

        amount = float(TARIFF_PRICES[profile.tariff])
        result = await self.tochka.charge_subscription(
            operation_id=profile.subscription_operation_id,
            amount=amount,
        )

        # Регистрируем рекуррентное списание в журнале. operation_id
        # списания — тот же что у подписки; используем суффикс времени
        # как составной ключ записи, чтобы записи не конфликтовали.
        # Точка не возвращает отдельного id для charge — статус один на
        # всю подписку. Поэтому в payments_store пишем тот же operationId
        # с типом recurring; webhook acquiringInternetPayment придёт
        # на этот же operationId и пометит как paid (идемпотентно).
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
        self.payments.mark_failed(
            result.operation_id, error=result.error_message,
        )
        self.users.record_renewal_failure(profile.user_id)
        return False, result.error_message or "declined"

    async def cancel_user_subscription(
        self, user_id: int,
    ) -> tuple[bool, str]:
        """Отменяет подписку на стороне Точки и выключает auto_renew у нас.

        После отмены вернуть подписку нельзя — клиент должен оформить
        новую. Текущий период доходит до конца expires_at.
        """
        profile = self.users.get(user_id)
        # Локально выключаем auto_renew всегда — даже если в Точке нет
        # активной подписки (например, истекла).
        self.users.disable_auto_renew(user_id)
        if not profile.subscription_operation_id:
            return True, "auto_renew disabled (no subscription on Tochka side)"

        try:
            ok = await self.tochka.cancel_subscription(
                profile.subscription_operation_id,
            )
        except Exception as exc:
            logger.warning("Tochka cancel_subscription error: %s", exc)
            return True, "auto_renew disabled (Tochka API error)"
        if ok:
            return True, "cancelled"
        return True, "auto_renew disabled (Tochka decline)"

    def expiring_soon(
        self, days: int = RENEWAL_LEAD_DAYS,
    ) -> list[UserProfile]:
        """Возвращает профили с истекающими подписками (для авто-продления)."""
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
        """Опрашивает Точку по всем платежам в статусе 'created' через
        get_subscription_status. Это safety-net на случай пропущенного
        webhook'а.

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
                data = await self.tochka.get_subscription_status(
                    rec.operation_id,
                )
            except Exception as exc:
                logger.warning(
                    "Poll status failed for %s: %s",
                    rec.operation_id, exc,
                )
                results.append((rec.operation_id, "error"))
                continue

            status = (data.get("status") or "").upper()
            amount = float(data.get("amount") or rec.amount)

            if status in ("APPROVED", "AUTHORIZED"):
                # Идемпотентно через handle_webhook_paid (не активирует
                # повторно paid-запись)
                self.handle_webhook_paid(
                    operation_id=rec.operation_id,
                    order_id=rec.order_id,
                    amount=amount,
                )
                results.append((rec.operation_id, "activated"))
                logger.info(
                    "Poller: activated subscription for op=%s",
                    rec.operation_id,
                )
            elif status in ("DECLINED", "CANCELLED", "REJECTED", "FAILED"):
                self.handle_webhook_failed(
                    operation_id=rec.operation_id,
                    error=str(data.get("errorMessage", "")),
                )
                results.append((rec.operation_id, "failed"))
            else:
                # Точка ещё думает (CREATED / pending / etc) — оставляем
                results.append((rec.operation_id, "still_pending"))
        return results
