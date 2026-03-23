"""
Фоновый мониторинг компаний — проверяет watchlist раз в 24 часа.
При изменении данных отправляет уведомление пользователю.
"""

import asyncio
import hashlib
import logging

from aiogram import Bot

import database as db
from dadata_client import fetch_company_data, extract_company_fields

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 24 * 3600  # 24 часа


async def start_monitoring(bot: Bot) -> None:
    """Запускает бесконечный цикл мониторинга."""
    # Ждём 60 секунд после старта, чтобы бот полностью инициализировался
    await asyncio.sleep(60)

    while True:
        try:
            await _check_all_watches(bot)
        except Exception as e:
            logger.exception("Monitor error: %s", e)

        await asyncio.sleep(_CHECK_INTERVAL)


async def _check_all_watches(bot: Bot) -> None:
    """Проверяет все записи мониторинга."""
    watches = await db.a_get_all_watches()
    if not watches:
        return

    logger.info("Monitor: checking %d watchlist entries", len(watches))

    for w in watches:
        try:
            await _check_single_watch(bot, w)
            # Небольшая пауза между запросами
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("Monitor error for INN %s: %s", w.get("inn"), e)


async def _check_single_watch(bot: Bot, watch: dict) -> None:
    """Проверяет одну запись мониторинга."""
    inn = watch["inn"]
    user_id = watch["user_id"]
    old_hash = watch.get("last_data_hash") or ""

    try:
        raw_data = await fetch_company_data(inn)
        fields = extract_company_fields(raw_data)
    except Exception:
        return  # Пропускаем — данные недоступны

    # Вычисляем хэш ключевых данных
    key_data = {
        "name": fields.get("name"),
        "status": fields.get("status"),
        "address": fields.get("address"),
        "management_name": fields.get("management_name"),
        "capital_value": fields.get("capital_value"),
        "okved_code": fields.get("okved_code"),
    }
    new_hash = hashlib.md5(str(key_data).encode()).hexdigest()

    if old_hash and new_hash != old_hash:
        # Данные изменились — отправляем уведомление
        company_name = fields.get("name") or inn
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"🔔 <b>Изменения обнаружены!</b>\n\n"
                    f"Компания: <b>{company_name}</b>\n"
                    f"ИНН: <code>{inn}</code>\n\n"
                    f"Отправьте ИНН для просмотра актуальных данных."
                ),
                parse_mode="HTML",
            )
            logger.info("Monitor: alert sent to user %s for INN %s", user_id, inn)
        except Exception as e:
            err_msg = str(e).lower()
            # Пользователь заблокировал бота или деактивировал аккаунт
            if any(kw in err_msg for kw in ("blocked", "deactivated", "not found", "forbidden")):
                logger.info("Monitor: user %s blocked bot, removing all watches", user_id)
                # Удаляем все watches этого пользователя
                user_watches = await db.a_get_user_watchlist(user_id)
                for uw in user_watches:
                    await db.a_remove_watch(user_id, uw["inn"])
                return
            logger.warning("Monitor: failed to send alert to user %s: %s", user_id, e)

    # Обновляем хэш
    await db.a_update_watch_hash(watch["id"], new_hash)
