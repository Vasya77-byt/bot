"""Единый сервис получения данных о компании.

Объединяет данные из DaData, API ФНС, СБИС и ЗаЧестныйБизнес.
Приоритет: DaData (база) → FNS (официальные) → SBIS (финансы)
            → ZCHB (fallback/обогащение для крупных компаний, где
              остальные источники могут отдавать данные филиала).
Данные мержатся — пустые / нулевые поля одного источника заполняются
из следующего.
"""

import logging
from typing import List, Optional

from dadata_client import DaDataClient
from fns_client import FnsClient
from sbis_client import SbisClient
from schemas import CompanyData
from zchb_client import CardSummary, ZchbClient

logger = logging.getLogger("financial-architect")


# Поля, для которых 0 / 0.0 считается «нет данных» (а не «реально ноль»).
# Например, capital=0 у действующей крупной компании — почти наверняка
# глюк парсера, а не настоящий нулевой капитал. Но 0 как «прибыль» —
# валидное значение и оставляется как есть; для прибыли такой логики нет.
_FIELDS_WHERE_ZERO_IS_MISSING = ("capital", "employees_count", "revenue_last_year")


class CompanyService:
    def __init__(self) -> None:
        self.dadata = DaDataClient()
        self.fns = FnsClient()
        self.sbis = SbisClient()
        self.zchb = ZchbClient()

    async def fetch_quick(self, inn: str) -> Optional[CompanyData]:
        """Краткая проверка (L1): только базовый источник.

        Используется в режиме «Quick» — preview перед платным «Полным
        отчётом». Один платный запрос (DaData, ~1₽). Если DaData
        недоступна — fallback на ФНС (тоже ~1 запрос).

        ВАЖНО: cross-user кэш в DaData/ФНС-клиентах работает на оба
        пути, поэтому популярные ИНН могут отдаваться без сетевого
        вызова вообще.
        """
        try:
            result = await self.dadata.fetch_company(inn)
            if result is not None:
                logger.info("DaData quick: found data for INN %s", inn)
                return result
        except Exception as exc:
            logger.warning("DaData quick error for INN %s: %s", inn, exc)

        try:
            result = await self.fns.fetch_company(inn)
            if result is not None:
                logger.info("FNS quick fallback: found data for INN %s", inn)
                return result
        except Exception as exc:
            logger.warning("FNS quick fallback error for INN %s: %s", inn, exc)

        return None

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

        # СБИС — финансовые данные (если настроен)
        try:
            sbis_result = await self.sbis.fetch_company_data(inn)
            if sbis_result:
                results.append(sbis_result)
                logger.info("SBIS: found data for INN %s", inn)
        except Exception as exc:
            logger.warning("SBIS error for INN %s: %s", inn, exc)

        # ЗЧБ — fallback/обогащение. Идёт последним, перетирается только
        # пустыми/нулевыми полями более приоритетных источников.
        try:
            if self.zchb.enabled:
                card = await self.zchb.get_card(inn)
                if card is not None and (card.inn or card.ogrn):
                    converted = self._card_to_company(card, inn)
                    results.append(converted)
                    logger.info("ZCHB card: found data for INN %s", inn)
        except Exception as exc:
            logger.warning("ZCHB card error for INN %s: %s", inn, exc)

        if not results:
            logger.info("No data found for INN %s from any source", inn)
            return None

        # Мержим данные — первый результат как база, остальные дополняют
        merged = self._merge(results, inn)
        logger.info("Merged company data for INN %s from %d source(s)", inn, len(results))
        return merged

    @staticmethod
    def _card_to_company(card: CardSummary, inn: str) -> CompanyData:
        """Преобразует CardSummary ЗЧБ в CompanyData для участия в merge."""
        return CompanyData(
            inn=card.inn or inn,
            name=card.name_short or card.name_full or None,
            ogrn=card.ogrn or None,
            status=card.status or None,
            employees_count=card.employees_count or None,
            revenue_last_year=card.revenue_last_year or None,
            profit_last_year=card.profit_last_year or None,
            capital=card.capital or None,
            source="zchb",
        )

    async def suggest(self, query: str, count: int = 5) -> List[CompanyData]:
        """Поиск компаний по началу названия. Возвращает список
        кандидатов от DaData; пустой список при ошибке/пустом запросе."""
        if not query or not query.strip():
            return []
        try:
            return await self.dadata.suggest_by_name(query, count=count)
        except Exception as exc:
            logger.warning("CompanyService suggest failed for '%s': %s", query, exc)
            return []

    @staticmethod
    def _merge(results: list[CompanyData], inn: str) -> CompanyData:
        """Объединяет данные из нескольких источников.

        Для каждого поля берётся первое непустое значение.
        Приоритет определяется порядком в списке results.
        Для денежных/численных полей (capital, employees_count,
        revenue_last_year) ноль трактуется как «нет данных», чтобы
        не оставить 0₽ капитала у Сбера от глючного источника.
        """
        def pick(field: str):
            zero_is_missing = field in _FIELDS_WHERE_ZERO_IS_MISSING
            for r in results:
                val = getattr(r, field, None)
                if val is None or val == "" or val == "не указано":
                    continue
                if zero_is_missing and isinstance(val, (int, float)) and val == 0:
                    continue
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
            source="+".join(sources) if sources else None,
        )
