"""Тесты TochkaClient: подписки, webhooks (RS256), управление webhook'ами.

HTTP мокается через подмену httpx.AsyncClient. JWT-вебхуки —
реальная RSA-подпись с тестовой парой ключей через cryptography.
"""
import json
from typing import Any, Dict, Optional

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

import tochka_client
from tochka_client import (
    ACQUIRING_EVENT,
    ChargeResult,
    SUCCESS_STATUSES,
    SubscriptionResult,
    TochkaClient,
    TochkaError,
    parse_payment_link_id,
    parse_order_id,
)


# ──────────────────────────────────────────────────────────────────────
# Утилиты: фейковый httpx.AsyncClient
# ──────────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(
        self, status_code: int = 200,
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

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        return FakeAsyncClient.handler("POST", url, headers, json)

    async def put(self, url, headers=None, json=None):
        return FakeAsyncClient.handler("PUT", url, headers, json)

    async def get(self, url, headers=None):
        return FakeAsyncClient.handler("GET", url, headers, None)


@pytest.fixture
def patched_httpx(monkeypatch):
    monkeypatch.setattr(tochka_client.httpx, "AsyncClient", FakeAsyncClient)
    yield
    FakeAsyncClient.handler = None


# ──────────────────────────────────────────────────────────────────────
# Тестовая RSA-пара ключей для проверки JWT-вебхуков
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rsa_keypair():
    """Генерируем тестовую RSA-пару один раз на модуль."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    pem_private = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # JWK с тестовым публичным ключом
    public_jwk_str = RSAAlgorithm.to_jwk(public_key)
    public_jwk = json.loads(public_jwk_str)
    return pem_private, public_jwk


@pytest.fixture
def client(rsa_keypair):
    _, public_jwk = rsa_keypair
    return TochkaClient(
        jwt_token="jwt-test",
        customer_code="cc-1",
        client_id="cli-1",
        merchant_id="mid-1",
        base_url="https://example.test/uapi",
        public_key_jwk=public_jwk,
        timeout=5.0,
    )


def _sign_webhook(claims: dict, private_key_pem: bytes) -> bytes:
    """Создаёт корректный JWT-вебхук от имени Точки."""
    token = jwt.encode(claims, private_key_pem, algorithm="RS256")
    return token.encode("utf-8") if isinstance(token, str) else token


# ──────────────────────────────────────────────────────────────────────
# parse_payment_link_id
# ──────────────────────────────────────────────────────────────────────


class TestParsePaymentLinkId:
    def test_sub_valid(self):
        assert parse_payment_link_id("sub_42_pro_abc12345") == (42, "pro")

    def test_renew_valid(self):
        assert parse_payment_link_id("renew_777_basic_xyz") == (777, "basic")

    def test_empty(self):
        assert parse_payment_link_id("") is None

    def test_unknown_prefix(self):
        assert parse_payment_link_id("payment_42_pro_abc") is None

    def test_too_few_parts(self):
        assert parse_payment_link_id("sub_42_pro") is None

    def test_non_int_user(self):
        assert parse_payment_link_id("sub_abc_pro_x") is None

    def test_alias_parse_order_id(self):
        # Алиас оставлен для обратной совместимости
        assert parse_order_id is parse_payment_link_id


# ──────────────────────────────────────────────────────────────────────
# create_subscription
# ──────────────────────────────────────────────────────────────────────


class TestCreateSubscription:
    @pytest.mark.asyncio
    async def test_success_returns_subscription_result(
        self, client, patched_httpx,
    ):
        captured = {}

        def handler(method, url, headers, json):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse(200, {"Data": {
                "operationId": "sub-op-1",
                "paymentLink": "https://pay.tochka/sub-op-1",
            }})

        FakeAsyncClient.handler = handler

        result = await client.create_subscription(
            amount=1290.0,
            purpose="Подписка PRO",
            user_id=42,
            tariff="pro",
            redirect_url="https://t.me/bot?start=ok",
            fail_redirect_url="https://t.me/bot?start=fail",
            email="user@example.com",
        )

        assert isinstance(result, SubscriptionResult)
        assert result.operation_id == "sub-op-1"
        assert result.payment_link == "https://pay.tochka/sub-op-1"
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/acquiring/v1.0/subscriptions_with_receipt")
        assert captured["headers"]["Authorization"] == "Bearer jwt-test"

    @pytest.mark.asyncio
    async def test_request_body_has_data_envelope(self, client, patched_httpx):
        captured = {}
        FakeAsyncClient.handler = lambda *a: (
            captured.setdefault("body", a[3]),
            FakeResponse(200, {"Data": {"operationId": "x", "paymentLink": "y"}}),
        )[1]

        await client.create_subscription(
            amount=490, purpose="P", user_id=1, tariff="start",
            redirect_url="r", fail_redirect_url="f",
        )
        body = captured["body"]
        # Точка ждёт обёртку Data
        assert "Data" in body
        data = body["Data"]
        assert data["customerCode"] == "cc-1"
        assert data["amount"] == "490.00"
        assert data["recurring"] is True
        assert data["saveCard"] is True
        # paymentLinkId формата sub_<uid>_<tariff>_<rand>
        assert data["paymentLinkId"].startswith("sub_1_start_")
        assert data["merchantId"] == "mid-1"

    @pytest.mark.asyncio
    async def test_request_includes_items_and_client(
        self, client, patched_httpx,
    ):
        captured = {}
        FakeAsyncClient.handler = lambda *a: (
            captured.setdefault("body", a[3]),
            FakeResponse(200, {"Data": {"operationId": "x", "paymentLink": "y"}}),
        )[1]

        await client.create_subscription(
            amount=1290, purpose="P", user_id=1, tariff="pro",
            redirect_url="r", fail_redirect_url="f",
            email="user@example.com",
        )
        data = captured["body"]["Data"]
        assert data["Client"]["email"] == "user@example.com"
        items = data["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["paymentMethod"] == "full_prepayment"
        assert item["paymentObject"] == "service"
        assert item["vatType"] == "none"
        assert item["amount"] == "1290.00"

    @pytest.mark.asyncio
    async def test_default_email_when_not_provided(
        self, client, patched_httpx,
    ):
        captured = {}
        FakeAsyncClient.handler = lambda *a: (
            captured.setdefault("body", a[3]),
            FakeResponse(200, {"Data": {"operationId": "x", "paymentLink": "y"}}),
        )[1]

        await client.create_subscription(
            amount=490, purpose="P", user_id=1, tariff="start",
            redirect_url="r", fail_redirect_url="f",
        )
        assert captured["body"]["Data"]["Client"]["email"] == "noreply@example.com"

    @pytest.mark.asyncio
    async def test_tax_system_code_passed_when_set(
        self, client, patched_httpx,
    ):
        captured = {}
        FakeAsyncClient.handler = lambda *a: (
            captured.setdefault("body", a[3]),
            FakeResponse(200, {"Data": {"operationId": "x", "paymentLink": "y"}}),
        )[1]

        await client.create_subscription(
            amount=490, purpose="P", user_id=1, tariff="start",
            redirect_url="r", fail_redirect_url="f",
            tax_system_code="6",
        )
        assert captured["body"]["Data"]["taxSystemCode"] == "6"

    @pytest.mark.asyncio
    async def test_non_200_raises(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            400, {}, text="bad request",
        )
        with pytest.raises(TochkaError):
            await client.create_subscription(
                amount=1, purpose="P", user_id=1, tariff="t",
                redirect_url="r", fail_redirect_url="f",
            )

    @pytest.mark.asyncio
    async def test_missing_link_or_op_raises(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            200, {"Data": {"operationId": "op"}},  # нет paymentLink
        )
        with pytest.raises(TochkaError):
            await client.create_subscription(
                amount=1, purpose="P", user_id=1, tariff="t",
                redirect_url="r", fail_redirect_url="f",
            )


# ──────────────────────────────────────────────────────────────────────
# charge_subscription
# ──────────────────────────────────────────────────────────────────────


class TestChargeSubscription:
    @pytest.mark.asyncio
    async def test_success_approved(self, client, patched_httpx):
        captured = {}

        def handler(method, url, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(200, {"Data": {"status": "Approved"}})

        FakeAsyncClient.handler = handler
        result = await client.charge_subscription(
            operation_id="sub-op-1", amount=1290.0,
        )
        assert isinstance(result, ChargeResult)
        assert result.operation_id == "sub-op-1"
        assert result.status == "approved"
        assert captured["url"].endswith("/subscriptions/sub-op-1/charge")
        # body c обёрткой Data
        assert captured["json"] == {"Data": {"amount": 1290.0}}

    @pytest.mark.asyncio
    async def test_pending_status(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            200, {"Data": {"status": "Pending"}},
        )
        result = await client.charge_subscription(
            operation_id="op", amount=100.0,
        )
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_default_pending_when_status_missing(
        self, client, patched_httpx,
    ):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            200, {"Data": {}},
        )
        result = await client.charge_subscription(
            operation_id="op", amount=100.0,
        )
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_non_200_returns_declined(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            402, {}, text="card declined",
        )
        result = await client.charge_subscription(
            operation_id="op", amount=100.0,
        )
        assert result.status == "declined"
        assert "402" in result.error_message
        assert "card declined" in result.error_message


# ──────────────────────────────────────────────────────────────────────
# cancel_subscription / get_subscription_status
# ──────────────────────────────────────────────────────────────────────


class TestSubscriptionStatusOps:
    @pytest.mark.asyncio
    async def test_cancel_success(self, client, patched_httpx):
        captured = {}

        def handler(method, url, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(200, {"Data": {}})

        FakeAsyncClient.handler = handler
        ok = await client.cancel_subscription("sub-op-1")
        assert ok is True
        assert captured["url"].endswith("/subscriptions/sub-op-1/status")
        assert captured["json"] == {"Data": {"status": "Cancelled"}}

    @pytest.mark.asyncio
    async def test_cancel_failure_returns_false(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            403, {}, text="forbidden",
        )
        assert await client.cancel_subscription("sub-op-1") is False

    @pytest.mark.asyncio
    async def test_get_status_returns_data(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            200, {"Data": {"status": "Approved", "amount": "1290.00"}},
        )
        data = await client.get_subscription_status("sub-op-1")
        assert data["status"] == "Approved"
        assert data["amount"] == "1290.00"

    @pytest.mark.asyncio
    async def test_get_status_non_200_raises(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            500, {}, text="server error",
        )
        with pytest.raises(TochkaError):
            await client.get_subscription_status("op")


# ──────────────────────────────────────────────────────────────────────
# Webhook management (PUT/GET)
# ──────────────────────────────────────────────────────────────────────


class TestWebhookManagement:
    @pytest.mark.asyncio
    async def test_register_webhook_uses_client_id(
        self, client, patched_httpx,
    ):
        captured = {}

        def handler(method, url, headers, json):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(200, {"Data": {}})

        FakeAsyncClient.handler = handler
        ok = await client.register_webhook(
            url="https://my-bot.example/tochka/webhook",
            events=["acquiringInternetPayment"],
        )
        assert ok is True
        assert captured["method"] == "PUT"
        assert captured["url"].endswith("/webhook/v1.0/cli-1")
        assert captured["json"] == {
            "webhooksList": ["acquiringInternetPayment"],
            "url": "https://my-bot.example/tochka/webhook",
        }

    @pytest.mark.asyncio
    async def test_register_webhook_no_client_id_raises(self, rsa_keypair):
        _, public_jwk = rsa_keypair
        c = TochkaClient(
            jwt_token="x", customer_code="cc",
            client_id="",  # пусто!
            public_key_jwk=public_jwk,
        )
        with pytest.raises(TochkaError):
            await c.register_webhook(url="x", events=["e"])

    @pytest.mark.asyncio
    async def test_register_webhook_failure_returns_false(
        self, client, patched_httpx,
    ):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            400, {}, text="bad",
        )
        assert await client.register_webhook(
            url="x", events=["acquiringInternetPayment"],
        ) is False

    @pytest.mark.asyncio
    async def test_get_webhooks(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            200, {"Data": {"webhooksList": ["acquiringInternetPayment"],
                           "url": "https://existing"}},
        )
        data = await client.get_webhooks()
        assert "webhooksList" in data
        assert "url" in data


# ──────────────────────────────────────────────────────────────────────
# get_retailers / get_customers
# ──────────────────────────────────────────────────────────────────────


class TestRetailersAndCustomers:
    @pytest.mark.asyncio
    async def test_get_retailers_passes_customer_code(
        self, client, patched_httpx,
    ):
        captured = {}
        FakeAsyncClient.handler = lambda *a: (
            captured.setdefault("url", a[1]),
            FakeResponse(200, {"Data": {"Retailer": []}}),
        )[1]

        await client.get_retailers()
        assert "customerCode=cc-1" in captured["url"]

    @pytest.mark.asyncio
    async def test_get_customers(self, client, patched_httpx):
        FakeAsyncClient.handler = lambda *a: FakeResponse(
            200, {"Data": {"Customer": [
                {"customerCode": "300123", "customerType": "Business"},
            ]}},
        )
        data = await client.get_customers()
        assert "Customer" in data


# ──────────────────────────────────────────────────────────────────────
# verify_webhook + parse_acquiring_webhook (RS256)
# ──────────────────────────────────────────────────────────────────────


class TestVerifyWebhook:
    def test_valid_jwt_returns_claims(self, client, rsa_keypair):
        private_pem, _ = rsa_keypair
        payload = {
            "webhookType": "acquiringInternetPayment",
            "operationId": "op-1",
            "amount": "1290.00",
            "status": "APPROVED",
            "paymentLinkId": "sub_42_pro_abc",
            "customerCode": "cc-1",
            "merchantId": "mid-1",
            "paymentType": "card",
            "consumerId": "buyer-1",
            "purpose": "test",
        }
        body = _sign_webhook(payload, private_pem)
        claims = client.verify_webhook(body)
        assert claims is not None
        assert claims["operationId"] == "op-1"
        assert claims["status"] == "APPROVED"

    def test_tampered_signature_rejected(self, client, rsa_keypair):
        private_pem, _ = rsa_keypair
        payload = {"webhookType": ACQUIRING_EVENT, "status": "APPROVED"}
        body = _sign_webhook(payload, private_pem)
        # Меняем последний байт — подпись инвалидна
        tampered = body[:-1] + b"X"
        assert client.verify_webhook(tampered) is None

    def test_random_bytes_rejected(self, client):
        assert client.verify_webhook(b"not-a-jwt") is None

    def test_empty_body_rejected(self, client):
        assert client.verify_webhook(b"") is None
        assert client.verify_webhook(b"   ") is None

    def test_signed_with_other_key_rejected(self, client):
        # Подписываем токен другим (своим) ключом — клиент должен отклонить
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        body = _sign_webhook({"webhookType": "x"}, other_pem)
        assert client.verify_webhook(body) is None


class TestParseAcquiringWebhook:
    def test_normalizes_card_payload(self):
        claims = {
            "webhookType": "acquiringInternetPayment",
            "customerCode": "cc",
            "merchantId": "mid",
            "operationId": "op-1",
            "amount": "1290.00",
            "paymentType": "card",
            "consumerId": "buyer-1",
            "purpose": "test",
            "status": "APPROVED",
            "paymentLinkId": "sub_42_pro_abc",
        }
        result = TochkaClient.parse_acquiring_webhook(claims)
        assert result["event"] == "acquiringInternetPayment"
        assert result["operation_id"] == "op-1"
        assert result["payment_link_id"] == "sub_42_pro_abc"
        assert result["status"] == "APPROVED"
        assert result["amount"] == 1290.0
        assert result["payment_type"] == "card"
        assert result["consumer_id"] == "buyer-1"

    def test_status_uppercased(self):
        result = TochkaClient.parse_acquiring_webhook(
            {"status": "approved"},
        )
        assert result["status"] == "APPROVED"

    def test_missing_fields_default_safely(self):
        result = TochkaClient.parse_acquiring_webhook({})
        assert result["operation_id"] == ""
        assert result["amount"] == 0.0
        assert result["status"] == ""
        assert result["event"] == ""

    def test_raw_passes_through(self):
        claims = {"some": "value"}
        result = TochkaClient.parse_acquiring_webhook(claims)
        assert result["raw"] == claims


class TestSuccessStatuses:
    """Контракт: APPROVED и AUTHORIZED считаются успехом."""

    def test_success_set_contents(self):
        assert SUCCESS_STATUSES == {"APPROVED", "AUTHORIZED"}
