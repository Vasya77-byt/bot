"""
Парсинг ЗаЧестныйБизнес (zachestnyibiznes.ru) и Русрпофайл (rusprofile.ru).

Получаем:
  - Индекс ЧБ (надёжность: Низкий/Средний/Высокий → red/yellow/green)
  - Факты (зелёные/жёлтые/красные)
  - Выручка, прибыль, расходы
  - Штат сотрудников
  - Суды (арбитраж) — детально: истец/ответчик/кол-во/сумма
  - ФССП (приставы)
"""

import logging
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_TIMEOUT = 12.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}


def generate_links(inn: str, ogrn: str | None = None) -> dict[str, str]:
    links: dict[str, str] = {
        "rusprofile": f"https://www.rusprofile.ru/search?query={inn}",
    }
    if ogrn:
        links["zachestnyibiznes"] = f"https://zachestnyibiznes.ru/company/ul/{ogrn}_{inn}"
    return links


# ─────────────────────────────────────────────────────────────────────
#  ЗаЧестныйБизнес
# ─────────────────────────────────────────────────────────────────────

async def fetch_zsk_data(inn: str, ogrn: str | None = None) -> dict[str, Any]:
    """Парсит главную страницу компании на ЗСК."""
    result: dict[str, Any] = {"source": "zachestnyibiznes.ru"}
    if not ogrn:
        return result

    base_url = f"https://zachestnyibiznes.ru/company/ul/{ogrn}_{inn}"

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(base_url)
            if resp.status_code == 200:
                _parse_zsk_main(resp.text, result)
    except Exception as e:
        logger.warning("Ошибка ЗСК: %s", e)

    return result


def _parse_zsk_main(html: str, result: dict) -> None:
    soup = BeautifulSoup(html, "lxml")

    # ── 1. Индекс ЧБ (кнопка с href содержащим zchb_risk) ──
    risk_btn = soup.select_one('a[href*="zchb_risk"]')
    if risk_btn:
        btn_class = risk_btn.get("class", [])
        btn_classes = " ".join(btn_class) if isinstance(btn_class, list) else str(btn_class)

        if "btn-success" in btn_classes:
            result["reliability_color"] = "green"
        elif "btn-danger" in btn_classes:
            result["reliability_color"] = "red"
        else:
            result["reliability_color"] = "yellow"

        # Текст уровня: "Низкий" / "Средний" / "Высокий"
        level_span = risk_btn.select_one("span.hidden-xs")
        if level_span:
            result["reliability_label"] = level_span.get_text(strip=True)

        # Балл
        score_span = risk_btn.select_one("span.badge")
        if score_span:
            try:
                result["reliability_score"] = int(score_span.get_text(strip=True))
            except ValueError:
                pass

    # ── 2. Факты: зелёные / жёлтые / красные ──
    for css_class, key in [
        ("success-risk", "green_facts"),
        ("warning-risk", "yellow_facts"),
        ("danger-risk", "red_facts"),
    ]:
        for icon in soup.select(f"span.{css_class}.icon-xs"):
            parent = icon.parent
            if parent:
                num_match = re.search(r"(\d+)", parent.get_text())
                if num_match:
                    val = int(num_match.group(1))
                    if val < 1000:
                        result[key] = val
                        break

    # ── 3. Штат сотрудников ──
    emp_label = soup.find(string=re.compile(r"Среднесписочная\s*численность", re.I))
    if emp_label:
        # Ищем блок с данными рядом
        container = emp_label.find_parent("div", class_="row")
        if container:
            # Ищем в нескольких вариантах
            text_div = container.select_one("div.text-content")
            if text_div:
                text = text_div.get_text(strip=True)
                m = re.search(r"(\d[\d\s]*)", text)
                if m and "Не найдено" not in text:
                    num = m.group(1).replace(" ", "")
                    if num.isdigit():
                        result["employee_count"] = int(num)
            # Старый вариант — через span.m-r-7
            if "employee_count" not in result:
                spans = container.select("span.m-r-7")
                for sp in spans:
                    m = re.search(r"(\d+)\s*чел", sp.get_text())
                    if m:
                        result["employee_count"] = int(m.group(1))
                        break

    # ── 4. Финансы (выручка, прибыль, расходы с главной страницы) ──
    # Определяем тип по контексту (тексту рядом), а НЕ по порядку ссылок
    finance_links = soup.select('a.no-underline.pjax.c-black[href*="/balance"]')
    for link in finance_links:
        txt = link.get_text(strip=True)
        parsed = _parse_amount(txt)
        if parsed is None:
            continue
        # Определяем тип по тексту в родительском контейнере
        parent = link.find_parent("div", class_="row") or link.find_parent("tr") or link.parent
        context = (parent.get_text(" ", strip=True).lower() if parent else "").replace(txt.lower(), "")
        if any(kw in context for kw in ("доход", "выручк")):
            result["revenue"] = parsed
        elif "прибыль" in context or "убыт" in context:
            result["net_profit"] = parsed
        elif "расход" in context:
            result["expense"] = parsed
        # Если контекст не определён — НЕ записываем (безопаснее пропустить)

    # ── 5. ФССП ──
    fssp_link = soup.select_one('a[href*="/fssp"].c-black')
    if fssp_link:
        fssp_text = fssp_link.get_text(" ", strip=True)
        count_match = re.match(r"(\d+)", fssp_text)
        if count_match:
            result["fssp_total"] = int(count_match.group(1))
        sum_match = re.search(r"на\s*сумму\s*([\d\s,.]+)\s*₽", fssp_text)
        if sum_match:
            result["fssp_sum"] = _parse_amount(sum_match.group(1) + " ₽")

    # ── 6. Суды — ДЕТАЛЬНЫЙ ПАРСИНГ ──
    _parse_zsk_courts(soup, result)


