"""Клиент Rusprofile.ru — суды, госконтракты, учредители через веб-скрапинг.

Rusprofile не имеет публичного API, данные получаются парсингом HTML.
Все ошибки обрабатываются gracefully — отсутствие данных не блокирует основной запрос.
"""

import asyncio
import logging
import re
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup, Tag

from schemas import CompanyData

logger = logging.getLogger("financial-architect.rusprofile")

_INN_URL = "https://www.rusprofile.ru/inn/{inn}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}


class RusprofileClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def fetch_company(self, inn: str) -> Optional[CompanyData]:
        """Получить дополнительные данные о компании с Rusprofile.ru."""
        try:
            html, final_url = await self._get(inn)
        except Exception as exc:
            logger.warning("Rusprofile request failed for INN %s: %s", inn, exc)
            return None
        if not html:
            return None
        return _parse(html, inn)

    async def _get(self, inn: str) -> tuple[str, str]:
        url = _INN_URL.format(inn=inn)
        async with aiohttp.ClientSession(
            timeout=self._timeout, headers=_HEADERS
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 404:
                    logger.info("Rusprofile: company not found for INN %s", inn)
                    return "", str(resp.url)
                if resp.status != 200:
                    logger.warning(
                        "Rusprofile returned HTTP %s for INN %s", resp.status, inn
                    )
                    return "", str(resp.url)
                return await resp.text(), str(resp.url)


# ── HTML parsing ────────────────────────────────────────────────────────────

def _parse(html: str, inn: str) -> Optional[CompanyData]:
    try:
        soup = BeautifulSoup(html, "lxml")
        return CompanyData(
            inn=inn,
            courts_plaintiff=_courts_plaintiff(soup),
            courts_defendant=_courts_defendant(soup),
            courts_total=_courts_total(soup),
            gov_contracts_count=_gov_contracts_count(soup),
            gov_contracts_amount=_gov_contracts_amount(soup),
            founders=_founders(soup),
            source="rusprofile",
        )
    except Exception as exc:
        logger.warning("Rusprofile parse error for INN %s: %s", inn, exc)
        return None


def _text_int(tag: Optional[Tag]) -> Optional[int]:
    """Извлечь целое число из текста тега."""
    if tag is None:
        return None
    raw = re.sub(r"[^\d]", "", tag.get_text())
    return int(raw) if raw else None


def _text_float(tag: Optional[Tag]) -> Optional[float]:
    """Извлечь число (возможно с пробелами-разделителями тысяч) из текста тега."""
    if tag is None:
        return None
    raw = re.sub(r"[^\d,.]", "", tag.get_text().replace(" ", "").replace("\xa0", ""))
    raw = raw.replace(",", ".")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _courts_plaintiff(soup: BeautifulSoup) -> Optional[int]:
    """Количество арбитражных дел, где компания — истец."""
    # <span class="courts-plaintiff-count">…</span> или data-атрибут
    tag = (
        soup.find("span", class_=re.compile(r"plaintiff", re.I))
        or soup.find(attrs={"data-courts-plaintiff": True})
        or _find_by_label(soup, r"истец")
    )
    return _text_int(tag)


def _courts_defendant(soup: BeautifulSoup) -> Optional[int]:
    """Количество арбитражных дел, где компания — ответчик."""
    tag = (
        soup.find("span", class_=re.compile(r"defendant", re.I))
        or soup.find(attrs={"data-courts-defendant": True})
        or _find_by_label(soup, r"ответчик")
    )
    return _text_int(tag)


def _courts_total(soup: BeautifulSoup) -> Optional[int]:
    """Суммарное количество арбитражных дел."""
    # Пробуем найти сводный счётчик
    tag = (
        soup.find("span", class_=re.compile(r"courts.?total|arb.?count", re.I))
        or soup.find(attrs={"data-courts-total": True})
        or _find_section_count(soup, r"арбитраж|суд")
    )
    result = _text_int(tag)
    # Если явного total нет — суммируем plaintiff + defendant
    if result is None:
        p = _courts_plaintiff(soup)
        d = _courts_defendant(soup)
        if p is not None or d is not None:
            result = (p or 0) + (d or 0)
    return result


def _gov_contracts_count(soup: BeautifulSoup) -> Optional[int]:
    """Количество государственных контрактов."""
    tag = (
        soup.find("span", class_=re.compile(r"contract.?count|gos.?kontrakt", re.I))
        or soup.find(attrs={"data-contracts-count": True})
        or _find_section_count(soup, r"госконтракт|контракт|44-фз|223-фз")
    )
    return _text_int(tag)


def _gov_contracts_amount(soup: BeautifulSoup) -> Optional[float]:
    """Суммарная стоимость госконтрактов в рублях."""
    tag = (
        soup.find("span", class_=re.compile(r"contract.?sum|contract.?amount", re.I))
        or soup.find(attrs={"data-contracts-amount": True})
        or _find_section_amount(soup, r"госконтракт|контракт")
    )
    val = _text_float(tag)
    # Если сумма выражена в тысячах/млн — нормализуем при необходимости
    return val


def _founders(soup: BeautifulSoup) -> Optional[List[str]]:
    """Список учредителей (названия/ФИО)."""
    results: List[str] = []

    # Ищем блок с учредителями
    founders_block = (
        soup.find(class_=re.compile(r"founder|uchreditel", re.I))
        or _find_section_block(soup, r"учредител")
    )
    if not founders_block:
        return None

    items = founders_block.find_all(
        class_=re.compile(r"founder.?item|founder.?name|uchreditel.?item", re.I)
    )
    if not items:
        # Просто все ссылки/строки внутри блока
        items = founders_block.find_all(["a", "span", "li"])

    for item in items[:10]:
        text = item.get_text(strip=True)
        if text and len(text) > 2:
            results.append(text)

    return results if results else None


# ── Вспомогательные хелперы ────────────────────────────────────────────────

def _find_by_label(soup: BeautifulSoup, label_pattern: str) -> Optional[Tag]:
    """Найти числовой тег, следующий за меткой с нужным текстом."""
    pattern = re.compile(label_pattern, re.I)
    for label in soup.find_all(string=pattern):
        parent = label.parent
        if parent is None:
            continue
        # Ищем число в соседнем теге
        sibling = parent.find_next_sibling()
        if sibling and re.search(r"\d", sibling.get_text()):
            return sibling
        # Или числовой span внутри родителя
        num = parent.find(string=re.compile(r"^\s*\d[\d\s]*$"))
        if num:
            return num.parent
    return None


def _find_section_count(soup: BeautifulSoup, section_pattern: str) -> Optional[Tag]:
    """Найти счётчик в секции с нужным заголовком."""
    pattern = re.compile(section_pattern, re.I)
    for heading in soup.find_all(["h2", "h3", "h4", "div"], string=pattern):
        section = heading.find_parent(["section", "div", "article"])
        if not section:
            continue
        count_tag = section.find(class_=re.compile(r"count|num|badge|total", re.I))
        if count_tag:
            return count_tag
    return None


def _find_section_amount(soup: BeautifulSoup, section_pattern: str) -> Optional[Tag]:
    """Найти сумму в секции с нужным заголовком."""
    pattern = re.compile(section_pattern, re.I)
    for heading in soup.find_all(["h2", "h3", "h4", "div"], string=pattern):
        section = heading.find_parent(["section", "div", "article"])
        if not section:
            continue
        amount_tag = section.find(class_=re.compile(r"sum|amount|price|total", re.I))
        if amount_tag:
            return amount_tag
    return None


def _find_section_block(soup: BeautifulSoup, section_pattern: str) -> Optional[Tag]:
    """Найти блок-контейнер секции с нужным заголовком."""
    pattern = re.compile(section_pattern, re.I)
    for heading in soup.find_all(["h2", "h3", "h4"], string=pattern):
        block = heading.find_parent(["section", "div", "article"])
        if block:
            return block
    return None
