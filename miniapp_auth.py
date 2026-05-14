"""Верификация Telegram Mini App initData.

Telegram SDK при открытии WebApp передаёт строку initData. Сервер
обязан проверить её подпись прежде чем доверять данным пользователя —
иначе любой может подделать user_id и получить чужую статистику.

Алгоритм по доке Telegram:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    1. Парсим query-string initData в пары key=value.
    2. Извлекаем поле "hash" — это эталон, с ним сравним.
    3. Из остальных полей строим data_check_string:
       отсортированные по ключу строки "key=value", соединённые \n.
    4. secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    5. expected_hash = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()
    6. Сравниваем с переданным hash в constant-time.
    7. Дополнительно проверяем auth_date — не старее 24 часов (защита
       от replay).

Возвращаем user_id (int) если всё ок, иначе None.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

logger = logging.getLogger("financial-architect")

# Максимальный возраст initData (24 часа) — после Telegram считает данные
# устаревшими и рекомендует не доверять. Защита от replay.
MAX_AGE_SECONDS = 24 * 3600


def verify_init_data(init_data: str, bot_token: str) -> Optional[int]:
    """Проверяет HMAC-подпись initData. Возвращает user_id или None."""
    if not init_data or not bot_token:
        return None

    # parse_qsl сохраняет порядок и не дедуплицирует — нам нужно все пары
    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)

    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    # auth_date — обязательно, проверяем возраст
    auth_date_raw = data.get("auth_date", "")
    try:
        auth_date = int(auth_date_raw)
    except (TypeError, ValueError):
        return None
    if abs(time.time() - auth_date) > MAX_AGE_SECONDS:
        logger.info("Mini App initData expired: auth_date=%s", auth_date)
        return None

    # data_check_string — отсортированные key=value, соединённые \n
    data_check_string = "\n".join(
        f"{k}={data[k]}" for k in sorted(data.keys())
    )

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    expected = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        logger.info("Mini App initData hash mismatch")
        return None

    # Подпись валидна. Извлекаем user_id из поля "user" (JSON).
    user_raw = data.get("user", "")
    if not user_raw:
        return None
    try:
        user_obj = json.loads(user_raw)
    except json.JSONDecodeError:
        return None

    user_id = user_obj.get("id")
    if not isinstance(user_id, int):
        return None
    return user_id
