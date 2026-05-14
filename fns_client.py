"""Клиент API-FNS.ru — официальные данные из ЕГРЮЛ/ЕГРИП по ИНН."""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import requests

from schemas import CompanyData

logger = logging.getLogger("financial-architect")

FNS_EGRUL_URL = "https://api-fns.ru/api/egr"


class FnsClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("FNS_API_KEY", "")
        self.timeout = float(os.getenv("FNS_TIMEOUT", "15"))

    async def fetch_company(self, inn: str) -> Optional[CompanyData]:
        """Получить данные о компании по ИНН из ЕГРЮЛ через API-FNS."""
        if not self.api_key:
            logger.warning("FNS_API_KEY not set, skipping FNS")
            return None

        def _call() -> Optional[Dict[str, Any]]:
            try:
                params = {
                    "req": inn,
                    "key": self.api_key,
                }
                resp = requests.get(
                    FNS_EGRUL_URL,
                    params=params,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    return resp.json()
                else:
                    logger.warning("FNS returned status %s: %s", resp.status_code, resp.text[:200])
            except Exception as exc:
                logger.warning("FNS request failed: %s", exc)
            return None

        raw = await asyncio.to_thread(_call)
        if not raw:
            return None

        return self._parse(raw, inn)

    @staticmethod
    def _parse(data: Dict[str, Any], inn: str) -> Optional[CompanyData]:
        """Парсинг ответа API-FNS в CompanyData."""
        items = data.get("items", [])
        if not items:
            logger.info("FNS: no results for INN %s", inn)
            return None

        item = items[0]

        # Юрлицо или ИП
        ul = item.get("ЮЛ") or {}
        ip = item.get("ИП") or {}

        if ul:
            return FnsClient._parse_ul(ul, inn)
        elif ip:
            return FnsClient._parse_ip(ip, inn)

        return None

    @staticmethod
    def _parse_ul(ul: Dict[str, Any], inn: str) -> CompanyData:
        """Парсинг данных юрлица."""
        # Название
        name = ul.get("НаимПолнЮЛ") or ul.get("НаимСокрЮЛ")

        # ОГРН
        ogrn = ul.get("ОГРН")

        # КПП
        kpp = ul.get("КПП")

        # Дата регистрации
        reg_date = ul.get("ДатаРег") or ul.get("ДатаОГРН")

        # Адрес
        address_data = ul.get("Адрес") or {}
        address = address_data.get("АдресПолн") or address_data.get("Адрес")
        if not address and isinstance(address_data, dict):
            # Собираем адрес из частей
            parts = []
            for key in ["Индекс", "Регион", "Город", "Улица", "Дом"]:
                val = address_data.get(key)
                if val:
                    parts.append(str(val))
            if parts:
                address = ", ".join(parts)

        # Регион
        region = address_data.get("Регион") if isinstance(address_data, dict) else None

        # ОКВЭД
        okved_main = None
        okved_name = None
        okved_data = ul.get("ОснВидДеят") or ul.get("ОснВидДеятельности")
        if isinstance(okved_data, dict):
            okved_main = okved_data.get("Код")
            okved_name = okved_data.get("Текст") or okved_data.get("Наим")
        # Альтернативный вариант: прямое поле
        if not okved_main:
            okved_main = ul.get("КодОКВЭД")

        # Руководитель
        director = None
        rukovoditel = ul.get("Руководитель") or {}
        if isinstance(rukovoditel, dict):
            fio = rukovoditel.get("ФИОПолн") or ""
            if not fio:
                parts = []
                for k in ["Фамилия", "Имя", "Отчество"]:
                    v = rukovoditel.get(k)
                    if v:
                        parts.append(v)
                fio = " ".join(parts)
            post = rukovoditel.get("Должн") or ""
            director = f"{post}: {fio}".strip(": ") if fio else None

        # Уставный капитал
        capital = None
        capital_data = ul.get("УстКап") or {}
        if isinstance(capital_data, dict):
            try:
                capital = float(capital_data.get("Сум") or capital_data.get("Размер") or 0)
            except (ValueError, TypeError):
                pass

        # Статус
        status = None
        status_data = ul.get("Статус") or ul.get("СвСтатус") or {}
        if isinstance(status_data, dict):
            status = status_data.get("Наим") or status_data.get("НаимСтатус")
        elif isinstance(status_data, str):
            status = status_data

        # Возраст
        age_years = None
        if reg_date:
            try:
                from datetime import datetime
                dt = datetime.strptime(reg_date, "%Y-%m-%d")
                age_years = (datetime.now() - dt).days // 365
            except Exception:
                pass

        return CompanyData(
            inn=ul.get("ИНН") or inn,
            name=name,
            ogrn=ogrn,
            region=region,
            address=address,
            reg_date=reg_date,
            age_years=age_years,
            okved_main=okved_main,
            okved_name=okved_name,
            director=director,
            status=status,
            kpp=kpp,
            capital=capital,
            source="fns",
        )

    @staticmethod
    def _parse_ip(ip: Dict[str, Any], inn: str) -> CompanyData:
        """Парсинг данных ИП."""
        # ФИО
        fio_parts = []
        for k in ["Фамилия", "Имя", "Отчество"]:
            v = ip.get(k)
            if v:
                fio_parts.append(v)
        name = "ИП " + " ".join(fio_parts) if fio_parts else None

        ogrn = ip.get("ОГРНИП")
        reg_date = ip.get("ДатаРег") or ip.get("ДатаОГРНИП")

        # ОКВЭД
        okved_main = ip.get("КодОКВЭД")

        # Статус
        status = None
        status_data = ip.get("Статус") or ip.get("СвСтатус") or {}
        if isinstance(status_data, dict):
            status = status_data.get("Наим")
        elif isinstance(status_data, str):
            status = status_data

        # Возраст
        age_years = None
        if reg_date:
            try:
                from datetime import datetime
                dt = datetime.strptime(reg_date, "%Y-%m-%d")
                age_years = (datetime.now() - dt).days // 365
            except Exception:
                pass

        return CompanyData(
            inn=ip.get("ИНН") or inn,
            name=name,
            ogrn=ogrn,
            reg_date=reg_date,
            age_years=age_years,
            okved_main=okved_main,
            status=status,
            source="fns",
        )
