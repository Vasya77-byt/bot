"""
Клиент API-FNS.ru — единый доступ к данным ФНС.

Методы:
  - egr: полные данные ЕГРЮЛ/ЕГРИП
  - check: проверка контрагента (массовый директор, реестры, недостоверность)
  - nalogbi: блокировка счетов ФНС
  - bo: бухгалтерская отчётность (выручка, расходы, налоги)
  - zsk: уровень риска "Знай своего клиента"
"""

import logging
from typing import Any

import httpx

from config import APIFNS_KEY

logger = logging.getLogger(__name__)

_BASE = "https://api-fns.ru/api"
_TIMEOUT = 20.0


async def _api_request(
    method: str,
    inn_or_ogrn: str,
    param_name: str = "req",
) -> dict[str, Any] | None:
    """
    Общий запрос к API-FNS.

    Returns dict с данными или None при ошибке.
    """
    if not APIFNS_KEY:
        return None

    # NB: API-FNS требует ключ в query params (не поддерживает заголовки).
    # Ключ может попасть в access-логи прокси — не размещать бота за HTTP-прокси.
    params = {param_name: inn_or_ogrn, "key": APIFNS_KEY}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{_BASE}/{method}", params=params)

            if r.status_code == 403:
                text = r.text
                if "ip-адреса" in text.lower():
                    logger.warning("API-FNS %s: IP blocked", method)
                elif "Исчерпано" in text:
                    logger.warning("API-FNS %s: request limit exhausted", method)
                else:
                    logger.warning("API-FNS %s: 403 — %s", method, text[:200])
                return None

            if r.status_code != 200:
                logger.warning("API-FNS %s: HTTP %s", method, r.status_code)
                return None

            # Ответ может быть JSON-объект или plain text с ошибкой
            ct = r.headers.get("content-type", "")
            if "json" not in ct:
                logger.warning("API-FNS %s: not JSON: %s", method, r.text[:200])
                return None

            try:
                data = r.json()
            except Exception:
                # Иногда ответ — plain text в JSON content-type
                text = r.text.strip()
                if text.startswith("Ошибка"):
                    logger.warning("API-FNS %s: %s", method, text)
                    return None
                logger.warning("API-FNS %s: invalid JSON", method)
                return None

            # Если ответ — строка с ошибкой, а не dict/list
            if isinstance(data, str):
                if "Ошибка" in data:
                    logger.warning("API-FNS %s: %s", method, data)
                    return None

            return data

    except (httpx.TimeoutException, httpx.ConnectError) as e:
        logger.warning("API-FNS %s timeout: %s", method, e)
        return None
    except Exception as e:
        logger.warning("API-FNS %s error: %s", method, e)
        return None


# ─────────────────────────────────────────────────────────────
# Публичные методы
# ─────────────────────────────────────────────────────────────

async def fetch_egr(inn: str) -> dict[str, Any] | None:
    """
    Полные данные из ЕГРЮЛ/ЕГРИП.

    Возвращает dict с ключами: ИНН, ОГРН, НаимСокрЮЛ, Статус,
    Адрес, Руководитель, Учредители, Капитал, ОснВидДеят и т.д.
    """
    data = await _api_request("egr", inn, param_name="req")
    if not data:
        return None

    # API возвращает dict с ключом "items" или напрямую массив
    items = data.get("items") if isinstance(data, dict) else None
    if isinstance(data, list) and data:
        return data[0]
    if items and isinstance(items, list) and items:
        return items[0]
    if isinstance(data, dict):
        return data
    return None


async def fetch_check(inn: str) -> dict[str, Any] | None:
    """
    Проверка контрагента: негативные реестры, массовый директор,
    недостоверные данные, решения о ликвидации.
    """
    data = await _api_request("check", inn, param_name="req")
    if not data:
        return None

    # Нормализуем ответ
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list) and items:
            return items[0]
        return data
    return None


