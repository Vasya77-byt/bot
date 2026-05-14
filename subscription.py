"""Логика подписок: создание платежа, обработка успеха, автопродление.

Поддерживает двух провайдеров эквайринга:
- Tochka Bank (legacy, через рекуррентные подписки)
- YooKassa (через save_payment_method + autocharge)

Активный провайдер выбирается через ``provider`` в конструкторе
(значение из Settings.payment_provider). Webhook'и обоих провайдеров
обрабатываются параллельно для миграционной совместимости.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from payments_store import PaymentsStore
from tochka_client import TochkaClient, parse_payment_link_id
from user_store import REFERRAL_BONUS_DAYS, TARIFF_PRICES, UserProfile, UserStore
from yookassa_client import YooKassaClient, YooKassaError

logger = logging.getLogger("financial-architect")

# За сколько дней до истечения пытаться продлить
RENEWAL_LEAD_DAYS = 1


class SubscriptionService:
    def __init__(
        self,
        tochka: Optional[TochkaClient] = None,
        users: UserStore = None,
        payments: PaymentsStore = None,
        *,
        redirect_url: str,
        fail_redirect_url: str,
        tax_system_code: str = "",
        provider: str = "tochka",
        yookassa: Optional[YooKassaClient] = None,
        yookassa_tax_system_code: int = 2,
        yookassa_vat_code: int = 1,
        yookassa_save_payment_method: bool = True,
    ) -> None:
        self.tochka = tochka
        self.yookassa = yookassa
        self.provider = provider
        self.users = users
        self.payments = payments
        self.redirect_url = redirect_url
        self.fail_redirect_url = fail_redirect_url
        self.tax_system_code = tax_system_code
        self.yookassa_tax_system_code = yookassa_tax_system_code
        self.yookassa_vat_code = yookassa_vat_code
        self.yookassa_save_payment_method = yookassa_save_payment_method
        # Очередь реф-уведомлений: handle_*_paid (sync) кладёт сюда,
        # webhook_server (async) забирает через consume_referral_events()
        # и шлёт через notify(). Так sync-логика подписок не зависит
        # от async-инфраструктуры Telegram.
        self._referral_events: list[dict] = []

    # Маппинг method (из UI) → конкретный payment_method_data.type у ЮKassa
    YOOKASSA_METHOD_TYPES = {
        "sbp": "sbp",
        "tpay": "tinkoff_bank",
        "sberpay": "sberbank",
    }
    # Типы payment_method_data, для которых ЮKassa разрешает save_payment_method.
    # СБП/T-Pay/SberPay в принципе не отдают токен рекуррентов через ЮKassa
    # — для них save_payment_method=true даёт 403. Только bank_card и "" (когда
    # тип не передан, ЮKassa сама покажет страницу выбора и сохранит карту).
    YOOKASSA_RECURRING_SUPPORTED = {"", "bank_card"}

    async def create_initial_payment(
        self, user_id: int, tariff: str, method: str = "",
    ) -> tuple[str, str]:
        """Создаёт первичный платёж. Возвращает (payment_url, op/payment_id).

        method:
            ""        — fallback на self.provider (для совместимости)
            "card"    — Точка (форма ввода карты)
            "sbp"     — ЮKassa, СБП
            "tpay"    — ЮKassa, T-Pay (Тинькофф)
            "sberpay" — ЮKassa, SberPay
        """
        if tariff not in TARIFF_PRICES:
            raise ValueError(f"Unknown tariff: {tariff}")
        if method == "card":
            return await self._create_tochka_initial(user_id, tariff)
        if method in self.YOOKASSA_METHOD_TYPES:
            return await self._create_yookassa_initial(
                user_id, tariff,
                payment_method_type=self.YOOKASSA_METHOD_TYPES[method],
            )
        if method:
            raise ValueError(f"Unknown payment method: {method}")
        if self.provider == "yookassa":
            return await self._create_yookassa_initial(user_id, tariff)
        return await self._create_tochka_initial(user_id, tariff)

    async def _create_tochka_initial(
        self, user_id: int, tariff: str,
    ) -> tuple[str, str]:
        """Создаёт подписку (recurring=true) в Точке. operationId подписки
        используется для последующих charge_subscription и для отмены."""
        if self.tochka is None:
            raise RuntimeError("TochkaClient is not configured")
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
            client_name=profile.full_name,
            client_phone=profile.phone,
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
            provider="tochka",
        )
        return result.payment_link, result.operation_id

    async def _create_yookassa_initial(
        self, user_id: int, tariff: str,
        payment_method_type: str = "",
    ) -> tuple[str, str]:
        """Создаёт первичный платёж в ЮKassa с save_payment_method=True.
        Возвращает (confirmation_url, payment_id). Email пользователя
        обязателен — без него ЮKassa не примет чек 54-ФЗ.
        """
        if self.yookassa is None:
            raise RuntimeError("YooKassaClient is not configured")
        profile = self.users.get(user_id)
        if not profile.email:
            raise ValueError("email_required")

        amount = float(TARIFF_PRICES[tariff])
        save_method = (
            self.yookassa_save_payment_method
            and payment_method_type in self.YOOKASSA_RECURRING_SUPPORTED
        )
        result = await self.yookassa.create_payment(
            amount=amount,
            description=f"Подписка на тариф {tariff} (месяц)",
            user_id=user_id,
            tariff=tariff,
            return_url=self.redirect_url,
            customer_email=profile.email,
            tax_system_code=self.yookassa_tax_system_code,
            vat_code=self.yookassa_vat_code,
            save_payment_method=save_method,
            kind="initial",
            payment_method_type=payment_method_type,
        )
        # У ЮKassa первичный платёж имеет собственный payment_id (UUID).
        # operation_id = payment_id; order_id = наш sub_<uid>_<tariff>_<rand>
        # (приходит обратно в webhook'е через metadata.order_id).
        self.payments.record_created(
            operation_id=result.payment_id,
            order_id=result.order_id,
            user_id=user_id,
            tariff=tariff,
            amount=amount,
            kind="initial",
            provider="yookassa",
        )
        return result.confirmation_url, result.payment_id

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
        self._process_referral_bonuses(rec.user_id)

        return profile

    def handle_yookassa_webhook_paid(
        self,
        *,
        payment_id: str,
        order_id: str,
        user_id: int,
        tariff: str,
        amount: float,
        payment_method_id: str = "",
        kind: str = "initial",
    ) -> Optional[UserProfile]:
        """Обрабатывает ЮKassa webhook payment.succeeded.

        ЮKassa возвращает в payment.payment_method.id токен для
        последующих автоплатежей — сохраняем его в профиле.
        Идемпотентно: повторное событие на тот же payment_id не
        переактивирует подписку.
        """
        rec = self.payments.find_by_operation(payment_id)
        if not rec:
            rec = self.payments.find_by_order(order_id)
        if not rec:
            # Webhook пришёл раньше, чем запись сохранилась (или
            # запись потеряна) — восстанавливаем из metadata.
            if not user_id or not tariff:
                logger.error(
                    "YooKassa webhook: unknown payment id=%s order=%s",
                    payment_id, order_id,
                )
                return None
            rec = self.payments.record_created(
                operation_id=payment_id,
                order_id=order_id or payment_id,
                user_id=user_id,
                tariff=tariff,
                amount=amount,
                kind=kind,
                provider="yookassa",
            )

        if rec.status == "paid":
            logger.info("YooKassa payment %s already processed", payment_id)
            return self.users.get(rec.user_id)

        self.payments.mark_paid(payment_id)

        # Сохраняем payment_method_id ТОЛЬКО при первом платеже —
        # рекуррентные используют тот же токен.
        save_method_id = ""
        if rec.kind == "initial" and payment_method_id:
            save_method_id = payment_method_id

        profile = self.users.activate_subscription(
            user_id=rec.user_id,
            tariff=rec.tariff,
            days=30,
            yookassa_payment_method_id=save_method_id,
            payment_id=payment_id,
        )
        logger.info(
            "YooKassa subscription activated: user=%s tariff=%s expires=%s",
            profile.user_id, profile.tariff, profile.tariff_expires_at,
        )

        self._process_referral_bonuses(rec.user_id)

        return profile

    # ────────────────────────────────────────────────────────────
    # Реферальные бонусы и события
    # ────────────────────────────────────────────────────────────

    def _process_referral_bonuses(self, paid_user_id: int) -> None:
        """Начисляет бонусы реферальной программы при первой оплате
        приглашённого. Все начисления идемпотентны (one-shot через
        флаги в профилях). События для уведомлений складываются в
        очередь — webhook_server заберёт и разошлёт notify."""
        from referral_tiers import current_tier

        # Запоминаем выданные tier'ы ДО вызова — чтобы понять, что
        # появилось нового именно в этом вызове. Если у приглашённого
        # нет реферера, snapshot не нужен — award_referral_bonus вернёт
        # None и tier-логика не сработает.
        invited = self.users.get(paid_user_id)
        granted_before: set[str] = set()
        if invited.referrer_id is not None:
            ref_profile = self.users._raw_profile(invited.referrer_id)
            if ref_profile is not None:
                granted_before = set(ref_profile.tier_rewards_granted)

        referrer = self.users.award_referral_bonus(paid_user_id)
        if referrer is not None:
            logger.info(
                "Referral bonus granted: referrer=%s paid_count=%s days_total=%s",
                referrer.user_id, referrer.referrals_paid_count,
                referrer.referral_bonus_days_total,
            )
            granted_now = set(referrer.tier_rewards_granted) - granted_before
            if granted_now:
                # Tier перекрыл базовый бонус — шлём только tier_unlocked
                # (без referrer_paid), чтобы не дублировать радостное
                # сообщение «+N дней».
                tier = current_tier(referrer.referrals_paid_count)
                self._referral_events.append({
                    "kind": "tier_unlocked",
                    "user_id": referrer.user_id,
                    "tier_key": tier.key,
                    "tier_label": tier.label,
                    "tier_emoji": tier.emoji,
                    "reward_text": tier.reward_text,
                })
            else:
                self._referral_events.append({
                    "kind": "referrer_paid",
                    "user_id": referrer.user_id,
                    "days": REFERRAL_BONUS_DAYS,
                })

        invitee = self.users.award_invitee_bonus(paid_user_id)
        if invitee is not None:
            logger.info(
                "Invitee bonus granted: user=%s tariff=%s expires=%s",
                invitee.user_id, invitee.tariff, invitee.tariff_expires_at,
            )
            self._referral_events.append({
                "kind": "invitee_received",
                "user_id": invitee.user_id,
                "days": REFERRAL_BONUS_DAYS,
            })

    def consume_referral_events(self) -> list[dict]:
        """Забирает накопленные события (referrer_paid / invitee_received)
        и очищает очередь. Вызывается webhook_server'ом после
        handle_*_paid для рассылки уведомлений."""
        events = self._referral_events
        self._referral_events = []
        return events

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
        """Автопродление по активному провайдеру."""
        if profile.tariff == "free" or not profile.auto_renew:
            return False, "auto_renew disabled"
        if profile.tariff not in TARIFF_PRICES:
            return False, f"unknown tariff {profile.tariff}"

        if self.provider == "yookassa":
            return await self._try_renew_yookassa(profile)
        return await self._try_renew_tochka(profile)

    async def _try_renew_tochka(
        self, profile: UserProfile,
    ) -> tuple[bool, str]:
        if self.tochka is None:
            return False, "tochka not configured"
        if not profile.subscription_operation_id:
            return False, "no subscription"

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

    async def _try_renew_yookassa(
        self, profile: UserProfile,
    ) -> tuple[bool, str]:
        """Автосписание через ЮKassa по сохранённому payment_method_id."""
        if self.yookassa is None:
            return False, "yookassa not configured"
        if not profile.yookassa_payment_method_id:
            return False, "no saved payment method"
        if not profile.email:
            return False, "no email for receipt"

        amount = float(TARIFF_PRICES[profile.tariff])
        try:
            result = await self.yookassa.charge_recurring(
                payment_method_id=profile.yookassa_payment_method_id,
                amount=amount,
                description=f"Продление тарифа {profile.tariff} (месяц)",
                user_id=profile.user_id,
                tariff=profile.tariff,
                customer_email=profile.email,
                tax_system_code=self.yookassa_tax_system_code,
                vat_code=self.yookassa_vat_code,
            )
        except YooKassaError as exc:
            logger.warning("YooKassa recurring error: %s", exc)
            self.users.record_renewal_failure(profile.user_id)
            return False, str(exc)

        # Регистрируем рекуррентный платёж в журнале.
        self.payments.record_created(
            operation_id=result.payment_id or "yk_unknown",
            order_id=result.payment_id or "yk_unknown",
            user_id=profile.user_id,
            tariff=profile.tariff,
            amount=amount,
            kind="recurring",
            provider="yookassa",
        )

        if result.status == "succeeded":
            self.payments.mark_paid(result.payment_id)
            self.users.activate_subscription(
                user_id=profile.user_id,
                tariff=profile.tariff,
                days=30,
                payment_id=result.payment_id,
            )
            return True, "renewed"

        if result.status == "pending":
            return True, "pending"

        # canceled / failure
        if result.payment_id:
            self.payments.mark_failed(
                result.payment_id, error=result.error_message,
            )
        self.users.record_renewal_failure(profile.user_id)
        return False, result.error_message or "canceled"

    async def cancel_user_subscription(
        self, user_id: int,
    ) -> tuple[bool, str]:
        """Отменяет автопродление. Для Tochka — отменяет подписку на стороне
        банка. Для YooKassa — API отзыва сохранённого payment_method нет,
        просто выключаем auto_renew локально (карта остаётся привязанной,
        но мы её не используем для списаний)."""
        profile = self.users.get(user_id)
        self.users.disable_auto_renew(user_id)

        if self.provider == "yookassa":
            return True, "auto_renew disabled"

        if not profile.subscription_operation_id:
            return True, "auto_renew disabled (no subscription on Tochka side)"
        if self.tochka is None:
            return True, "auto_renew disabled (tochka not configured)"
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
        """Опрашивает провайдер по всем pending-платежам в журнале.

        Развилка по rec.provider: Tochka — через get_subscription_status,
        ЮKassa — через get_payment. Это safety-net на случай пропущенного
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
            if rec.provider == "yookassa":
                action = await self._poll_yookassa(rec)
            else:
                action = await self._poll_tochka(rec)
            results.append((rec.operation_id, action))
        return results

    async def _poll_tochka(self, rec) -> str:
        if self.tochka is None:
            return "error"
        try:
            data = await self.tochka.get_subscription_status(rec.operation_id)
        except Exception as exc:
            logger.warning(
                "Poll Tochka failed for %s: %s", rec.operation_id, exc,
            )
            return "error"

        status = (data.get("status") or "").upper()
        amount = float(data.get("amount") or rec.amount)

        if status in ("APPROVED", "AUTHORIZED"):
            # Идемпотентно через handle_webhook_paid
            self.handle_webhook_paid(
                operation_id=rec.operation_id,
                order_id=rec.order_id,
                amount=amount,
            )
            logger.info(
                "Poller (tochka): activated for op=%s", rec.operation_id,
            )
            return "activated"
        if status in ("DECLINED", "CANCELLED", "REJECTED", "FAILED"):
            self.handle_webhook_failed(
                operation_id=rec.operation_id,
                error=str(data.get("errorMessage", "")),
            )
            return "failed"
        return "still_pending"

    async def _poll_yookassa(self, rec) -> str:
        if self.yookassa is None:
            return "error"
        try:
            data = await self.yookassa.get_payment(rec.operation_id)
        except Exception as exc:
            logger.warning(
                "Poll YooKassa failed for %s: %s", rec.operation_id, exc,
            )
            return "error"

        status = data.get("status", "")
        amount_block = data.get("amount") or {}
        try:
            amount = float(amount_block.get("value") or rec.amount)
        except (TypeError, ValueError):
            amount = rec.amount

        if status == "succeeded":
            method = (data.get("payment_method") or {}).get("id", "")
            self.handle_yookassa_webhook_paid(
                payment_id=rec.operation_id,
                order_id=rec.order_id,
                user_id=rec.user_id,
                tariff=rec.tariff,
                amount=amount,
                payment_method_id=method,
                kind=rec.kind,
            )
            logger.info(
                "Poller (yookassa): activated for id=%s", rec.operation_id,
            )
            return "activated"
        if status == "canceled":
            self.payments.mark_failed(
                rec.operation_id,
                error=str(data.get("cancellation_details", "")),
            )
            return "failed"
        return "still_pending"
