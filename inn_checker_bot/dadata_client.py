"""Клиент для DaData API: поиск компании по ИНН и извлечение полей."""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from config import DADATA_API_KEY, DADATA_FIND_BY_ID_URL, DADATA_TIMEOUT

logger = logging.getLogger(__name__)


class DaDataError(Exception):
    """Ошибка при работе с DaData API."""


_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"


async def search_company_by_name(query: str, count: int = 5) -> list[dict[str, Any]]:
    """
    Поиск компаний по названию через DaData suggest/party.
    Возвращает список [{inn, name, status, address}, ...].
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {DADATA_API_KEY}",
    }
    payload = {"query": query, "count": count}

    try:
        async with httpx.AsyncClient(timeout=DADATA_TIMEOUT) as client:
            response = await client.post(_SUGGEST_URL, json=payload, headers=headers)

        if response.status_code != 200:
            return []

        body = response.json()
        results = []
        for s in body.get("suggestions", []):
            data = s.get("data", {})
            inn = data.get("inn", "")
            name = s.get("value", "")
            status = _safe_get(data, "state", "status")
            address = _safe_get(data, "address", "unrestricted_value") or ""
            raw_type = data.get("type", "")
            entity = "ИП" if raw_type == "INDIVIDUAL" else "ЮЛ"
            results.append({
                "inn": inn,
                "name": name,
                "status": status,
                "address": address[:80],
                "entity": entity,
            })
        return results
    except Exception as e:
        logger.warning("DaData suggest error: %s", e)
        return []


async def fetch_company_data(inn: str) -> dict[str, Any]:
    """
    Запрашивает данные о компании по ИНН через DaData findById/party.
    Возвращает сырой dict из первого suggestion или кидает DaDataError.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {DADATA_API_KEY}",
    }
    payload = {"query": inn, "count": 1}

    try:
        async with httpx.AsyncClient(timeout=DADATA_TIMEOUT) as client:
            response = await client.post(
                DADATA_FIND_BY_ID_URL, json=payload, headers=headers
            )
    except httpx.TimeoutException:
        raise DaDataError("Таймаут при запросе к DaData. Попробуйте позже.")
    except httpx.RequestError as e:
        logger.error("Ошибка сети DaData: %s", e)
        raise DaDataError("Ошибка сети при запросе к DaData.")

    if response.status_code == 403:
        raise DaDataError("Ошибка авторизации DaData. Проверьте API-ключ.")
    if response.status_code != 200:
        logger.error("DaData HTTP %s: %s", response.status_code, response.text[:300])
        raise DaDataError(f"DaData вернула HTTP {response.status_code}.")

    body = response.json()
    suggestions = body.get("suggestions", [])
    if not suggestions:
        raise DaDataError("Компания с таким ИНН не найдена в DaData.")

    return suggestions[0].get("data", {})


