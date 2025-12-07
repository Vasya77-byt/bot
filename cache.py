import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


class FileTTLCache:
    """
    Simple file-based TTL cache (JSON). Stores a dict of key -> {ts, value}.
    """

    def __init__(self, name: str, ttl: float, dir_path: Optional[str] = None) -> None:
        self.ttl = ttl
        base_dir = dir_path or os.getenv("CACHE_DIR", ".cache")
        self.path = Path(base_dir) / f"{name}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[Any]:
        data = self._read()
        if not data or key not in data:
            return None
        entry = data.get(key)
        if not isinstance(entry, dict) or "ts" not in entry or "value" not in entry:
            return None
        if time.time() - entry["ts"] > self.ttl:
            data.pop(key, None)
            self._write(data)
            return None
        return entry["value"]

    def set(self, key: str, value: Any) -> None:
        data = self._read() or {}
        data[key] = {"ts": time.time(), "value": value}
        self._write(data)

    def _read(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write(self, data: Dict[str, Any]) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            # cache write failure is non-fatal
            pass
 