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
