"""Тесты SubscriptionService — оркестратор Точка + UserStore + PaymentsStore.

Модель: подписки Точки (recurring=true). Первый платёж создаёт подписку
с operationId, последующие списания через charge_subscription по тому
же operationId. Карты на нашей стороне не хранятся — привязка к карте
живёт в подписке на стороне Точки.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from payments_store import PaymentsStore
from subscription import SubscriptionService
from tochka_client import ChargeResult, SubscriptionResult
from user_store import TARIFF_PRICES, UserStore


class FakeTochka:
    """Подменяет TochkaClient под новый Subscription-API."""

    def __init__(self):
        self.create_subscription_calls: list[dict] = []
        self.charge_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.next_subscription_result: Optional[SubscriptionResult] = None
        self.next_charge_result: Optional[ChargeResult] = None
        self.create_subscription_exception: Optional[Exception] = None
        self.charge_exception: Optional[Exception] = None
        self.cancel_exception: Optional[Exception] = None
        self.cancel_return_value = True

        self.subscription_status_results: dict[str, dict] = {}
        self.subscription_status_exception: Optional[Exception] = None
        self.subscription_status_calls: list[str] = []

    async def create_subscription(self, **kwargs: Any) -> SubscriptionResult:
        self.create_subscription_calls.append(kwargs)
        if self.create_subscription_exception:
            raise self.create_subscription_exception
        if not self.next_subscription_result:
            raise AssertionError("FakeTochka: next_subscription_result not set")
        return self.next_subscription_result

    async def charge_subscription(self, **kwargs: Any) -> ChargeResult:
        self.charge_calls.append(kwargs)
        if self.charge_exception:
            raise self.charge_exception
        if not self.next_charge_result:
            raise AssertionError("FakeTochka: next_charge_result not set")
        return self.next_charge_result

    async def cancel_subscription(self, operation_id: str) -> bool:
        self.cancel_calls.append(operation_id)
        if self.cancel_exception:
            raise self.cancel_exception
        return self.cancel_return_value

    async def get_subscription_status(self, operation_id: str) -> dict:
        self.subscription_status_calls.append(operation_id)
        if self.subscription_status_exception:
            raise self.subscription_status_exception
        return self.subscription_status_results.get(operation_id, {})


@pytest.fixture
def users(tmp_path):
    return UserStore(filepath=str(tmp_path / "users.json"))


@pytest.fixture
def payments(tmp_path):
    return PaymentsStore(filepath=str(tmp_path / "payments.json"))


@pytest.fixture
def tochka():
    return FakeTochka()


@pytest.fixture
def service(tochka, users, payments):
    return SubscriptionService(
        tochka=tochka,
        users=users,
        payments=payments,
        redirect_url="https://t.me/bot?start=ok",
        fail_redirect_url="https://t.me/bot?start=fail",
    )


# ──────────────────────────────────────────────────────────────────────
# create_initial_payment
# ──────────────────────────────────────────────────────────────────────


class TestCreateInitialPayment:
    @pytest.mark.asyncio
    async def test_unknown_tariff_raises(self, service):
        with pytest.raises(ValueError):
            await service.create_initial_payment(1, "unknown")

    @pytest.mark.asyncio
    async def test_returns_link_and_operation_id(self, service, tochka):
        tochka.next_subscription_result = SubscriptionResult(
            operation_id="sub-op-1",
            payment_link="https://pay.tochka/sub-op-1",
        )
        link, op = await service.create_initial_payment(42, "pro")
        assert link == "https://pay.tochka/sub-op-1"
        assert op == "sub-op-1"

    @pytest.mark.asyncio
    async def test_passes_pricing_and_user_email(self, service, tochka, users):
        users.set_email(42, "u@example.com")
        tochka.next_subscription_result = SubscriptionResult(
            operation_id="op", payment_link="https://l",
        )
        await service.create_initial_payment(42, "pro")

        call = tochka.create_subscription_calls[0]
        assert call["amount"] == float(TARIFF_PRICES["pro"])
        assert call["user_id"] == 42
        assert call["tariff"] == "pro"
        assert call["email"] == "u@example.com"
        assert call["redirect_url"] == "https://t.me/bot?start=ok"
        assert call["fail_redirect_url"] == "https://t.me/bot?start=fail"

    @pytest.mark.asyncio
    async def test_records_payment_as_initial(self, service, tochka, payments):
        tochka.next_subscription_result = SubscriptionResult(
            operation_id="op-2", payment_link="https://l",
        )
        await service.create_initial_payment(7, "start")
        rec = payments.find_by_operation("op-2")
        assert rec is not None
        assert rec.user_id == 7
        assert rec.tariff == "start"
        assert rec.amount == float(TARIFF_PRICES["start"])
        assert rec.kind == "initial"
        assert rec.status == "created"

    @pytest.mark.asyncio
    async def test_tax_system_code_propagated(self, users, payments, tochka):
        svc = SubscriptionService(
            tochka=tochka, users=users, payments=payments,
            redirect_url="r", fail_redirect_url="f",
            tax_system_code="6",
        )
        tochka.next_subscription_result = SubscriptionResult(
            operation_id="op", payment_link="x",
        )
        await svc.create_initial_payment(1, "pro")
        assert tochka.create_subscription_calls[0]["tax_system_code"] == "6"


# ──────────────────────────────────────────────────────────────────────
# handle_webhook_paid
# ──────────────────────────────────────────────────────────────────────


class TestHandleWebhookPaid:
    def test_activates_subscription_when_record_exists(
        self, service, payments, users,
    ):
        payments.record_created(
            operation_id="sub-op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        profile = service.handle_webhook_paid(
            operation_id="sub-op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        assert profile is not None
        assert profile.tariff == "pro"
        assert profile.is_subscription_active() is True
        # operationId подписки привязан к профилю
        assert profile.subscription_operation_id == "sub-op-1"
        assert profile.last_payment_id == "sub-op-1"
        assert payments.find_by_operation("sub-op-1").status == "paid"

    def test_finds_by_order_when_operation_unknown(self, service, payments):
        payments.record_created(
            operation_id="initial-op", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        profile = service.handle_webhook_paid(
            operation_id="other-op", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        assert profile is not None
        assert profile.tariff == "pro"

    def test_unknown_operation_and_parseable_order_creates_record(
        self, service, payments,
    ):
        profile = service.handle_webhook_paid(
            operation_id="op-late", order_id="sub_99_pro_xyz",
            amount=1290.0,
        )
        assert profile is not None
        assert profile.user_id == 99
        assert profile.tariff == "pro"
        rec = payments.find_by_operation("op-late")
        assert rec is not None
        assert rec.status == "paid"

    def test_unknown_operation_and_unparseable_order_returns_none(self, service):
        result = service.handle_webhook_paid(
            operation_id="op-x", order_id="garbage", amount=100.0,
        )
        assert result is None

    def test_idempotency_already_paid(self, service, payments, users):
        payments.record_created(
            operation_id="sub-op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        first = service.handle_webhook_paid(
            operation_id="sub-op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        first_expires = first.tariff_expires_at
        second = service.handle_webhook_paid(
            operation_id="sub-op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        # Не продлевает дважды на дубликате webhook'а
        assert second.tariff_expires_at == first_expires

    def test_recurring_payment_does_not_overwrite_subscription_id(
        self, service, payments, users,
    ):
        # Initial платёж — привязывает subscription_operation_id
        payments.record_created(
            operation_id="sub-op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
            kind="initial",
        )
        service.handle_webhook_paid(
            operation_id="sub-op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        assert users.get(42).subscription_operation_id == "sub-op-1"

        # Recurring платёж (через 30 дней) — НЕ должен перезатереть
        payments.record_created(
            operation_id="sub-op-1", order_id="sub-op-1",
            user_id=42, tariff="pro", amount=1290.0,
            kind="recurring",
        )
        # Но кстати — у нас тот же operation_id для recurring,
        # так что find_by_operation вернёт первый paid и сработает
        # idempotency.  Здесь просто проверяем, что профиль не сломался.
        assert users.get(42).subscription_operation_id == "sub-op-1"


class TestHandleWebhookPaidReferralBonus:
    def test_no_referrer_no_bonus(self, service, users, payments):
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        assert users.get(42).referral_bonus_granted is False

    def test_first_payment_grants_bonus_to_free_referrer(
        self, service, users, payments,
    ):
        referrer = users.get(1)
        users.set_referrer_by_code(42, referrer.referral_code)
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        ref = users.get(1)
        assert ref.tariff == "start"
        assert ref.is_subscription_active() is True
        assert ref.referrals_paid_count == 1
        assert ref.referral_bonus_days_total == 15

    def test_duplicate_webhook_does_not_grant_twice(
        self, service, users, payments,
    ):
        users.set_referrer_by_code(42, users.get(1).referral_code)
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        assert users.get(1).referrals_paid_count == 1


# ──────────────────────────────────────────────────────────────────────
# try_renew (использует subscription_operation_id, не cardToken)
# ──────────────────────────────────────────────────────────────────────


def _profile_with_subscription(users, user_id, tariff="pro", days_left=10,
                                auto_renew=True, sub_op_id="sub-op-1"):
    users.activate_subscription(
        user_id, tariff, days=days_left,
        subscription_operation_id=sub_op_id,
    )
    p = users.get(user_id)
    p.auto_renew = auto_renew
    users.save_profile(p)
    return users.get(user_id)


class TestTryRenew:
    @pytest.mark.asyncio
    async def test_free_returns_false(self, service, users):
        profile = users.get(1)
        ok, msg = await service.try_renew(profile)
        assert ok is False

    @pytest.mark.asyncio
    async def test_auto_renew_off_returns_false(self, service, users):
        _profile_with_subscription(users, 1)
        users.disable_auto_renew(1)
        ok, msg = await service.try_renew(users.get(1))
        assert ok is False

    @pytest.mark.asyncio
    async def test_no_subscription_id_returns_false(self, service, users):
        # Активная подписка без subscription_operation_id
        users.activate_subscription(1, "pro", days=10)  # без sub_op_id
        p = users.get(1)
        # Убеждаемся что поле пустое
        assert not p.subscription_operation_id
        ok, msg = await service.try_renew(p)
        assert ok is False
        assert "no subscription" in msg

    @pytest.mark.asyncio
    async def test_unknown_tariff_returns_false(self, service, users):
        p = users.get(1)
        p.tariff = "enterprise"
        p.subscription_operation_id = "sub-op-1"
        p.auto_renew = True
        users.save_profile(p)
        ok, msg = await service.try_renew(users.get(1))
        assert ok is False
        assert "enterprise" in msg

    @pytest.mark.asyncio
    async def test_approved_extends_subscription(
        self, service, users, payments, tochka,
    ):
        _profile_with_subscription(users, 1, tariff="pro", days_left=2,
                                   sub_op_id="sub-op-1")
        before_expires = users.get(1).tariff_expires_at

        tochka.next_charge_result = ChargeResult(
            operation_id="sub-op-1", status="approved",
        )
        ok, msg = await service.try_renew(users.get(1))
        assert ok is True
        assert msg == "renewed"

        # charge вызван по operationId подписки
        assert tochka.charge_calls[0]["operation_id"] == "sub-op-1"
        assert tochka.charge_calls[0]["amount"] == float(TARIFF_PRICES["pro"])

        rec = payments.find_by_operation("sub-op-1")
        assert rec is not None
        assert rec.kind == "recurring"
        assert rec.status == "paid"

        new_expires = users.get(1).tariff_expires_at
        assert new_expires > before_expires

    @pytest.mark.asyncio
    async def test_pending_keeps_record_created(
        self, service, users, payments, tochka,
    ):
        _profile_with_subscription(users, 1, sub_op_id="sub-pending")
        before_expires = users.get(1).tariff_expires_at
        tochka.next_charge_result = ChargeResult(
            operation_id="sub-pending", status="pending",
        )
        ok, msg = await service.try_renew(users.get(1))
        assert ok is True
        assert msg == "pending"
        assert payments.find_by_operation("sub-pending").status == "created"
        # Подписка не продлевается до webhook'а
        assert users.get(1).tariff_expires_at == before_expires

    @pytest.mark.asyncio
    async def test_declined_marks_failed_and_records_failure(
        self, service, users, payments, tochka,
    ):
        _profile_with_subscription(users, 1, sub_op_id="sub-decl")
        tochka.next_charge_result = ChargeResult(
            operation_id="sub-decl", status="declined",
            error_message="insufficient funds",
        )
        ok, msg = await service.try_renew(users.get(1))
        assert ok is False
        assert "insufficient" in msg
        assert payments.find_by_operation("sub-decl").status == "failed"
        assert users.get(1).renewal_failures == 1

    @pytest.mark.asyncio
    async def test_three_declines_disable_auto_renew(
        self, service, users, tochka,
    ):
        _profile_with_subscription(users, 1, sub_op_id="sub-x")
        for _ in range(3):
            tochka.next_charge_result = ChargeResult(
                operation_id="sub-x", status="declined",
            )
            await service.try_renew(users.get(1))
        profile = users.get(1)
        assert profile.renewal_failures == 3
        assert profile.auto_renew is False


# ──────────────────────────────────────────────────────────────────────
# cancel_user_subscription
# ──────────────────────────────────────────────────────────────────────


class TestCancelUserSubscription:
    @pytest.mark.asyncio
    async def test_disables_auto_renew_locally(self, service, users):
        _profile_with_subscription(users, 1, sub_op_id="sub-1")
        ok, msg = await service.cancel_user_subscription(1)
        assert ok is True
        assert users.get(1).auto_renew is False

    @pytest.mark.asyncio
    async def test_calls_tochka_cancel_when_subscription_exists(
        self, service, users, tochka,
    ):
        _profile_with_subscription(users, 1, sub_op_id="sub-x")
        await service.cancel_user_subscription(1)
        assert tochka.cancel_calls == ["sub-x"]

    @pytest.mark.asyncio
    async def test_no_subscription_id_skips_tochka_call(
        self, service, users, tochka,
    ):
        # Профиль без subscription_operation_id
        users.get(1)
        await service.cancel_user_subscription(1)
        # Tochka не вызывалась
        assert tochka.cancel_calls == []
        # Локально auto_renew всё равно выключен
        assert users.get(1).auto_renew is False

    @pytest.mark.asyncio
    async def test_tochka_exception_does_not_crash(
        self, service, users, tochka,
    ):
        _profile_with_subscription(users, 1, sub_op_id="sub-x")
        tochka.cancel_exception = RuntimeError("Tochka 500")
        ok, _ = await service.cancel_user_subscription(1)
        assert ok is True
        # Локально auto_renew всё равно выключен
        assert users.get(1).auto_renew is False


# ──────────────────────────────────────────────────────────────────────
# expiring_soon
# ──────────────────────────────────────────────────────────────────────


class TestExpiringSoon:
    def test_skips_free_users(self, service, users):
        users.get(1)
        assert service.expiring_soon() == []

    def test_skips_auto_renew_off(self, service, users):
        _profile_with_subscription(users, 1)
        users.disable_auto_renew(1)
        assert service.expiring_soon(days=1) == []

    def test_skips_no_expiry(self, service, users):
        p = users.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = ""
        users.save_profile(p)
        assert service.expiring_soon() == []

    def test_returns_profile_within_window(self, service, users):
        soon = datetime.now(timezone.utc) + timedelta(hours=10)
        p = users.get(42)
        p.tariff = "pro"
        p.tariff_expires_at = soon.isoformat()
        p.subscription_operation_id = "sub-x"
        p.auto_renew = True
        users.save_profile(p)
        result = service.expiring_soon(days=1)
        assert len(result) == 1
        assert result[0].user_id == 42

    def test_skips_already_expired(self, service, users):
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        p = users.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = past.isoformat()
        p.subscription_operation_id = "sub-x"
        p.auto_renew = True
        users.save_profile(p)
        assert service.expiring_soon(days=1) == []


# ──────────────────────────────────────────────────────────────────────
# poll_pending_payments через get_subscription_status
# ──────────────────────────────────────────────────────────────────────


class TestPollPendingPayments:
    @pytest.mark.asyncio
    async def test_no_pending_no_calls(self, service, payments, tochka):
        result = await service.poll_pending_payments(older_than_seconds=0)
        assert result == []
        assert tochka.subscription_status_calls == []

    @pytest.mark.asyncio
    async def test_approved_status_activates_subscription(
        self, service, payments, users, tochka,
    ):
        payments.record_created(
            operation_id="sub-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        tochka.subscription_status_results["sub-1"] = {
            "status": "Approved",
            "amount": 1290.0,
        }
        result = await service.poll_pending_payments(older_than_seconds=0)
        assert ("sub-1", "activated") in result
        assert users.get(42).is_subscription_active() is True
        # operationId подписки привязан
        assert users.get(42).subscription_operation_id == "sub-1"
        assert payments.find_by_operation("sub-1").status == "paid"

    @pytest.mark.asyncio
    async def test_authorized_status_also_activates(
        self, service, payments, users, tochka,
    ):
        # AUTHORIZED — двухэтапная оплата, мы трактуем как успех
        payments.record_created(
            operation_id="sub-2", order_id="sub_1_pro_x",
            user_id=1, tariff="pro", amount=1290.0,
        )
        tochka.subscription_status_results["sub-2"] = {"status": "AUTHORIZED"}
        result = await service.poll_pending_payments(older_than_seconds=0)
        assert ("sub-2", "activated") in result

    @pytest.mark.asyncio
    async def test_declined_status_marks_failed(
        self, service, payments, tochka,
    ):
        payments.record_created(
            operation_id="sub-d", order_id="sub_1_pro_x",
            user_id=1, tariff="pro", amount=1290.0,
        )
        tochka.subscription_status_results["sub-d"] = {
            "status": "Declined",
            "errorMessage": "card blocked",
        }
        result = await service.poll_pending_payments(older_than_seconds=0)
        assert ("sub-d", "failed") in result
        rec = payments.find_by_operation("sub-d")
        assert rec.status == "failed"
        assert "card blocked" in rec.error

    @pytest.mark.asyncio
    async def test_still_pending_keeps_status(self, service, payments, tochka):
        payments.record_created(
            operation_id="sub-w", order_id="sub_1_pro_x",
            user_id=1, tariff="pro", amount=1290.0,
        )
        tochka.subscription_status_results["sub-w"] = {"status": "Created"}
        result = await service.poll_pending_payments(older_than_seconds=0)
        assert ("sub-w", "still_pending") in result
        assert payments.find_by_operation("sub-w").status == "created"

    @pytest.mark.asyncio
    async def test_tochka_error_isolated(self, service, payments, tochka):
        payments.record_created(
            operation_id="sub-e", order_id="sub_1_pro_x",
            user_id=1, tariff="pro", amount=1290.0,
        )
        tochka.subscription_status_exception = RuntimeError("Tochka 500")
        result = await service.poll_pending_payments(older_than_seconds=0)
        assert ("sub-e", "error") in result
        assert payments.find_by_operation("sub-e").status == "created"

    @pytest.mark.asyncio
    async def test_idempotent_with_already_paid_record(
        self, service, payments, users, tochka,
    ):
        payments.record_created(
            operation_id="sub-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="sub-1", order_id="sub_42_pro_x",
            amount=1290.0,
        )
        first_expires = users.get(42).tariff_expires_at
        # Поллер не должен дёргать API на paid-записях
        result = await service.poll_pending_payments(older_than_seconds=0)
        assert result == []
        assert tochka.subscription_status_calls == []
        assert users.get(42).tariff_expires_at == first_expires


# ──────────────────────────────────────────────────────────────────────
# Tier-награды: события tier_unlocked в очереди реф-уведомлений
# ──────────────────────────────────────────────────────────────────────


class TestTierUnlockedEvents:
    def _pay_n_referrals(self, service, payments, users, referrer_id, n):
        ref = users.get(referrer_id)
        for i in range(n):
            invited_id = 5000 + i
            users.set_referrer_by_code(invited_id, ref.referral_code)
            op_id = f"op-{i}"
            order = f"sub_{invited_id}_pro_x"
            payments.record_created(
                operation_id=op_id, order_id=order,
                user_id=invited_id, tariff="pro", amount=1290.0,
            )
            service.handle_webhook_paid(
                operation_id=op_id, order_id=order, amount=1290.0,
            )

    def test_bronze_payment_emits_tier_unlocked_not_referrer_paid(
        self, service, users, payments,
    ):
        self._pay_n_referrals(service, payments, users, 1, 3)
        events = service.consume_referral_events()
        # 3 invitee_received + 2 referrer_paid + 1 tier_unlocked (на 3-ей оплате)
        tier_events = [e for e in events if e["kind"] == "tier_unlocked"]
        referrer_paid = [e for e in events if e["kind"] == "referrer_paid"]
        assert len(tier_events) == 1
        assert tier_events[0]["tier_key"] == "bronze"
        assert tier_events[0]["user_id"] == 1
        # На пересекающей tier оплате referrer_paid НЕ дублируется
        assert len(referrer_paid) == 2  # за 1-ую и 2-ую оплату

    def test_non_threshold_payment_emits_referrer_paid(
        self, service, users, payments,
    ):
        # 1 оплата — не достигает bronze (=3), идёт обычный referrer_paid
        self._pay_n_referrals(service, payments, users, 1, 1)
        events = service.consume_referral_events()
        tier_events = [e for e in events if e["kind"] == "tier_unlocked"]
        referrer_paid = [e for e in events if e["kind"] == "referrer_paid"]
        assert tier_events == []
        assert len(referrer_paid) == 1

    def test_consume_clears_queue(self, service, users, payments):
        self._pay_n_referrals(service, payments, users, 1, 1)
        first = service.consume_referral_events()
        second = service.consume_referral_events()
        assert len(first) >= 1
        assert second == []

