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
    name = _text(soup, "h1.company-name", "h1[itemprop='name']", ".company__name h1", "h1")
    if not name:
        return None

    # Строим словарь: метка → значение из пар .company-info__title / следующий элемент
    info: dict[str, str] = {}
    for title_el in soup.select(".company-info__title"):
        label = title_el.get_text(strip=True).lower()
        # Значение — следующий sibling или ближайший .company-info__text в родителе
        val_el = title_el.find_next_sibling()
        if val_el is None:
            parent = title_el.parent
            if parent:
                val_el = parent.find(class_="company-info__text")
        if val_el:
            val = val_el.get_text(" ", strip=True)
            if val:
                info[label] = val

    ogrn = info.get("огрн")
    reg_date = info.get("дата регистрации")
    capital_raw = info.get("уставный капитал")
    capital: Optional[float] = float(_clean_num(capital_raw)) if capital_raw and _clean_num(capital_raw) else None
    address = info.get("юридический адрес")
    director_raw = info.get("руководитель", "")
    status = _text(soup, ".company-status", ".status-label", ".company__status")

    # Из "Председатель правления Акимов Андрей Игоревич с 27 февраля 2003 г."
    # убираем должность и дату — оставляем только ФИО
    director: Optional[str] = None
    if director_raw:
        m = re.search(r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)", director_raw)
        director = m.group(1) if m else director_raw.split(" с ")[0].strip()

    # ИНН/КПП — в одной ячейке "7744001497 / 774401001"
    inn_kpp = info.get("инн/кпп", "")
    kpp: Optional[str] = None
    if "/" in inn_kpp:
        kpp = inn_kpp.split("/")[-1].strip()

    # ОКВЭД — "Денежное посредничество прочее (64.19)"
    okved_raw = info.get("основной вид деятельности", "")
    okved_code: Optional[str] = None
    okved_name: Optional[str] = None
    if okved_raw:
        m = re.search(r"\((\d[\d.]+)\)", okved_raw)
        if m:
            okved_code = m.group(1)
            okved_name = okved_raw[:okved_raw.rfind("(")].strip() or None
        else:
            okved_name = okved_raw

    # Сотрудники
    employees: Optional[int] = None
    emp_raw = info.get("среднесписочная численность", "")
    if emp_raw and emp_raw != "нет данных":
        employees = _clean_num(emp_raw)

    # Финансы из таблицы
    revenue: Optional[int] = None
    profit: Optional[int] = None
    for row in soup.select("table tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        label_text = cells[0].get_text(strip=True).lower()
        val_text = cells[-1].get_text(strip=True)
        val = _clean_num(val_text)
        if val and "выручка" in label_text and revenue is None:
            revenue = val * 1000
        elif val and "прибыль" in label_text and profit is None:
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
        capital=capital,
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
            if not result:
                return None
            logger.info("Rusprofile: got data for INN %s", inn)

            # Подгружаем страницу надёжности
            company_id = re.search(r"/id/(\d+)", company_url)
            if company_id:
                rel = await self._fetch_reliability(company_id.group(1))
                if rel:
                    result = result.model_copy(update=rel)
            return result
        except Exception as exc:
            logger.warning("Rusprofile error for INN %s: %s", inn, exc)
            return None

    async def _fetch_reliability(self, company_id: str) -> Optional[dict]:
        """Парсит страницу надёжности и возвращает dict с полями reliability_*."""
        url = f"{_BASE_URL}/reliability/{company_id}"
        html = await self._get_html(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")

        facts: dict[str, str] = {}
        for el in soup.select(".facts__title"):
            spans = el.select("span")
            if len(spans) >= 2:
                label = spans[0].get_text(strip=True).rstrip(":").lower()
                value = spans[1].get_text(strip=True).capitalize()
                facts[label] = value

        rating_el = soup.select_one(".content-frame__title .rating")
        rating = rating_el.get_text(strip=True) if rating_el else None

        if not facts and not rating:
            return None

        return {
            "reliability_rating": rating,
            "reliability_obligations": facts.get("риски неисполнения обязательств"),
            "reliability_shell": facts.get("признаки однодневки"),
            "reliability_tax": facts.get("налоговые риски"),
        }

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
