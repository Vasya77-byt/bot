"""Тесты SubscriptionService для провайдера ЮKassa.

Модель: одностадийный платёж с save_payment_method=True. Первый платёж
открывает confirmation_url у ЮKassa. Webhook payment.succeeded приносит
payment_method.id для последующих автосписаний без участия клиента.
"""
from typing import Any, Optional

import pytest

from payments_store import PaymentsStore
from subscription import SubscriptionService
from user_store import TARIFF_PRICES, UserStore
from yookassa_client import (
    ChargeResult,
    PaymentResult,
    YooKassaError,
)


class FakeYooKassa:
    """Подменяет YooKassaClient для тестов SubscriptionService."""

    def __init__(self):
        self.create_payment_calls: list[dict] = []
        self.charge_recurring_calls: list[dict] = []
        self.next_payment_result: Optional[PaymentResult] = None
        self.next_charge_result: Optional[ChargeResult] = None
        self.create_payment_exception: Optional[Exception] = None
        self.charge_recurring_exception: Optional[Exception] = None

    async def create_payment(self, **kwargs: Any) -> PaymentResult:
        self.create_payment_calls.append(kwargs)
        if self.create_payment_exception:
            raise self.create_payment_exception
        if not self.next_payment_result:
            raise AssertionError("FakeYooKassa: next_payment_result not set")
        return self.next_payment_result

    async def charge_recurring(self, **kwargs: Any) -> ChargeResult:
        self.charge_recurring_calls.append(kwargs)
        if self.charge_recurring_exception:
            raise self.charge_recurring_exception
        if not self.next_charge_result:
            raise AssertionError("FakeYooKassa: next_charge_result not set")
        return self.next_charge_result


@pytest.fixture
def users(tmp_path):
    return UserStore(filepath=str(tmp_path / "users.json"))


@pytest.fixture
def payments(tmp_path):
    return PaymentsStore(filepath=str(tmp_path / "payments.json"))


@pytest.fixture
def yookassa():
    return FakeYooKassa()


@pytest.fixture
def service(yookassa, users, payments):
    return SubscriptionService(
        tochka=None,
        yookassa=yookassa,
        provider="yookassa",
        users=users,
        payments=payments,
        redirect_url="https://t.me/mybot",
        fail_redirect_url="https://t.me/mybot?fail=1",
        yookassa_tax_system_code=2,
        yookassa_vat_code=1,
    )


# ──────────────────────────────────────────────────────────────────────
# create_initial_payment
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["sbp", "tpay", "sberpay"])
@pytest.mark.asyncio
async def test_yookassa_methods_force_save_payment_method_false(
    yookassa, users, payments, method,
):
    """СБП/T-Pay/SberPay у ЮKassa не поддерживают save_payment_method.
    Передача true даёт 403. Поэтому даже когда глобальный флаг True —
    для этих методов мы принудительно передаём False."""
    svc = SubscriptionService(
        tochka=None, yookassa=yookassa, provider="yookassa",
        users=users, payments=payments,
        redirect_url="https://t.me/x", fail_redirect_url="https://t.me/x",
        yookassa_save_payment_method=True,  # глобально true
    )
    users.set_email(1, "a@b.ru")
    yookassa.next_payment_result = PaymentResult(
        payment_id="yk_x", confirmation_url="https://example.com/pay",
        order_id="sub_1_start_xx", status="pending",
    )
    await svc.create_initial_payment(1, "start", method=method)
    # Глобальный true → но из-за метода передаётся false
    assert yookassa.create_payment_calls[0]["save_payment_method"] is False


@pytest.mark.asyncio
async def test_initial_payment_save_method_disabled_via_flag(
    yookassa, users, payments,
):
    """Если save_payment_method=False — флаг улетает к ЮKassa как False.
    Используется когда у магазина не подключены рекуррентные платежи."""
    svc = SubscriptionService(
        tochka=None, yookassa=yookassa, provider="yookassa",
        users=users, payments=payments,
        redirect_url="https://t.me/x", fail_redirect_url="https://t.me/x",
        yookassa_save_payment_method=False,
    )
    users.set_email(1, "a@b.ru")
    yookassa.next_payment_result = PaymentResult(
        payment_id="yk_x", confirmation_url="https://example.com/pay",
        order_id="sub_1_start_xx", status="pending",
    )
    await svc.create_initial_payment(1, "start")
    assert yookassa.create_payment_calls[0]["save_payment_method"] is False


