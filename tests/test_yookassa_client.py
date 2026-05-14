"""Тесты YooKassaClient: создание платежа, автоплатёж, webhook.

HTTP мокается через подмену httpx.AsyncClient.
"""
import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional

import pytest

import yookassa_client
from yookassa_client import (
    ChargeResult,
    PAYMENT_CANCELED_EVENT,
    PAYMENT_STATUS_CANCELED,
    PAYMENT_STATUS_PENDING,
    PAYMENT_STATUS_SUCCEEDED,
    PAYMENT_SUCCEEDED_EVENT,
    PaymentResult,
    WebhookEvent,
    YooKassaClient,
    YooKassaError,
    parse_order_id,
)


# ──────────────────────────────────────────────────────────────────────
# Утилиты: фейковый httpx.AsyncClient
# ──────────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Optional[Dict[str, Any]] = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(payload or {})

    def json(self) -> Dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    handler = None  # callable(method, url, headers, json) -> FakeResponse
    last_call: Dict[str, Any] = {}

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        FakeAsyncClient.last_call = {
            "method": "POST", "url": url,
            "headers": headers, "json": json,
        }
        return FakeAsyncClient.handler("POST", url, headers, json)

    async def get(self, url, headers=None):
        FakeAsyncClient.last_call = {
            "method": "GET", "url": url,
            "headers": headers, "json": None,
        }
        return FakeAsyncClient.handler("GET", url, headers, None)


@pytest.fixture
def patched_httpx(monkeypatch):
    monkeypatch.setattr(yookassa_client.httpx, "AsyncClient", FakeAsyncClient)
    yield
    FakeAsyncClient.handler = None
    FakeAsyncClient.last_call = {}


@pytest.fixture
def client():
    return YooKassaClient(shop_id="123456", secret_key="test_secret_key")


# ──────────────────────────────────────────────────────────────────────
# Аутентификация
# ──────────────────────────────────────────────────────────────────────


