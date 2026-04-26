"""Фоновый планировщик мониторинга изменений в компаниях."""

import asyncio
import logging
import os
from typing import Callable, Awaitable

from watchlist_store import WatchlistStore, MONITORED_FIELDS

logger = logging.getLogger("financial-architect")

_INTERVAL_SEC = int(os.getenv("WATCH_INTERVAL_HOURS", "24")) * 3600


def make_snapshot(company) -> dict:
    return {f: getattr(company, f, None) for f, _ in MONITORED_FIELDS}


def diff_snapshot(old: dict, new: dict) -> list:
    """Возвращает список (label, old_val, new_val) для изменившихся полей."""
    changes = []
    for field, label in MONITORED_FIELDS:
        ov = old.get(field)
        nv = new.get(field)
        if ov != nv and (ov or nv):
            changes.append((label, str(ov or "—"), str(nv or "—")))
    return changes


async def run_watch_loop(
    watchlist_store: WatchlistStore,
    company_service,
    notify: Callable[[int, str], Awaitable[None]],
    interval_sec: int = _INTERVAL_SEC,
) -> None:
    """Бесконечный цикл проверки изменений."""
    while True:
        await asyncio.sleep(interval_sec)
        logger.info("Watch loop: checking all tracked companies...")
        try:
            await _check_all(watchlist_store, company_service, notify)
        except Exception as exc:
            logger.error("Watch loop error: %s", exc)


async def _check_all(watchlist_store, company_service, notify) -> None:
    for user_id_str, entries in watchlist_store.iter_all():
        user_id = int(user_id_str)
        for entry in entries:
            await asyncio.sleep(2)  # пауза между запросами чтобы не перегружать источники
            try:
                company = await company_service.fetch(entry.inn)
                if not company:
                    continue
                new_snap = make_snapshot(company)
                old_snap = entry.snapshot
                if old_snap:
                    changes = diff_snapshot(old_snap, new_snap)
                    if changes:
                        name = company.name or entry.name or entry.inn
                        lines = [f"🔔 Изменения в компании {name} (ИНН {entry.inn})\n"]
                        for label, old_val, new_val in changes:
                            lines.append(f"• {label}:\n  было: {old_val}\n  стало: {new_val}")
                        lines.append(f"\nОтправьте ИНН {entry.inn} чтобы получить актуальный отчёт.")
                        await notify(user_id, "\n".join(lines))
                watchlist_store.update_snapshot(user_id, entry.inn, new_snap)
            except Exception as exc:
                logger.warning("Watch check failed for INN %s: %s", entry.inn, exc)
