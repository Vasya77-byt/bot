"""
Поиск связанных компаний через учредителей и руководителя.

Алгоритм:
1. Берём ИНН директора и имена учредителей из данных проверяемой компании
2. Для каждого лица ищем компании через DaData suggest (бесплатный API)
3. Строим граф связей: Company A → директор → Company B, C
"""

import asyncio
import logging
from typing import Any

import httpx

from config import DADATA_API_KEY

logger = logging.getLogger(__name__)

_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
_FIND_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
_TIMEOUT = 12.0


async def fetch_affiliated(
    inn: str,
    fields: dict[str, Any] | None = None,
    zchb_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Ищет связанные компании через учредителей и директора.
    Использует бесплатные API DaData (suggest + findById).

    Возвращает список: [{name, inn, ogrn, role, status, connection}, ...]
    """
    if not DADATA_API_KEY:
        return []

    fields = fields or {}
    zchb = zchb_data or {}
    result: list[dict[str, Any]] = []
    seen_inns = {inn}  # Исключаем саму проверяемую компанию

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {DADATA_API_KEY}",
    }

    # Собираем лица для поиска
    persons: list[dict[str, str]] = []

    # Директор
    mgr_name = fields.get("management_name")
    mgr_inn = zchb.get("director_inn")
    if mgr_name:
        persons.append({
            "name": mgr_name,
            "inn": mgr_inn or "",
            "role": fields.get("management_post") or "Руководитель",
        })

    # Учредители
    founders = fields.get("founders") or []
    for f in founders:
        fname = f.get("name", "")
        if fname and fname != mgr_name:
            persons.append({"name": fname, "inn": "", "role": "Учредитель"})

    if not persons:
        return []

    # Ищем компании для каждого лица
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = []
        for person in persons[:5]:  # Макс 5 лиц
            tasks.append(_search_by_person(client, headers, person, seen_inns))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, list):
                for company in r:
                    if company["inn"] not in seen_inns:
                        seen_inns.add(company["inn"])
                        result.append(company)

    # Сортируем: действующие первыми
    result.sort(key=lambda x: (0 if x["status"] == "ACTIVE" else 1, x["name"]))
    return result[:20]  # Макс 20 связей


async def _search_by_person(
    client: httpx.AsyncClient,
    headers: dict,
    person: dict[str, str],
    seen_inns: set,
) -> list[dict[str, Any]]:
    """Ищет компании где лицо является директором или учредителем."""
    results: list[dict[str, Any]] = []
    name = person["name"]
    role = person["role"]

    try:
        # Поиск по ФИО через DaData suggest
        resp = await client.post(
            _SUGGEST_URL,
            json={"query": name, "count": 10},
            headers=headers,
        )
        if resp.status_code != 200:
            return []

        body = resp.json()
        for s in body.get("suggestions", []):
            data = s.get("data", {})
            company_inn = data.get("inn", "")
            if not company_inn or company_inn in seen_inns:
                continue

            # Проверяем что лицо действительно связано с компанией
            mgmt = data.get("management") or {}
            founders = data.get("founders") or []
            mgmt_name = (mgmt.get("name") or "").upper()
            is_director = name.upper() in mgmt_name or mgmt_name in name.upper()
            is_founder = any(
                name.upper() in (f.get("name") or "").upper()
                for f in founders if isinstance(f, dict)
            )

            if not is_director and not is_founder:
                continue

            company_name = s.get("value") or ""
            status = (data.get("state") or {}).get("status", "")

            connection = f"{role}: {name}"
            company_role = "Руководитель" if is_director else "Учредитель"

            results.append({
                "name": company_name,
                "inn": company_inn,
                "ogrn": data.get("ogrn", ""),
                "role": company_role,
                "status": status,
                "connection": connection,
            })

    except Exception as e:
        logger.warning("Affiliated search error for %s: %s", name, e)

    return results
