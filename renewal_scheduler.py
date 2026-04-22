"""Фоновая задача автопродления подписок.

Раз в час проходится по пользователям, находит истекающие в ближайшие
сутки подписки с включённым автопродлением и пытается списать с сохранённой
карты через Точку.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from subscription import SubscriptionService

logger = logging.getLogger("financial-architect")

CHECK_INTERVAL_SECONDS = 60 * 60  # каждый час

NotifyFn = Callable[[int, str], Awaitable[None]]


async def run_renewal_loop(
    subscription: SubscriptionService,
    notify: Optional[NotifyFn] = None,
    interval: int = CHECK_INTERVAL_SECONDS,
) -> None:
    logger.info("Renewal scheduler started (interval=%ss)", interval)
    while True:
        try:
            await _renew_once(subscription, notify)
        except Exception as exc:
            logger.exception("Renewal loop error: %s", exc)
        await asyncio.sleep(interval)


async def _renew_once(
    subscription: SubscriptionService,
    notify: Optional[NotifyFn],
) -> None:
    candidates = subscription.expiring_soon(days=1)
    if not candidates:
        return
    logger.info("Renewal: %s candidates", len(candidates))

    for profile in candidates:
        ok, msg = await subscription.try_renew(profile)
        if ok and msg == "renewed":
            logger.info("Renewed user=%s tariff=%s", profile.user_id, profile.tariff)
            if notify:
                try:
                    await notify(
                        profile.user_id,
                        f"✅ Подписка {profile.tariff.upper()} автоматически продлена на 30 дней.",
                    )
                except Exception as exc:
                    logger.error("Notify failed: %s", exc)
        elif ok and msg == "pending":
            logger.info("Renewal pending for user=%s", profile.user_id)
        else:
            logger.warning(
                "Renewal failed user=%s tariff=%s: %s",
                profile.user_id,
                profile.tariff,
                msg,
            )
            if notify:
                try:
                    await notify(
                        profile.user_id,
                        "⚠️ Не удалось автоматически продлить подписку.\n\n"
                        "Карта могла быть заблокирована или на счёте недостаточно средств. "
                        "Оплатите вручную через меню «Тарифы», иначе доступ переключится на Free.",
                    )
                except Exception as exc:
                    logger.error("Notify failed: %s", exc)