def _safe_get(data: dict, *keys, default=None):
    """Безопасное извлечение вложенных ключей."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


# Маппинг кодов системы налогообложения DaData → человекопонятные названия
TAX_SYSTEM_NAMES = {
    "OSNO": "ОСНО (общая)",
    "USN6": "УСН 6% (доходы)",
    "USN15": "УСН 15% (доходы минус расходы)",
    "USN": "УСН",
    "ENVD": "ЕНВД",
    "ESHN": "ЕСХН",
    "PATENT": "Патент",
}


def extract_company_fields(raw_data: dict[str, Any]) -> dict[str, Any]:
    """
    Извлекает и нормализует поля компании из сырого ответа DaData.
    Если поле отсутствует — значение будет None.
    """
    # Основные поля
    name = (
        _safe_get(raw_data, "name", "short_with_opf")
        or _safe_get(raw_data, "name", "full_with_opf")
        or None
    )
    inn = _safe_get(raw_data, "inn")
    kpp = _safe_get(raw_data, "kpp")
    ogrn = _safe_get(raw_data, "ogrn")
    address = _safe_get(raw_data, "address", "unrestricted_value")

    # Город из структурированного адреса
    city = (
        _safe_get(raw_data, "address", "data", "city")
        or _safe_get(raw_data, "address", "data", "region")
        or None
    )

    # Руководитель
    management_name = _safe_get(raw_data, "management", "name")
    management_post = _safe_get(raw_data, "management", "post")

    # Статус
    status = _safe_get(raw_data, "state", "status")

    # Дата регистрации (unix timestamp в мс)
    reg_ts = _safe_get(raw_data, "state", "registration_date")
    registration_date = None
    company_age_years = None
    if reg_ts:
        try:
            reg_dt = datetime.fromtimestamp(reg_ts / 1000, tz=timezone.utc)
            registration_date = reg_dt.strftime("%d.%m.%Y")
            delta = datetime.now(tz=timezone.utc) - reg_dt
            company_age_years = round(delta.days / 365.25, 1)
        except (OSError, ValueError):
            pass

    # Уставный капитал (DaData возвращает число в рублях или None)
    _capital_raw = _safe_get(raw_data, "capital", "value")
    capital_value = None
    if _capital_raw is not None:
        try:
            capital_value = float(_capital_raw)
        except (ValueError, TypeError):
            pass

    # ОКВЭД
    okved_code = _safe_get(raw_data, "okved")
    # Ищем текстовое описание в массиве okveds
    okved_text = None
    okveds_list = raw_data.get("okveds") or []
    for ov in okveds_list:
        if isinstance(ov, dict) and ov.get("main"):
            okved_text = ov.get("name")
            break
    # Fallback: если не нашли main, берём первый
    if not okved_text and okveds_list:
        first = okveds_list[0]
        if isinstance(first, dict):
            okved_text = first.get("name")

    # Лицензии
    licenses_raw = raw_data.get("licenses") or []
    licenses = []
    for lic in licenses_raw:
        if isinstance(lic, dict):
            activity = lic.get("activity") or "без описания"
            licenses.append(activity)

    # Финансовый блок
    finance = raw_data.get("finance") or {}
    tax_system_code = finance.get("tax_system")
    tax_system = TAX_SYSTEM_NAMES.get(tax_system_code, tax_system_code)
    income = finance.get("income")
    expense = finance.get("expense")
    debt = finance.get("debt")
    penalty = finance.get("penalty")
    finance_year = finance.get("year")

    # Штат
    employee_count = raw_data.get("employee_count")

    # Учредители (для информации)
    founders_raw = raw_data.get("founders") or []
    founders = []
    for f in founders_raw:
        if isinstance(f, dict):
            fname = _safe_get(f, "name") or _safe_get(f, "fio", "surname")
            if fname:
                share_obj = f.get("share") or {}
                share_value = share_obj.get("value") if isinstance(share_obj, dict) else None
                share_type = share_obj.get("type") if isinstance(share_obj, dict) else None
                # share_type: PERCENT=проценты, DECIMAL=дробь, NOMINAL=рубли
                founders.append({
                    "name": fname,
                    "share": share_value,
                    "share_type": share_type,
                })

    # Определяем тип: ЮЛ или ИП
    raw_type = raw_data.get("type")
    entity_type = "ip" if raw_type == "INDIVIDUAL" else "ul"

    return {
        "entity_type": entity_type,
        "name": name,
        "inn": inn,
        "kpp": kpp if entity_type == "ul" else None,  # ИП не имеют КПП
        "ogrn": ogrn,
        "address": address,
        "city": city,
        "management_name": management_name if entity_type == "ul" else None,
        "management_post": management_post if entity_type == "ul" else None,
        "status": status,
        "registration_date": registration_date,
        "company_age_years": company_age_years,
        "capital_value": capital_value if entity_type == "ul" else None,
        "okved_code": okved_code,
        "okved_text": okved_text,
        "licenses": licenses,
        "tax_system": tax_system,
        "income": income,
        "expense": expense,
        "debt": debt,
        "penalty": penalty,
        "finance_year": finance_year,
        "employee_count": employee_count,
        "founders": founders if entity_type == "ul" else [],
    }