@pytest.mark.asyncio
async def test_initial_payment_save_method_true_by_default(
    service, yookassa, users,
):
    """По умолчанию save_payment_method=True (для авто-продлений)."""
    users.set_email(1, "a@b.ru")
    yookassa.next_payment_result = PaymentResult(
        payment_id="yk_x", confirmation_url="https://example.com/pay",
        order_id="sub_1_start_xx", status="pending",
    )
    await service.create_initial_payment(1, "start")
    assert yookassa.create_payment_calls[0]["save_payment_method"] is True


@pytest.mark.parametrize("method,expected_yk_type", [
    ("sbp", "sbp"),
    ("tpay", "tinkoff_bank"),
    ("sberpay", "sberbank"),
])
@pytest.mark.asyncio
async def test_initial_payment_method_routes_to_yookassa_with_type(
    service, yookassa, users, method, expected_yk_type,
):
    """Каждый UI-метод → ЮKassa с правильным payment_method_data.type."""
    users.set_email(1, "a@b.ru")
    yookassa.next_payment_result = PaymentResult(
        payment_id="yk_x", confirmation_url="https://example.com/pay",
        order_id="sub_1_start_xx", status="pending",
    )
    await service.create_initial_payment(1, "start", method=method)
    call = yookassa.create_payment_calls[0]
    assert call["payment_method_type"] == expected_yk_type


@pytest.mark.asyncio
async def test_initial_payment_method_card_routes_to_tochka(
    yookassa, users, payments,
):
    """method=card должен идти в Tochka, даже если provider=yookassa."""

    class FakeTochka:
        def __init__(self):
            self.calls: list = []

        async def create_subscription(self, **kwargs):
            self.calls.append(kwargs)

            class R:
                payment_link = "https://tochka.example/pay/abc"
                operation_id = "op-abc"
            return R()

    tochka = FakeTochka()
    svc = SubscriptionService(
        tochka=tochka, yookassa=yookassa, provider="yookassa",
        users=users, payments=payments,
        redirect_url="https://t.me/x", fail_redirect_url="https://t.me/x",
    )
    link, op_id = await svc.create_initial_payment(1, "start", method="card")
    assert link == "https://tochka.example/pay/abc"
    assert op_id == "op-abc"
    assert len(tochka.calls) == 1
    assert yookassa.create_payment_calls == []


@pytest.mark.asyncio
async def test_initial_payment_unknown_method_raises(service, users):
    """Неизвестный method → ValueError."""
    users.set_email(1, "a@b.ru")
    with pytest.raises(ValueError, match="Unknown payment method"):
        await service.create_initial_payment(1, "start", method="bitcoin")


@pytest.mark.asyncio
async def test_initial_payment_no_method_falls_back_to_provider(
    service, yookassa, users,
):
    """Пустой method (legacy) → fallback на self.provider=yookassa."""
    users.set_email(1, "a@b.ru")
    yookassa.next_payment_result = PaymentResult(
        payment_id="yk_x", confirmation_url="https://example.com/pay",
        order_id="sub_1_start_xx", status="pending",
    )
    await service.create_initial_payment(1, "start")
    assert yookassa.create_payment_calls[0]["payment_method_type"] == ""


@pytest.mark.asyncio
async def test_initial_payment_creates_yookassa_payment(
    service, yookassa, users, payments,
):
    users.set_email(42, "user@example.com")
    yookassa.next_payment_result = PaymentResult(
        payment_id="yk_payment_uuid",
        confirmation_url="https://yoomoney.ru/checkout/xyz",
        order_id="sub_42_pro_abc",
        status="pending",
    )

    url, op_id = await service.create_initial_payment(42, "pro")

    assert url == "https://yoomoney.ru/checkout/xyz"
    assert op_id == "yk_payment_uuid"

    # Проверяем, что в ЮKassa отправились правильные параметры
    call = yookassa.create_payment_calls[0]
    assert call["amount"] == float(TARIFF_PRICES["pro"])
    assert call["user_id"] == 42
    assert call["tariff"] == "pro"
    assert call["customer_email"] == "user@example.com"
    assert call["save_payment_method"] is True
    assert call["tax_system_code"] == 2
    assert call["vat_code"] == 1

    # И что запись платежа создалась
    rec = payments.find_by_operation("yk_payment_uuid")
    assert rec is not None
    assert rec.provider == "yookassa"
    assert rec.user_id == 42
    assert rec.tariff == "pro"
    assert rec.amount == float(TARIFF_PRICES["pro"])
    assert rec.status == "created"
    assert rec.order_id == "sub_42_pro_abc"