def test_basic_auth_header(client):
    headers = client._headers()
    auth = headers["Authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth[len("Basic "):]).decode("ascii")
    assert decoded == "123456:test_secret_key"


def test_idempotence_key_passed_when_provided(client):
    headers = client._headers(idempotence_key="abc-123")
    assert headers["Idempotence-Key"] == "abc-123"


def test_no_idempotence_key_by_default(client):
    headers = client._headers()
    assert "Idempotence-Key" not in headers


# ──────────────────────────────────────────────────────────────────────
# create_payment
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_payment_success(patched_httpx, client):
    def handler(method, url, headers, body):
        assert method == "POST"
        assert url.endswith("/payments")
        assert "Idempotence-Key" in headers
        # Проверяем payload
        assert body["amount"] == {"value": "1290.00", "currency": "RUB"}
        assert body["capture"] is True
        assert body["save_payment_method"] is True
        assert body["metadata"]["user_id"] == "42"
        assert body["metadata"]["tariff"] == "pro"
        assert body["metadata"]["kind"] == "initial"
        assert body["receipt"]["customer"]["email"] == "user@example.com"
        assert body["receipt"]["tax_system_code"] == 2
        assert body["receipt"]["items"][0]["vat_code"] == 1
        return FakeResponse(payload={
            "id": "2c93dbf5-0001-5000-9000-1b68e7b15f3f",
            "status": "pending",
            "confirmation": {
                "type": "redirect",
                "confirmation_url": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=xxx",
            },
        })

    FakeAsyncClient.handler = handler
    result = await client.create_payment(
        amount=1290.0,
        description="Подписка на тариф Pro",
        user_id=42,
        tariff="pro",
        return_url="https://t.me/mybot",
        customer_email="user@example.com",
    )
    assert isinstance(result, PaymentResult)
    assert result.payment_id == "2c93dbf5-0001-5000-9000-1b68e7b15f3f"
    assert result.confirmation_url.startswith("https://yoomoney.ru/")
    assert result.order_id.startswith("sub_42_pro_")
    assert result.status == "pending"


@pytest.mark.asyncio
async def test_create_payment_http_error_raises(patched_httpx, client):
    FakeAsyncClient.handler = lambda *a: FakeResponse(
        status_code=400, text='{"type":"error","description":"Invalid"}',
    )
    with pytest.raises(YooKassaError) as exc_info:
        await client.create_payment(
            amount=100.0, description="x", user_id=1, tariff="start",
            return_url="https://t.me/", customer_email="a@b.ru",
        )
    assert "400" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_payment_incomplete_response_raises(patched_httpx, client):
    FakeAsyncClient.handler = lambda *a: FakeResponse(payload={
        "id": "abc", "status": "pending",
        # confirmation отсутствует
    })
    with pytest.raises(YooKassaError):
        await client.create_payment(
            amount=100.0, description="x", user_id=1, tariff="start",
            return_url="https://t.me/", customer_email="a@b.ru",
        )


@pytest.mark.asyncio
async def test_create_payment_truncates_long_description(patched_httpx, client):
    captured = {}

    def handler(method, url, headers, body):
        captured["body"] = body
        return FakeResponse(payload={
            "id": "x", "status": "pending",
            "confirmation": {"confirmation_url": "https://example.com/pay"},
        })

    FakeAsyncClient.handler = handler
    long_desc = "А" * 200
    await client.create_payment(
        amount=100.0, description=long_desc, user_id=1, tariff="start",
        return_url="https://t.me/", customer_email="a@b.ru",
    )
    assert len(captured["body"]["description"]) == 128


@pytest.mark.asyncio
async def test_create_payment_tax_codes_passed(patched_httpx, client):
    captured = {}

    def handler(method, url, headers, body):
        captured["body"] = body
        return FakeResponse(payload={
            "id": "x", "status": "pending",
            "confirmation": {"confirmation_url": "https://example.com/pay"},
        })

    FakeAsyncClient.handler = handler
    await client.create_payment(
        amount=100.0, description="x", user_id=1, tariff="start",
        return_url="https://t.me/", customer_email="a@b.ru",
        tax_system_code=6, vat_code=4,
    )
    assert captured["body"]["receipt"]["tax_system_code"] == 6
    assert captured["body"]["receipt"]["items"][0]["vat_code"] == 4


# ──────────────────────────────────────────────────────────────────────
# charge_recurring
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_charge_recurring_success(patched_httpx, client):
    def handler(method, url, headers, body):
        assert body["payment_method_id"] == "pm_token_abc"
        assert "confirmation" not in body
        assert body["metadata"]["kind"] == "recurring"
        return FakeResponse(payload={
            "id": "pay_recurring_1", "status": "succeeded",
            "payment_method": {"id": "pm_token_abc"},
        })

    FakeAsyncClient.handler = handler
    result = await client.charge_recurring(
        payment_method_id="pm_token_abc",
        amount=490.0,
        description="Продление Start",
        user_id=7,
        tariff="start",
        customer_email="user@example.com",
    )
    assert isinstance(result, ChargeResult)
    assert result.status == "succeeded"
    assert result.payment_id == "pay_recurring_1"
    assert result.payment_method_id == "pm_token_abc"


@pytest.mark.asyncio
async def test_charge_recurring_http_error_returns_canceled(
    patched_httpx, client,
):
    FakeAsyncClient.handler = lambda *a: FakeResponse(
        status_code=402, text='{"description":"Insufficient funds"}',
    )
    result = await client.charge_recurring(
        payment_method_id="pm_x", amount=100.0, description="x",
        user_id=1, tariff="start", customer_email="a@b.ru",
    )
    assert result.status == "canceled"
    assert "402" in result.error_message


# ──────────────────────────────────────────────────────────────────────
# get_payment / cancel_payment
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_payment_success(patched_httpx, client):
    FakeAsyncClient.handler = lambda *a: FakeResponse(payload={
        "id": "abc", "status": "succeeded",
    })
    data = await client.get_payment("abc")
    assert data["status"] == "succeeded"


@pytest.mark.asyncio
async def test_get_payment_error_raises(patched_httpx, client):
    FakeAsyncClient.handler = lambda *a: FakeResponse(
        status_code=404, text='{"description":"Not found"}',
    )
    with pytest.raises(YooKassaError):
        await client.get_payment("abc")


@pytest.mark.asyncio
async def test_cancel_payment_success(patched_httpx, client):
    FakeAsyncClient.handler = lambda *a: FakeResponse(payload={
        "id": "abc", "status": "canceled",
    })
    assert await client.cancel_payment("abc") is True


@pytest.mark.asyncio
async def test_cancel_payment_failure_returns_false(patched_httpx, client):
    FakeAsyncClient.handler = lambda *a: FakeResponse(
        status_code=400, text='{"description":"already captured"}',
    )
    assert await client.cancel_payment("abc") is False


# ──────────────────────────────────────────────────────────────────────
# Чек 54-ФЗ
# ──────────────────────────────────────────────────────────────────────


def test_build_receipt_structure():
    receipt = YooKassaClient._build_receipt(
        customer_email="u@example.com",
        description="Подписка Pro",
        amount_str="1290.00",
        tax_system_code=2,
        vat_code=1,
    )
    assert receipt["customer"]["email"] == "u@example.com"
    assert receipt["tax_system_code"] == 2
    items = receipt["items"]
    assert len(items) == 1
    assert items[0]["description"] == "Подписка Pro"
    assert items[0]["quantity"] == "1.00"
    assert items[0]["amount"] == {"value": "1290.00", "currency": "RUB"}
    assert items[0]["vat_code"] == 1
    assert items[0]["payment_subject"] == "service"
    assert items[0]["payment_mode"] == "full_prepayment"


# ──────────────────────────────────────────────────────────────────────
# Webhook: IP whitelist
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ip", [
    "185.71.76.1",
    "185.71.76.31",
    "77.75.153.50",
    "77.75.156.11",
])
def test_is_yookassa_ip_accepts_whitelisted(ip):
    assert YooKassaClient.is_yookassa_ip(ip) is True


