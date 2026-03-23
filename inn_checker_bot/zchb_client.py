"""
Клиент ЗаЧестныйБизнес API — точные данные вместо web scraping.

Методы:
  - card: основные сведения, финансы по годам, суды (сводка), закупки, капитал
  - court-arbitration: детальные судебные дела
  - fssp-list: исполнительные производства ФССП

Все данные — из официальных реестров через API ЗЧБ.
"""

import logging
from typing import Any

import httpx

from config import ZCHB_API_KEY

logger = logging.getLogger(__name__)

_BASE = "https://zachestnyibiznesapi.ru/paid/data"
_TIMEOUT = 20.0


async def _api_request(method: str, inn_or_ogrn: str) -> dict[str, Any] | None:
    """Общий запрос к API ЗЧБ."""
    if not ZCHB_API_KEY:
        return None

    url = f"{_BASE}/{method}"
    params = {"id": inn_or_ogrn, "api_key": ZCHB_API_KEY, "_format": "json"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params=params)

        if r.status_code != 200:
            logger.warning("ZChB API %s: HTTP %s", method, r.status_code)
            return None

        data = r.json()
        status = data.get("status")
        if str(status) not in ("200", "201"):
            logger.warning("ZChB API %s: status %s — %s", method, status, data.get("message", ""))
            return None

        docs = data.get("body", {}).get("docs", [])
        if not docs:
            return None

        return docs[0] if isinstance(docs, list) and docs else None

    except (httpx.TimeoutException, httpx.ConnectError) as e:
        logger.warning("ZChB API %s timeout: %s", method, e)
        return None
    except Exception as e:
        logger.warning("ZChB API %s error: %s", method, e)
        return None


# ─────────────────────────────────────────────────
# Основная карточка компании
# ─────────────────────────────────────────────────

async def fetch_company_card(inn: str) -> dict[str, Any]:
    """
    Получает полную карточку компании из ЗЧБ API.

    Возвращает структурированный dict:
      - capital, address, employees, courts, fssp, finances, licenses, etc.
    """
    result: dict[str, Any] = {"source": "zachestnyibiznes.ru/api"}

    raw = await _api_request("card", inn)
    if not raw:
        return result

    # ── Уставной капитал (точные данные из ЕГРЮЛ) ──
    cap_raw = raw.get("СумКап")
    if cap_raw is not None:
        try:
            result["capital"] = int(cap_raw)
        except (ValueError, TypeError):
            pass

    # ── Полный адрес ──
    result["address"] = raw.get("Адрес")

    # ── Штат сотрудников ──
    emp = raw.get("ЧислСотруд")
    if emp is not None:
        try:
            result["employee_count"] = int(emp)
        except (ValueError, TypeError):
            pass

    # ── Руководители (с ИНН и проверкой на массовость) ──
    leaders = raw.get("Руководители") or []
    result["leaders"] = []
    for ldr in leaders[:5]:
        if isinstance(ldr, dict):
            result["leaders"].append({
                "name": ldr.get("fl", ""),
                "post": ldr.get("post", ""),
                "inn": ldr.get("inn", ""),
                "date": ldr.get("date", ""),
                "mass_leaders": ldr.get("mass_leaders", "0"),
                "mass_founders": ldr.get("mass_founders", "0"),
                "disqualified": bool(ldr.get("disqual")),
            })

    # ── Суды (точная статистика) ──
    courts = raw.get("СудыСтатистика") or {}
    plaintiff = courts.get("Истец") or {}
    defendant = courts.get("Ответчик") or {}
    result["courts_plaintiff"] = plaintiff.get("Количество", 0)
    result["courts_plaintiff_sum"] = plaintiff.get("Сумма", 0)
    result["courts_defendant"] = defendant.get("Количество", 0)
    result["courts_defendant_sum"] = defendant.get("Сумма", 0)
    result["courts_total"] = result["courts_plaintiff"] + result["courts_defendant"]

    # ── Госзакупки ──
    purchases = raw.get("ЗакупкиСтат") or {}
    if purchases:
        result["purchases_supplier_count"] = purchases.get("КонтрПоставщКолв", 0)
        result["purchases_supplier_sum"] = purchases.get("КонтрПоставщСум", 0)
        result["purchases_customer_count"] = purchases.get("КонтрЗакупщКолв", 0)
        result["purchases_customer_sum"] = purchases.get("КонтрЗакупщСум", 0)

    # ── Финансы по годам (из Росстат/ФНС) ──
    years_data: list[dict[str, Any]] = []
    for year in range(2024, 2010, -1):
        fo = raw.get(f"ФО{year}") or {}
        rev = fo.get("ВЫРУЧКА")
        profit = fo.get("ПРИБЫЛЬ")
        if rev is None and profit is None:
            continue
        yd: dict[str, Any] = {"year": year}
        if rev is not None:
            try:
                yd["revenue"] = int(rev)
            except (ValueError, TypeError):
                pass
        if profit is not None:
            try:
                yd["net_profit"] = int(profit)
            except (ValueError, TypeError):
                pass
        assets = fo.get("ОСНСРЕДСТВА")
        if assets is not None:
            try:
                yd["fixed_assets"] = int(assets)
            except (ValueError, TypeError):
                pass
        cred = fo.get("КРЕДИТОРЗАДОЛЖН")
        if cred is not None:
            try:
                yd["creditor_debt"] = int(cred)
            except (ValueError, TypeError):
                pass
        debit = fo.get("ДЕБИТОРЗАДОЛЖН")
        if debit is not None:
            try:
                yd["debitor_debt"] = int(debit)
            except (ValueError, TypeError):
                pass
        years_data.append(yd)

    result["finances"] = years_data
    if years_data:
        latest = years_data[0]
        if "revenue" in latest:
            result["revenue"] = latest["revenue"]
            result["revenue_year"] = latest["year"]
        if "net_profit" in latest:
            result["net_profit"] = latest["net_profit"]

    # ── Лицензии (количество) ──
    lic_count = raw.get("СвЛицензия")
    if lic_count is not None:
        try:
            result["licenses_count"] = int(lic_count)
        except (ValueError, TypeError):
            pass

    # ── Филиалы / представительства ──
    branches = raw.get("СвФилиал")
    if branches:
        try:
            result["branches_count"] = int(branches)
        except (ValueError, TypeError):
            pass
    repr_count = raw.get("СвПредстав")
    if repr_count:
        try:
            result["representatives_count"] = int(repr_count)
        except (ValueError, TypeError):
            pass

    # ── Проверки ──
    checks = raw.get("Проверки")
    if checks:
        try:
            result["inspections_count"] = int(checks)
        except (ValueError, TypeError):
            pass

    # ── Флаги безопасности ──
    result["terrorist"] = bool(raw.get("ТеррористЭкстремист"))
    result["unreliable_address"] = bool(raw.get("СвНедАдресЮЛ"))
    result["bad_supplier"] = int(raw.get("НедобросовПостав") or 0) > 0

    # ── Товарные знаки ──
    trademarks = raw.get("ТоварЗнак") or []
    result["trademarks_count"] = len(trademarks)

    # ── Категория МСП ──
    result["msp_category"] = raw.get("КатСубМСП")

    # ── Учредители ──
    founders_data = raw.get("СвУчредит") or {}
    founders_list = founders_data.get("all") or []
    result["founders"] = []
    for f in founders_list[:10]:
        if isinstance(f, dict):
            result["founders"].append({
                "name": f.get("name", ""),
                "inn": f.get("inn", ""),
                "share": f.get("share"),
                "share_percent": f.get("share_percent"),
            })

    # ── Доп. ОКВЭДы ──
    dop_okveds = raw.get("СвОКВЭДДоп") or []
    result["additional_okveds"] = []
    for ok in dop_okveds[:20]:
        if isinstance(ok, dict):
            result["additional_okveds"].append({
                "code": ok.get("КодОКВЭД", ""),
                "name": ok.get("НаимОКВЭД", ""),
            })

    # ── Ссылка на карточку ──
    result["url"] = raw.get("urlCard", "")

    return result


