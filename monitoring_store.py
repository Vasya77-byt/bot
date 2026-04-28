"""Стор подписок на мониторинг изменений по ИНН.

Каждая подписка хранит снимок ключевых полей компании на момент
последней проверки. Скедулер периодически перечитывает данные и
сравнивает с этим снимком — если что-то изменилось, шлёт пользователю
уведомление.

Снимок (snapshot) — структурированный dict, не хэш. Это позволяет в
уведомлении показать «директор сменился: было X, стало Y», а не
безликое «что-то изменилось».
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("financial-architect")

STORAGE_FILE = os.getenv("MONITORING_FILE", "monitoring.json")


@dataclass
class MonitoringSubscription:
    user_id: int
    inn: str
    name: str = ""                          # отображаемое имя компании
    snapshot: Dict[str, Any] = field(default_factory=dict)
    last_checked: str = ""                  # ISO UTC timestamp
    created_at: str = ""                    # ISO UTC timestamp


def _key(user_id: int, inn: str) -> str:
    return f"{user_id}:{inn}"


class MonitoringStore:
    def __init__(self, filepath: str = STORAGE_FILE) -> None:
        self.filepath = filepath
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception as exc:
            logger.warning("MonitoringStore: failed to load %s: %s", self.filepath, exc)
            self._data = {}

    def _save(self) -> None:
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("MonitoringStore: failed to save %s: %s", self.filepath, exc)

    @staticmethod
    def _from_raw(raw: dict) -> MonitoringSubscription:
        known = {f for f in MonitoringSubscription.__dataclass_fields__}
        clean = {k: v for k, v in raw.items() if k in known}
        return MonitoringSubscription(**clean)

    def add(
        self,
        user_id: int,
        inn: str,
        name: str = "",
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> MonitoringSubscription:
        """Добавляет подписку. Если уже есть — обновляет name/snapshot и
        возвращает существующую (не дублирует)."""
        now = datetime.now(timezone.utc).isoformat()
        key = _key(user_id, inn)
        if key in self._data:
            sub = self._from_raw(self._data[key])
            if name:
                sub.name = name
            if snapshot is not None:
                sub.snapshot = snapshot
            sub.last_checked = now
            self._data[key] = asdict(sub)
            self._save()
            return sub

        sub = MonitoringSubscription(
            user_id=user_id,
            inn=inn,
            name=name,
            snapshot=snapshot or {},
            last_checked=now,
            created_at=now,
        )
        self._data[key] = asdict(sub)
        self._save()
        return sub

    def remove(self, user_id: int, inn: str) -> bool:
        """Удаляет подписку. True — если была, False — не было."""
        key = _key(user_id, inn)
        if key not in self._data:
            return False
        del self._data[key]
        self._save()
        return True

    def get(self, user_id: int, inn: str) -> Optional[MonitoringSubscription]:
        raw = self._data.get(_key(user_id, inn))
        return self._from_raw(raw) if raw else None

    def list_for_user(self, user_id: int) -> List[MonitoringSubscription]:
        return [
            self._from_raw(raw)
            for raw in self._data.values()
            if raw.get("user_id") == user_id
        ]

    def count_for_user(self, user_id: int) -> int:
        return sum(1 for raw in self._data.values() if raw.get("user_id") == user_id)

    def iter_all(self) -> Iterable[MonitoringSubscription]:
        for raw in self._data.values():
            yield self._from_raw(raw)

    def update_snapshot(
        self, user_id: int, inn: str, snapshot: Dict[str, Any]
    ) -> Optional[MonitoringSubscription]:
        key = _key(user_id, inn)
        if key not in self._data:
            return None
        sub = self._from_raw(self._data[key])
        sub.snapshot = snapshot
        sub.last_checked = datetime.now(timezone.utc).isoformat()
        self._data[key] = asdict(sub)
        self._save()
        return sub
