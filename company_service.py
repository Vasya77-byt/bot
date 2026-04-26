"""Единый сервис получения данных о компании.

Объединяет данные из DaData, API ФНС и Руспрофайл.
Приоритет: DaData (база) → FNS (официальные) → Rusprofile (финансы + доп. данные).
Данные мержатся — пустые поля одного источника заполняются из другого.
"""

import logging
from typing import Optional

from dadata_client import DaDataClient
from fns_client import FnsClient
from rusprofile_client import RusprofileClient
from schemas import CompanyData

logger = logging.getLogger("financial-architect")


class CompanyService:
    def __init__(self) -> None:
        self.dadata = DaDataClient()
        self.fns = FnsClient()
        self.rusprofile = RusprofileClient()

    async def fetch(self, inn: str) -> Optional[CompanyData]:
        """Получить данные о компании из всех доступных источников."""
        results: list[CompanyData] = []

        # DaData — базовые данные (адрес, ОКВЭД, руководитель)
        try:
            dadata_result = await self.dadata.fetch_company(inn)
            if dadata_result:
                results.append(dadata_result)
                logger.info("DaData: found data for INN %s", inn)
        except Exception as exc:
            logger.warning("DaData error for INN %s: %s", inn, exc)

        # API ФНС — официальные данные из ЕГРЮЛ
        try:
            fns_result = await self.fns.fetch_company(inn)
            if fns_result:
                results.append(fns_result)
                logger.info("FNS: found data for INN %s", inn)
        except Exception as exc:
            logger.warning("FNS error for INN %s: %s", inn, exc)

        # Руспрофайл — финансовые данные и дополнительная информация
        try:
            rusprofile_result = await self.rusprofile.fetch_company(inn)
            if rusprofile_result:
                results.append(rusprofile_result)
                logger.info("Rusprofile: found data for INN %s", inn)
        except Exception as exc:
            logger.warning("Rusprofile error for INN %s: %s", inn, exc)

        if not results:
            logger.info("No data found for INN %s from any source", inn)
            return None

        # Мержим данные — первый результат как база, остальные дополняют
        merged = self._merge(results, inn)
        logger.info("Merged company data for INN %s from %d source(s)", inn, len(results))
        return merged

    @staticmethod
    def _merge(results: list[CompanyData], inn: str) -> CompanyData:
        """Объединяет данные из нескольких источников.

        Для каждого поля берётся первое непустое значение.
        Приоритет определяется порядком в списке results.
        """
        def pick(field: str):
            for r in results:
                val = getattr(r, field, None)
                if val is not None and val != "" and val != "не указано":
                    return val
            return None

        sources = [r.source for r in results if r.source]

        return CompanyData(
            inn=pick("inn") or inn,
            name=pick("name"),
            ogrn=pick("ogrn"),
            region=pick("region"),
            address=pick("address"),
            reg_date=pick("reg_date"),
            age_years=pick("age_years"),
            okved_main=pick("okved_main"),
            okved_name=pick("okved_name"),
            employees_count=pick("employees_count"),
            revenue_last_year=pick("revenue_last_year"),
            profit_last_year=pick("profit_last_year"),
            licenses=pick("licenses"),
            director=pick("director"),
            status=pick("status"),
            kpp=pick("kpp"),
            capital=pick("capital"),
            reliability_rating=pick("reliability_rating"),
            reliability_obligations=pick("reliability_obligations"),
            reliability_shell=pick("reliability_shell"),
            reliability_tax=pick("reliability_tax"),
            reliability_financial=pick("reliability_financial"),
            source="+".join(sources) if sources else None,
        )
