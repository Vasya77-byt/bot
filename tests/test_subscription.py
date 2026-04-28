"""Тесты SubscriptionService — оркестратор Точка + UserStore + PaymentsStore."""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from payments_store import PaymentsStore
from subscription import SubscriptionService
from tochka_client import PaymentResult, RecurringResult
from user_store import TARIFF_PRICES, UserStore


class FakeTochka:
    """Подменяет TochkaClient. Поведение задаётся per-test."""

    def __init__(self):
        self.create_payment_calls: list[dict] = []
        self.charge_recurring_calls: list[dict] = []
        self.next_payment_result: Optional[PaymentResult] = None
        self.next_recurring_result: Optional[RecurringResult] = None
        self.create_payment_exception: Optional[Exception] = None
        self.charge_recurring_exception: Optional[Exception] = None

    async def create_payment(self, **kwargs: Any) -> PaymentResult:
        self.create_payment_calls.append(kwargs)
        if self.create_payment_exception:
            raise self.create_payment_exception
        if not self.next_payment_result:
            raise AssertionError("FakeTochka: next_payment_result not set")
        return self.next_payment_result

    async def charge_recurring(self, **kwargs: Any) -> RecurringResult:
        self.charge_recurring_calls.append(kwargs)
        if self.charge_recurring_exception:
            raise self.charge_recurring_exception
        if not self.next_recurring_result:
            raise AssertionError("FakeTochka: next_recurring_result not set")
        return self.next_recurring_result


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


class TestCreateInitialPayment:
    @pytest.mark.asyncio
    async def test_unknown_tariff_raises(self, service):
        with pytest.raises(ValueError):
            await service.create_initial_payment(1, "unknown")

    @pytest.mark.asyncio
    async def test_returns_link_and_operation_id(self, service, tochka):
        tochka.next_payment_result = PaymentResult(
            operation_id="op-1", payment_link="https://pay.tochka/op-1",
        )
        link, op = await service.create_initial_payment(42, "pro")
        assert link == "https://pay.tochka/op-1"
        assert op == "op-1"

    @pytest.mark.asyncio
    async def test_passes_pricing_and_user_email(self, service, tochka, users):
        users.set_email(42, "u@example.com")
        tochka.next_payment_result = PaymentResult(operation_id="op", payment_link="https://l")
        await service.create_initial_payment(42, "pro")

        call = tochka.create_payment_calls[0]
        assert call["amount"] == float(TARIFF_PRICES["pro"])
        assert call["user_id"] == 42
        assert call["tariff"] == "pro"
        assert call["email"] == "u@example.com"
        assert call["save_card"] is True
        assert call["redirect_url"] == "https://t.me/bot?start=ok"
        assert call["fail_redirect_url"] == "https://t.me/bot?start=fail"

    @pytest.mark.asyncio
    async def test_records_payment_as_initial(self, service, tochka, payments):
        tochka.next_payment_result = PaymentResult(operation_id="op-2", payment_link="https://l")
        await service.create_initial_payment(7, "start")
        rec = payments.find_by_operation("op-2")
        assert rec is not None
        assert rec.user_id == 7
        assert rec.tariff == "start"
        assert rec.amount == float(TARIFF_PRICES["start"])
        assert rec.kind == "initial"
        assert rec.status == "created"


class TestHandleWebhookPaid:
    def test_activates_subscription_when_record_exists(self, service, payments, users):
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        profile = service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok-1", amount=1290.0,
        )
        assert profile is not None
        assert profile.tariff == "pro"
        assert profile.is_subscription_active() is True
        assert profile.card_token == "tok-1"
        assert profile.last_payment_id == "op-1"
        assert payments.find_by_operation("op-1").status == "paid"

    def test_finds_by_order_when_operation_unknown(self, service, payments):
        payments.record_created(
            operation_id="initial-op", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        # Webhook приходит с operation_id, которого нет (другой), но с тем же order_id
        profile = service.handle_webhook_paid(
            operation_id="other-op", order_id="sub_42_pro_x",
            card_token="t", amount=1290.0,
        )
        assert profile is not None
        assert profile.tariff == "pro"

    def test_unknown_operation_and_parseable_order_creates_record(
        self, service, payments
    ):
        # Webhook пришёл первее, чем мы успели записать платёж
        profile = service.handle_webhook_paid(
            operation_id="op-late", order_id="sub_99_pro_xyz",
            card_token="t", amount=1290.0,
        )
        assert profile is not None
        assert profile.user_id == 99
        assert profile.tariff == "pro"
        # Запись создана и помечена paid
        rec = payments.find_by_operation("op-late")
        assert rec is not None
        assert rec.status == "paid"

    def test_unknown_operation_and_unparseable_order_returns_none(self, service):
        result = service.handle_webhook_paid(
            operation_id="op-x", order_id="garbage",
            card_token="t", amount=100.0,
        )
        assert result is None

    def test_idempotency_already_paid(self, service, payments, users):
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        # Первый webhook — активирует
        first = service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok-A", amount=1290.0,
        )
        first_expires = first.tariff_expires_at
        # Второй webhook (дубликат) — не должен второй раз продлевать
        second = service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok-B", amount=1290.0,
        )
        assert second.tariff_expires_at == first_expires
        # Карточный токен от повторного webhook'а не должен затирать (подписка не реактивирована)
        assert second.card_token == "tok-A"


