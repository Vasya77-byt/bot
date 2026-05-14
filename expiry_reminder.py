"""Фоновая задача напоминаний об окончании подписки.

Раз в 12 часов проходится по пользователям и шлёт два типа уведомлений:

1. **Напоминание за 3 и 1 день до окончания** — для платников с
   ВЫКЛЮЧЕННЫМ auto_renew. У них автосписания не будет, надо
   напомнить чтобы продлили вручную через «💎 Тарифы».

2. **Уведомление об окончании** — для тех, чья подписка истекла
   и тариф фактически переключился на Free. Шлётся один раз
   (по флагу expired_notice_sent), чтобы не спамить.

Дубли в течение дня защищаются полем last_expiry_reminder_date
в UserProfile.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Awaitable, Callable, Optional

from user_store import UserStore

logger = logging.getLogger("financial-architect")

# Раз в 12 часов — этого хватает, чтобы не пропустить первый день
# окончания и не перегружать бот.
CHECK_INTERVAL_SECONDS = 60 * 60 * 12

NotifyFn = Callable[[int, str], Awaitable[None]]

# Сколько дней ДО окончания шлём напоминание. Шлём дважды:
# за 3 дня (планирование) и за 1 день (последнее предупреждение).
REMINDER_DAYS = (3, 1)


async def run_expiry_reminder_loop(
    users: UserStore,
    notify: NotifyFn,
    interval: int = CHECK_INTERVAL_SECONDS,
) -> None:
    logger.info("Expiry reminder scheduler started (interval=%ss)", interval)
    while True:
        try:
            await _check_once(users, notify)
        except Exception as exc:
            logger.exception("Expiry reminder error: %s", exc)
        await asyncio.sleep(interval)


async def _check_once(users: UserStore, notify: NotifyFn) -> None:
    today_iso = date.today().isoformat()
    now = datetime.now(timezone.utc)

    sent_count = 0
    for profile in users.iter_profiles():
        # Free и без даты окончания — пропускаем
        if profile.tariff == "free" or not profile.tariff_expires_at:
            continue

        try:
            expires = datetime.fromisoformat(profile.tariff_expires_at)
        except ValueError:
            continue

        days_left = (expires - now).total_seconds() / 86400

        # ── 1. Подписка уже истекла ──
        if days_left < 0:
            if profile.expired_notice_sent:
                continue
            # Шлём один раз
            await _safe_notify(
                notify, profile.user_id,
                "❌ Срок подписки истёк\n\n"
                f"Тариф {profile.tariff.upper()} закончился. "
                "Доступ переключён на Free (3 проверки в день).\n\n"
                "Чтобы вернуть полный доступ — нажмите «💎 Тарифы» "
                "или /tarifs."
            )
            profile.expired_notice_sent = True
            users.save_profile(profile)
            sent_count += 1
            continue

        # ── 2. Напоминание за N дней (только при выключенном auto_renew) ──
        if profile.auto_renew:
            # При включённом автопродлении напоминать не нужно — система
            # сама спишет в renewal_scheduler. Но сбросим expired_notice
            # если подписка снова активна (на случай ручного продления)
            if profile.expired_notice_sent:
                profile.expired_notice_sent = False
                users.save_profile(profile)
            continue

        # Сравниваем дату последнего напоминания — не дублируем за день
        if profile.last_expiry_reminder_date == today_iso:
            continue

        days_int = int(days_left)
        # Шлём только в дни из REMINDER_DAYS (3 и 1)
        if days_int not in REMINDER_DAYS:
            continue

        if days_int == 1:
            text = (
                "⏰ Завтра заканчивается подписка\n\n"
                f"Тариф {profile.tariff.upper()} истекает "
                f"{profile.tariff_expires_at[:10]}.\n"
                "Автопродление у вас выключено — после этой даты "
                "доступ переключится на Free.\n\n"
                "Продлите сейчас через «💎 Тарифы» или /tarifs, "
                "чтобы не потерять доступ."
            )
        else:  # 3 дня
            text = (
                f"📅 Подписка заканчивается через {days_int} дня\n\n"
                f"Тариф {profile.tariff.upper()} истекает "
                f"{profile.tariff_expires_at[:10]}.\n"
                "Автопродление у вас выключено.\n\n"
                "Продлить заранее можно через «💎 Тарифы» или /tarifs."
            )

        await _safe_notify(notify, profile.user_id, text)
        profile.last_expiry_reminder_date = today_iso
        users.save_profile(profile)
        sent_count += 1

    if sent_count:
        logger.info("Expiry reminder: sent %d notifications", sent_count)


async def _safe_notify(
    notify: NotifyFn, user_id: int, text: str,
) -> None:
    try:
        await notify(user_id, text)
    except Exception as exc:
        logger.error(
            "Expiry reminder notify failed for user=%s: %s", user_id, exc,
        )
