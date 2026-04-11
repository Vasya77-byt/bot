"""
Клиент ЦБ РФ — проверка по реестру предупреждений ЦБ.

Источники:
1. API ЦБ warning-list (нелегальная деятельность) — бесплатный JSON
2. Данные ЗСК ЦБ через API-FNS (уже подключён отдельно)
3. Данные о стоп-листах через ЗЧБ API (уже подключён отдельно)

Реестр 550-П/639-П отказов банков НЕ доступен публично —
эти данные получают только банки через закрытый канал ЦБ.
Для проверки отказов используем ЗЧБ API (поле bad_supplier, terrorist).
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
# API ЦБ — список организаций с нелегальной деятельностью (JSON)
_WARNING_LIST_URL = "https://www.cbr.ru/api/warning-list"


async def check_bank_refusals(inn: str) -> dict[str, Any]:
    """
    Проверяет ИНН по реестру предупреждений ЦБ РФ (нелегальная деятельность).

    Возвращает:
        {source, found: bool, count: int, details: [{name, type, date}]}
    """
    result: dict[str, Any] = {"source": "cbr.ru/warning-list"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            # Запрашиваем весь реестр и ищем по ИНН
            resp = await client.get(_WARNING_LIST_URL)

            if resp.status_code != 200:
                logger.warning("CBR warning-list HTTP %s", resp.status_code)
                # Фоллбэк: пробуем альтернативный URL
                resp = await client.get(
                    "https://www.cbr.ru/vfs/finmarkets/files/supervision/list_warning.json"
                )
                if resp.status_code != 200:
                    logger.warning("CBR warning-list fallback HTTP %s", resp.status_code)
                    result["found"] = False
                    result["count"] = 0
                    return result

            data = resp.json()
            entries = data if isinstance(data, list) else data.get("list", data.get("items", []))

            if not isinstance(entries, list):
                result["found"] = False
                result["count"] = 0
                return result

            # Ищем по ИНН в списке
            matches = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_inn = str(entry.get("inn", "") or entry.get("ИНН", ""))
                if entry_inn == inn:
                    matches.append({
                        "name": entry.get("name", entry.get("НаимЮЛ", "")),
                        "type": entry.get("type", entry.get("Тип", "")),
                        "date": entry.get("date", entry.get("Дата", "")),
                    })

            result["found"] = len(matches) > 0
            result["count"] = len(matches)
            if matches:
                result["details"] = matches

    except httpx.TimeoutException:
        logger.warning("CBR warning-list timeout for INN %s", inn)
        result["found"] = False
        result["count"] = 0
    except Exception as e:
        logger.warning("CBR warning-list error for INN %s: %s", inn, e)
        result["found"] = False
        result["count"] = 0

    return result