@pytest.mark.asyncio
async def test_initial_payment_without_email_raises(service, users):
    # email пустой — должно бросить ValueError
    with pytest.raises(ValueError) as exc_info:
        await service.create_initial_payment(7, "start")
    assert "email_required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_initial_payment_unknown_tariff_raises(service):
    with pytest.raises(ValueError):
        await service.create_initial_payment(1, "unknown_plan")


@pytest.mark.asyncio
async def test_initial_payment_yookassa_error_propagates(service, yookassa, users):
    users.set_email(42, "u@example.com")
    yookassa.create_payment_exception = YooKassaError("API down")
    with pytest.raises(YooKassaError):
        await service.create_initial_payment(42, "pro")


# ──────────────────────────────────────────────────────────────────────
# handle_yookassa_webhook_paid
# ──────────────────────────────────────────────────────────────────────


def test_handle_webhook_activates_subscription(service, users, payments):
    payments.record_created(
        operation_id="yk_pay_1",
        order_id="sub_42_pro_abc",
        user_id=42,
        tariff="pro",
        amount=1290.0,
        kind="initial",
        provider="yookassa",
    )
    profile = service.handle_yookassa_webhook_paid(
        payment_id="yk_pay_1",
        order_id="sub_42_pro_abc",
        user_id=42,
        tariff="pro",
        amount=1290.0,
        payment_method_id="pm_token_xyz",
        kind="initial",
    )
    assert profile is not None
    assert profile.tariff == "pro"
    assert profile.user_id == 42
    assert profile.tariff_expires_at  # установлен
    assert profile.yookassa_payment_method_id == "pm_token_xyz"
    assert profile.auto_renew is True

    # Запись должна стать paid
    rec = payments.find_by_operation("yk_pay_1")
    assert rec.status == "paid"


def test_handle_webhook_idempotent_for_paid(service, users, payments):
    """Повторное получение того же webhook не активирует повторно."""
    payments.record_created(
        operation_id="yk_pay_2",
        order_id="sub_7_start_xx",
        user_id=7, tariff="start", amount=490.0,
        kind="initial", provider="yookassa",
    )
    service.handle_yookassa_webhook_paid(
        payment_id="yk_pay_2",
        order_id="sub_7_start_xx",
        user_id=7, tariff="start", amount=490.0,
        payment_method_id="pm_a",
    )
    first_expires = users.get(7).tariff_expires_at

    # Повторный webhook — статус уже paid, не должен продлевать срок
    service.handle_yookassa_webhook_paid(
        payment_id="yk_pay_2",
        order_id="sub_7_start_xx",
        user_id=7, tariff="start", amount=490.0,
        payment_method_id="pm_a",
    )
    second_expires = users.get(7).tariff_expires_at
    assert first_expires == second_expires


def test_handle_webhook_restores_record_from_metadata(service, payments):
    """Если запись потерялась, webhook сам её создаёт по metadata."""
    profile = service.handle_yookassa_webhook_paid(
        payment_id="yk_lost_pay",
        order_id="sub_99_business_zz",
        user_id=99,
        tariff="business",
        amount=2490.0,
        payment_method_id="pm_b",
    )
    assert profile is not None
    assert profile.user_id == 99
    assert profile.tariff == "business"
    rec = payments.find_by_operation("yk_lost_pay")
    assert rec is not None
    assert rec.provider == "yookassa"


