"""
Клиент DaData findAffiliated — поиск связанных компаний.
Использует DaData findAffiliated/party API.
"""

import logging
from typing import Any

import httpx

from config import DADATA_API_KEY

logger = logging.getLogger(__name__)

_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findAffiliated/party"
_TIMEOUT = 15.0


async def fetch_affiliated(inn: str) -> list[dict[str, Any]]:
    """
    Ищет связанные компании по ИНН через DaData findAffiliated.

    Возвращает список dict:
      [{name, inn, ogrn, role, status}, ...]
    """
    if not DADATA_API_KEY:
        return []

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {DADATA_API_KEY}",
    }
    payload = {"query": inn, "count": 20}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(_URL, json=payload, headers=headers)

        if response.status_code != 200:
            logger.warning("DaData affiliated HTTP %s for INN %s", response.status_code, inn)
            return []

        body = response.json()
        suggestions = body.get("suggestions", [])
        if not suggestions:
            return []

        result: list[dict[str, Any]] = []
        for s in suggestions:
            data = s.get("data", {})
            name = s.get("value") or data.get("name", {}).get("short_with_opf", "")
            state = data.get("state", {})
            status = state.get("status", "")

            # Определяем роль из данных
            role = ""
            management = data.get("management", {})
            if management and management.get("name"):
                role = management.get("post", "Руководитель")
            founders = data.get("founders", [])
            if founders:
                for f in founders:
                    if f.get("inn") == inn or (f.get("fio", {}) and inn in str(f)):
                        role = "Учредитель"
                        break

            result.append({
                "name": name,
                "inn": data.get("inn", ""),
                "ogrn": data.get("ogrn", ""),
                "role": role or "Связь",
                "status": status,
            })

        return result

    except httpx.TimeoutException:
        logger.warning("DaData affiliated timeout for INN %s", inn)
        return []
    except Exception as e:
        logger.warning("DaData affiliated error: %s", e)
        return []
