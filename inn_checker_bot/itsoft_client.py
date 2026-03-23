"""
Клиент egrul.itsoft.ru — финансовые данные за несколько лет.

Бесплатный API, лимит ~100 запросов/сутки.
Возвращает доходы (income) и расходы (outcome) по годам.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_BASE_URL = "https://egrul.itsoft.ru/fin/"


async def fetch_finance_history(inn: str) -> dict[str, Any]:
    """
    Получает финансовые данные за все доступные годы.

    Возвращает dict:
        years: list of {year, income, outcome}  (отсортирован от нового к старому)
        latest_income: float | None
        latest_outcome: float | None
        trend: "up" | "down" | "stable" | None  (тренд дохода за последние 3 года)
    """
    result: dict[str, Any] = {"source": "egrul.itsoft.ru", "years": []}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(_BASE_URL, params={"inn": inn})
            if resp.status_code != 200:
                logger.warning("itsoft HTTP %s for INN %s", resp.status_code, inn)
                return result

            data = resp.json()

    except (httpx.TimeoutException, httpx.ConnectError):
        logger.warning("itsoft timeout for INN %s", inn)
        return result
    except Exception as e:
        logger.warning("itsoft error for INN %s: %s", inn, e)
        return result

    if not isinstance(data, dict):
        return result

    # Собираем данные по годам
    years_data: list[dict[str, Any]] = []
    for year_str, values in data.items():
        if not year_str.isdigit():
            continue
        year = int(year_str)
        if not isinstance(values, dict):
            continue

        income_raw = values.get("income")
        outcome_raw = values.get("outcome")

        income = _to_float(income_raw)
        outcome = _to_float(outcome_raw)

        # Пропускаем годы без данных
        if income is None and outcome is None:
            continue
        # Пропускаем нулевые годы
        if (income is None or income == 0) and (outcome is None or outcome == 0):
            continue

        years_data.append({
            "year": year,
            "income": income,
            "outcome": outcome,
        })

    # Сортируем от нового к старому
    years_data.sort(key=lambda x: x["year"], reverse=True)
    result["years"] = years_data

    if years_data:
        result["latest_income"] = years_data[0].get("income")
        result["latest_outcome"] = years_data[0].get("outcome")

    # Считаем тренд за последние 3 года
    result["trend"] = _calc_trend(years_data)

    return result


def _to_float(val: Any) -> float | None:
    """Безопасно конвертирует строку/число в float."""
    if val is None:
        return None
    try:
        f = float(str(val).replace(" ", "").replace(",", "."))
        return f
    except (ValueError, TypeError):
        return None


def _calc_trend(years_data: list[dict]) -> str | None:
    """
    Определяет тренд дохода: up / down / stable.
    Берёт 3 последних года с ненулевым доходом.
    """
    incomes = []
    for yd in years_data[:3]:
        inc = yd.get("income")
        if inc is not None and inc > 0:
            incomes.append(inc)

    if len(incomes) < 2:
        return None

    # Сравниваем последний год с предпоследним
    latest = incomes[0]
    prev = incomes[1]

    if prev == 0:
        return "up" if latest > 0 else None

    change = (latest - prev) / prev

    if change > 0.1:
        return "up"
    elif change < -0.1:
        return "down"
    else:
        return "stable"
