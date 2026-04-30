"""Одноразовые токены доступа к веб-отчёту.

Token = UUID hex (32 символа). Хранит привязку (user_id, inn) с TTL.
Используется для безопасного открытия отчёта по URL — ИНН в URL
не светится, доступ ограничен временем и привязан к создателю.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("financial-architect")

DEFAULT_TTL_SECONDS = 24 * 3600  # 24 часа


class ReportTokenStore:
    """JSON-стор: {token: {user_id, inn, expires_at}}."""

    def __init__(self, filepath: Optional[str] = None) -> None:
        path = filepath or os.getenv("REPORT_TOKENS_FILE", "report_tokens.json")
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def create(self, user_id: int, inn: str,
               ttl: int = DEFAULT_TTL_SECONDS) -> str:
        """Создаёт новый токен и возвращает его."""
        token = uuid.uuid4().hex
        data = self._read()
        data[token] = {
            "user_id": user_id,
            "inn": inn,
            "expires_at": int(time.time()) + ttl,
        }
        self._cleanup_expired(data)
        self._write(data)
        return token

    def resolve(self, token: str) -> Optional[dict]:
        """Возвращает привязку для валидного токена или None."""
        if not token:
            return None
        data = self._read()
        info = data.get(token)
        if not isinstance(info, dict):
            return None
        if int(info.get("expires_at", 0)) < int(time.time()):
            data.pop(token, None)
            self._write(data)
            return None
        return info

    @staticmethod
    def _cleanup_expired(data: dict) -> None:
        """Удаляет просроченные записи из dict in-place."""
        now = int(time.time())
        expired = [t for t, v in data.items()
                   if isinstance(v, dict) and int(v.get("expires_at", 0)) < now]
        for t in expired:
            data.pop(t, None)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as exc:
            logger.warning("ReportTokenStore write failed: %s", exc)
