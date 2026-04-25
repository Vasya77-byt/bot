"""Клиент для получения данных о компании с rusprofile.ru (веб-скрейпинг).

Rusprofile не имеет официального API, поэтому используется парсинг HTML.
Если сайт вернёт 403/429 — клиент вернёт None, бот продолжит работу
на данных из DaData и ФНС.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from schemas import CompanyData

logger = logging.getLogger("financial-architect")

_SEARCH_URL = "https://www.rusprofile.ru/search"
_BASE_URL = "https://www.rusprofile.ru"
_TIMEOUT = 20.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _clean_num(text: str) -> Optional[int]:
    """Извлекает число из строки вида '1 234 567 тыс. руб.'"""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _text(soup: BeautifulSoup, *selectors: str) -> Optional[str]:
    """Возвращает текст первого найденного элемента."""
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return None


def _parse_page(html: str, inn: str) -> Optional[CompanyData]:
    soup = BeautifulSoup(html, "lxml")

    # Название компании
    name = _text(
        soup,
        "h1.company-name",
        "h1[itemprop='name']",
        ".company__name h1",
        "h1",
    )
    if not name:
        return None

    def field(label: str) -> Optional[str]:
        """Ищет значение поля по тексту метки в таблице реквизитов."""
        for row in soup.select(".requisites-row, .company-info__row, dl.requisites dt"):
            if label.lower() in row.get_text().lower():
                # Берём следующий sibling или dd
                sibling = row.find_next_sibling()
                val_el = (
                    sibling
                    or row.select_one("dd, .company-info__text, .requisites-row__value")
                )
                if val_el:
                    return val_el.get_text(" ", strip=True) or None
        # Fallback: data-атрибуты
        mapping = {
            "огрн": "[data-field='PSRN'], [data-field='ogrn']",
            "кпп": "[data-field='KPP'], [data-field='kpp']",
            "адрес": "[data-field='address']",
            "дата регистрации": "[data-field='registration_date']",
            "руководитель": "[data-field='director'], [data-field='ceo']",
            "оквэд": "[data-field='okved']",
            "статус": ".company-status, .company__status, [data-field='status']",
        }
        sel = mapping.get(label.lower())
        if sel:
            return _text(soup, *sel.split(", "))
        return None

    ogrn = field("огрн")
    kpp = field("кпп")
    address = field("адрес")
    reg_date = field("дата регистрации")
    director = field("руководитель")
    status = field("статус") or _text(soup, ".company-status", ".status-label")

    # ОКВЭД
    okved_raw = field("оквэд")
    okved_code: Optional[str] = None
    okved_name: Optional[str] = None
    if okved_raw:
        m = re.match(r"^([\d.]+)\s*(.*)", okved_raw)
        if m:
            okved_code = m.group(1)
            okved_name = m.group(2) or None
        else:
            okved_code = okved_raw

    # Сотрудники
    employees: Optional[int] = None
    emp_text = field("сотрудник") or field("численность")
    if emp_text:
        employees = _clean_num(emp_text)

    # Финансы: ищем в таблице финансовых показателей
    revenue: Optional[int] = None
    profit: Optional[int] = None
    for row in soup.select(
        ".financial-index__item, .finance-table tr, .company-finance__row"
    ):
        row_text = row.get_text(" ", strip=True).lower()
        val_el = row.select_one(
            ".financial-index__value, td:last-child, .company-finance__value"
        )
        if not val_el:
            continue
        val = _clean_num(val_el.get_text())
        if val is None:
            continue
        if "выручка" in row_text and revenue is None:
            revenue = val * 1000  # тыс. руб. → руб.
        elif "прибыль" in row_text and profit is None:
            profit = val * 1000

    return CompanyData(
        inn=inn,
        name=name,
        ogrn=ogrn,
        kpp=kpp,
        address=address,
        reg_date=reg_date,
        director=director,
        status=status,
        okved_main=okved_code,
        okved_name=okved_name,
        employees_count=employees,
        revenue_last_year=revenue,
        profit_last_year=profit,
        source="rusprofile",
    )


class RusprofileClient:
    def __init__(self, timeout: float = _TIMEOUT) -> None:
        self.timeout = timeout

    async def fetch_company(self, inn: str) -> Optional[CompanyData]:
        try:
            company_url = await self._find_url(inn)
            if not company_url:
                logger.info("Rusprofile: company not found for INN %s", inn)
                return None
            html = await self._get_html(company_url)
            if not html:
                return None
            result = _parse_page(html, inn)
            if result:
                logger.info("Rusprofile: got data for INN %s", inn)
            return result
        except Exception as exc:
            logger.warning("Rusprofile error for INN %s: %s", inn, exc)
            return None

    async def _find_url(self, inn: str) -> Optional[str]:
        entity_type = "ip" if len(inn) == 12 else "ul"
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=_HEADERS
        ) as client:
            resp = await client.get(
                _SEARCH_URL, params={"query": inn, "type": entity_type}
            )
        if resp.status_code != 200:
            logger.warning("Rusprofile search HTTP %s for INN %s", resp.status_code, inn)
            return None

        final_url = str(resp.url)
        # Если редирект сразу на страницу компании
        if re.search(r"/id/\d+|/egr/\d+", final_url):
            return final_url

        soup = BeautifulSoup(resp.text, "lxml")
        link = soup.select_one(
            "a.company-name__link, .search-result a[href*='/id/'], "
            ".company-item a, .search-list a[href*='/id/']"
        )
        if link and link.get("href"):
            href = str(link["href"])
            return href if href.startswith("http") else f"{_BASE_URL}{href}"
        return None

    async def _get_html(self, url: str) -> Optional[str]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=_HEADERS
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning("Rusprofile page HTTP %s: %s", resp.status_code, url)
            return None
        return resp.text