# ─────────────────────────────────────────────────
# Судебные дела (детально)
# ─────────────────────────────────────────────────

async def fetch_court_cases(inn: str, page: int = 1) -> dict[str, Any]:
    """Получает детальные судебные дела."""
    result: dict[str, Any] = {"source": "zachestnyibiznes.ru/api", "cases": []}

    raw = await _api_request("court-arbitration", inn)
    if not raw:
        return result

    exact = raw.get("точно") or {}
    result["total"] = exact.get("всего", 0)

    cases_dict = exact.get("дела") or {}
    for case_id, case in list(cases_dict.items())[:10]:
        if not isinstance(case, dict):
            continue
        plaintiffs = [p.get("Наименование", "") for p in (case.get("Истец") or [])]
        defendants = [d.get("Наименование", "") for d in (case.get("Ответчик") or [])]
        result["cases"].append({
            "number": case.get("НомерДела", ""),
            "status": case.get("Статус", ""),
            "category": case.get("Категория", ""),
            "amount": case.get("СуммаИска"),
            "date": case.get("СтартДата", ""),
            "plaintiffs": plaintiffs[:3],
            "defendants": defendants[:3],
        })

    return result


# ─────────────────────────────────────────────────
# ФССП (исполнительные производства)
# ─────────────────────────────────────────────────

async def fetch_fssp(inn: str) -> dict[str, Any]:
    """Получает исполнительные производства ФССП."""
    result: dict[str, Any] = {"source": "zachestnyibiznes.ru/api", "items": []}

    raw = await _api_request("fssp-list", inn)
    if not raw:
        return result

    # fssp-list может возвращать список или dict
    items = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("items") or raw.get("docs") or []
        if not items and "НомерИП" in str(raw):
            items = [raw]

    result["total"] = len(items)
    for item in items[:10]:
        if isinstance(item, dict):
            result["items"].append({
                "number": item.get("НомерИП", ""),
                "date": item.get("ДатаВозб", ""),
                "subject": item.get("ПредметИсп", ""),
                "amount": item.get("СуммаДолга"),
                "department": item.get("ОтделСП", ""),
            })

    return result
