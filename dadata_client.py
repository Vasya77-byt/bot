"""Клиент DaData.ru — базовые данные о компании по ИНН."""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import requests

from schemas import CompanyData

logger = logging.getLogger("financial-architect")

DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
DADATA_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"


class DaDataClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("DADATA_API_KEY", "")
        self.timeout = float(os.getenv("DADATA_TIMEOUT", "10"))

    async def fetch_company(self, inn: str) -> Optional[CompanyData]:
        """Получить данные о компании по ИНН из DaData."""
        if not self.api_key:
            logger.warning("DADATA_API_KEY not set, skipping DaData")
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
                    logger.warning("DaData returned status %s: %s", resp.status_code, resp.text[:200])
            except Exception as exc:
                logger.warning("DaData request failed: %s", exc)
            return None

        raw = await asyncio.to_thread(_call)
        if not raw:
            return None

        return self._parse(raw, inn)

    async def search_by_name(self, query: str, count: int = 5) -> List[Dict[str, Any]]:
        """Поиск компаний по названию. Возвращает список {inn, name, address, status}."""
        if not self.api_key:
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
                    json={"query": query, "count": count, "status": ["ACTIVE"]},
                    headers=headers,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.warning("DaData suggest status %s", resp.status_code)
            except Exception as exc:
                logger.warning("DaData suggest failed: %s", exc)
            return None

        raw = await asyncio.to_thread(_call)
        if not raw:
            return []

        results = []
        for item in raw.get("suggestions", []):
            d = item.get("data", {})
            inn = d.get("inn")
            if not inn:
                continue
            name = item.get("value") or ""
            address_data = d.get("address") or {}
            city = None
            if isinstance(address_data.get("data"), dict):
                city = address_data["data"].get("city") or address_data["data"].get("region_with_type")
            state = d.get("state") or {}
            status_map = {
                "ACTIVE": "действующая",
                "LIQUIDATING": "ликвидируется",
                "LIQUIDATED": "ликвидирована",
                "BANKRUPT": "банкрот",
                "REORGANIZING": "реорганизация",
            }
            status = status_map.get(state.get("status", ""), "")
            results.append({"inn": inn, "name": name, "city": city, "status": status})

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
