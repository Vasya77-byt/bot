"""Фоновый поллер pending-платежей.

Раз в N минут опрашивает Точку по всем платежам в статусе 'created'
старше M секунд. Это safety-net на случай, когда webhook не дошёл до
нашего сервера (потеря на сети, прокси, кратковременное падение
webhook-сервера).

Без поллера: пользователь оплатил → подписка не активирована →
жалоба → ручная разборка по payments.json.

С поллером: даже если webhook ушёл в null, через 5 минут поллер
запросит статус, увидит paid и активирует подписку.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from subscription import SubscriptionService

logger = logging.getLogger("financial-architect")

CHECK_INTERVAL_SECONDS = 5 * 60          # каждые 5 минут
MIN_PAYMENT_AGE_SECONDS = 5 * 60         # не трогаем платежи моложе 5 мин
MAX_PAYMENT_AGE_SECONDS = 24 * 60 * 60   # старше суток — брошенные

NotifyFn = Callable[[int, str], Awaitable[None]]


async def run_payment_poller(
    subscription: SubscriptionService,
    notify: Optional[NotifyFn] = None,
    interval: int = CHECK_INTERVAL_SECONDS,
    older_than_seconds: int = MIN_PAYMENT_AGE_SECONDS,
    max_age_seconds: int = MAX_PAYMENT_AGE_SECONDS,
) -> None:
    """Бесконечный цикл проверки pending-платежей.

    Если поллер ловит активацию (которую пропустил webhook), уведомляет
    пользователя — иначе клиент не узнает что подписка работает.
    """
    logger.info("Payment poller started (interval=%ss)", interval)
    while True:
        try:
            await _poll_once(
                subscription, notify,
                older_than_seconds=older_than_seconds,
                max_age_seconds=max_age_seconds,
            )
        except Exception as exc:
            logger.exception("Payment poller error: %s", exc)
        await asyncio.sleep(interval)


async def _poll_once(
    subscription: SubscriptionService,
    notify: Optional[NotifyFn],
    *,
    older_than_seconds: int,
    max_age_seconds: int,
) -> None:
    results = await subscription.poll_pending_payments(
        older_than_seconds=older_than_seconds,
        max_age_seconds=max_age_seconds,
    )
    if not results:
        return

    activated = [op for op, action in results if action == "activated"]
    if activated and notify:
        # Для каждой свежеактивированной — найдём пользователя и
        # уведомим (Точка наверняка пыталась webhook'ом, но не дошёл).
        for op_id in activated:
            rec = subscription.payments.find_by_operation(op_id)
            if not rec:
                continue
            try:
                await notify(
                    rec.user_id,
                    "✅ Оплата подтверждена!\n\n"
                    f"Тариф: {rec.tariff.upper()} активирован.\n"
                    "Если уведомление пришло с задержкой — это нормально.",
                )
            except Exception as exc:
                logger.error("Poller notify failed for user=%s: %s",
                             rec.user_id, exc)

    counts = {}
    for _, action in results:
        counts[action] = counts.get(action, 0) + 1
    logger.info("Payment poller cycle: %s", counts)