async def fetch_nalogbi(inn: str) -> dict[str, Any] | None:
    """
    Блокировка счетов ФНС.
    Возвращает информацию о решениях о приостановлении операций.
    """
    # nalogbi использует параметр "inn" (не "req")
    return await _api_request("nalogbi", inn, param_name="inn")


async def fetch_bo(inn: str) -> dict[str, Any] | None:
    """
    Бухгалтерская отчётность.
    Возвращает финансовые данные: выручка, расходы, прибыль,
    среднесписочная численность, налоговая нагрузка.
    """
    return await _api_request("bo", inn, param_name="req")


async def fetch_zsk(inn: str) -> dict[str, Any] | None:
    """
    Уровень риска "Знай своего клиента" (ЗСК ЦБ).

    Метод асинхронный:
      Состояние -1 = в очереди, -2 = выполняется, 0 = ошибка, 1 = готово.
      Риск: 0 = нет высокого риска, 2 = высокий риск.
    """
    import asyncio

    # zsk использует параметр "inn" (не "req")
    for attempt in range(5):
        data = await _api_request("zsk", inn, param_name="inn")
        if not data:
            return None

        state = str(data.get("Состояние", ""))
        if state == "1":
            # Результат готов
            return data
        elif state in ("-1", "-2"):
            # Ожидание — повторяем через 2 секунды
            await asyncio.sleep(2)
            continue
        elif state == "0":
            logger.warning("API-FNS zsk error: %s", data.get("Текст", ""))
            return None
        else:
            # Неизвестный формат — возвращаем как есть
            return data

    logger.warning("API-FNS zsk: timeout after 5 polls")
    return None


async def fetch_changes(inn: str) -> dict[str, Any] | None:
    """
    Получает историю изменений компании через API-FNS метод 'changes'.
    Возвращает список изменений (записи в ЕГРЮЛ).
    """
    return await _api_request("changes", inn, param_name="req")


