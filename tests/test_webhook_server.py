"""Тесты webhook_server: aiohttp-эндпоинты /health и /tochka/webhook."""
import hashlib
import hmac
import json
from typing import Any, List, Optional, Tuple

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from user_store import UserProfile
from webhook_server import build_app


class FakeTochka:
    """Лёгкий мок TochkaClient, нужный webhook'у:
    - verify_webhook (instance method)
    - parse_webhook (static — вызывается через TochkaClient)
    """

    def __init__(self, signature_valid: bool = True):
        self.signature_valid = signature_valid
        self.verify_calls: list[tuple[bytes, str]] = []

    def verify_webhook(self, raw: bytes, signature: str) -> bool:
        self.verify_calls.append((raw, signature))
        return self.signature_valid


class FakeSubscription:
    def __init__(self, paid_profile: Optional[UserProfile] = None):
        self.paid_profile = paid_profile
        self.paid_calls: list[dict] = []
        self.failed_calls: list[dict] = []

    def handle_webhook_paid(self, *, operation_id, order_id, card_token, amount):
        self.paid_calls.append({
            "operation_id": operation_id, "order_id": order_id,
            "card_token": card_token, "amount": amount,
        })
        return self.paid_profile

    def handle_webhook_failed(self, *, operation_id, error=""):
        self.failed_calls.append({"operation_id": operation_id, "error": error})


def make_notify():
    """Возвращает (notify_fn, calls_list)."""
    calls: list[tuple[int, str]] = []

    async def notify(user_id: int, text: str) -> None:
        calls.append((user_id, text))

    return notify, calls


@pytest.fixture
def tochka():
    return FakeTochka(signature_valid=True)


@pytest.fixture
def subscription():
    return FakeSubscription()


@pytest_asyncio.fixture
async def client(tochka, subscription):
    """aiohttp TestClient с приложением и no-op notify."""
    notify_fn, _ = make_notify()
    app = build_app(tochka, subscription, notify=notify_fn)
    async with TestClient(TestServer(app)) as c:
        yield c


def _post_payload(parsed_status: str = "approved", **fields) -> dict:
    return {"Data": {
        "operationId": fields.get("operation_id", "op-1"),
        "orderId": fields.get("order_id", "sub_42_pro_xyz"),
        "status": parsed_status,
        "amount": fields.get("amount", 1290.0),
        "cardToken": fields.get("card_token", "tok-1"),
        **fields.get("extra", {}),
    }}


class TestHealth:
    @pytest.mark.asyncio
    async def test_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"status": "ok"}


class TestWebhookSignature:
    @pytest.mark.asyncio
    async def test_bad_signature_returns_403(self, tochka, subscription):
        tochka.signature_valid = False
        notify, calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload(),
                                headers={"X-Signature": "bad"})
        assert resp.status == 403
        # subscription methods не должны вызываться
        assert subscription.paid_calls == []
        assert subscription.failed_calls == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_signature_header_passed_to_verify(self, client, tochka):
        await client.post("/tochka/webhook", json=_post_payload(),
                          headers={"X-Signature": "sig-from-header"})
        assert len(tochka.verify_calls) == 1
        _, sig = tochka.verify_calls[0]
        assert sig == "sig-from-header"

    @pytest.mark.asyncio
    async def test_signature_fallback_to_signature_header(self, client, tochka):
        await client.post("/tochka/webhook", json=_post_payload(),
                          headers={"Signature": "fallback-sig"})
        _, sig = tochka.verify_calls[0]
        assert sig == "fallback-sig"

    @pytest.mark.asyncio
    async def test_no_signature_header_passes_empty_string(self, client, tochka):
        await client.post("/tochka/webhook", json=_post_payload())
        _, sig = tochka.verify_calls[0]
        assert sig == ""


class TestWebhookBadJson:
    @pytest.mark.asyncio
    async def test_bad_json_returns_400(self, client, subscription):
        resp = await client.post(
            "/tochka/webhook",
            data=b"not a json {{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        # ничего не записано в стор
        assert subscription.paid_calls == []
        assert subscription.failed_calls == []


class TestWebhookPaidFlow:
    @pytest.mark.asyncio
    async def test_paid_status_calls_handle_webhook_paid(self, tochka, subscription):
        future_iso = "2099-01-01T00:00:00+00:00"
        subscription.paid_profile = UserProfile(
            user_id=42, tariff="pro",
            tariff_expires_at=future_iso,
        )
        notify, notify_calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload("approved"))

        assert resp.status == 200
        assert len(subscription.paid_calls) == 1
        call = subscription.paid_calls[0]
        assert call["operation_id"] == "op-1"
        assert call["order_id"] == "sub_42_pro_xyz"
        assert call["card_token"] == "tok-1"
        assert call["amount"] == 1290.0

        # Уведомление пользователю
        assert len(notify_calls) == 1
        uid, text = notify_calls[0]
        assert uid == 42
        assert "Оплата прошла" in text
        assert "PRO" in text
        assert "2099-01-01" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["paid", "approved", "confirmed", "completed"])
    async def test_all_success_statuses_trigger_paid(self, tochka, subscription, status):
        subscription.paid_profile = UserProfile(
            user_id=1, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload(status))
        assert resp.status == 200
        assert len(subscription.paid_calls) == 1

    @pytest.mark.asyncio
    async def test_paid_without_profile_does_not_notify(self, tochka, subscription):
        subscription.paid_profile = None  # запись не нашлась и не парсится
        notify, calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload("approved"))
        assert resp.status == 200
        assert calls == []

    @pytest.mark.asyncio
    async def test_paid_without_notify_does_not_crash(self, tochka, subscription):
        subscription.paid_profile = UserProfile(
            user_id=1, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        app = build_app(tochka, subscription, notify=None)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload("approved"))
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_paid_notify_exception_swallowed(self, tochka, subscription):
        subscription.paid_profile = UserProfile(
            user_id=1, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )

        async def angry_notify(uid, text):
            raise RuntimeError("telegram is down")

        app = build_app(tochka, subscription, notify=angry_notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload("approved"))
        # Webhook всё равно отвечает 200 — Точка не ретраит
        assert resp.status == 200


