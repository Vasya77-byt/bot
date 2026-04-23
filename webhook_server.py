"""HTTP-сервер для приёма webhook-уведомлений от Точка Банка.

Запускается параллельно с ботом через asyncio. После успешной оплаты
Точка стучится на /tochka/webhook с данными платежа.
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

from subscription import SubscriptionService
from tochka_client import TochkaClient

logger = logging.getLogger("financial-architect")

# Колбэк для отправки уведомления пользователю в Telegram
# Сигнатура: async def notify(user_id: int, text: str) -> None
NotifyFn = Callable[[int, str], Awaitable[None]]


def build_app(
    tochka: TochkaClient,
    subscription: SubscriptionService,
    notify: Optional[NotifyFn] = None,
) -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def webhook(request: web.Request) -> web.Response:
        raw = await request.read()
        signature = request.headers.get("X-Signature") or request.headers.get("Signature", "")

        if not tochka.verify_webhook(raw, signature):
            logger.warning("Webhook: bad signature from %s", request.remote)
            return web.json_response({"error": "bad signature"}, status=403)

        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return web.json_response({"error": "bad json"}, status=400)

        parsed = TochkaClient.parse_webhook(body)
        logger.info("Webhook received: %s", parsed)

        status = parsed["status"]
        op = parsed["operation_id"]
        order = parsed["order_id"]

        if status in ("paid", "approved", "confirmed", "completed"):
            profile = subscription.handle_webhook_paid(
                operation_id=op,
                order_id=order,
                card_token=parsed["card_token"],
                amount=parsed["amount"],
            )
            if profile and notify:
                text = (
                    f"✅ Оплата прошла!\n\n"
                    f"Тариф: {profile.tariff.upper()}\n"
                    f"Подписка действует до: {profile.tariff_expires_at[:10]}\n\n"
                    f"Автопродление включено. Отключить: /cancel_subscription"
                )
                try:
                    await notify(profile.user_id, text)
                except Exception as exc:
                    logger.error("Notify failed: %s", exc)

        elif status in ("failed", "declined", "cancelled", "rejected"):
            subscription.handle_webhook_failed(
                operation_id=op,
                error=str(parsed.get("raw", {}).get("errorMessage", "")),
            )
            # Уведомляем пользователя только если можем определить его
            from tochka_client import parse_order_id
            parsed_order = parse_order_id(order)
            if parsed_order and notify:
                user_id, tariff = parsed_order
                try:
                    await notify(
                        user_id,
                        f"❌ Оплата тарифа {tariff} не прошла. Попробуйте ещё раз из меню «Тарифы».",
                    )
                except Exception as exc:
                    logger.error("Notify failed: %s", exc)

        # Точка ожидает 200 OK, иначе будет ретраить
        return web.json_response({"status": "ok"})

    app.router.add_get("/health", health)
    app.router.add_post("/tochka/webhook", webhook)

    return app


async def start_webhook_server(
    app: web.Application, host: str = "0.0.0.0", port: int = 8080
) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Webhook server listening on %s:%s", host, port)
    return runner