def extract_changes_data(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    Извлекает и нормализует историю изменений из ответа API-FNS.
    Возвращает список записей: [{date, type, description}, ...]
    """
    if not raw:
        return []

    changes: list[dict[str, Any]] = []

    # Ответ может быть dict с items или list
    items = None
    if isinstance(raw, dict):
        items = raw.get("items") or raw.get("Записи") or raw.get("changes")
        if items is None:
            # Попробуем найти ключ ЮЛ → ГРН
            ul = raw.get("ЮЛ") or raw
            grn_list = ul.get("ГРН") or ul.get("Записи") or []
            if isinstance(grn_list, list):
                items = grn_list
    elif isinstance(raw, list):
        items = raw

    if not items or not isinstance(items, list):
        return changes

    for item in items:
        if not isinstance(item, dict):
            continue
        change = {
            "date": (
                item.get("ДатаЗап") or item.get("Дата")
                or item.get("date") or item.get("ДатаГРН")
            ),
            "grn": item.get("ГРН") or item.get("grn"),
            "type": item.get("ВидЗап") or item.get("Вид") or item.get("type") or "",
            "description": (
                item.get("ТекстЗап") or item.get("Текст")
                or item.get("description") or item.get("Наименование") or ""
            ),
        }
        if change["date"] or change["description"]:
            changes.append(change)

    # Сортируем по дате (свежие первыми)
    changes.sort(key=lambda c: c.get("date") or "", reverse=True)
    return changes


async def fetch_stat() -> dict[str, Any] | None:
    """Статистика использования API (лимиты)."""
    if not APIFNS_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{_BASE}/stat", params={"key": APIFNS_KEY})
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning("API-FNS stat error: %s", e)
    return None


# ─────────────────────────────────────────────────────────────
# Извлечение структурированных данных из ответа egr
# ─────────────────────────────────────────────────────────────

def extract_egr_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Извлекает и нормализует поля из ответа egr.
    Совместимо с форматом extract_company_fields() из dadata_client.
    """
    result: dict[str, Any] = {}

    # Тип: ЮЛ или ИП
    ul = raw.get("ЮЛ") or {}
    ip = raw.get("ИП") or {}
    entity = ul or ip

    if ul:
        result["entity_type"] = "ul"
        result["inn"] = ul.get("ИНН")
        result["kpp"] = ul.get("КПП")
        result["ogrn"] = ul.get("ОГРН")
        result["name"] = ul.get("НаимСокрЮЛ") or ul.get("НаимПолнЮЛ")
        result["full_name"] = ul.get("НаимПолнЮЛ")
        result["status"] = _map_status(ul.get("Статус"))

        # Адрес
        addr = ul.get("Адрес") or {}
        result["address"] = addr.get("АдресПол662") or addr.get("Адрес")

        # Руководитель
        leader = ul.get("Руководитель") or {}
        if isinstance(leader, list) and leader:
            leader = leader[0]
        if isinstance(leader, dict):
            fio = leader.get("ФИОПолн") or ""
            result["management_name"] = fio
            result["management_post"] = leader.get("Должн") or leader.get("Должность") or ""
            result["management_inn"] = leader.get("ИННФЛ")

        # Капитал
        capital = ul.get("Капитал") or {}
        if isinstance(capital, dict):
            try:
                result["capital_value"] = float(capital.get("СумКап") or 0)
            except (ValueError, TypeError):
                pass

        # ОКВЭД
        okved = ul.get("ОснВидДеят") or {}
        if isinstance(okved, dict):
            result["okved_code"] = okved.get("Код")
            result["okved_text"] = okved.get("Текст") or okved.get("Наим")

        # Учредители
        founders_raw = ul.get("Учредители") or ul.get("УчрЮЛРос") or []
        if isinstance(founders_raw, list):
            founders = []
            for f in founders_raw:
                if isinstance(f, dict):
                    name = f.get("НаимПолнЮЛ") or f.get("ФИОПолн") or f.get("Наим") or ""
                    share = None
                    share_data = f.get("ДоляУстКап") or {}
                    if isinstance(share_data, dict):
                        nom = share_data.get("Числит")
                        denom = share_data.get("Знаменат")
                        if nom and denom:
                            try:
                                share = round(float(nom) / float(denom) * 100, 1)
                            except (ValueError, TypeError, ZeroDivisionError):
                                pass
                    if name:
                        founders.append({"name": name, "share": share})
            result["founders"] = founders

        # Дата регистрации
        reg_date = ul.get("ДатаРег") or ul.get("ДатаОГРН")
        if reg_date:
            result["registration_date"] = reg_date

    elif ip:
        result["entity_type"] = "ip"
        result["inn"] = ip.get("ИННФЛ")
        result["ogrn"] = ip.get("ОГРНИП")
        result["name"] = ip.get("ФИОПолн")
        result["status"] = _map_status(ip.get("Статус"))
        reg_date = ip.get("ДатаРег") or ip.get("ДатаОГРНИП")
        if reg_date:
            result["registration_date"] = reg_date

    return result


def extract_check_data(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Извлекает данные проверки контрагента.

    Формат ответа API:
    {"items":[{"ЮЛ":{"ОГРН":"...","ИНН":"...",
      "Позитив":{"Лицензии":"Есть","Текст":"..."},
      "Негатив":{"МассАдрес":"Да (16 юрлиц)","Текст":"..."}
    }}]}
    """
    result: dict[str, Any] = {"source": "api-fns.ru/check"}

    if not raw:
        return result

    # Навигация к данным ЮЛ
    items = raw.get("items") if isinstance(raw, dict) else None
    if isinstance(items, list) and items:
        item = items[0]
    elif isinstance(raw, dict):
        item = raw
    else:
        return result

    ul = item.get("ЮЛ") or item
    if not isinstance(ul, dict):
        return result

    negativ = ul.get("Негатив") or {}
    positiv = ul.get("Позитив") or {}

    # Негативный текст целиком (для отображения)
    neg_text = negativ.get("Текст", "")
    pos_text = positiv.get("Текст", "")
    if neg_text:
        result["negative_text"] = neg_text
    if pos_text:
        result["positive_text"] = pos_text

    # --- Негативные признаки ---

    # Массовый руководитель
    for key in ("МассРук", "РеестрМассРук"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["mass_director"] = True
            result["mass_director_detail"] = val

    # Массовый адрес
    for key in ("МассАдрес", "РеестрМассАдрес"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["mass_address"] = True
            result["mass_address_detail"] = val

    # Массовый учредитель
    for key in ("МассУчред", "РеестрМассУчред"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["mass_founder"] = True

    # Недостоверные сведения
    for key in ("НедАдрес", "НедостовернАдрес"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["unreliable_address"] = True

    for key in ("НедРук", "НедостовернРук"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["unreliable_director"] = True

    for key in ("НедУчред", "НедостовернУчред"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["unreliable_founder"] = True

    # Дисквалификация руководителя
    for key in ("ДисквРук", "Дисквалификация"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["disqualified"] = True

    # Решение о ликвидации / реорганизации / исключении
    for key, flag in [
        ("РешЛикв", "liquidation_decision"),
        ("РешИскл", "exclusion_decision"),
        ("РешРеорг", "reorganization_decision"),
        ("УменьшКап", "capital_decrease"),
    ]:
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result[flag] = True

    # Задолженность по налогам
    for key in ("ЗадолжНалог", "Задолженность"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["tax_debt"] = True

    # Непредоставление отчётности
    for key in ("НеСдачаОтч", "НепредоставлениеОтчетности"):
        val = negativ.get(key)
        if val and _is_positive_flag(val):
            result["no_reports"] = True

    # --- Позитивные признаки ---
    if positiv.get("Лицензии"):
        result["has_licenses"] = True
    if positiv.get("Филиалы"):
        result["has_branches"] = True
    cap = positiv.get("КапБолее50тыс")
    if cap and _is_positive_flag(cap):
        result["capital_above_50k"] = True

    # Если нет негативных — чистая проверка
    has_negatives = any(
        result.get(k) for k in [
            "mass_director", "mass_address", "mass_founder",
            "unreliable_address", "unreliable_director", "unreliable_founder",
            "disqualified", "liquidation_decision", "exclusion_decision",
            "tax_debt", "no_reports",
        ]
    )
    if not has_negatives and negativ == {}:
        result["clean"] = True

    return result


def _is_positive_flag(val: Any) -> bool:
    """Проверяет, является ли значение положительным флагом ('Да', 'Есть', etc.)."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        return s.startswith("да") or s.startswith("есть") or s == "true"
    return bool(val)


def extract_nalogbi_data(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Извлекает данные о блокировках счетов.

    Формат ответа API (предполагаемый — лимиты исчерпаны при тестировании):
    {"items": [...]} или {"ИНН": [{решение}, ...]} или список решений
    Каждое решение содержит поля о банке, дате, номере решения.
    """
    result: dict[str, Any] = {"source": "api-fns.ru/nalogbi"}

    if not raw:
        return result

    # Ищем массив блокировок в разных форматах ответа
    blockings: list | None = None
    if isinstance(raw, list):
        blockings = raw
    elif isinstance(raw, dict):
        # Пробуем разные ключи
        for key in ("items", "Решения"):
            val = raw.get(key)
            if isinstance(val, list):
                blockings = val
                break
        if blockings is None:
            # Может быть формат {ИНН: [решения]}
            for key, val in raw.items():
                if isinstance(val, list):
                    blockings = val
                    break
        if blockings is None and "НомерРеш" in str(raw):
            # Единственное решение в виде dict
            blockings = [raw]

    if blockings is not None:
        result["blocked_accounts_count"] = len(blockings)
        result["has_blocked_accounts"] = len(blockings) > 0

        if blockings:
            details = []
            for b in blockings[:5]:
                if isinstance(b, dict):
                    detail = {
                        "bank": (
                            b.get("НаимБанк") or b.get("БанкНаим")
                            or b.get("Банк") or b.get("bank_name")
                        ),
                        "bic": b.get("БИК") or b.get("bic"),
                        "date": (
                            b.get("ДатаРеш") or b.get("Дата")
                            or b.get("date")
                        ),
                    }
                    details.append(detail)
            result["blocking_details"] = details
    else:
        # Не удалось распарсить — но данные получены (нет блокировок)
        result["has_blocked_accounts"] = False
        result["blocked_accounts_count"] = 0

    return result


def extract_bo_data(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Извлекает финансовые данные из бухотчётности.

    Формат ответа API:
    {"7707083893": {
      "2023": {
        "credit_profit_and_loss": {"1": "выручка", "22": "чистая прибыль", ...},
        "credit_assets": {"14": "итого актив", ...},
        "credit_passives": {"23": "итого пассив", ...},
        ...
      },
      "2022": {...}
    }}

    Номера строк (credit_profit_and_loss):
      1 = Выручка (код 2110)
      2 = Себестоимость продаж (код 2120)
      3 = Валовая прибыль (код 2100)
      5 = Прибыль от продаж (код 2200)
      14 = Прибыль до налогообложения (код 2300)
      22 = Чистая прибыль (код 2400)

    Номера строк (credit_assets):
      14 = БАЛАНС (итого актив, код 1600)

    Значения — строки в тыс. руб.
    """
    result: dict[str, Any] = {"source": "api-fns.ru/bo"}

    if not raw or not isinstance(raw, dict):
        return result

    # Формат: {ИНН: {год: {раздел: {строка: значение}}}}
    # Находим первый ИНН-ключ (обычно один)
    years_data: dict | None = None
    for key, val in raw.items():
        if isinstance(val, dict) and any(
            k.isdigit() and len(k) == 4 for k in val.keys()
        ):
            years_data = val
            break

    if not years_data:
        return result

    # Собираем данные по годам (сортируем по убыванию — свежие первыми)
    sorted_years = sorted(
        [y for y in years_data.keys() if y.isdigit()],
        reverse=True,
    )

    if not sorted_years:
        return result

    years_list: list[dict[str, Any]] = []
    for year_str in sorted_years[:5]:  # Берём до 5 последних лет
        year_data = years_data[year_str]
        if not isinstance(year_data, dict):
            continue

        pnl = year_data.get("credit_profit_and_loss") or {}
        assets = year_data.get("credit_assets") or {}

        year_info: dict[str, Any] = {"year": int(year_str)}

        # Выручка — строка 1 (код 2110)
        revenue = _safe_float(pnl.get("1"))
        if revenue is not None:
            year_info["revenue"] = revenue * 1000  # тыс. → руб.

        # Себестоимость — строка 2 (код 2120)
        cost = _safe_float(pnl.get("2"))
        if cost is not None:
            year_info["cost"] = cost * 1000

        # Валовая прибыль — строка 3 (код 2100)
        gross = _safe_float(pnl.get("3"))
        if gross is not None:
            year_info["gross_profit"] = gross * 1000

        # Прибыль от продаж — строка 5 (код 2200)
        operating = _safe_float(pnl.get("5"))
        if operating is not None:
            year_info["operating_profit"] = operating * 1000

        # Прибыль до налогообложения — строка 14 (код 2300)
        pretax = _safe_float(pnl.get("14"))
        if pretax is not None:
            year_info["pretax_profit"] = pretax * 1000

        # Чистая прибыль — строка 22 (код 2400)
        net_profit = _safe_float(pnl.get("22"))
        if net_profit is not None:
            year_info["net_profit"] = net_profit * 1000

        # Итого актив — строка 14 в credit_assets (код 1600)
        total_assets = _safe_float(assets.get("14"))
        if total_assets is not None:
            year_info["total_assets"] = total_assets * 1000

        years_list.append(year_info)

    result["years"] = years_list

    # Последний год — основные показатели для быстрого доступа
    if years_list:
        latest = years_list[0]
        if "revenue" in latest:
            result["revenue"] = latest["revenue"]
        if "net_profit" in latest:
            result["net_profit"] = latest["net_profit"]
        if "total_assets" in latest:
            result["total_assets"] = latest["total_assets"]
        result["latest_year"] = latest["year"]

    return result


def _safe_float(val: Any) -> float | None:
    """Безопасное преобразование строки/числа в float."""
    if val is None:
        return None
    try:
        return float(str(val).replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def extract_zsk_data(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Извлекает уровень риска ЗСК.

    Формат ответа API:
    {"Состояние": "1", "Риск": "0", "Текст": "..."}
      Риск: 0 = нет высокого риска, 2 = высокий риск.
    """
    result: dict[str, Any] = {"source": "api-fns.ru/zsk"}

    if not raw or not isinstance(raw, dict):
        return result

    # Основной формат: Состояние + Риск
    risk = raw.get("Риск")
    text = raw.get("Текст")

    if risk is not None:
        risk_str = str(risk).strip()
        if risk_str == "0":
            result["zsk_level"] = "Нет высокого риска"
            result["zsk_color"] = "green"
        elif risk_str == "2":
            result["zsk_level"] = "Высокий риск"
            result["zsk_color"] = "red"
        else:
            result["zsk_level"] = text or f"Риск: {risk_str}"
            result["zsk_color"] = "yellow"

        if text:
            result["zsk_text"] = text
        return result

    # Фоллбэк: другие возможные форматы
    level = (
        raw.get("Уровень") or raw.get("УровеньРиска")
        or raw.get("level") or raw.get("Цвет")
    )
    if level:
        result["zsk_level"] = level
        level_lower = str(level).lower()
        if "зелен" in level_lower or "низк" in level_lower or "green" in level_lower:
            result["zsk_color"] = "green"
        elif "красн" in level_lower or "высок" in level_lower or "red" in level_lower:
            result["zsk_color"] = "red"
        else:
            result["zsk_color"] = "yellow"

    return result


def _map_status(status: str | None) -> str | None:
    """Маппинг статусов ФНС → внутренний формат."""
    if not status:
        return None
    s = str(status).lower()
    if "действ" in s:
        return "ACTIVE"
    elif "ликвид" in s and "процесс" in s:
        return "LIQUIDATING"
    elif "ликвид" in s:
        return "LIQUIDATED"
    elif "банкрот" in s:
        return "BANKRUPT"
    elif "реорг" in s:
        return "REORGANIZING"
    elif "прекра" in s:
        return "LIQUIDATED"
    return status


# ─────────────────────────────────────────────────────────────
# Комплексная проверка через API-FNS
# ─────────────────────────────────────────────────────────────

async def full_apifns_check(inn: str) -> dict[str, Any]:
    """
    Выполняет все доступные проверки через API-FNS.
    Возвращает объединённый dict со всеми данными.
    Если API-FNS недоступен — возвращает пустой dict.
    """
    import asyncio

    result: dict[str, Any] = {"source": "api-fns.ru"}

    if not APIFNS_KEY:
        return result

    # Запускаем все методы параллельно
    check_task = asyncio.create_task(fetch_check(inn))
    nalogbi_task = asyncio.create_task(fetch_nalogbi(inn))
    bo_task = asyncio.create_task(fetch_bo(inn))
    zsk_task = asyncio.create_task(fetch_zsk(inn))

    check_raw, nalogbi_raw, bo_raw, zsk_raw = await asyncio.gather(
        check_task, nalogbi_task, bo_task, zsk_task
    )

    # Извлекаем структурированные данные
    if check_raw:
        result["check"] = extract_check_data(check_raw)
    if nalogbi_raw:
        result["nalogbi"] = extract_nalogbi_data(nalogbi_raw)
    if bo_raw:
        result["bo"] = extract_bo_data(bo_raw)
    if zsk_raw:
        result["zsk"] = extract_zsk_data(zsk_raw)

    return result
