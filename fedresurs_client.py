"""Клиент Федресурса (fedresurs.ru) — банкротства и значимые события компании.

Использует публичные JSON-эндпоинты, которыми пользуется сам сайт.
Возвращает None если компания не найдена или сервис недоступен.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("financial-architect")

_BASE_URL = "https://fedresurs.ru/backend"
_TIMEOUT = 15.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://fedresurs.ru/",
}

# Типы публикаций которые считаем критичными
_CRITICAL_TYPES = {
    "BankruptMessage": "Сообщение о банкротстве",
    "ArbitralCaseMessage": "Арбитражное дело",
    "ReorganizationMessage": "Реорганизация",
    "LiquidationMessage": "Ликвидация",
}


def _summarise(publications: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Анализирует список публикаций, возвращает свод."""
    if not publications:
        return {"status": "Нет данных о банкротстве", "messages": []}

    bankruptcy = False
    liquidation = False
    reorganization = False
    messages: List[str] = []

    for pub in publications[:20]:
        type_id = pub.get("type") or pub.get("publicationType") or ""
        title = pub.get("title") or pub.get("name") or pub.get("description") or ""
        date_str = pub.get("datePublish") or pub.get("publishDate") or pub.get("date") or ""

        type_lower = str(type_id).lower()
        title_lower = str(title).lower()

        if "bankrupt" in type_lower or "банкрот" in title_lower:
            bankruptcy = True
        if "liquidat" in type_lower or "ликвидац" in title_lower:
            liquidation = True
        if "reorganiz" in type_lower or "реорганизац" in title_lower:
            reorganization = True

        if title:
            short_date = str(date_str)[:10] if date_str else ""
            short_title = title if len(title) <= 100 else title[:97] + "..."
            messages.append(f"{short_date}: {short_title}".strip(": "))

    if bankruptcy:
        status = "🔴 Банкротство"
    elif liquidation:
        status = "🟠 Ликвидация"
    elif reorganization:
        status = "🟡 Реорганизация"
    elif messages:
        status = "🟢 Есть публикации, банкротства нет"
    else:
        status = "🟢 Нет критичных событий"

    return {"status": status, "messages": messages[:5]}


class FedresursClient:
    def __init__(self, timeout: float = _TIMEOUT) -> None:
        self.timeout = timeout

    async def fetch(self, inn: str) -> Optional[Dict[str, Any]]:
        """Возвращает {status, messages} или None при ошибке."""
        try:
            guid = await self._find_guid(inn)
            if not guid:
                logger.info("Fedresurs: company not found for INN %s", inn)
                return {"status": "🟢 Нет данных о банкротстве", "messages": []}

            publications = await self._fetch_publications(guid)
            return _summarise(publications)
        except Exception as exc:
            logger.warning("Fedresurs error for INN %s: %s", inn, exc)
            return None

    async def _find_guid(self, inn: str) -> Optional[str]:
        url = f"{_BASE_URL}/companies"
        async with httpx.AsyncClient(timeout=self.timeout, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url, params={"searchString": inn, "limit": 5, "offset": 0})
        if resp.status_code != 200:
            logger.warning("Fedresurs search HTTP %s for INN %s", resp.status_code, inn)
            return None
        data = resp.json()
        items = data.get("pageData") or data.get("items") or []
        if not items:
            return None
        # Берём первый результат с совпадающим ИНН
        for item in items:
            if str(item.get("inn", "")) == inn:
                return item.get("guid") or item.get("id")
        return items[0].get("guid") or items[0].get("id")

    async def _fetch_publications(self, guid: str) -> List[Dict[str, Any]]:
        url = f"{_BASE_URL}/companies/{guid}/publications"
        async with httpx.AsyncClient(timeout=self.timeout, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url, params={"limit": 20, "offset": 0})
        if resp.status_code != 200:
            logger.warning("Fedresurs publications HTTP %s for guid %s", resp.status_code, guid)
            return []
        data = resp.json()
        return data.get("pageData") or data.get("items") or []
