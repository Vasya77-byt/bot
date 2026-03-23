"""
Клиент для получения данных о госзакупках.
Пробует несколько источников:
1. clearspending.ru
2. budgetapps.ru
3. Если оба не работают — возвращает None (graceful degradation).
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


async def fetch_contracts(inn: str) -> dict[str, Any] | None:
    """
    Получает данные о госзакупках компании по ИНН.

    Возвращает dict:
      {total_count, total_sum, contracts: [{date, amount, subject, customer}, ...]}
    или None если данные недоступны.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Источник 1: ClearSpending
            result = await _fetch_clearspending(inn, client)
            if result:
                return result

            # Источник 2: BudgetApps
            result = await _fetch_budgetapps(inn, client)
            if result:
                return result

    except Exception as e:
        logger.warning("Goscontract error for INN %s: %s", inn, e)

    return None


async def _fetch_clearspending(inn: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Пробуем ClearSpending API."""
    try:
        r = await client.get(
            "https://clearspending.ru/api/v1/contracts/select",
            params={
                "supplierinn": inn,
                "perpage": 10,
                "sort": "-signDate",
            },
        )
        if r.status_code != 200:
            logger.debug("ClearSpending HTTP %s for INN %s", r.status_code, inn)
            return None

        data = r.json()
        contracts_data = data.get("contracts", {})
        total = contracts_data.get("total", 0)

        contracts_list = []
        for c in contracts_data.get("data", [])[:10]:
            contracts_list.append({
                "date": c.get("signDate", ""),
                "amount": _safe_float(c.get("price")),
                "subject": c.get("subject", ""),
                "customer": c.get("customer", {}).get("fullName", ""),
            })

        if total == 0 and not contracts_list:
            return None

        total_sum = sum(c.get("amount") or 0 for c in contracts_list)

        return {
            "total_count": total,
            "total_sum": total_sum,
            "contracts": contracts_list,
        }
    except httpx.TimeoutException:
        logger.debug("ClearSpending timeout for INN %s", inn)
        return None
    except Exception as e:
        logger.debug("ClearSpending error: %s", e)
        return None


async def _fetch_budgetapps(inn: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Фоллбэк через budgetapps.ru API."""
    try:
        r = await client.get(
            "https://budgetapps.ru/api/v1/contracts",
            params={"inn": inn, "limit": 10},
        )
        if r.status_code != 200:
            return None

        data = r.json()
        items = data.get("items", data.get("data", []))
        if not items:
            return None

        contracts_list = []
        for c in items[:10]:
            contracts_list.append({
                "date": c.get("date", c.get("signDate", "")),
                "amount": _safe_float(c.get("amount", c.get("price"))),
                "subject": c.get("subject", c.get("name", "")),
                "customer": c.get("customer", ""),
            })

        return {
            "total_count": data.get("total", len(contracts_list)),
            "total_sum": sum(c.get("amount") or 0 for c in contracts_list),
            "contracts": contracts_list,
        }
    except Exception:
        return None


def _safe_float(val) -> float | None:
    """Безопасное преобразование в float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