def _parse_zsk_courts(soup: BeautifulSoup, result: dict) -> None:
    """
    Парсит детальные данные по судам из tpanel-блока на главной странице ЗСК.
    Извлекает: кол-во дел (истец/ответчик), общая сумма, статус.
    """
    # Ищем текст с разбивкой: '585 (в качестве "Ответчика"), 411 (в качестве "Истца")'
    full_text = soup.get_text(" ", strip=True)

    # Ответчик/Истец из текста вида: N (в качестве "Ответчика"), M (в качестве "Истца")
    defendant_match = re.search(r"(\d[\d\s]*)\s*\(в качестве\s*[«\"]?Ответчик", full_text)
    plaintiff_match = re.search(r"(\d[\d\s]*)\s*\(в качестве\s*[«\"]?Истц", full_text)

    if defendant_match:
        result["courts_defendant"] = int(defendant_match.group(1).replace(" ", ""))
    if plaintiff_match:
        result["courts_plaintiff"] = int(plaintiff_match.group(1).replace(" ", ""))

    # Общее количество дел из tpanel
    for tpanel in soup.select("div.tpanel"):
        panel_text = tpanel.get_text(" ", strip=True)
        if "судебн" in panel_text.lower():
            # Количество дел: "1 482 судебных дела"
            total_match = re.search(r"([\d\s]+)\s*судебн", panel_text)
            if total_match:
                num = total_match.group(1).strip().replace(" ", "").replace("\u00a0", "")
                if num.isdigit():
                    result["courts_total"] = int(num)

            # Общая сумма: "Общая сумма 3 трлн ₽"
            sum_match = re.search(r"Общая\s*сумма\s*([\d\s,.]+\s*(?:трлн|млрд|млн|тыс)?\.?\s*₽)", panel_text)
            if sum_match:
                result["courts_sum"] = _parse_amount(sum_match.group(1))

            # Рассматривается сумма: "Рассматривается 4.5 млрд ₽"
            active_match = re.search(r"Рассматривается\s*([\d\s,.]+\s*(?:трлн|млрд|млн|тыс)?\.?\s*₽)", panel_text)
            if active_match:
                result["courts_active_sum"] = _parse_amount(active_match.group(1))

            break

    # Если не нашли courts_total но нашли defendant/plaintiff — суммируем
    if "courts_total" not in result:
        d = result.get("courts_defendant", 0)
        p = result.get("courts_plaintiff", 0)
        if d or p:
            result["courts_total"] = d + p

    # Fallback: старый способ — ищем в tpanel "Ответчик N, Истец M"
    if "courts_total" not in result:
        arb_link = soup.select_one('a[href*="/arbitration"]')
        if arb_link:
            court_panel = arb_link.find_parent("div", class_="tpanel")
            if court_panel:
                panel_text = court_panel.get_text(" ", strip=True)
                if "Не найдено" in panel_text or "не найдено" in panel_text:
                    result["courts_total"] = 0
                else:
                    total = 0
                    for role in ["Ответчик", "Истец", "Третье лицо"]:
                        m = re.search(rf"{role}\s+(\d+)", panel_text)
                        if m:
                            total += int(m.group(1))
                    result["courts_total"] = total


def _parse_amount(text: str) -> float | None:
    """Парсит '802.6 млн ₽' / '26.6 тыс ₽' / '3 трлн ₽' → float."""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"([\d\s,.]+)\s*(трлн|млрд|млн|тыс)?\.?\s*₽?", text, re.I)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        unit = (m.group(2) or "").lower()
        if "трлн" in unit:
            num *= 1_000_000_000_000
        elif "млрд" in unit:
            num *= 1_000_000_000
        elif "млн" in unit:
            num *= 1_000_000
        elif "тыс" in unit:
            num *= 1_000
        return num
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────
#  Русрпофайл (запасной источник надёжности)
# ─────────────────────────────────────────────────────────────────────

async def fetch_rusprofile_data(inn: str) -> dict[str, Any]:
    result: dict[str, Any] = {"source": "rusprofile.ru"}
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(f"https://www.rusprofile.ru/search?query={inn}")
            if resp.status_code == 200:
                _parse_rusprofile(resp.text, result)
    except Exception as e:
        logger.warning("Ошибка Русрпофайл: %s", e)
    return result


def _parse_rusprofile(html: str, result: dict) -> None:
    soup = BeautifulSoup(html, "lxml")

    tile = soup.select_one(".reliability-tile h2[data-label]")
    if tile:
        label = tile.get("data-label", "").strip()
        label_type = tile.get("data-label-type", "").strip()

        if label_type == "positive" or "высок" in label.lower():
            result["reliability_color"] = "green"
        elif label_type == "negative" or "низк" in label.lower():
            result["reliability_color"] = "red"
        else:
            result["reliability_color"] = "yellow"
        result["reliability_label"] = label

    pos = soup.select(".reliability-tile .bg-positive")
    warn = soup.select(".reliability-tile .bg-warning")
    if pos:
        m = re.search(r"(\d+)", pos[0].get_text())
        if m:
            result["positive_facts"] = int(m.group(1))
    if warn:
        m = re.search(r"(\d+)", warn[0].get_text())
        if m:
            result["warning_facts"] = int(m.group(1))
