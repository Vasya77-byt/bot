"""HTTP-сервер для приёма webhook-уведомлений от платёжных систем.

Поддерживаемые провайдеры:
- Точка Банк: ``POST /tochka/webhook`` — JWT (RS256), Content-Type
  text/plain. Подпись проверяется публичным ключом банка.
- ЮKassa:     ``POST /yookassa/webhook`` — JSON, опционально HMAC-SHA256
  в заголовке ``Y-Signature``. Безопасность IP-whitelist'ом отправителей
  (185.71.76.0/27 и др.) + double-check через GET /v3/payments/{id}.

Обрабатываются только успешные платежи. Failed-сценарии узнаются
через payment-poller, который опрашивает провайдер по pending-записям
из payments.json.

Дополнительно сервер отдаёт HTML-отчёт по компании на маршруте
``/report/{token}``: токен генерируется в боте при нажатии кнопки
«🌐 Веб-отчёт» (премиум-тариф), привязан к user_id и ИНН и
действует ограниченное время. INN в URL не светится.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

from company_service import CompanyService
from report_renderer import render_report
from report_tokens import ReportTokenStore
from security_check import SecurityService
from subscription import SubscriptionService
from tochka_client import (
    ACQUIRING_EVENT,
    SUCCESS_STATUSES,
    TochkaClient,
    parse_payment_link_id,
)
from yookassa_client import (
    PAYMENT_SUCCEEDED_EVENT,
    SUCCESS_STATUSES as YK_SUCCESS_STATUSES,
    YooKassaClient,
)
from zchb_client import ZchbClient

logger = logging.getLogger("financial-architect")

NotifyFn = Callable[[int, str], Awaitable[None]]


def build_app(
    tochka: Optional[TochkaClient],
    subscription: SubscriptionService,
    notify: Optional[NotifyFn] = None,
    *,
    yookassa: Optional[YooKassaClient] = None,
    report_tokens: Optional[ReportTokenStore] = None,
    company_service: Optional[CompanyService] = None,
    security_service: Optional[SecurityService] = None,
    zchb: Optional[ZchbClient] = None,
) -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _notify_payment_success(profile) -> None:
        if not (profile and notify):
            return
        expires_short = (
            profile.tariff_expires_at[:10]
            if profile.tariff_expires_at else "—"
        )
        text = (
            "✅ Оплата прошла!\n\n"
            f"Тариф: {profile.tariff.upper()}\n"
            f"Подписка действует до: {expires_short}\n\n"
            "Автопродление включено. "
            "Отключить: /cancel_subscription"
        )
        try:
            await notify(profile.user_id, text)
        except Exception as exc:
            logger.error("Notify failed: %s", exc)

    async def tochka_webhook(request: web.Request) -> web.Response:
        if tochka is None:
            return web.json_response(
                {"error": "tochka not configured"}, status=503,
            )
        raw = await request.read()

        # 1) Проверяем JWT-подпись публичным ключом Точки
        claims = tochka.verify_webhook(raw)
        if claims is None:
            logger.warning("Webhook: bad signature from %s", request.remote)
            # Точка ждёт 200 для успеха или другой код для retry.
            # На bad signature отдаём 403 — Точка ретраит и узнает.
            return web.json_response({"error": "bad signature"}, status=403)

        # 2) Игнорируем все события кроме нашего
        event_type = claims.get("webhookType", "")
        if event_type != ACQUIRING_EVENT:
            logger.info("Webhook: ignoring event %s", event_type)
            return web.json_response({"status": "ignored"})

        # 3) Парсим acquiring-уведомление
        parsed = TochkaClient.parse_acquiring_webhook(claims)
        logger.info("Tochka webhook: %s status=%s op=%s",
                    parsed["event"], parsed["status"],
                    parsed["operation_id"])

        # Точка шлёт webhook ТОЛЬКО для успешных платежей
        # (AUTHORIZED — заморожено в двухэтапной, APPROVED — списано).
        # Failed/declined webhook'ом не приходят — узнаются через poller
        # (get_subscription_status).
        if parsed["status"] in SUCCESS_STATUSES:
            profile = subscription.handle_webhook_paid(
                operation_id=parsed["operation_id"],
                order_id=parsed["payment_link_id"],
                card_token="",  # у Точки cardToken не отдаётся, привязка
                                # к карте — на стороне подписки
                amount=parsed["amount"],
            )
            await _notify_payment_success(profile)

        # Точка ожидает 200 OK, иначе будет ретраить (30 раз × 10 сек)
        return web.json_response({"status": "ok"})

    async def yookassa_webhook(request: web.Request) -> web.Response:
        if yookassa is None:
            return web.json_response(
                {"error": "yookassa not configured"}, status=503,
            )
        raw = await request.read()
        # X-Forwarded-For при работе через nginx-прокси — берём первый IP
        remote = request.headers.get("X-Forwarded-For", request.remote or "")
        remote = remote.split(",")[0].strip()

        # 1) IP-whitelist: webhook должен прийти с серверов ЮKassa
        if not YooKassaClient.is_yookassa_ip(remote):
            logger.warning(
                "YooKassa webhook: rejected non-whitelisted IP %s", remote,
            )
            return web.json_response({"error": "forbidden"}, status=403)

        # 2) Опциональная HMAC-проверка (если включена в личном кабинете)
        signature = request.headers.get("Y-Signature", "")
        if not yookassa.verify_webhook_signature(raw, signature):
            logger.warning("YooKassa webhook: bad HMAC signature")
            return web.json_response({"error": "bad signature"}, status=403)

        # 3) Парсим JSON
        event = YooKassaClient.parse_webhook(raw)
        if event is None:
            return web.json_response({"error": "bad body"}, status=400)

        logger.info(
            "YooKassa webhook: %s status=%s payment_id=%s",
            event.event, event.status, event.payment_id,
        )

        # 4) Обрабатываем только успешные платежи. Остальные события
        # (canceled / refund.succeeded) можно расширить позже — сейчас
        # просто 200 OK, чтобы ЮKassa не ретраила.
        if event.event == PAYMENT_SUCCEEDED_EVENT \
                and event.status in YK_SUCCESS_STATUSES:
            profile = subscription.handle_yookassa_webhook_paid(
                payment_id=event.payment_id,
                order_id=event.order_id,
                user_id=event.user_id,
                tariff=event.tariff,
                amount=event.amount,
                payment_method_id=event.payment_method_id,
                kind=event.kind,
            )
            await _notify_payment_success(profile)

        # ЮKassa ждёт 200 OK для подтверждения — иначе будет ретраить
        return web.json_response({"status": "ok"})

    async def report(request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        if report_tokens is None or company_service is None \
                or security_service is None:
            return web.Response(
                text="Сервис веб-отчётов не сконфигурирован.",
                status=503,
                content_type="text/plain",
                charset="utf-8",
            )
        info = report_tokens.resolve(token)
        if info is None:
            return web.Response(
                text=("Ссылка недействительна или просрочена.\n"
                      "Откройте отчёт повторно из бота."),
                status=404,
                content_type="text/plain",
                charset="utf-8",
            )
        inn = str(info.get("inn") or "")
        if not inn:
            return web.Response(text="bad token", status=400)

        try:
            company = await company_service.fetch(inn)
        except Exception as exc:
            logger.warning("report: company fetch failed inn=%s: %s", inn, exc)
            company = None

        card = None
        if zchb is not None and zchb.enabled:
            try:
                card = await zchb.get_card(inn)
            except Exception as exc:
                logger.warning("report: get_card failed inn=%s: %s", inn, exc)

        try:
            security = await security_service.check(
                inn=inn,
                name=company.name if company else None,
                okved=company.okved_main if company else None,
                ogrn=company.ogrn if company else None,
            )
        except Exception as exc:
            logger.warning("report: security check failed inn=%s: %s", inn, exc)
            security = None

        try:
            html = render_report(
                inn=inn, company=company, card=card, security=security,
            )
        except Exception as exc:
            logger.exception("report: render failed inn=%s: %s", inn, exc)
            return web.Response(
                text="Не удалось собрать отчёт. Попробуйте позже.",
                status=500,
                content_type="text/plain",
                charset="utf-8",
            )

        return web.Response(
            body=html,
            content_type="text/html",
            charset="utf-8",
        )

    app.router.add_get("/health", health)
    app.router.add_post("/tochka/webhook", tochka_webhook)
    app.router.add_post("/yookassa/webhook", yookassa_webhook)
    app.router.add_get("/report/{token}", report)

    return app


async def start_webhook_server(
    app: web.Application, host: str = "0.0.0.0", port: int = 8080,
) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Webhook server listening on %s:%s", host, port)
    return runner