class TestWebhookFailedFlow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "declined", "cancelled", "rejected"])
    async def test_all_failure_statuses_trigger_failed(
        self, tochka, subscription, status
    ):
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload(status))
        assert resp.status == 200
        assert len(subscription.failed_calls) == 1
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_failed_extracts_error_message_from_raw(self, tochka, subscription):
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        payload = _post_payload("declined", extra={"errorMessage": "card blocked"})
        async with TestClient(TestServer(app)) as c:
            await c.post("/tochka/webhook", json=payload)
        assert subscription.failed_calls[0]["error"] == "card blocked"

    @pytest.mark.asyncio
    async def test_failed_with_parseable_order_notifies_user(self, tochka, subscription):
        notify, calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            await c.post("/tochka/webhook", json=_post_payload(
                "declined", order_id="sub_77_pro_abc",
            ))
        assert len(calls) == 1
        uid, text = calls[0]
        assert uid == 77
        assert "не прошла" in text
        assert "pro" in text

    @pytest.mark.asyncio
    async def test_failed_with_unparseable_order_no_notify(self, tochka, subscription):
        notify, calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            await c.post("/tochka/webhook", json=_post_payload(
                "declined", order_id="garbage-order",
            ))
        assert calls == []
        # subscription.handle_webhook_failed всё равно вызван
        assert len(subscription.failed_calls) == 1

    @pytest.mark.asyncio
    async def test_failed_without_notify_does_not_crash(self, tochka, subscription):
        app = build_app(tochka, subscription, notify=None)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload(
                "declined", order_id="sub_1_pro_x",
            ))
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_failed_notify_exception_swallowed(self, tochka, subscription):
        async def angry(uid, text):
            raise RuntimeError("nope")
        app = build_app(tochka, subscription, notify=angry)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", json=_post_payload(
                "declined", order_id="sub_1_pro_x",
            ))
        assert resp.status == 200


class TestWebhookUnknownStatus:
    @pytest.mark.asyncio
    async def test_unknown_status_returns_200_without_state_change(
        self, client, subscription
    ):
        # status="pending" не входит ни в paid-, ни в failed-список
        resp = await client.post("/tochka/webhook", json=_post_payload("pending"))
        assert resp.status == 200
        assert subscription.paid_calls == []
        assert subscription.failed_calls == []

    @pytest.mark.asyncio
    async def test_empty_payload_returns_200_no_action(self, client, subscription):
        resp = await client.post("/tochka/webhook", json={})
        assert resp.status == 200
        assert subscription.paid_calls == []
        assert subscription.failed_calls == []


class TestSignatureIntegrationWithRealVerify:
    """Интеграционная проверка с настоящим TochkaClient.verify_webhook."""

    @pytest.mark.asyncio
    async def test_valid_hmac_passes(self, subscription):
        from tochka_client import TochkaClient
        tochka = TochkaClient(jwt_token="x", customer_code="x",
                              webhook_secret="topsecret")
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)

        body = json.dumps(_post_payload("approved")).encode("utf-8")
        signature = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/tochka/webhook", data=body,
                headers={"X-Signature": signature,
                         "Content-Type": "application/json"},
            )
        # subscription.handle_webhook_paid вызывается → 200
        # При valid signature и approved статусе доходим до handle_webhook_paid
        assert resp.status == 200
        assert len(subscription.paid_calls) == 1

    @pytest.mark.asyncio
    async def test_invalid_hmac_rejected(self, subscription):
        from tochka_client import TochkaClient
        tochka = TochkaClient(jwt_token="x", customer_code="x",
                              webhook_secret="topsecret")
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)

        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/tochka/webhook", json=_post_payload("approved"),
                headers={"X-Signature": "deadbeef"},
            )
        assert resp.status == 403
        assert subscription.paid_calls == []
