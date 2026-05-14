"""Клиент для интернет-эквайринга ЮKassa.

Документация:
- Создание платежа: POST /v3/payments
- Подтверждение (двухстадийное): POST /v3/payments/{id}/capture
- Отмена платежа: POST /v3/payments/{id}/cancel
- Получение платежа: GET /v3/payments/{id}
- Возврат: POST /v3/refunds

Webhook'и:
- Контент JSON: {event: "payment.succeeded", object: {...payment...}}
- Подпись: ЮKassa по умолчанию подписи не шлёт. Безопасность
  обеспечивается IP-whitelist'ом отправителей + двойной проверкой
  через GET /v3/payments/{id}. Опционально можно включить HMAC-SHA256
  в личном кабинете (заголовок Y-Signature) — проверяется здесь же.

Аутентификация: HTTP Basic Auth(shopId, secretKey).
Идемпотентность: каждый POST требует уникальный Idempotence-Key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("financial-architect")

# IP-диапазоны, с которых ЮKassa отправляет webhook'и.
# Источник: https://yookassa.ru/developers/using-api/webhooks#ip
YOOKASSA_WEBHOOK_NETWORKS = [
    ipaddress.ip_network("185.71.76.0/27"),
    ipaddress.ip_network("185.71.77.0/27"),
    ipaddress.ip_network("77.75.153.0/25"),
    ipaddress.ip_network("77.75.154.128/25"),
    ipaddress.ip_network("77.75.156.11/32"),
    ipaddress.ip_network("77.75.156.35/32"),
    ipaddress.ip_network("2a02:5180::/32"),
]

# События webhook, которые мы обрабатываем
PAYMENT_SUCCEEDED_EVENT = "payment.succeeded"
PAYMENT_CANCELED_EVENT = "payment.canceled"
REFUND_SUCCEEDED_EVENT = "refund.succeeded"

# Статусы платежей
PAYMENT_STATUS_PENDING = "pending"
PAYMENT_STATUS_WAITING_FOR_CAPTURE = "waiting_for_capture"
PAYMENT_STATUS_SUCCEEDED = "succeeded"
PAYMENT_STATUS_CANCELED = "canceled"

SUCCESS_STATUSES = {PAYMENT_STATUS_SUCCEEDED}


@dataclass
class PaymentResult:
    """Результат создания платежа (initial)."""
    payment_id: str          # ID платежа в ЮKassa (UUID)
    confirmation_url: str    # ссылка для оплаты
    order_id: str            # наш составной идентификатор в metadata
    status: str = PAYMENT_STATUS_PENDING


@dataclass
class ChargeResult:
    """Результат автоплатежа по сохранённому payment_method."""
    payment_id: str
    status: str              # "succeeded" | "pending" | "canceled"
    payment_method_id: str = ""
    error_message: str = ""


@dataclass
class WebhookEvent:
    """Нормализованное webhook-событие."""
    event: str                # "payment.succeeded" | "payment.canceled" | ...
    payment_id: str           # ЮKassa id (UUID)
    status: str               # "succeeded" | "canceled" | ...
    amount: float
    order_id: str             # из metadata.order_id (наш sub_<uid>_<tariff>_<rand>)
    user_id: int              # из metadata.user_id
    tariff: str               # из metadata.tariff
    kind: str                 # "initial" | "recurring"
    payment_method_id: str    # для последующих автоплатежей
    raw: dict[str, Any]


class YooKassaError(Exception):
    """Ошибка при работе с API ЮKassa."""


class YooKassaClient:
    """Клиент API ЮKassa v3.

    Платёжный сценарий с автоплатежами:
    1. create_payment(save_payment_method=True) → confirmation_url + payment_id.
       Клиент идёт по ссылке, оплачивает. Webhook payment.succeeded приносит
       payment_method.id для последующих списаний.
    2. При продлении вызываем charge_recurring(payment_method_id, amount) →
       платёж проходит без участия пользователя. Webhook сообщает результат.
    3. payment_method действует пока пользователь не отзовёт карту.
    """

    BASE_URL = "https://api.yookassa.ru/v3"

    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        webhook_secret: str = "",
    ) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Опциональный HMAC-секрет для проверки заголовка Y-Signature.
        # Если пустой — проверяем только IP-whitelist.
        self.webhook_secret = webhook_secret

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self.shop_id}:{self.secret_key}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _headers(self, *, idempotence_key: Optional[str] = None) -> dict[str, str]:
        headers = {
            **self._auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotence_key is not None:
            headers["Idempotence-Key"] = idempotence_key
        return headers

    # ────────────────────────────────────────────────────────────
    # Создание платежей
    # ────────────────────────────────────────────────────────────

    async def create_payment(
        self,
        *,
        amount: float,
        description: str,
        user_id: int,
        tariff: str,
        return_url: str,
        customer_email: str,
        tax_system_code: int = 2,
        vat_code: int = 1,
        save_payment_method: bool = True,
        kind: str = "initial",
    ) -> PaymentResult:
        """Создаёт платёж с фискализацией чека.

        amount     — в рублях, преобразуется в строку "1290.00" для ЮKassa.
        tax_system_code — система налогообложения:
            1 ОСН, 2 УСН доход, 3 УСН доход-расход, 4 ЕНВД, 5 ЕСХН, 6 ПСН.
        vat_code   — ставка НДС: 1 без НДС, 2 0%, 3 10%, 4 20%.
        save_payment_method=True — сохраняет карту для автоплатежей;
            payment_method.id придёт в webhook payment.succeeded.
        """
        order_id = f"sub_{user_id}_{tariff}_{uuid.uuid4().hex[:8]}"
        idempotence_key = f"create-{order_id}-{uuid.uuid4().hex[:8]}"

        amount_str = f"{amount:.2f}"
        payload: dict[str, Any] = {
            "amount": {"value": amount_str, "currency": "RUB"},
            "capture": True,
            "description": description[:128],
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "save_payment_method": save_payment_method,
            "metadata": {
                "order_id": order_id,
                "user_id": str(user_id),
                "tariff": tariff,
                "kind": kind,
            },
            "receipt": self._build_receipt(
                customer_email=customer_email,
                description=description,
                amount_str=amount_str,
                tax_system_code=tax_system_code,
                vat_code=vat_code,
            ),
        }

        url = f"{self.base_url}/payments"
        logger.info(
            "YooKassa: creating payment order=%s amount=%s",
            order_id, amount_str,
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers=self._headers(idempotence_key=idempotence_key),
                json=payload,
            )

        if resp.status_code >= 400:
            logger.error(
                "YooKassa create_payment %s: %s",
                resp.status_code, resp.text[:500],
            )
            raise YooKassaError(
                f"YooKassa {resp.status_code}: {resp.text[:500]}"
            )

        body = resp.json()
        payment_id = body.get("id", "")
        status = body.get("status", PAYMENT_STATUS_PENDING)
        confirmation = body.get("confirmation") or {}
        confirmation_url = confirmation.get("confirmation_url", "")

        if not payment_id or not confirmation_url:
            raise YooKassaError(
                f"YooKassa: incomplete response: {resp.text[:500]}"
            )
        return PaymentResult(
            payment_id=payment_id,
            confirmation_url=confirmation_url,
            order_id=order_id,
            status=status,
        )

    async def charge_recurring(
        self,
        *,
        payment_method_id: str,
        amount: float,
        description: str,
        user_id: int,
        tariff: str,
        customer_email: str,
        tax_system_code: int = 2,
        vat_code: int = 1,
    ) -> ChargeResult:
        """Автоплатёж по сохранённому payment_method без участия пользователя."""
        order_id = f"sub_{user_id}_{tariff}_{uuid.uuid4().hex[:8]}"
        idempotence_key = f"charge-{order_id}-{uuid.uuid4().hex[:8]}"

        amount_str = f"{amount:.2f}"
        payload: dict[str, Any] = {
            "amount": {"value": amount_str, "currency": "RUB"},
            "capture": True,
            "payment_method_id": payment_method_id,
            "description": description[:128],
            "metadata": {
                "order_id": order_id,
                "user_id": str(user_id),
                "tariff": tariff,
                "kind": "recurring",
            },
            "receipt": self._build_receipt(
                customer_email=customer_email,
                description=description,
                amount_str=amount_str,
                tax_system_code=tax_system_code,
                vat_code=vat_code,
            ),
        }

        url = f"{self.base_url}/payments"
        logger.info(
            "YooKassa: charging recurring user=%s tariff=%s amount=%s",
            user_id, tariff, amount_str,
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers=self._headers(idempotence_key=idempotence_key),
                json=payload,
            )

        if resp.status_code >= 400:
            logger.error(
                "YooKassa charge_recurring %s: %s",
                resp.status_code, resp.text[:500],
            )
            return ChargeResult(
                payment_id="",
                status="canceled",
                error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

        body = resp.json()
        return ChargeResult(
            payment_id=body.get("id", ""),
            status=body.get("status", PAYMENT_STATUS_PENDING),
            payment_method_id=(body.get("payment_method") or {}).get("id", ""),
        )

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        """Возвращает текущее состояние платежа. Используется поллером
        и для верификации webhook'а (double-check)."""
        url = f"{self.base_url}/payments/{payment_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise YooKassaError(
                f"YooKassa get_payment {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    async def cancel_payment(self, payment_id: str) -> bool:
        """Отменяет платёж в статусе waiting_for_capture. Платежи уже в
        succeeded — отменяются только через refund."""
        url = f"{self.base_url}/payments/{payment_id}/cancel"
        idempotence_key = f"cancel-{payment_id}-{uuid.uuid4().hex[:8]}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers=self._headers(idempotence_key=idempotence_key),
                json={},
            )
        if resp.status_code >= 400:
            logger.warning(
                "YooKassa cancel_payment %s: %s",
                resp.status_code, resp.text[:200],
            )
            return False
        return True

    # ────────────────────────────────────────────────────────────
    # Чеки 54-ФЗ
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_receipt(
        *,
        customer_email: str,
        description: str,
        amount_str: str,
        tax_system_code: int,
        vat_code: int,
    ) -> dict[str, Any]:
        """Чек 54-ФЗ для ФНС. ЮKassa передаст его на онлайн-кассу,
        подключённую к магазину в личном кабинете."""
        return {
            "customer": {"email": customer_email},
            "tax_system_code": tax_system_code,
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": {"value": amount_str, "currency": "RUB"},
                    "vat_code": vat_code,
                    "payment_subject": "service",
                    "payment_mode": "full_prepayment",
                }
            ],
        }

    # ────────────────────────────────────────────────────────────
    # Webhook: валидация и парсинг
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def is_yookassa_ip(remote_addr: str) -> bool:
        """Проверяет, что webhook пришёл с одного из официальных IP ЮKassa.

        Помогает блокировать спуфинг — но при работе через прокси/nginx
        учтите, что remote_addr может быть IP прокси. В таком случае
        проверяйте через X-Forwarded-For, прежде передавая сюда.
        """
        if not remote_addr:
            return False
        try:
            ip = ipaddress.ip_address(remote_addr)
        except ValueError:
            return False
        return any(ip in net for net in YOOKASSA_WEBHOOK_NETWORKS)

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Проверяет HMAC-SHA256 подпись Y-Signature (если в кабинете
        включён HMAC). Если webhook_secret пустой — считаем что HMAC
        не настроен и пропускаем проверку (полагаемся на IP)."""
        if not self.webhook_secret:
            return True
        if not signature:
            return False
        expected = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature.strip())

    @staticmethod
    def parse_webhook(raw_body: bytes) -> Optional[WebhookEvent]:
        """Декодирует JSON и нормализует payload в WebhookEvent.

        Возвращает None если тело невалидно или это не payment-событие.
        """
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("YooKassa webhook: bad body: %s", exc)
            return None
        if not isinstance(data, dict):
            return None

        event = data.get("event", "")
        obj = data.get("object") or {}
        if not isinstance(obj, dict):
            return None

        amount_block = obj.get("amount") or {}
        try:
            amount = float(amount_block.get("value") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        metadata = obj.get("metadata") or {}
        try:
            user_id = int(metadata.get("user_id") or 0)
        except (TypeError, ValueError):
            user_id = 0

        payment_method = obj.get("payment_method") or {}

        return WebhookEvent(
            event=event,
            payment_id=obj.get("id", ""),
            status=obj.get("status", ""),
            amount=amount,
            order_id=metadata.get("order_id", ""),
            user_id=user_id,
            tariff=metadata.get("tariff", ""),
            kind=metadata.get("kind", "initial"),
            payment_method_id=payment_method.get("id", ""),
            raw=data,
        )


def parse_order_id(order_id: str) -> Optional[tuple[int, str]]:
    """Разбирает order_id формата sub_{user_id}_{tariff}_{rand}.
    Возвращает (user_id, tariff) или None."""
    if not order_id:
        return None
    parts = order_id.split("_")
    if len(parts) < 4 or parts[0] != "sub":
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    return user_id, parts[2]
