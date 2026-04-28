"""Тесты TochkaClient: pure helpers + HTTP-операции через мок httpx.AsyncClient."""
from typing import Any, Dict, Optional

import pytest

import tochka_client
from tochka_client import (
    PaymentResult,
    RecurringResult,
    TochkaClient,
    TochkaError,
    parse_order_id,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None,
                 text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    """Подменяет httpx.AsyncClient. Маршрутизатор задаётся per-test через handler."""

    handler = None  # callable(method, url, headers, json) -> FakeResponse

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        return FakeAsyncClient.handler("POST", url, headers, json)

    async def get(self, url, headers=None):
        return FakeAsyncClient.handler("GET", url, headers, None)


@pytest.fixture
def patched_httpx(monkeypatch):
    monkeypatch.setattr(tochka_client.httpx, "AsyncClient", FakeAsyncClient)
    yield
    FakeAsyncClient.handler = None


@pytest.fixture
def client():
    return TochkaClient(
        jwt_token="jwt-test",
        customer_code="cc-1",
        merchant_id="mid-1",
        base_url="https://example.test/uapi",
        webhook_secret="secret",
        timeout=5.0,
    )


class TestParseOrderId:
    def test_sub_valid(self):
        assert parse_order_id("sub_42_pro_abc12345") == (42, "pro")

    def test_renew_valid(self):
        assert parse_order_id("renew_777_basic_xyz") == (777, "basic")

    def test_empty_returns_none(self):
        assert parse_order_id("") is None

    def test_unknown_prefix(self):
        assert parse_order_id("payment_42_pro_abc") is None

    def test_too_few_parts(self):
        assert parse_order_id("sub_42_pro") is None

    def test_non_int_user_id(self):
        assert parse_order_id("sub_abc_pro_123") is None

    def test_negative_user_id_accepted(self):
        # int("-42") валиден, поэтому tochka это примет
        assert parse_order_id("sub_-42_pro_x") == (-42, "pro")


class TestParseWebhook:
    def test_extracts_from_data_envelope(self):
        body = {"Data": {
            "operationId": "op-1",
            "orderId": "sub_1_pro_x",
            "status": "APPROVED",
            "amount": "999.50",
            "cardToken": "tok-1",
        }}
        result = TochkaClient.parse_webhook(body)
        assert result["operation_id"] == "op-1"
        assert result["order_id"] == "sub_1_pro_x"
        assert result["status"] == "approved"  # lowercased
        assert result["amount"] == 999.50
        assert result["card_token"] == "tok-1"

    def test_works_without_data_envelope(self):
        body = {"operationId": "op-2", "status": "Declined", "amount": 0}
        result = TochkaClient.parse_webhook(body)
        assert result["operation_id"] == "op-2"
        assert result["status"] == "declined"

    def test_id_fallback_to_id_field(self):
        body = {"Data": {"id": "fallback-op", "status": "ok", "amount": 1}}
        assert TochkaClient.parse_webhook(body)["operation_id"] == "fallback-op"

    def test_card_token_fallback(self):
        body = {"Data": {"savedCardToken": "saved-1", "status": "ok", "amount": 1}}
        assert TochkaClient.parse_webhook(body)["card_token"] == "saved-1"

    def test_missing_fields_default_safely(self):
        result = TochkaClient.parse_webhook({})
        assert result["operation_id"] == ""
        assert result["order_id"] == ""
        assert result["status"] == ""
        assert result["amount"] == 0.0
        assert result["card_token"] == ""

    def test_raw_passthrough(self):
        body = {"Data": {"foo": "bar"}}
        assert TochkaClient.parse_webhook(body)["raw"] == {"foo": "bar"}


class TestVerifyWebhook:
    def test_no_secret_skips_check(self):
        c = TochkaClient(jwt_token="x", customer_code="x", webhook_secret="")
        assert c.verify_webhook(b"any body", "any-signature") is True

    def test_empty_signature_with_secret_returns_false(self, client):
        assert client.verify_webhook(b"body", "") is False

    def test_valid_signature(self, client):
        import hashlib
        import hmac
        body = b'{"k":"v"}'
        expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        assert client.verify_webhook(body, expected) is True

    def test_invalid_signature(self, client):
        assert client.verify_webhook(b"body", "deadbeef") is False

    def test_signature_case_insensitive(self, client):
        import hashlib
        import hmac
        body = b"hi"
        expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        assert client.verify_webhook(body, expected.upper()) is True


class TestCreatePayment:
    @pytest.mark.asyncio
    async def test_success_returns_payment_result(self, client, patched_httpx):
        captured = {}

        def handler(method, url, headers, json):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse(200, {"Data": {
                "operationId": "op-100",
                "paymentLink": "https://pay.tochka/op-100",
            }})

        FakeAsyncClient.handler = handler

        result = await client.create_payment(
            amount=499.0,
            purpose="Подписка",
            user_id=42,
            tariff="pro",
            redirect_url="https://t.me/bot?start=ok",
            fail_redirect_url="https://t.me/bot?start=fail",
            email="user@example.com",
        )

        assert isinstance(result, PaymentResult)
        assert result.operation_id == "op-100"
        assert result.payment_link == "https://pay.tochka/op-100"
        assert result.status == "created"

        assert captured["method"] == "POST"
        assert captured["url"].endswith("/acquiring/v1.0/payments_with_receipt")
        assert captured["headers"]["Authorization"] == "Bearer jwt-test"
        data = captured["json"]["Data"]
        assert data["amount"] == "499.00"
        assert data["customerCode"] == "cc-1"
        assert data["merchantId"] == "mid-1"
        assert data["orderId"].startswith("sub_42_pro_")
        assert data["Client"]["email"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_non_200_raises_tochka_error(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(400, {}, text="bad request")
        with pytest.raises(TochkaError):
            await client.create_payment(
                amount=1.0, purpose="x", user_id=1, tariff="t",
                redirect_url="r", fail_redirect_url="f",
            )

    @pytest.mark.asyncio
    async def test_missing_payment_link_raises(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(200, {"Data": {"operationId": "op-1"}})
        with pytest.raises(TochkaError):
            await client.create_payment(
                amount=1.0, purpose="x", user_id=1, tariff="t",
                redirect_url="r", fail_redirect_url="f",
            )

    @pytest.mark.asyncio
    async def test_default_email_when_not_provided(self, client, patched_httpx):
        captured = {}

        def handler(method, url, headers, json):
            captured["json"] = json
            return FakeResponse(200, {"Data": {
                "operationId": "op", "paymentLink": "https://l",
            }})

        FakeAsyncClient.handler = handler
        await client.create_payment(
            amount=1.0, purpose="x", user_id=1, tariff="t",
            redirect_url="r", fail_redirect_url="f",
        )
        assert captured["json"]["Data"]["Client"]["email"] == "noreply@example.com"


class TestChargeRecurring:
    @pytest.mark.asyncio
    async def test_success_returns_status(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(200, {"Data": {
            "operationId": "renew-op-1", "status": "APPROVED",
        }})
        result = await client.charge_recurring(
            amount=100.0, purpose="renew", card_token="tok",
            user_id=42, tariff="pro",
        )
        assert isinstance(result, RecurringResult)
        assert result.operation_id == "renew-op-1"
        assert result.status == "approved"
        assert result.error_message == ""

    @pytest.mark.asyncio
    async def test_non_200_returns_declined_with_message(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(402, {}, text="card declined")
        result = await client.charge_recurring(
            amount=100.0, purpose="renew", card_token="tok",
            user_id=42, tariff="pro",
        )
        assert result.status == "declined"
        assert "402" in result.error_message
        assert "card declined" in result.error_message

    @pytest.mark.asyncio
    async def test_default_pending_status_when_missing(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(200, {"Data": {
            "operationId": "op",
        }})
        result = await client.charge_recurring(
            amount=10.0, purpose="r", card_token="t",
            user_id=1, tariff="x",
        )
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_payload_uses_card_token_and_renew_order_id(self, client, patched_httpx):
        captured = {}

        def handler(method, url, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(200, {"Data": {"operationId": "op", "status": "approved"}})

        FakeAsyncClient.handler = handler
        await client.charge_recurring(
            amount=50.0, purpose="renew", card_token="card-tok-x",
            user_id=99, tariff="pro",
        )
        data = captured["json"]["Data"]
        assert data["cardToken"] == "card-tok-x"
        assert data["orderId"].startswith("renew_99_pro_")
        assert captured["url"].endswith("/acquiring/v1.0/payments_recurring")


class TestGetOperationStatus:
    @pytest.mark.asyncio
    async def test_success_returns_data(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(200, {"Data": {
            "operationId": "op-1", "status": "approved",
        }})
        result = await client.get_operation_status("op-1")
        assert result == {"operationId": "op-1", "status": "approved"}

    @pytest.mark.asyncio
    async def test_non_200_raises(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(404, {}, text="not found")
        with pytest.raises(TochkaError):
            await client.get_operation_status("nope")