@pytest.mark.parametrize("ip", [
    "8.8.8.8",
    "1.1.1.1",
    "10.0.0.1",
    "185.71.76.32",   # на границе диапазона (вне)
])
def test_is_yookassa_ip_rejects_other(ip):
    assert YooKassaClient.is_yookassa_ip(ip) is False


def test_is_yookassa_ip_empty_string():
    assert YooKassaClient.is_yookassa_ip("") is False


def test_is_yookassa_ip_garbage():
    assert YooKassaClient.is_yookassa_ip("not-an-ip") is False


# ──────────────────────────────────────────────────────────────────────
# Webhook: HMAC signature
# ──────────────────────────────────────────────────────────────────────


def test_verify_signature_no_secret_always_passes():
    client = YooKassaClient("shop", "key", webhook_secret="")
    assert client.verify_webhook_signature(b"any body", "any sig") is True
    assert client.verify_webhook_signature(b"any body", "") is True


def test_verify_signature_valid_hmac():
    secret = "my_secret"
    body = b'{"event":"payment.succeeded","object":{}}'
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    client = YooKassaClient("shop", "key", webhook_secret=secret)
    assert client.verify_webhook_signature(body, expected) is True


def test_verify_signature_invalid_hmac():
    client = YooKassaClient("shop", "key", webhook_secret="my_secret")
    assert client.verify_webhook_signature(b"body", "wrong_sig") is False


def test_verify_signature_empty_when_secret_set():
    client = YooKassaClient("shop", "key", webhook_secret="my_secret")
    assert client.verify_webhook_signature(b"body", "") is False


# ──────────────────────────────────────────────────────────────────────
# Webhook: парсинг
# ──────────────────────────────────────────────────────────────────────


def test_parse_webhook_payment_succeeded():
    body = json.dumps({
        "event": "payment.succeeded",
        "object": {
            "id": "2c93dbf5-0001-5000-9000-1b68e7b15f3f",
            "status": "succeeded",
            "amount": {"value": "1290.00", "currency": "RUB"},
            "payment_method": {"id": "pm_method_abc", "saved": True},
            "metadata": {
                "order_id": "sub_42_pro_a1b2c3d4",
                "user_id": "42",
                "tariff": "pro",
                "kind": "initial",
            },
        },
    }).encode("utf-8")
    event = YooKassaClient.parse_webhook(body)
    assert isinstance(event, WebhookEvent)
    assert event.event == "payment.succeeded"
    assert event.payment_id == "2c93dbf5-0001-5000-9000-1b68e7b15f3f"
    assert event.status == "succeeded"
    assert event.amount == 1290.0
    assert event.user_id == 42
    assert event.tariff == "pro"
    assert event.kind == "initial"
    assert event.payment_method_id == "pm_method_abc"
    assert event.order_id == "sub_42_pro_a1b2c3d4"


def test_parse_webhook_payment_canceled():
    body = json.dumps({
        "event": "payment.canceled",
        "object": {
            "id": "abc",
            "status": "canceled",
            "amount": {"value": "490.00", "currency": "RUB"},
            "metadata": {"user_id": "7", "tariff": "start", "kind": "recurring"},
        },
    }).encode("utf-8")
    event = YooKassaClient.parse_webhook(body)
    assert event.event == "payment.canceled"
    assert event.status == "canceled"
    assert event.user_id == 7


def test_parse_webhook_bad_json_returns_none():
    assert YooKassaClient.parse_webhook(b"not json") is None


def test_parse_webhook_not_dict_returns_none():
    assert YooKassaClient.parse_webhook(b"[1,2,3]") is None


def test_parse_webhook_missing_object_is_safe():
    body = json.dumps({"event": "payment.succeeded"}).encode("utf-8")
    event = YooKassaClient.parse_webhook(body)
    assert event is not None
    assert event.event == "payment.succeeded"
    assert event.payment_id == ""


def test_parse_webhook_invalid_user_id_becomes_zero():
    body = json.dumps({
        "event": "payment.succeeded",
        "object": {
            "metadata": {"user_id": "garbage"},
            "amount": {"value": "100.00"},
        },
    }).encode("utf-8")
    event = YooKassaClient.parse_webhook(body)
    assert event.user_id == 0


# ──────────────────────────────────────────────────────────────────────
# parse_order_id
# ──────────────────────────────────────────────────────────────────────


def test_parse_order_id_valid():
    assert parse_order_id("sub_42_pro_a1b2c3d4") == (42, "pro")


def test_parse_order_id_with_underscore_in_tariff_takes_third_part():
    # tariff не должен содержать "_", но если случилось — берём третий сегмент
    assert parse_order_id("sub_7_start_xxxx_extra") == (7, "start")


@pytest.mark.parametrize("bad", [
    "",
    "sub_42",                # слишком мало частей
    "renew_42_pro_x",        # не sub_
    "sub_NOT_INT_pro_x",     # user_id не int
])
def test_parse_order_id_invalid(bad):
    assert parse_order_id(bad) is None