def test_handle_webhook_recurring_does_not_overwrite_payment_method(
    service, users, payments,
):
    """payment_method_id обновляется только при initial-платеже."""
    users.activate_subscription(
        user_id=5, tariff="pro", days=30,
        yookassa_payment_method_id="old_pm_token",
    )
    payments.record_created(
        operation_id="yk_recur_1",
        order_id="sub_5_pro_yy",
        user_id=5, tariff="pro", amount=1290.0,
        kind="recurring", provider="yookassa",
    )
    service.handle_yookassa_webhook_paid(
        payment_id="yk_recur_1",
        order_id="sub_5_pro_yy",
        user_id=5, tariff="pro", amount=1290.0,
        payment_method_id="new_pm_token",  # пришёл новый, но мы не сохраняем
        kind="recurring",
    )
    assert users.get(5).yookassa_payment_method_id == "old_pm_token"


def test_handle_webhook_no_metadata_returns_none(service):
    profile = service.handle_yookassa_webhook_paid(
        payment_id="yk_garbage",
        order_id="",
        user_id=0, tariff="", amount=0.0,
        payment_method_id="",
    )
    assert profile is None


# ──────────────────────────────────────────────────────────────────────
# try_renew (YooKassa)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_renew_succeeded(service, yookassa, users, payments):
    users.set_email(42, "u@example.com")
    users.activate_subscription(
        user_id=42, tariff="pro", days=30,
        yookassa_payment_method_id="pm_saved",
    )
    yookassa.next_charge_result = ChargeResult(
        payment_id="yk_recur_ok",
        status="succeeded",
        payment_method_id="pm_saved",
    )

    profile = users.get(42)
    ok, msg = await service.try_renew(profile)
    assert ok is True
    assert msg == "renewed"

    rec = payments.find_by_operation("yk_recur_ok")
    assert rec is not None
    assert rec.kind == "recurring"
    assert rec.status == "paid"


@pytest.mark.asyncio
async def test_try_renew_pending(service, yookassa, users):
    users.set_email(1, "a@b.ru")
    users.activate_subscription(
        user_id=1, tariff="start", days=30,
        yookassa_payment_method_id="pm_x",
    )
    yookassa.next_charge_result = ChargeResult(
        payment_id="yk_pending", status="pending",
    )
    ok, msg = await service.try_renew(users.get(1))
    assert ok is True
    assert msg == "pending"


@pytest.mark.asyncio
async def test_try_renew_canceled_records_failure(
    service, yookassa, users, payments,
):
    users.set_email(1, "a@b.ru")
    users.activate_subscription(
        user_id=1, tariff="start", days=30,
        yookassa_payment_method_id="pm_x",
    )
    yookassa.next_charge_result = ChargeResult(
        payment_id="yk_decl",
        status="canceled",
        error_message="Insufficient funds",
    )
    ok, msg = await service.try_renew(users.get(1))
    assert ok is False
    assert "Insufficient funds" in msg
    # Счётчик неудач должен увеличиться
    assert users.get(1).renewal_failures == 1


@pytest.mark.asyncio
async def test_try_renew_no_payment_method(service, users):
    users.set_email(1, "a@b.ru")
    users.activate_subscription(user_id=1, tariff="pro", days=30)
    # yookassa_payment_method_id остался пустым
    ok, msg = await service.try_renew(users.get(1))
    assert ok is False
    assert "no saved payment method" in msg


@pytest.mark.asyncio
async def test_try_renew_no_email(service, users):
    users.activate_subscription(
        user_id=1, tariff="pro", days=30,
        yookassa_payment_method_id="pm_x",
    )
    ok, msg = await service.try_renew(users.get(1))
    assert ok is False
    assert "no email" in msg


@pytest.mark.asyncio
async def test_try_renew_disabled_auto_renew(service, users):
    users.set_email(1, "a@b.ru")
    users.activate_subscription(
        user_id=1, tariff="pro", days=30,
        yookassa_payment_method_id="pm_x",
    )
    users.disable_auto_renew(1)
    ok, msg = await service.try_renew(users.get(1))
    assert ok is False
    assert "auto_renew disabled" in msg


