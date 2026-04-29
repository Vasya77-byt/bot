"""Клиент для интернет-эквайринга Точка Банка.

Документация:
- Платёжные ссылки: POST /uapi/acquiring/v1.0/payments_with_receipt
- Подписки (рекурренты): /uapi/acquiring/v1.0/subscriptions_with_receipt
- Списание по подписке: /uapi/acquiring/v1.0/subscriptions/{operationId}/charge
- Изменение статуса подписки: POST /uapi/acquiring/v1.0/subscriptions/{operationId}/status
- Получение статуса: GET /uapi/acquiring/v1.0/subscriptions/{operationId}/status
- Список ритейлеров: GET /uapi/acquiring/v1.0/retailers?customerCode=...
- Управление вебхуками: /uapi/webhook/v1.0/{client_id}

Webhook'и приходят как JWT-токен (RS256), подписанный приватным ключом
Точки. Проверяется их публичным ключом. Тело уведомления —
строка с JWT, Content-Type: text/plain.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger("financial-architect")

# Публичный ключ Точка Банка для проверки JWT-вебхуков (алгоритм RS256).
# Получается с https://enter.tochka.com/doc/openapi/static/keys/public.
# Хардкодится здесь как fallback; при ротации ключа у Точки можно
# переопределить через окружение TOCHKA_WEBHOOK_PUBLIC_KEY (JWK JSON).
TOCHKA_PUBLIC_KEY_JWK = {
    "kty": "RSA",
    "e": "AQAB",
    "n": (
        "rwm77av7GIttq-JF1itEgLCGEZW_zz16RlUQVYlLbJtyRSu61fCec_rroP6PxjXU2uLzU"
        "OaGaLgAPeUZAJrGuVp9nryKgbZceHckdHDYgJd9TsdJ1MYUsXaOb9joN9vmsCscBx1lw"
        "SlFQyNQsHUsrjuDk-opf6RCuazRQ9gkoDCX70HV8WBMFoVm-YWQKJHZEaIQxg_DU4gMF"
        "yKRkDGKsYKA0POL-UgWA1qkg6nHY5BOMKaqxbc5ky87muWB5nNk4mfmsckyFv9j1gBiX"
        "LKekA_y4UwG2o1pbOLpJS3bP_c95rm4M9ZBmGXqfOQhbjz8z-s9C11i-jmOQ2ByohS-S"
        "T3E5sqBzIsxxrxyQDTw--bZNhzpbciyYW4GfkkqyeYoOPd_84jPTBDKQXssvj8ZOj2Xb"
        "oS77tvEO1n1WlwUzh8HPCJod5_fEgSXuozpJtOggXBv0C2ps7yXlDZf-7Jar0UYc_NJE"
        "HJF-xShlqd6Q3sVL02PhSCM-ibn9DN9BKmD"
    ),
}

# События, которые мы умеем обрабатывать
ACQUIRING_EVENT = "acquiringInternetPayment"

# Статусы успешной оплаты по acquiringInternetPayment
SUCCESS_STATUSES = {"APPROVED", "AUTHORIZED"}


@dataclass
class SubscriptionResult:
    """Результат создания подписки."""
    operation_id: str             # ID подписки в Точке
    payment_link: str             # ссылка для оплаты первым платежом
    status: str = "created"


@dataclass
class ChargeResult:
    """Результат списания по подписке."""
    operation_id: str
    status: str                   # "approved" | "declined" | "pending"
    error_message: str = ""


class TochkaError(Exception):
    """Ошибка при работе с API Точки."""


class TochkaClient:
    """Клиент Open API Точки.

    Платёжный сценарий через подписки (recurring=true):
    1. create_subscription(...) → operation_id подписки + payment_link
       Клиент переходит по ссылке, оплачивает первый раз. Карта
       привязывается к подписке на стороне Точки. Webhook
       acquiringInternetPayment подтверждает успех.
    2. При наступлении срока продления вызываем
       charge_subscription(operation_id, amount) → списание с привязанной
       карты. Webhook сообщает результат.
    3. Отмена подписки клиентом → cancel_subscription(operation_id) →
       Точка переводит подписку в статус Cancelled.
    """

    BASE_URL = "https://enter.tochka.com/uapi"

    def __init__(
        self,
        jwt_token: str,
        customer_code: str,
        client_id: str = "",
        merchant_id: str = "",
        base_url: str = BASE_URL,
        public_key_jwk: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> None:
        self.jwt_token = jwt_token
        self.customer_code = customer_code
        self.client_id = client_id  # для webhook-эндпоинтов
        self.merchant_id = merchant_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Заранее парсим публичный ключ для верификации webhook'ов
        self._public_key = RSAAlgorithm.from_jwk(
            json.dumps(public_key_jwk or TOCHKA_PUBLIC_KEY_JWK)
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ────────────────────────────────────────────────────────────
    # Подписки (recurring=true): сами дёргаем charge при продлении
    # ────────────────────────────────────────────────────────────

    async def create_subscription(
        self,
        *,
        amount: float,
        purpose: str,
        user_id: int,
        tariff: str,
        redirect_url: str,
        fail_redirect_url: str,
        email: str = "",
        client_name: str = "",
        client_phone: str = "",
        tax_system_code: str = "",
    ) -> SubscriptionResult:
        """Создаёт подписку (рекуррентный платёж) с фискализацией чека.

        Используется recurring=true — без графика, списания вручную через
        charge_subscription. Это позволяет отключать продление командой
        /cancel_subscription без обращения к Точке.
        """
        payment_link_id = f"sub_{user_id}_{tariff}_{uuid.uuid4().hex[:8]}"

        items = [
            {
                "vatType": "none",
                "name": f"Подписка на тариф {tariff}",
                "amount": f"{amount:.2f}",
                "quantity": 1,
                "paymentMethod": "full_prepayment",
                "paymentObject": "service",
                "measure": "шт.",
            }
        ]
        client_block = {"email": email or "noreply@example.com"}
        if client_name:
            client_block["name"] = client_name
        if client_phone:
            client_block["phone"] = client_phone

        data: dict[str, Any] = {
            "customerCode": self.customer_code,
            "amount": f"{amount:.2f}",
            "purpose": purpose,
            "redirectUrl": redirect_url,
            "failRedirectUrl": fail_redirect_url,
            "saveCard": True,
            "paymentLinkId": payment_link_id,
            "Client": client_block,
            "Items": items,
            "recurring": True,
        }
        if self.merchant_id:
            data["merchantId"] = self.merchant_id
        if tax_system_code:
            data["taxSystemCode"] = tax_system_code

        url = f"{self.base_url}/acquiring/v1.0/subscriptions_with_receipt"
        logger.info("Tochka: creating subscription %s amount=%s",
                    payment_link_id, amount)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self._headers(),
                                     json={"Data": data})

        if resp.status_code >= 400:
            logger.error("Tochka create_subscription %s: %s",
                         resp.status_code, resp.text[:500])
            raise TochkaError(
                f"Tochka {resp.status_code}: {resp.text[:500]}"
            )

        body = resp.json().get("Data", {})
        operation_id = body.get("operationId") or body.get("id") or ""
        link = body.get("paymentLink") or body.get("url") or ""
        if not operation_id or not link:
            raise TochkaError(
                f"Tochka: incomplete response: {resp.text[:500]}"
            )
        return SubscriptionResult(
            operation_id=operation_id,
            payment_link=link,
            status="created",
        )

    async def charge_subscription(
        self, *, operation_id: str, amount: float,
    ) -> ChargeResult:
        """Списание средств по существующей подписке."""
        url = (
            f"{self.base_url}/acquiring/v1.0/"
            f"subscriptions/{operation_id}/charge"
        )
        payload = {"Data": {"amount": float(f"{amount:.2f}")}}
        logger.info("Tochka: charging subscription %s amount=%s",
                    operation_id, amount)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self._headers(),
                                     json=payload)

        if resp.status_code >= 400:
            logger.error("Tochka charge_subscription %s: %s",
                         resp.status_code, resp.text[:500])
            return ChargeResult(
                operation_id=operation_id,
                status="declined",
                error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

        body = resp.json().get("Data", {})
        status = (body.get("status") or "pending").lower()
        return ChargeResult(operation_id=operation_id, status=status)

    async def cancel_subscription(self, operation_id: str) -> bool:
        """Отменяет подписку на стороне Точки. После отмены вернуть
        нельзя — придётся создавать новую."""
        url = (
            f"{self.base_url}/acquiring/v1.0/"
            f"subscriptions/{operation_id}/status"
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url, headers=self._headers(),
                json={"Data": {"status": "Cancelled"}},
            )
        if resp.status_code >= 400:
            logger.warning("Tochka cancel_subscription %s: %s",
                           resp.status_code, resp.text[:200])
            return False
        return True

    async def get_subscription_status(
        self, operation_id: str,
    ) -> dict[str, Any]:
        """Получить актуальный статус подписки (для поллера)."""
        url = (
            f"{self.base_url}/acquiring/v1.0/"
            f"subscriptions/{operation_id}/status"
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise TochkaError(
                f"Tochka status {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json().get("Data", {})

    # ────────────────────────────────────────────────────────────
    # Управление вебхуками
    # ────────────────────────────────────────────────────────────

    async def register_webhook(
        self, url: str, events: list[str],
    ) -> bool:
        """Создаёт/перерегистрирует webhook на стороне Точки.
        Точка делает PUT — это создание (или замена при существующем)."""
        if not self.client_id:
            raise TochkaError("client_id required for webhook ops")
        endpoint = f"{self.base_url}/webhook/v1.0/{self.client_id}"
        payload = {"webhooksList": events, "url": url}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.put(endpoint, headers=self._headers(),
                                    json=payload)
        if resp.status_code >= 400:
            logger.warning("Tochka register_webhook %s: %s",
                           resp.status_code, resp.text[:300])
            return False
        return True

    async def get_webhooks(self) -> dict[str, Any]:
        """Возвращает текущую регистрацию webhook'а."""
        if not self.client_id:
            raise TochkaError("client_id required for webhook ops")
        endpoint = f"{self.base_url}/webhook/v1.0/{self.client_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(endpoint, headers=self._headers())
        if resp.status_code >= 400:
            raise TochkaError(
                f"Tochka get_webhooks {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json().get("Data", {})

    # ────────────────────────────────────────────────────────────
    # Информация о ритейлерах и customer code (диагностика)
    # ────────────────────────────────────────────────────────────

    async def get_retailers(self) -> dict[str, Any]:
        """Список торговых точек интернет-эквайринга. Используется для
        проверки что эквайринг подключён (status=REG, isActive=true)."""
        url = (
            f"{self.base_url}/acquiring/v1.0/retailers"
            f"?customerCode={self.customer_code}"
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise TochkaError(
                f"Tochka retailers {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json().get("Data", {})

    async def get_customers(self) -> dict[str, Any]:
        """Список компаний клиента — нужен для определения customerCode."""
        url = f"{self.base_url}/open-banking/v1.0/customers"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise TochkaError(
                f"Tochka customers {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json().get("Data", {})

    # ────────────────────────────────────────────────────────────
    # Webhook: верификация JWT и парсинг
    # ────────────────────────────────────────────────────────────

    def verify_webhook(self, raw_body: bytes) -> Optional[dict[str, Any]]:
        """Проверяет JWT-подпись webhook'а и возвращает claims dict.

        Тело webhook'а — это JWT-строка (не JSON), подписанная
        приватным ключом Точки. Проверка через RS256 + публичный ключ.

        Возвращает dict с полезной нагрузкой при успехе, None — при
        невалидной подписи или ошибке декодирования.
        """
        try:
            token = raw_body.decode("utf-8").strip()
        except UnicodeDecodeError:
            logger.warning("Webhook: non-UTF8 body")
            return None
        if not token:
            return None
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_iss": False},
            )
        except jwt.PyJWTError as exc:
            logger.warning("Webhook: JWT verify failed: %s", exc)
            return None
        if not isinstance(claims, dict):
            return None
        return claims

    @staticmethod
    def parse_acquiring_webhook(claims: dict[str, Any]) -> dict[str, Any]:
        """Нормализует acquiringInternetPayment payload для нашей логики.

        Точка шлёт claims напрямую (без обёртки Data). Поля:
        webhookType, customerCode, merchantId, operationId, amount,
        paymentType, consumerId, purpose, status, paymentLinkId
        (для подписок — это наш sub_<uid>_<tariff>_<rand>).
        """
        return {
            "event": claims.get("webhookType", ""),
            "operation_id": claims.get("operationId", ""),
            "payment_link_id": claims.get("paymentLinkId", ""),
            "status": (claims.get("status") or "").upper(),
            "amount": float(claims.get("amount") or 0),
            "payment_type": claims.get("paymentType", ""),
            "consumer_id": claims.get("consumerId", ""),
            "raw": claims,
        }


def parse_payment_link_id(link_id: str) -> Optional[tuple[int, str]]:
    """Разбирает paymentLinkId формата sub_{user_id}_{tariff}_{rand}.
    Возвращает (user_id, tariff) или None.

    Имя сохранено для обратной совместимости с тестами и старым кодом."""
    if not link_id:
        return None
    parts = link_id.split("_")
    if len(parts) < 4 or parts[0] not in ("sub", "renew"):
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    tariff = parts[2]
    return user_id, tariff


# Алиас для обратной совместимости — раньше функция называлась parse_order_id
parse_order_id = parse_payment_link_id
