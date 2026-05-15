"""Клиент DaData.ru — базовые данные о компании по ИНН и поиск по названию.

Включает cross-user persistent cache (FileTTLCache):
- fetch_company по ИНН кэшируется на 24ч (DADATA_FETCH_CACHE_TTL)
- suggest_by_name кэшируется на 1ч (DADATA_SUGGEST_CACHE_TTL)
Кэш разделяется между всеми пользователями — один и тот же ИНН,
проверенный сотней клиентов, делает 1 запрос к DaData.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import requests

from api_quota import ApiQuotaExhausted, get_quota
from cache import FileTTLCache
from schemas import CompanyData

logger = logging.getLogger("financial-architect")

DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
DADATA_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"


class DaDataClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("DADATA_API_KEY", "")
        self.timeout = float(os.getenv("DADATA_TIMEOUT", "10"))
        self._cache_fetch = FileTTLCache(
            "dadata_fetch",
            ttl=float(os.getenv("DADATA_FETCH_CACHE_TTL", str(24 * 3600))),
        )
        self._cache_suggest = FileTTLCache(
            "dadata_suggest",
            ttl=float(os.getenv("DADATA_SUGGEST_CACHE_TTL", "3600")),
        )

    async def fetch_company(self, inn: str) -> Optional[CompanyData]:
        """Получить данные о компании по ИНН из DaData.

        Кэшируется на 24ч (DADATA_FETCH_CACHE_TTL) — ЕГРЮЛ-данные
        в DaData обновляются раз в сутки, чаще запрашивать не имеет
        смысла. Cross-user: один и тот же ИНН для разных юзеров —
        один платный запрос.
        """
        if not self.api_key:
            logger.warning("DADATA_API_KEY not set, skipping DaData")
            return None

        cached_raw = self._cache_fetch.get(inn)
        if cached_raw is not None:
            return self._parse(cached_raw, inn)

        # Глобальная квота: cache miss = реальный сетевой вызов.
        # Если апстрим в auto-degradation — возвращаем None, как при
        # любой другой ошибке. fetch продолжится с другими источниками.
        try:
            get_quota().check("dadata_fetch")
        except ApiQuotaExhausted as exc:
            logger.warning("DaData fetch skipped: %s", exc)
            return None

        def _call() -> Optional[Dict[str, Any]]:
            try:
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Token {self.api_key}",
                }
                resp = requests.post(
                    DADATA_URL,
                    json={"query": inn, "count": 1},
                    headers=headers,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    return resp.json()
                else:
                    logger.warning(
                        "DaData returned status %s: %s",
                        resp.status_code, resp.text[:200],
                    )
            except Exception as exc:
                logger.warning("DaData request failed: %s", exc)
            return None

        raw = await asyncio.to_thread(_call)
        if not raw:
            return None

        # Учёт квоты только на успешный ответ — DaData биллит за 200 OK.
        # Сетевые ошибки/5xx не считаем (raw is None).
        get_quota().record("dadata_fetch")
        # Кэшируем только успешные ответы (даже если parse вернёт None
        # из-за пустых suggestions — это валидный ответ DaData,
        # дёргать API ещё раз нет смысла).
        self._cache_fetch.set(inn, raw)
        return self._parse(raw, inn)

    async def suggest_by_name(self, query: str, count: int = 10) -> List[CompanyData]:
        """Поиск компаний по началу названия. Возвращает до `count`
        результатов, в порядке релевантности DaData. Пустой результат
        — если ключ не настроен или запрос пустой.

        Кэшируется на 1ч по ключу `query|count` (нормализованному).
        """
        if not self.api_key or not query.strip():
            return []

        # DaData ограничивает count: max 20 для suggest
        count = max(1, min(count, 20))
        normalized_query = query.strip().lower()
        cache_key = f"{normalized_query}|{count}"
        cached_raw = self._cache_suggest.get(cache_key)
        if cached_raw is not None:
            raw = cached_raw
        else:
            try:
                get_quota().check("dadata_suggest")
            except ApiQuotaExhausted as exc:
                logger.warning("DaData suggest skipped: %s", exc)
                return []

            def _call() -> Optional[Dict[str, Any]]:
                try:
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Authorization": f"Token {self.api_key}",
                    }
                    resp = requests.post(
                        DADATA_SUGGEST_URL,
                        json={"query": query.strip(), "count": count},
                        headers=headers,
                        timeout=self.timeout,
                    )
                    if resp.status_code == 200:
                        return resp.json()
                    logger.warning("DaData suggest status %s: %s",
                                   resp.status_code, resp.text[:200])
                except Exception as exc:
                    logger.warning("DaData suggest failed: %s", exc)
                return None

            raw = await asyncio.to_thread(_call)
            if not raw:
                return []
            get_quota().record("dadata_suggest")
            self._cache_suggest.set(cache_key, raw)

        suggestions = raw.get("suggestions", [])
        results: List[CompanyData] = []
        for item in suggestions:
            inn = (item.get("data") or {}).get("inn") or ""
            # Переиспользуем _parse, обернув один suggestion в формат findById
            company = self._parse({"suggestions": [item]}, inn)
            if company is not None:
                results.append(company)
        return results

    @staticmethod
    def _parse(data: Dict[str, Any], inn: str) -> Optional[CompanyData]:
        """Парсинг ответа DaData в CompanyData."""
        suggestions = data.get("suggestions", [])
        if not suggestions:
            logger.info("DaData: no results for INN %s", inn)
            return None

        item = suggestions[0]
        d = item.get("data", {})

        # Название
        name = item.get("value") or d.get("name", {}).get("full_with_opf")

        # Адрес
        address_data = d.get("address", {})
        address = address_data.get("unrestricted_value") or address_data.get("value")

        # Регион
        region = None
        if isinstance(address_data.get("data"), dict):
            region = address_data["data"].get("region_with_type")

        # ОКВЭД
        okved_main = d.get("okved")
        okved_name = d.get("okved_type2") if d.get("okved_type2") else None

        # Руководитель
        management = d.get("management", {})
        director = management.get("name") if isinstance(management, dict) else None
        director_post = management.get("post") if isinstance(management, dict) else None
        if director and director_post:
            director = f"{director_post}: {director}"

        # Дата регистрации (timestamp в мс)
        reg_date = None
        age_years = None
        ogrn_date = d.get("ogrn_date")
        if ogrn_date:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(ogrn_date / 1000, tz=timezone.utc)
                reg_date = dt.strftime("%Y-%m-%d")
                age_years = (datetime.now(tz=timezone.utc) - dt).days // 365
            except Exception:
                pass

        # Статус
        state = d.get("state", {})
        status_code = state.get("status") if isinstance(state, dict) else None
        status_map = {
            "ACTIVE": "Действующая",
            "LIQUIDATING": "Ликвидируется",
            "LIQUIDATED": "Ликвидирована",
            "BANKRUPT": "Банкрот",
            "REORGANIZING": "Реорганизация",
        }
        status = status_map.get(status_code, status_code)

        # Уставный капитал
        capital = None
        if d.get("capital") and isinstance(d["capital"], dict):
            capital = d["capital"].get("value")

        # Штат
        employees = None
        employee_count = d.get("employee_count")
        if employee_count is not None:
            try:
                employees = int(employee_count)
            except (ValueError, TypeError):
                pass

        return CompanyData(
            inn=d.get("inn") or inn,
            name=name,
            ogrn=d.get("ogrn"),
            region=region,
            address=address,
            reg_date=reg_date,
            age_years=age_years,
            okved_main=okved_main,
            okved_name=okved_name,
            employees_count=employees,
            director=director,
            status=status,
            kpp=d.get("kpp"),
            capital=capital,
            source="dadata",
        )