@pytest.mark.asyncio
async def test_try_renew_yookassa_exception_returns_false(
    service, yookassa, users,
):
    users.set_email(1, "a@b.ru")
    users.activate_subscription(
        user_id=1, tariff="pro", days=30,
        yookassa_payment_method_id="pm_x",
    )
    yookassa.charge_recurring_exception = YooKassaError("network down")
    ok, msg = await service.try_renew(users.get(1))
    assert ok is False
    assert "network down" in msg
    assert users.get(1).renewal_failures == 1


# ──────────────────────────────────────────────────────────────────────
# cancel_user_subscription (YooKassa)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_disables_auto_renew(service, users):
    users.activate_subscription(
        user_id=10, tariff="pro", days=30,
        yookassa_payment_method_id="pm_x",
    )
    assert users.get(10).auto_renew is True
    ok, msg = await service.cancel_user_subscription(10)
    assert ok is True
    assert "disabled" in msg
    assert users.get(10).auto_renew is False


# ──────────────────────────────────────────────────────────────────────
# Реферальные события: очередь для async-рассылки уведомлений
# ──────────────────────────────────────────────────────────────────────


def test_referral_events_empty_by_default(service):
    assert service.consume_referral_events() == []


def test_referral_events_emitted_on_first_payment(
    service, users, payments,
):
    """При первой оплате приглашённого:
    1) Реферер получает +15 дней → событие 'referrer_paid'
    2) Приглашённый получает +15 дней → событие 'invitee_received'
    """
    referrer = users.get(1)
    users.set_referrer_by_code(2, referrer.referral_code)
    users.set_email(2, "a@b.ru")
    # Подготовка: создаём запись initial-платежа и обрабатываем webhook
    payments.record_created(
        operation_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        kind="initial", provider="yookassa",
    )
    service.handle_yookassa_webhook_paid(
        payment_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        payment_method_id="pm-x", kind="initial",
    )

    events = service.consume_referral_events()
    kinds = sorted(e["kind"] for e in events)
    assert kinds == ["invitee_received", "referrer_paid"]
    # referrer_paid → реферер user_id=1
    referrer_evt = next(e for e in events if e["kind"] == "referrer_paid")
    assert referrer_evt["user_id"] == 1
    assert referrer_evt["days"] == 15
    # invitee_received → приглашённый user_id=2
    invitee_evt = next(e for e in events if e["kind"] == "invitee_received")
    assert invitee_evt["user_id"] == 2
    assert invitee_evt["days"] == 15


def test_referral_events_consume_clears_queue(service, users, payments):
    """consume_referral_events очищает очередь — повторный вызов = []."""
    referrer = users.get(1)
    users.set_referrer_by_code(2, referrer.referral_code)
    users.set_email(2, "a@b.ru")
    payments.record_created(
        operation_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        kind="initial", provider="yookassa",
    )
    service.handle_yookassa_webhook_paid(
        payment_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        payment_method_id="pm-x", kind="initial",
    )

    first = service.consume_referral_events()
    second = service.consume_referral_events()
    assert len(first) >= 1
    assert second == []


def test_referral_events_idempotent_on_duplicate_webhook(
    service, users, payments,
):
    """Повторный webhook не должен повторно класть события в очередь."""
    referrer = users.get(1)
    users.set_referrer_by_code(2, referrer.referral_code)
    users.set_email(2, "a@b.ru")
    payments.record_created(
        operation_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        kind="initial", provider="yookassa",
    )

    service.handle_yookassa_webhook_paid(
        payment_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        payment_method_id="pm-x", kind="initial",
    )
    service.consume_referral_events()  # забрали первую партию

    # Повторный webhook (ЮKassa at-least-once)
    service.handle_yookassa_webhook_paid(
        payment_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        payment_method_id="pm-x", kind="initial",
    )
    # Никаких новых событий — флаги в профилях уже выставлены
    assert service.consume_referral_events() == []


def test_referral_events_empty_when_no_referrer(
    service, users, payments,
):
    """Если у платящего нет реферера — никаких реф-событий."""
    users.set_email(2, "a@b.ru")
    payments.record_created(
        operation_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        kind="initial", provider="yookassa",
    )
    service.handle_yookassa_webhook_paid(
        payment_id="op-x", order_id="sub_2_pro_xx",
        user_id=2, tariff="pro", amount=1290.0,
        payment_method_id="pm-x", kind="initial",
    )
    assert service.consume_referral_events() == []