class TestHandleWebhookPaidReferralBonus:
    """Бонус референту за первую оплату приглашённого."""

    def test_no_referrer_no_bonus(self, service, users, payments):
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok", amount=1290.0,
        )
        # Профиль 42 не привязан ни к кому — никаких бонусов
        assert users.get(42).referral_bonus_granted is False

    def test_first_payment_grants_bonus_to_free_referrer(
        self, service, users, payments
    ):
        # Референт - free, его пригласил никто
        referrer = users.get(1)
        # Приглашённый привязан к референту
        users.set_referrer_by_code(42, referrer.referral_code)

        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok", amount=1290.0,
        )

        # Референт получил start на 15 дней
        ref = users.get(1)
        assert ref.tariff == "start"
        assert ref.is_subscription_active() is True
        assert ref.referrals_paid_count == 1
        assert ref.referral_bonus_days_total == 15
        # Приглашённый помечен
        assert users.get(42).referral_bonus_granted is True

    def test_first_payment_extends_paid_referrer(self, service, users, payments):
        # Референт уже на pro
        users.activate_subscription(1, "pro", days=10, card_token="t")
        before_expires = users.get(1).tariff_expires_at

        users.set_referrer_by_code(42, users.get(1).referral_code)
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok", amount=1290.0,
        )

        from datetime import datetime, timedelta
        new_expires = datetime.fromisoformat(users.get(1).tariff_expires_at)
        old_expires = datetime.fromisoformat(before_expires)
        delta = new_expires - old_expires
        assert timedelta(days=14, hours=23) < delta < timedelta(days=15, minutes=1)

    def test_duplicate_webhook_does_not_grant_twice(
        self, service, users, payments
    ):
        users.set_referrer_by_code(42, users.get(1).referral_code)
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        # Первый webhook
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok", amount=1290.0,
        )
        # Дубликат webhook'а
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok", amount=1290.0,
        )
        # Бонус начислен только один раз
        assert users.get(1).referrals_paid_count == 1
        assert users.get(1).referral_bonus_days_total == 15

    def test_second_payment_by_same_user_no_extra_bonus(
        self, service, users, payments
    ):
        # Первый платёж выдал бонус
        users.set_referrer_by_code(42, users.get(1).referral_code)
        payments.record_created(
            operation_id="op-1", order_id="sub_42_pro_x",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-1", order_id="sub_42_pro_x",
            card_token="tok", amount=1290.0,
        )

        # Второй ПЛАТЁЖ от того же приглашённого (например, продление)
        payments.record_created(
            operation_id="op-2", order_id="sub_42_pro_y",
            user_id=42, tariff="pro", amount=1290.0,
        )
        service.handle_webhook_paid(
            operation_id="op-2", order_id="sub_42_pro_y",
            card_token="tok", amount=1290.0,
        )
        # Бонус только один — на первую оплату
        assert users.get(1).referrals_paid_count == 1
        assert users.get(1).referral_bonus_days_total == 15


class TestHandleWebhookFailed:
    def test_marks_failed_in_store(self, service, payments):
        payments.record_created(
            operation_id="op-fail", order_id="o", user_id=1,
            tariff="pro", amount=1290.0,
        )
        service.handle_webhook_failed(operation_id="op-fail", error="declined")
        rec = payments.find_by_operation("op-fail")
        assert rec.status == "failed"
        assert rec.error == "declined"

    def test_unknown_operation_does_not_raise(self, service):
        # Не должно бросить — payments_store вернёт None, но это OK
        service.handle_webhook_failed(operation_id="ghost")


def _profile_with_card(users, user_id, tariff="pro", days_left=10, auto_renew=True,
                      card_token="card-tok"):
    users.activate_subscription(user_id, tariff, days=days_left, card_token=card_token)
    p = users.get(user_id)
    p.auto_renew = auto_renew
    users.save_profile(p)
    return users.get(user_id)


