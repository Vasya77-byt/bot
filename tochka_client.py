"""Клиент для интернет-эквайринга Точка Банка.

Документация: https://enter.tochka.com/doc/v2/redoc/ (Open API)

TODO: VERIFY — перед продом сверьте:
1. Базовый URL (enter.tochka.com/uapi/ или sandbox)
2. Точные пути acquiring/v1.0/payments_with_receipt и payments_recurring
3. Структуру запроса и схему webhook-уведомлений в вашем личном кабинете

Для получения JWT-токена:
- Зайти в https://enter.tochka.com → Настройки → Открытый API
- Запросить разрешения на acquiring (чтение+запись)
- Скопировать JWT (вида eyJhbGci...)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("financial-architect")


@dataclass
class PaymentResult:
    """Результат создания платежа."""
    operation_id: str
    payment_link: str
    order_id: str = ""
    status: str = "created"


@dataclass
class RecurringResult:
    """Результат рекуррентного списания."""
    operation_id: str
    status: str  # "approved" | "declined" | "pending"
    order_id: str = ""
    error_message: str = ""


class TochkaError(Exception):
    """Ошибка при работе с API Точки."""


class TochkaClient:
    """Клиент Open API Точки для приёма платежей и рекуррентных списаний."""

    def __init__(
        self,
        jwt_token: str,
        customer_code: str,
        merchant_id: str = "",
        base_url: str = "https://enter.tochka.com/uapi",
        webhook_secret: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.jwt_token = jwt_token
        self.customer_code = customer_code
        self.merchant_id = merchant_id
        self.base_url = base_url.rstrip("/")
        self.webhook_secret = webhook_secret
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def create_payment(
        self,
        *,
        amount: float,
        purpose: str,
        user_id: int,
        tariff: str,
        redirect_url: str,
        fail_redirect_url: str,
        email: str = "",
        save_card: bool = True,
    ) -> PaymentResult:
        """Создаёт платёжную ссылку. При save_card=True после оплаты
        Точка вернёт cardToken в webhook'е для последующих рекуррентов."""

        # В purpose прячем метаданные, т.к. Точка их вернёт в webhook
        # но правильнее хранить маппинг у себя (payments_store.py)
        order_id = f"sub_{user_id}_{tariff}_{uuid.uuid4().hex[:8]}"

        payload = {
            "Data": {
                "customerCode": self.customer_code,
                "amount": f"{amount:.2f}",
                "purpose": purpose,
                "redirectUrl": redirect_url,
                "failRedirectUrl": fail_redirect_url,
                "paymentMode": ["card", "sbp"],
                "saveCard": save_card,
                "merchantId": self.merchant_id or None,
                "preAuthorization": False,
                "ttl": 60,  # минут на оплату
                "orderId": order_id,
                "Client": {
                    "email": email or "noreply@example.com",
                },
                # Чек по 54-ФЗ. TODO: VERIFY — формат items в вашей оферте Точки
                "Items": [
                    {
                        "name": f"Подписка на тариф {tariff}",
                        "amount": f"{amount:.2f}",
                        "quantity": 1.0,
                        "vatType": "none",  # УСН без НДС
                        "paymentMethod": "full_prepayment",
                        "paymentObject": "service",
                        "measure": "piece",
                    }
                ],
            }
        }
        payload["Data"] = {k: v for k, v in payload["Data"].items() if v is not None}

        url = f"{self.base_url}/acquiring/v1.0/payments_with_receipt"
        logger.info("Tochka: creating payment %s amount=%s", order_id, amount)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)

        if resp.status_code >= 400:
            logger.error("Tochka create_payment failed: %s %s", resp.status_code, resp.text)
            raise TochkaError(f"Tochka {resp.status_code}: {resp.text[:500]}")

        data = resp.json().get("Data", {})
        operation_id = data.get("operationId") or data.get("id") or order_id
        link = data.get("paymentLink") or data.get("url") or ""
        if not link:
            raise TochkaError(f"Tochka: no paymentLink in response: {resp.text[:500]}")

        return PaymentResult(
            operation_id=operation_id,
            payment_link=link,
            order_id=order_id,
            status="created",
        )

    async def charge_recurring(
        self,
        *,
        amount: float,
        purpose: str,
        card_token: str,
        user_id: int,
        tariff: str,
        email: str = "",
    ) -> RecurringResult:
        """Рекуррентное списание с сохранённой карты (без участия пользователя)."""
        order_id = f"renew_{user_id}_{tariff}_{uuid.uuid4().hex[:8]}"

        payload = {
            "Data": {
                "customerCode": self.customer_code,
                "amount": f"{amount:.2f}",
                "purpose": purpose,
                "cardToken": card_token,
                "merchantId": self.merchant_id or None,
                "orderId": order_id,
                "Client": {"email": email or "noreply@example.com"},
                "Items": [
                    {
                        "name": f"Продление подписки {tariff}",
                        "amount": f"{amount:.2f}",
                        "quantity": 1.0,
                        "vatType": "none",
                        "paymentMethod": "full_prepayment",
                        "paymentObject": "service",
                        "measure": "piece",
                    }
                ],
            }
        }
        payload["Data"] = {k: v for k, v in payload["Data"].items() if v is not None}

        url = f"{self.base_url}/acquiring/v1.0/payments_recurring"
        logger.info("Tochka: recurring charge %s amount=%s", order_id, amount)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)

        if resp.status_code >= 400:
            logger.error("Tochka recurring failed: %s %s", resp.status_code, resp.text)
            return RecurringResult(
                operation_id=order_id,
                status="declined",
                order_id=order_id,
                error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

        data = resp.json().get("Data", {})
        op_id = data.get("operationId") or order_id
        status = (data.get("status") or "pending").lower()
        return RecurringResult(operation_id=op_id, status=status, order_id=order_id)

    async def get_operation_status(self, operation_id: str) -> dict[str, Any]:
        """Запрос статуса операции — на случай пропущенного webhook'а."""
        url = f"{self.base_url}/acquiring/v1.0/payments/{operation_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise TochkaError(f"Tochka status {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("Data", {})

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        """Проверяет HMAC-подпись webhook'а.

        TODO: VERIFY — формат подписи Точки. Обычно это HMAC-SHA256
        от тела запроса с webhook_secret в качестве ключа, передаётся
        в заголовке 'X-Signature' или 'Signature' как hex.
        """
        if not self.webhook_secret:
            logger.warning("Tochka webhook_secret not set — signature check skipped")
            return True
        if not signature:
            return False
        expected = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected.lower(), signature.strip().lower())

    @staticmethod
    def parse_webhook(body: dict) -> dict:
        """Нормализует webhook-нотификацию от Точки.

        Ожидаемые поля в Data: operationId, status, amount, cardToken (опц.),
        orderId (наш sub_{user_id}_{tariff}_...).
        """
        data = body.get("Data") or body
        return {
            "operation_id": data.get("operationId") or data.get("id") or "",
            "order_id": data.get("orderId") or "",
            "status": (data.get("status") or "").lower(),
            "amount": float(data.get("amount") or 0),
            "card_token": data.get("cardToken") or data.get("savedCardToken") or "",
            "raw": data,
        }


def parse_order_id(order_id: str) -> Optional[tuple[int, str]]:
    """Разбирает orderId формата sub_{user_id}_{tariff}_{rand}
    или renew_{user_id}_{tariff}_{rand}. Возвращает (user_id, tariff) или None."""
    if not order_id:
        return None
    parts = order_id.split("_")
    if len(parts) < 4 or parts[0] not in ("sub", "renew"):
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    tariff = parts[2]
    return user_id, tariff
