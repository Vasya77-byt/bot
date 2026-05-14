"""Тесты верификации Telegram Mini App initData.

Алгоритм: HMAC-SHA256 over sorted "k=v\\n..." with secret_key derived
from bot_token via HMAC-SHA256("WebAppData", bot_token).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from miniapp_auth import MAX_AGE_SECONDS, verify_init_data


BOT_TOKEN = "1234:test_token_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _build_init_data(
    user_obj: dict,
    bot_token: str = BOT_TOKEN,
    auth_date: int | None = None,
    extra: dict | None = None,
) -> str:
    """Строит правильно подписанный initData (как делает Telegram SDK)."""
    if auth_date is None:
        auth_date = int(time.time())
    data = {
        "auth_date": str(auth_date),
        "user": json.dumps(user_obj, separators=(",", ":")),
    }
    if extra:
        data.update(extra)

    data_check = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256,
    ).digest()
    sig = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    data["hash"] = sig
    return urlencode(data)


class TestValidFlow:
    def test_returns_user_id_on_valid_data(self):
        init = _build_init_data({"id": 12345, "first_name": "Test"})
        assert verify_init_data(init, BOT_TOKEN) == 12345

    def test_handles_extra_fields(self):
        init = _build_init_data(
            {"id": 7, "first_name": "X"},
            extra={"query_id": "QID_123"},
        )
        assert verify_init_data(init, BOT_TOKEN) == 7


class TestInvalidCases:
    def test_empty_data_returns_none(self):
        assert verify_init_data("", BOT_TOKEN) is None

    def test_empty_token_returns_none(self):
        init = _build_init_data({"id": 1, "first_name": "X"})
        assert verify_init_data(init, "") is None

    def test_no_hash_returns_none(self):
        init = "auth_date=123&user=%7B%22id%22%3A1%7D"
        assert verify_init_data(init, BOT_TOKEN) is None

    def test_wrong_hash_returns_none(self):
        init = _build_init_data({"id": 1, "first_name": "X"})
        # Подменяем хеш на мусорный
        tampered = init.replace("hash=", "hash=00000")
        assert verify_init_data(tampered, BOT_TOKEN) is None

    def test_wrong_token_returns_none(self):
        init = _build_init_data({"id": 1, "first_name": "X"})
        assert verify_init_data(init, "wrong_token") is None

    def test_expired_auth_date_returns_none(self):
        # auth_date старше 24 часов
        old = int(time.time()) - (MAX_AGE_SECONDS + 60)
        init = _build_init_data({"id": 1, "first_name": "X"}, auth_date=old)
        assert verify_init_data(init, BOT_TOKEN) is None

    def test_missing_user_field_returns_none(self):
        # Подпишем initData без поля user
        data = {"auth_date": str(int(time.time()))}
        data_check = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        init = urlencode(data)
        assert verify_init_data(init, BOT_TOKEN) is None

    def test_user_id_not_int_returns_none(self):
        init = _build_init_data({"id": "string-id", "first_name": "X"})
        assert verify_init_data(init, BOT_TOKEN) is None

    def test_invalid_user_json_returns_none(self):
        data = {
            "auth_date": str(int(time.time())),
            "user": "{not json",
        }
        data_check = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        init = urlencode(data)
        assert verify_init_data(init, BOT_TOKEN) is None