class TestTryRenew:
    @pytest.mark.asyncio
    async def test_free_returns_false(self, service, users):
        profile = users.get(1)  # default free
        ok, msg = await service.try_renew(profile)
        assert ok is False
        assert "auto_renew" in msg or "free" in msg.lower() or msg == "auto_renew disabled"

    @pytest.mark.asyncio
    async def test_auto_renew_disabled_returns_false(self, service, users):
        _profile_with_card(users, 1)
        users.disable_auto_renew(1)
        profile = users.get(1)
        ok, msg = await service.try_renew(profile)
        assert ok is False

    @pytest.mark.asyncio
    async def test_no_card_token_returns_false(self, service, users):
        users.activate_subscription(1, "pro", days=10, card_token="")
        profile = users.get(1)
        ok, msg = await service.try_renew(profile)
        assert ok is False
        assert msg == "no saved card"

    @pytest.mark.asyncio
    async def test_unknown_tariff_returns_false(self, service, users):
        # Создаём «грязный» профиль с тарифом не из прайса
        p = users.get(1)
        p.tariff = "enterprise"  # нет в TARIFF_PRICES
        p.card_token = "tok"
        p.auto_renew = True
        users.save_profile(p)
        ok, msg = await service.try_renew(users.get(1))
        assert ok is False
        assert "enterprise" in msg

    @pytest.mark.asyncio
    async def test_approved_marks_paid_and_extends(self, service, users, payments, tochka):
        _profile_with_card(users, 1, tariff="pro", days_left=2)
        before_expires = users.get(1).tariff_expires_at

        tochka.next_recurring_result = RecurringResult(
            operation_id="renew-op-1", status="approved",
        )
        ok, msg = await service.try_renew(users.get(1))
        assert ok is True
        assert msg == "renewed"

        rec = payments.find_by_operation("renew-op-1")
        assert rec is not None
        assert rec.kind == "recurring"
        assert rec.status == "paid"

        new_expires = users.get(1).tariff_expires_at
        assert new_expires > before_expires  # подписка продлилась

    @pytest.mark.asyncio
    async def test_pending_keeps_record_created(self, service, users, payments, tochka):
        _profile_with_card(users, 1)
        before_expires = users.get(1).tariff_expires_at
        tochka.next_recurring_result = RecurringResult(
            operation_id="renew-pending", status="pending",
        )
        ok, msg = await service.try_renew(users.get(1))
        assert ok is True
        assert msg == "pending"

        rec = payments.find_by_operation("renew-pending")
        assert rec.status == "created"  # ждём подтверждения через webhook

        # Подписка не продлевается до webhook'а
        assert users.get(1).tariff_expires_at == before_expires

    @pytest.mark.asyncio
    async def test_declined_marks_failed_and_records_failure(
        self, service, users, payments, tochka
    ):
        _profile_with_card(users, 1)
        tochka.next_recurring_result = RecurringResult(
            operation_id="renew-decl", status="declined",
            error_message="insufficient funds",
        )
        ok, msg = await service.try_renew(users.get(1))
        assert ok is False
        assert "insufficient" in msg

        rec = payments.find_by_operation("renew-decl")
        assert rec.status == "failed"
        assert rec.error == "insufficient funds"

        # Счётчик неудачных продлений увеличился
        assert users.get(1).renewal_failures == 1

    @pytest.mark.asyncio
    async def test_three_declines_disable_auto_renew(self, service, users, tochka):
        _profile_with_card(users, 1)
        for i in range(3):
            tochka.next_recurring_result = RecurringResult(
                operation_id=f"r-{i}", status="declined",
            )
            await service.try_renew(users.get(1))

        profile = users.get(1)
        assert profile.renewal_failures == 3
        assert profile.auto_renew is False

    @pytest.mark.asyncio
    async def test_declined_with_empty_error_returns_declined_msg(
        self, service, users, tochka
    ):
        _profile_with_card(users, 1)
        tochka.next_recurring_result = RecurringResult(
            operation_id="r", status="declined", error_message="",
        )
        ok, msg = await service.try_renew(users.get(1))
        assert ok is False
        assert msg == "declined"


class TestExpiringSoon:
    def test_skips_free_users(self, service, users):
        users.get(1)  # free, default
        assert service.expiring_soon() == []

    def test_skips_auto_renew_off(self, service, users):
        users.activate_subscription(1, "pro", days=0, card_token="t")
        # Оставшиеся часы прибавились, но отключаем auto_renew
        users.disable_auto_renew(1)
        assert service.expiring_soon(days=1) == []

    def test_skips_no_expiry(self, service, users):
        p = users.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = ""
        users.save_profile(p)
        assert service.expiring_soon() == []

    def test_skips_invalid_isoformat(self, service, users):
        p = users.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = "not-a-date"
        users.save_profile(p)
        assert service.expiring_soon() == []

    def test_skips_already_expired(self, service, users):
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        p = users.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = past.isoformat()
        p.card_token = "t"
        p.auto_renew = True
        users.save_profile(p)
        # Истекли — не подходят (расписание ловит только expires >= now)
        assert service.expiring_soon(days=1) == []

    def test_skips_far_future(self, service, users):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        p = users.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = future.isoformat()
        p.card_token = "t"
        p.auto_renew = True
        users.save_profile(p)
        assert service.expiring_soon(days=1) == []

    def test_returns_profile_within_window(self, service, users):
        soon = datetime.now(timezone.utc) + timedelta(hours=10)
        p = users.get(42)
        p.tariff = "pro"
        p.tariff_expires_at = soon.isoformat()
        p.card_token = "t"
        p.auto_renew = True
        users.save_profile(p)

        result = service.expiring_soon(days=1)
        assert len(result) == 1
        assert result[0].user_id == 42

    def test_window_respects_days_argument(self, service, users):
        in_three_days = datetime.now(timezone.utc) + timedelta(days=3)
        p = users.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = in_three_days.isoformat()
        p.card_token = "t"
        p.auto_renew = True
        users.save_profile(p)

        assert service.expiring_soon(days=1) == []
        assert len(service.expiring_soon(days=5)) == 1
