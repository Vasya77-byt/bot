"""Тесты webhook_server: aiohttp-эндпоинты /health и /tochka/webhook
под новый JWT/RS256-формат уведомлений Точки.
"""
import json
from typing import Optional

import jwt
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from tochka_client import TochkaClient
from user_store import UserProfile
from webhook_server import build_app


# ──────────────────────────────────────────────────────────────────────
# Тестовая RSA-пара ключей для подписи webhook'ов
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    pem_private = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    return pem_private, public_jwk


@pytest.fixture
def tochka(rsa_keypair):
    _, public_jwk = rsa_keypair
    return TochkaClient(
        jwt_token="x", customer_code="cc",
        public_key_jwk=public_jwk,
    )


class FakeSubscription:
    def __init__(self, paid_profile: Optional[UserProfile] = None):
        self.paid_profile = paid_profile
        self.paid_calls: list[dict] = []
        self.failed_calls: list[dict] = []

    def handle_webhook_paid(
        self, *, operation_id, order_id, card_token="", amount,
    ):
        self.paid_calls.append({
            "operation_id": operation_id, "order_id": order_id,
            "card_token": card_token, "amount": amount,
        })
        return self.paid_profile

    def handle_webhook_failed(self, *, operation_id, error=""):
        self.failed_calls.append({
            "operation_id": operation_id, "error": error,
        })

    def handle_yookassa_webhook_paid(
        self,
        *,
        payment_id, order_id, user_id, tariff, amount,
        payment_method_id="", kind="initial",
    ):
        self.paid_calls.append({
            "payment_id": payment_id, "order_id": order_id,
            "user_id": user_id, "tariff": tariff, "amount": amount,
            "payment_method_id": payment_method_id, "kind": kind,
        })
        return self.paid_profile


def make_notify():
    calls: list[tuple[int, str]] = []

    async def notify(user_id: int, text: str) -> None:
        calls.append((user_id, text))

    return notify, calls


@pytest.fixture
def subscription():
    return FakeSubscription()


@pytest_asyncio.fixture
async def client_with_app(tochka, subscription):
    notify_fn, _ = make_notify()
    app = build_app(tochka, subscription, notify=notify_fn)
    async with TestClient(TestServer(app)) as c:
        yield c


def _sign_jwt(claims: dict, private_pem: bytes) -> bytes:
    """Подписывает claims приватным ключом — имитирует Точку."""
    token = jwt.encode(claims, private_pem, algorithm="RS256")
    return token.encode("utf-8") if isinstance(token, str) else token


def _acquiring_payload(
    *, status: str = "APPROVED",
    operation_id: str = "sub-op-1",
    payment_link_id: str = "sub_42_pro_xyz",
    amount: str = "1290.00",
    payment_type: str = "card",
    extra: Optional[dict] = None,
) -> dict:
    payload = {
        "webhookType": "acquiringInternetPayment",
        "customerCode": "cc-1",
        "merchantId": "mid-1",
        "operationId": operation_id,
        "amount": amount,
        "paymentType": payment_type,
        "consumerId": "buyer-1",
        "purpose": "Подписка PRO",
        "status": status,
        "paymentLinkId": payment_link_id,
    }
    if extra:
        payload.update(extra)
    return payload


# ──────────────────────────────────────────────────────────────────────
# /health
# ──────────────────────────────────────────────────────────────────────


class TestHealth:
    @pytest.mark.asyncio
    async def test_returns_ok(self, client_with_app):
        resp = await client_with_app.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"status": "ok"}


# ──────────────────────────────────────────────────────────────────────
# Подпись JWT
# ──────────────────────────────────────────────────────────────────────


class TestWebhookSignature:
    @pytest.mark.asyncio
    async def test_valid_signature_accepted(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        subscription.paid_profile = UserProfile(
            user_id=42, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        notify, calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)

        body = _sign_jwt(_acquiring_payload(), private_pem)

        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        assert len(subscription.paid_calls) == 1

    @pytest.mark.asyncio
    async def test_tampered_signature_returns_403(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)

        body = _sign_jwt(_acquiring_payload(), private_pem)
        tampered = body[:-3] + b"XYZ"

        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=tampered)
        assert resp.status == 403
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_random_body_returns_403(
        self, tochka, subscription,
    ):
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=b"not-a-jwt")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_signed_with_wrong_key_returns_403(
        self, tochka, subscription,
    ):
        # Подписываем чужим ключом — Точка-публичный должен отклонить
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        body = _sign_jwt(_acquiring_payload(), other_pem)
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 403


