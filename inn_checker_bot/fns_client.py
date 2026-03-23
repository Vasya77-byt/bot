"""
Клиент ФНС ЕГРЮЛ — получение выписки PDF.

API egrul.nalog.ru — бесплатный, без капчи.
Позволяет скачать официальную выписку из ЕГРЮЛ в PDF.
"""

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_BASE = "https://egrul.nalog.ru"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


async def get_egrul_pdf(inn: str) -> bytes | None:
    """
    Скачивает выписку ЕГРЮЛ в формате PDF.

    Возвращает bytes PDF или None если ошибка.
    Весь процесс занимает ~5 секунд.
    """
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS
        ) as client:
            # 1. Поиск по ИНН
            r1 = await client.post(_BASE + "/", data={"query": inn})
            if r1.status_code != 200:
                logger.warning("EGRUL search HTTP %s", r1.status_code)
                return None

            data1 = r1.json()
            if data1.get("captchaRequired"):
                logger.warning("EGRUL requires captcha")
                return None

            search_token = data1.get("t")
            if not search_token:
                logger.warning("EGRUL no search token")
                return None

            # 2. Получение результатов поиска (подождём)
            await asyncio.sleep(2)

            r2 = await client.get(f"{_BASE}/search-result/{search_token}")
            if r2.status_code != 200:
                logger.warning("EGRUL search-result HTTP %s", r2.status_code)
                return None

            data2 = r2.json()
            rows = data2.get("rows", [])
            if not rows:
                logger.warning("EGRUL no results for INN %s", inn)
                return None

            row_token = rows[0].get("t")
            if not row_token:
                logger.warning("EGRUL no row token")
                return None

            # 3. Запрос выписки
            r3 = await client.get(f"{_BASE}/vyp-request/{row_token}")
            if r3.status_code != 200:
                logger.warning("EGRUL vyp-request HTTP %s", r3.status_code)
                return None

            data3 = r3.json()
            if data3.get("captchaRequired"):
                logger.warning("EGRUL vyp requires captcha")
                return None

            vyp_token = data3.get("t")
            if not vyp_token:
                logger.warning("EGRUL no vyp token")
                return None

            # 4. Ждём готовности (до 10 секунд)
            for _ in range(5):
                await asyncio.sleep(2)
                r4 = await client.get(f"{_BASE}/vyp-status/{vyp_token}")
                if r4.status_code == 200:
                    status = r4.json().get("status")
                    if status == "ready":
                        break
            else:
                logger.warning("EGRUL vyp timeout for INN %s", inn)
                return None

            # 5. Скачиваем PDF
            r5 = await client.get(f"{_BASE}/vyp-download/{vyp_token}")
            if r5.status_code != 200:
                logger.warning("EGRUL download HTTP %s", r5.status_code)
                return None

            content_type = r5.headers.get("content-type", "")
            if "pdf" not in content_type:
                logger.warning("EGRUL unexpected content-type: %s", content_type)
                return None

            logger.info("EGRUL PDF downloaded for INN %s (%d bytes)", inn, len(r5.content))
            return r5.content

    except (httpx.TimeoutException, httpx.ConnectError) as e:
        logger.warning("EGRUL timeout for INN %s: %s", inn, e)
        return None
    except Exception as e:
        logger.warning("EGRUL error for INN %s: %s", inn, e)
        return None


def generate_fns_links(inn: str) -> dict[str, str]:
    """Генерирует ссылки на сервисы ФНС для ручной проверки."""
    return {
        "blocked_accounts": f"https://service.nalog.ru/bi.do",
        "mass_address": f"https://service.nalog.ru/addrfind.do",
        "disqualified": f"https://service.nalog.ru/disqualified.do",
        "egrul": f"https://egrul.nalog.ru/index.html",
    }
