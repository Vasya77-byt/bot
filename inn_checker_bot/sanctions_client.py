"""
Клиент для проверки по санкционным спискам.
Использует OpenSanctions API (бесплатный tier).
Требует API-ключ: https://opensanctions.org
"""

import logging
from typing import Any

import httpx

from config import OPENSANCTIONS_API_KEY

logger = logging.getLogger(__name__)

_API_URL = "https://api.opensanctions.org/match/default"
_TIMEOUT = 15.0


async def check_sanctions(inn: str, name: str = "") -> dict[str, Any]:
    """
    Проверяет ИНН/название по санкционным спискам OpenSanctions.

    Возвращает dict:
      {found: bool, matches: [{name, datasets, score}], source: str}
    """
    result: dict[str, Any] = {
        "source": "opensanctions.org",
        "found": False,
        "matches": [],
    }

    # Если API-ключ не настроен — возвращаем пустой результат
    if not OPENSANCTIONS_API_KEY:
        logger.debug("OpenSanctions API key not configured, skipping")
        return result

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"ApiKey {OPENSANCTIONS_API_KEY}",
    }

    # Ищем по ИНН и названию
    queries = []
    if inn:
        queries.append({"schema": "Company", "properties": {"innCode": [inn]}})
    if name:
        queries.append({"schema": "Company", "properties": {"name": [name]}})

    for query in queries:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(_API_URL, json=query, headers=headers)

                if r.status_code == 401 or r.status_code == 403:
                    logger.warning("OpenSanctions auth error (HTTP %s). Check API key.", r.status_code)
                    return result

                if r.status_code != 200:
                    logger.warning("OpenSanctions HTTP %s", r.status_code)
                    continue

                data = r.json()
                responses = data.get("responses", data.get("results", []))

                if isinstance(responses, dict):
                    results_list = responses.get("results", [])
                elif isinstance(responses, list):
                    results_list = responses
                else:
                    continue

                for match in results_list:
                    score = match.get("score", 0)
                    if score < 0.7:
                        continue

                    match_name = match.get("caption") or match.get("name", "?")
                    datasets = match.get("datasets", [])

                    result["matches"].append({
                        "name": match_name,
                        "datasets": datasets[:5] if isinstance(datasets, list) else [],
                        "score": round(score * 100),
                    })
                    result["found"] = True

        except httpx.TimeoutException:
            logger.warning("OpenSanctions timeout")
        except Exception as e:
            logger.warning("OpenSanctions error: %s", e)

    return result