# ──────────────────────────────────────────────────────────────────────
# Игнорирование чужих событий
# ──────────────────────────────────────────────────────────────────────


class TestEventFiltering:
    @pytest.mark.asyncio
    async def test_incoming_payment_ignored(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        body = _sign_jwt(
            {"webhookType": "incomingPayment", "amount": "100"},
            private_pem,
        )
        notify, calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        # subscription methods не дёргаются на чужих событиях
        assert subscription.paid_calls == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_outgoing_payment_ignored(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        body = _sign_jwt(
            {"webhookType": "outgoingPayment"}, private_pem,
        )
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_incoming_sbp_ignored(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        body = _sign_jwt(
            {"webhookType": "incomingSbpPayment"}, private_pem,
        )
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        assert subscription.paid_calls == []


# ──────────────────────────────────────────────────────────────────────
# acquiringInternetPayment — успешные статусы
# ──────────────────────────────────────────────────────────────────────


class TestAcquiringApproved:
    @pytest.mark.asyncio
    async def test_approved_calls_handle_webhook_paid(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        subscription.paid_profile = UserProfile(
            user_id=42, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        notify, notify_calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)

        body = _sign_jwt(_acquiring_payload(status="APPROVED"), private_pem)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)

        assert resp.status == 200
        assert len(subscription.paid_calls) == 1
        call = subscription.paid_calls[0]
        assert call["operation_id"] == "sub-op-1"
        assert call["order_id"] == "sub_42_pro_xyz"
        assert call["amount"] == 1290.0

        # Уведомление пользователю
        assert len(notify_calls) == 1
        uid, text = notify_calls[0]
        assert uid == 42
        assert "Оплата прошла" in text
        assert "PRO" in text

    @pytest.mark.asyncio
    async def test_authorized_also_treated_as_success(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        subscription.paid_profile = UserProfile(
            user_id=1, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        body = _sign_jwt(_acquiring_payload(status="AUTHORIZED"), private_pem)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        assert len(subscription.paid_calls) == 1

    @pytest.mark.asyncio
    async def test_unknown_status_no_action(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        body = _sign_jwt(_acquiring_payload(status="PENDING"), private_pem)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        # PENDING/CREATED — не успех, обрабатывает поллер позже
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_no_profile_does_not_notify(
        self, tochka, subscription, rsa_keypair,
    ):
        subscription.paid_profile = None
        private_pem, _ = rsa_keypair
        notify, calls = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        body = _sign_jwt(_acquiring_payload(), private_pem)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        assert calls == []

    @pytest.mark.asyncio
    async def test_no_notify_callback_does_not_crash(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        subscription.paid_profile = UserProfile(
            user_id=1, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        app = build_app(tochka, subscription, notify=None)
        body = _sign_jwt(_acquiring_payload(), private_pem)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_notify_exception_swallowed(
        self, tochka, subscription, rsa_keypair,
    ):
        private_pem, _ = rsa_keypair
        subscription.paid_profile = UserProfile(
            user_id=1, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )

        async def angry_notify(uid, text):
            raise RuntimeError("telegram is down")

        app = build_app(tochka, subscription, notify=angry_notify)
        body = _sign_jwt(_acquiring_payload(), private_pem)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        # Webhook всё равно отвечает 200 — Точка не ретраит
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_sbp_payment_type_works(
        self, tochka, subscription, rsa_keypair,
    ):
        # СБП-оплата — тот же event acquiringInternetPayment, paymentType=sbp
        private_pem, _ = rsa_keypair
        subscription.paid_profile = UserProfile(
            user_id=1, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        notify, _ = make_notify()
        app = build_app(tochka, subscription, notify=notify)
        body = _sign_jwt(
            _acquiring_payload(payment_type="sbp"), private_pem,
        )
        async with TestClient(TestServer(app)) as c:
            resp = await c.post("/tochka/webhook", data=body)
        assert resp.status == 200
        assert len(subscription.paid_calls) == 1


# ──────────────────────────────────────────────────────────────────────
# /report/{token} — HTML-отчёт (Telegram WebApp)
# ──────────────────────────────────────────────────────────────────────


class FakeCompanyService:
    async def fetch(self, inn: str):
        from schemas import CompanyData
        return CompanyData(inn=inn, name="ТЕСТОВАЯ ООО",
                           ogrn="1027700000001", status="Действующее",
                           address="Москва")


class FakeSecurityService:
    async def check(self, *, inn, name=None, okved=None, ogrn=None):
        from security_check import SecurityResult
        return SecurityResult(enforcement_count=2)


class FakeZchb:
    enabled = True

    async def get_card(self, inn: str):
        from zchb_client import CardSummary
        return CardSummary(name_short="ТЕСТОВАЯ ООО",
                           ogrn="1027700000001", status="Действующее")


class TestReportEndpoint:
    @pytest.mark.asyncio
    async def test_report_returns_html_for_valid_token(
        self, tochka, subscription, tmp_path,
    ):
        from report_tokens import ReportTokenStore

        store = ReportTokenStore(filepath=str(tmp_path / "tokens.json"))
        token = store.create(user_id=1, inn="7707083893")
        app = build_app(
            tochka, subscription,
            report_tokens=store,
            company_service=FakeCompanyService(),
            security_service=FakeSecurityService(),
            zchb=FakeZchb(),
        )
        async with TestClient(TestServer(app)) as c:
            resp = await c.get(f"/report/{token}")
            assert resp.status == 200
            assert resp.content_type == "text/html"
            text = await resp.text()
        assert "ТЕСТОВАЯ ООО" in text
        assert "7707083893" in text

    @pytest.mark.asyncio
    async def test_report_returns_404_for_invalid_token(
        self, tochka, subscription, tmp_path,
    ):
        from report_tokens import ReportTokenStore

        store = ReportTokenStore(filepath=str(tmp_path / "tokens.json"))
        app = build_app(
            tochka, subscription,
            report_tokens=store,
            company_service=FakeCompanyService(),
            security_service=FakeSecurityService(),
            zchb=FakeZchb(),
        )
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/report/" + "f" * 32)
            assert resp.status == 404
            text = await resp.text()
        assert "ссылка" in text.lower() or "просрочен" in text.lower()

    @pytest.mark.asyncio
    async def test_report_returns_503_when_not_configured(
        self, tochka, subscription,
    ):
        # Без report_tokens — отвечаем 503
        app = build_app(tochka, subscription)
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/report/anytoken")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_report_handles_zchb_failure_gracefully(
        self, tochka, subscription, tmp_path,
    ):
        from report_tokens import ReportTokenStore

        class FailingZchb:
            enabled = True

            async def get_card(self, inn):
                raise RuntimeError("zchb down")

        store = ReportTokenStore(filepath=str(tmp_path / "tokens.json"))
        token = store.create(user_id=1, inn="7707083893")
        app = build_app(
            tochka, subscription,
            report_tokens=store,
            company_service=FakeCompanyService(),
            security_service=FakeSecurityService(),
            zchb=FailingZchb(),
        )
        async with TestClient(TestServer(app)) as c:
            resp = await c.get(f"/report/{token}")
            # Даже без zchb отчёт должен отрендериться — есть данные company
            assert resp.status == 200
            text = await resp.text()
        assert "ТЕСТОВАЯ ООО" in text


# ──────────────────────────────────────────────────────────────────────
# YooKassa webhook
# ──────────────────────────────────────────────────────────────────────


import hashlib as _hashlib
import hmac as _hmac

from yookassa_client import YooKassaClient


def _yk_body(
    *,
    event: str = "payment.succeeded",
    status: str = "succeeded",
    payment_id: str = "yk-pay-1",
    amount: str = "1290.00",
    user_id: str = "42",
    tariff: str = "pro",
    kind: str = "initial",
    payment_method_id: str = "pm-token-abc",
    order_id: str = "sub_42_pro_xxx",
) -> bytes:
    return json.dumps({
        "event": event,
        "object": {
            "id": payment_id,
            "status": status,
            "amount": {"value": amount, "currency": "RUB"},
            "payment_method": {"id": payment_method_id, "saved": True},
            "metadata": {
                "order_id": order_id,
                "user_id": user_id,
                "tariff": tariff,
                "kind": kind,
            },
        },
    }).encode("utf-8")


YK_GOOD_IP = "185.71.76.1"  # внутри 185.71.76.0/27


class TestYooKassaWebhook:
    @pytest.mark.asyncio
    async def test_successful_payment_activates(self, tochka, subscription):
        subscription.paid_profile = UserProfile(
            user_id=42, tariff="pro",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        yookassa = YooKassaClient("shop", "key")  # без HMAC
        notify, calls = make_notify()
        app = build_app(tochka, subscription, yookassa=yookassa, notify=notify)

        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=_yk_body(),
                headers={"X-Forwarded-For": YK_GOOD_IP},
            )

        assert resp.status == 200
        assert len(subscription.paid_calls) == 1
        call = subscription.paid_calls[0]
        assert call["payment_id"] == "yk-pay-1"
        assert call["user_id"] == 42
        assert call["tariff"] == "pro"
        assert call["payment_method_id"] == "pm-token-abc"
        assert len(calls) == 1
        assert "✅" in calls[0][1]

    @pytest.mark.asyncio
    async def test_non_whitelisted_ip_returns_403(self, tochka, subscription):
        yookassa = YooKassaClient("shop", "key")
        app = build_app(tochka, subscription, yookassa=yookassa)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=_yk_body(),
                headers={"X-Forwarded-For": "8.8.8.8"},
            )
        assert resp.status == 403
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_valid_hmac_signature_accepted(self, tochka, subscription):
        secret = "my_hmac_secret"
        yookassa = YooKassaClient("shop", "key", webhook_secret=secret)
        app = build_app(tochka, subscription, yookassa=yookassa)
        body = _yk_body()
        signature = _hmac.new(
            secret.encode("utf-8"), body, _hashlib.sha256,
        ).hexdigest()

        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=body,
                headers={
                    "X-Forwarded-For": YK_GOOD_IP,
                    "Y-Signature": signature,
                },
            )
        assert resp.status == 200
        assert len(subscription.paid_calls) == 1

    @pytest.mark.asyncio
    async def test_invalid_hmac_signature_rejected(self, tochka, subscription):
        yookassa = YooKassaClient("shop", "key", webhook_secret="secret")
        app = build_app(tochka, subscription, yookassa=yookassa)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=_yk_body(),
                headers={
                    "X-Forwarded-For": YK_GOOD_IP,
                    "Y-Signature": "wrong_signature",
                },
            )
        assert resp.status == 403
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_bad_json_returns_400(self, tochka, subscription):
        yookassa = YooKassaClient("shop", "key")
        app = build_app(tochka, subscription, yookassa=yookassa)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=b"not json",
                headers={"X-Forwarded-For": YK_GOOD_IP},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_payment_canceled_event_no_activation(self, tochka, subscription):
        yookassa = YooKassaClient("shop", "key")
        app = build_app(tochka, subscription, yookassa=yookassa)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=_yk_body(event="payment.canceled", status="canceled"),
                headers={"X-Forwarded-For": YK_GOOD_IP},
            )
        # ЮKassa должен получить 200, чтобы не ретраить
        assert resp.status == 200
        # Но активация не должна происходить
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_refund_event_no_activation(self, tochka, subscription):
        yookassa = YooKassaClient("shop", "key")
        app = build_app(tochka, subscription, yookassa=yookassa)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=_yk_body(event="refund.succeeded"),
                headers={"X-Forwarded-For": YK_GOOD_IP},
            )
        assert resp.status == 200
        assert subscription.paid_calls == []

    @pytest.mark.asyncio
    async def test_yookassa_not_configured_returns_503(
        self, tochka, subscription,
    ):
        # yookassa=None — роут должен вернуть 503
        app = build_app(tochka, subscription, yookassa=None)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=_yk_body(),
                headers={"X-Forwarded-For": YK_GOOD_IP},
            )
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_recurring_payment_passes_kind(self, tochka, subscription):
        subscription.paid_profile = UserProfile(
            user_id=7, tariff="start",
            tariff_expires_at="2099-01-01T00:00:00+00:00",
        )
        yookassa = YooKassaClient("shop", "key")
        notify, _ = make_notify()
        app = build_app(tochka, subscription, yookassa=yookassa, notify=notify)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/yookassa/webhook",
                data=_yk_body(
                    user_id="7", tariff="start",
                    kind="recurring", payment_id="yk-recur-1",
                ),
                headers={"X-Forwarded-For": YK_GOOD_IP},
            )
        assert resp.status == 200
        assert subscription.paid_calls[0]["kind"] == "recurring"

