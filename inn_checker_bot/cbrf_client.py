"""
Клиент ЦБ РФ — проверка по реестру отказов банков (550-П).

Парсит https://www.cbr.ru/ для определения, есть ли компания
в списке отказов в проведении операций.
Graceful degradation: если сайт недоступен → None.
"""

import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_SEARCH_URL = "https://www.cbr.ru/banking_sector/otkazano/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}


async def check_bank_refusals(inn: str) -> dict[str, Any]:
    """
    Проверяет ИНН по реестру отказов ЦБ (550-П).

    Возвращает:
        {source, found: bool, count: int, details: [{bank, date, reason}]}
    или {source} если данные недоступны.
    """
    result: dict[str, Any] = {"source": "cbr.ru"}

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
        ) as client:
            # POST-запрос с ИНН для поиска
            resp = await client.post(
                _SEARCH_URL,
                data={"inn": inn},
                headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            )

            if resp.status_code != 200:
                logger.warning("CBRF HTTP %s", resp.status_code)
                return result

            _parse_refusals(resp.text, inn, result)

    except httpx.TimeoutException:
        logger.warning("CBRF timeout for INN %s", inn)
    except Exception as e:
        logger.warning("CBRF error for INN %s: %s", inn, e)

    return result


def _parse_refusals(html: str, inn: str, result: dict) -> None:
    """Парсит результаты поиска отказов."""
    soup = BeautifulSoup(html, "lxml")

    # Ищем таблицу с результатами
    table = soup.select_one("table.data") or soup.select_one("table")
    if not table:
        # Нет таблицы — может, нет отказов или другой формат
        text = soup.get_text(" ", strip=True).lower()
        if "не найден" in text or "нет данных" in text or "отсутств" in text:
            result["found"] = False
            result["count"] = 0
            return
        # Пробуем найти по тексту страницы
        if inn in soup.get_text():
            result["found"] = True
            result["count"] = 1
        else:
            result["found"] = False
            result["count"] = 0
        return

    rows = table.select("tr")
    details = []
    for row in rows[1:]:  # Пропускаем заголовок
        cells = row.select("td")
        if len(cells) >= 2:
            row_text = row.get_text(" ", strip=True)
            if inn in row_text:
                detail: dict[str, str] = {}
                if len(cells) >= 1:
                    detail["bank"] = cells[0].get_text(strip=True)
                if len(cells) >= 2:
                    detail["date"] = cells[1].get_text(strip=True)
                if len(cells) >= 3:
                    detail["reason"] = cells[2].get_text(strip=True)
                details.append(detail)

    result["found"] = len(details) > 0
    result["count"] = len(details)
    result["details"] = details
