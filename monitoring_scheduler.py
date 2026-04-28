"""Фоновая задача: периодически перепроверяет подписки на мониторинг
и шлёт пользователю уведомление, если что-то изменилось.

Раз в N часов (по умолчанию 24) обходит все подписки, дёргает
CompanyService + SecurityService, считает diff с предыдущим снимком.
Снимок обновляется ВСЕГДА после успешной проверки — даже если diff
пустой; так мы избегаем накопления изменений и одинаковых уведомлений
несколько раз подряд.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from company_service import CompanyService
from monitoring import (
    diff_snapshots,
    format_change_message,
    make_snapshot,
)
from monitoring_store import MonitoringStore
from security_check import SecurityService

logger = logging.getLogger("financial-architect")

CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # раз в сутки

NotifyFn = Callable[[int, str], Awaitable[None]]


async def run_monitoring_loop(
    monitoring: MonitoringStore,
    company_service: CompanyService,
    security_service: SecurityService,
    notify: Optional[NotifyFn] = None,
    interval: int = CHECK_INTERVAL_SECONDS,
) -> None:
    logger.info("Monitoring scheduler started (interval=%ss)", interval)
    while True:
        try:
            await _check_once(monitoring, company_service, security_service, notify)
        except Exception as exc:
            logger.exception("Monitoring loop error: %s", exc)
        await asyncio.sleep(interval)


async def _check_once(
    monitoring: MonitoringStore,
    company_service: CompanyService,
    security_service: SecurityService,
    notify: Optional[NotifyFn],
) -> None:
    subscriptions = list(monitoring.iter_all())
    if not subscriptions:
        return
    logger.info("Monitoring: checking %s subscriptions", len(subscriptions))

    for sub in subscriptions:
        try:
            company = await company_service.fetch(sub.inn)
        except Exception as exc:
            logger.warning("Monitoring fetch failed for %s: %s", sub.inn, exc)
            continue

        security = None
        try:
            security = await security_service.check(
                inn=sub.inn,
                name=company.name if company else sub.name,
                okved=company.okved_main if company else None,
            )
        except Exception as exc:
            logger.warning("Monitoring security check failed for %s: %s", sub.inn, exc)

        new_snapshot = make_snapshot(company, security)
        changes = diff_snapshots(sub.snapshot, new_snapshot)

        # Обновляем снимок всегда — даже если изменений нет.
        # Это важно: при первой проверке snapshot был пустым и diff
        # вернул []; следующая проверка должна сравниваться с уже
        # заполненным состоянием.
        display_name = (company.name if company else sub.name) or sub.name
        monitoring.add(
            user_id=sub.user_id,
            inn=sub.inn,
            name=display_name,
            snapshot=new_snapshot,
        )

        if changes and notify:
            text = format_change_message(sub.inn, display_name, changes)
            try:
                await notify(sub.user_id, text)
            except Exception as exc:
                logger.error("Monitoring notify failed for %s: %s", sub.user_id, exc)
