"""
Простой in-memory кэш с TTL для результатов проверки.
Ключ — ИНН, значение — готовый отчёт.
"""

import time
from typing import Any

_DEFAULT_TTL = 3600  # 1 час
MAX_CACHE_SIZE = 500  # Максимум записей

_store: dict[str, tuple[float, Any]] = {}


def get(key: str) -> Any | None:
    """Возвращает значение из кэша или None если истёк/нет."""
    entry = _store.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        del _store[key]
        return None
    return value


def put(key: str, value: Any, ttl: int = _DEFAULT_TTL) -> None:
    """Сохраняет значение в кэш с TTL в секундах."""
    now = time.time()

    # Чистим expired
    expired = [k for k, (exp, _) in _store.items() if now > exp]
    for k in expired:
        del _store[k]

    # Если после чистки всё ещё превышаем лимит — удаляем 10% самых старых
    if len(_store) >= MAX_CACHE_SIZE:
        to_remove = max(1, len(_store) // 10)
        sorted_keys = sorted(_store.keys(), key=lambda k: _store[k][0])
        for k in sorted_keys[:to_remove]:
            del _store[k]

    _store[key] = (now + ttl, value)


def delete(key: str) -> None:
    """Удаляет ключ из кэша."""
    _store.pop(key, None)


def clear() -> None:
    """Очищает весь кэш."""
    _store.clear()


def size() -> int:
    """Возвращает количество записей в кэше."""
    # Чистим протухшие
    now = time.time()
    expired = [k for k, (exp, _) in _store.items() if now > exp]
    for k in expired:
        del _store[k]
    return len(_store)
