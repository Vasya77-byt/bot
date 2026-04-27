"""Хранилище списков отслеживаемых компаний."""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("financial-architect")

WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", "watchlist.json")

# Поля для сравнения снапшотов
MONITORED_FIELDS: List[Tuple[str, str]] = [
    ("status", "Статус"),
    ("director", "Руководитель"),
    ("address", "Юридический адрес"),
    ("okved_main", "ОКВЭД"),
    ("employees_count", "Численность сотрудников"),
]


@dataclass
class WatchEntry:
    inn: str
    name: str
    added_at: str = ""
    last_checked: str = ""
    snapshot: dict = field(default_factory=dict)


class WatchlistStore:
    def __init__(self, filepath: str = WATCHLIST_FILE) -> None:
        self.filepath = filepath
        self._data: Dict[str, List[dict]] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as exc:
                logger.warning("WatchlistStore: failed to load %s: %s", self.filepath, exc)
                self._data = {}

    def _save(self) -> None:
        try:
            dir_ = os.path.dirname(os.path.abspath(self.filepath))
            with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False,
                                            suffix=".tmp", encoding="utf-8") as tf:
                json.dump(self._data, tf, ensure_ascii=False, indent=2)
                tmp_path = tf.name
            os.replace(tmp_path, self.filepath)
        except Exception as exc:
            logger.error("WatchlistStore: failed to save %s: %s", self.filepath, exc)

    def add(self, user_id: int, inn: str, name: str) -> bool:
        """Добавляет компанию. Возвращает True если добавлена, False если уже была."""
        key = str(user_id)
        entries = self._data.get(key, [])
        for e in entries:
            if e["inn"] == inn:
                return False
        entry = WatchEntry(
            inn=inn,
            name=name,
            added_at=datetime.now(timezone.utc).isoformat(),
        )
        entries.append(asdict(entry))
        self._data[key] = entries
        self._save()
        return True

    def remove(self, user_id: int, inn: str) -> bool:
        key = str(user_id)
        entries = self._data.get(key, [])
        new_entries = [e for e in entries if e["inn"] != inn]
        if len(new_entries) == len(entries):
            return False
        self._data[key] = new_entries
        self._save()
        return True

    def get_list(self, user_id: int) -> List[WatchEntry]:
        key = str(user_id)
        return [WatchEntry(**e) for e in self._data.get(key, [])]

    def update_snapshot(self, user_id: int, inn: str, snapshot: dict) -> None:
        key = str(user_id)
        entries = self._data.get(key, [])
        now = datetime.now(timezone.utc).isoformat()
        for e in entries:
            if e["inn"] == inn:
                e["snapshot"] = snapshot
                e["last_checked"] = now
                break
        self._data[key] = entries
        self._save()

    def iter_all(self) -> Iterator[Tuple[str, List[WatchEntry]]]:
        for user_id_str, entries in self._data.items():
            if entries:
                yield user_id_str, [WatchEntry(**e) for e in entries]
