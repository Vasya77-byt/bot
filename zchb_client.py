"""Клиент API ЗаЧестныйБизнес (zachestnyibiznesapi.ru).

Поддерживаемые методы:
- court-arbitration — арбитражные дела (аналог kad.arbitr.ru)
- fssp / fssp-list — исполнительные производства
- rating — Индекс компании + налоговые риски
- card — расширенная карточка (статистика судов, госконтракты,
  проверки, недоимки, реестры массовых директоров и пр.)

Все методы кешируются на 24 часа по умолчанию (TTL переопределяется
через ZCHB_CACHE_TTL). Это критично — тарифы у ЗЧБ ограниченные,
а одна и та же компания часто проверяется повторно.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Dict, List, Optional

import requests

from api_quota import ApiQuotaExhausted, get_quota
from cache import FileTTLCache

logger = logging.getLogger("financial-architect")

ZCHB_BASE_URL = "https://zachestnyibiznesapi.ru/paid/data"

# Статусы из docs (answer-code-status-list). Перечислены те, что нам важны
# отдельно от просто success/failure.
ZCHB_STATUS_NO_CASES = {"220", "221"}     # «по данному ИНН не найдено судебных дел»
ZCHB_STATUS_NO_DATA = {"223", "224"}      # «не найдено информации»
ZCHB_STATUS_RATE_LIMIT = "239"            # «слишком частые запросы»
ZCHB_STATUS_QUOTA = "212"                 # лимит тарифа исчерпан
ZCHB_STATUS_KEY_INVALID = {"211", "215"}


def _restore_dataclass(cls, data: Dict[str, Any]):
    """Восстанавливает dataclass из dict (asdict-формата).
    Поддерживает один уровень вложенности — list[dataclass]."""
    fields_info = {f.name: f for f in cls.__dataclass_fields__.values()}
    kwargs: Dict[str, Any] = {}
    for k, v in data.items():
        if k not in fields_info:
            continue
        # Грубо: если это список и содержит dict — пытаемся определить
        # тип элемента и тоже восстановить
        if isinstance(v, list) and v and isinstance(v[0], dict):
            f = fields_info[k]
            type_repr = repr(f.type)
            # Поддержка ArbitrationCase в cases. Если других вложенных
            # списков dataclass'ов не появится — этого хватит.
            if "ArbitrationCase" in type_repr:
                v = [_restore_dataclass(ArbitrationCase, item) for item in v]
            elif "FsspProceeding" in type_repr:
                v = [_restore_dataclass(FsspProceeding, item) for item in v]
            elif "FounderInfo" in type_repr:
                v = [_restore_dataclass(FounderInfo, item) for item in v]
            elif "YearFinance" in type_repr:
                v = [_restore_dataclass(YearFinance, item) for item in v]
            elif "TaxDebtItem" in type_repr:
                v = [_restore_dataclass(TaxDebtItem, item) for item in v]
            elif "FlCompanyLink" in type_repr:
                v = [_restore_dataclass(FlCompanyLink, item) for item in v]
            elif "InspectionRecord" in type_repr:
                v = [_restore_dataclass(InspectionRecord, item) for item in v]
            elif "EgrulRecord" in type_repr:
                v = [_restore_dataclass(EgrulRecord, item) for item in v]
            elif "CompanyChangeEvent" in type_repr:
                v = [_restore_dataclass(CompanyChangeEvent, item) for item in v]
            # tax_violations_history — list[tuple] — оставляем как list[list]
            # из JSON, обработаем в рендерере как итерацию пар.
        kwargs[k] = v
    return cls(**kwargs)


@dataclass
class ArbitrationCase:
    """Одно судебное дело в сводке."""
    case_number: str           # "А40-178019/2016"
    case_uuid: str             # UUID для запроса карточки дела
    role: str                  # "истец" | "ответчик" | "третье лицо"
    sum_rub: int               # сумма иска в рублях (0 если не указана)
    started_at: str            # дата начала "26.08.2016"
    counterparty_name: str = ""  # короткое имя противоположной стороны
    counterparty_inn: str = ""   # ИНН противоположной стороны
    accuracy: str = "exact"      # "exact" — точно по ИНН, "fuzzy" — по имени


@dataclass
class ArbitrationSummary:
    """Агрегат по арбитражным делам компании."""
    # Точные совпадения по ИНН/ОГРН
    total_exact: int = 0
    # «Неточные» — по имени, могут содержать чужие дела (использовать с
    # осторожностью, скорее как индикатор)
    total_fuzzy: int = 0
    # Разбивка по роли (только по точным)
    as_plaintiff_count: int = 0
    as_defendant_count: int = 0
    # Суммы исков (только по точным)
    total_claim_sum: int = 0          # суммарно по всем точным
    plaintiff_claim_sum: int = 0      # как истец
    defendant_claim_sum: int = 0      # как ответчик
    # Список дел (ограниченный — обычно 50 на страницу). Не возвращаем
    # все 1000 дел в боте, рендерим топ-N по сумме / последним датам.
    cases: List[ArbitrationCase] = field(default_factory=list)

    @property
    def has_cases(self) -> bool:
        return self.total_exact > 0 or self.total_fuzzy > 0


@dataclass
class FsspProceeding:
    """Одно исполнительное производство."""
    case_number: str           # "26967/18/77021-ИП"
    started_at: int = 0        # unix timestamp возбуждения
    doc_type: str = ""         # тип исполнительного документа
    subject: str = ""          # предмет производства
    debt_total: float = 0.0    # сумма долга (руб)
    debt_remaining: float = 0.0  # остаток непогашенной задолженности (руб)
    department: str = ""       # отдел судебных приставов


@dataclass
class FsspSummary:
    """Сводка по ФССП."""
    total: int = 0
    total_debt: float = 0.0       # сумма всех долгов
    total_remaining: float = 0.0  # сумма остатков
    proceedings: List[FsspProceeding] = field(default_factory=list)

    @property
    def has_proceedings(self) -> bool:
        return self.total > 0


@dataclass
class RatingResult:
    """Результат метода rating."""
    rating_category: str = ""   # "высокий", "средний", "низкий"
    risk_level: str = ""        # уровень налоговых рисков


@dataclass
class CompanyChangeEvent:
    """Одно изменение из метода diffs ЗЧБ."""
    timestamp: int = 0           # unix timestamp когда зафиксировано
    date_iso: str = ""           # дата записи в ЕГРЮЛ (если есть)
    field_type: str = ""         # 'director' / 'founders' / 'address' /
                                 # 'okved_main' / 'okved_extra' / 'name' /
                                 # 'capital' / 'other'
    summary: str = ""            # человекочитаемое описание
    person_name: str = ""        # ФИО (для директора/учредителя)
    person_inn: str = ""         # ИНН ФЛ
    extra: str = ""              # дополнительная инфа (доля, должность)


@dataclass
class EgrulRecord:
    """Одна запись из ЕГРЮЛ (СвЗапЕГРЮЛ)."""
    grn: str = ""             # Государственный регистрационный номер
    record_id: str = ""       # ИдЗап
    date: str = ""            # ДатаЗап
    type_code: str = ""       # КодСПВЗ
    type_name: str = ""       # НаимВидЗап (например, "Создание ЮЛ")
    authority_code: str = ""  # КодНО
    authority_name: str = ""  # НаимНО


@dataclass
class InspectionRecord:
    """Одна проверка из Единого Реестра Проверок."""
    erp_id: str = ""              # учётный номер
    inspection_type: str = ""     # «Плановая проверка», «Внеплановая»
    fz: str = ""                  # «294 ФЗ», «248 ФЗ»
    prosecutor: str = ""          # наименование прокуратуры
    start_date: str = ""          # дата начала ISO
    end_date: str = ""            # дата окончания (если есть)
    status: str = ""              # «Завершена», «В работе»
    authority: str = ""           # орган контроля (ФРГУ)
    carryout_form: str = ""       # «Выездная», «Документарная»
    risk_category: str = ""       # «Умеренный риск (5 класс)»
    has_violations: bool = False  # выявлены ли нарушения


@dataclass
class FlCompanyLink:
    """Компания, связанная с физлицом (директор/учредитель/ИП)."""
    ogrn: str = ""
    inn: str = ""
    name_short: str = ""
    name_full: str = ""
    address: str = ""
    reg_date: str = ""           # ISO: "2003-04-22"
    is_active: bool = True       # «Действующее» = True


@dataclass
class FlCard:
    """Карточка физлица из ЗЧБ — список где этот человек директор/учредитель/ИП."""
    inn_fl: str = ""
    full_name: str = ""
    region_inn: str = ""         # регион получения ИНН
    region_business: str = ""    # регион ведения бизнеса
    is_mass_leader: bool = False
    is_mass_founder: bool = False
    leads: List[FlCompanyLink] = field(default_factory=list)
    founds: List[FlCompanyLink] = field(default_factory=list)
    sole_props: List[FlCompanyLink] = field(default_factory=list)


@dataclass
class YearFinance:
    """Финансы компании за один год."""
    year: int
    revenue: float = 0.0
    profit: float = 0.0
    income: float = 0.0       # для УСН (СумДоход)
    expense: float = 0.0      # для УСН (СумРасход)


@dataclass
class TaxDebtItem:
    """Запись о налоговой недоимке/задолженности."""
    tax_name: str = ""
    debt: float = 0.0          # СумНедНалог
    fines: float = 0.0         # СумПени
    penalties: float = 0.0     # СумШтраф
    total: float = 0.0         # ОбщСумНедоим


@dataclass
class FounderInfo:
    """Один учредитель компании."""
    name: str = ""              # ФИО или название ЮЛ
    inn: str = ""               # ИНН (может быть пустым у физлица-нерезидента)
    type: str = ""              # "fl" (физлицо) или "ul" (юрлицо)
    share_abs: float = 0.0      # номинальная доля в рублях
    share_pct: float = 0.0      # доля в процентах от уставного капитала
    is_mass: bool = False       # признак массового учредителя
    started_at: str = ""        # дата приобретения доли


@dataclass
class CardSummary:
    """Расширенные данные из метода card.

    Берём только те поля, что добавляют ценность поверх DaData/SBIS.
    Все суммы — в рублях, числа — целые.
    """
    # Идентификация (для матчинга с базовыми данными)
    inn: str = ""
    ogrn: str = ""
    name_full: str = ""
    name_short: str = ""
    status: str = ""

    # Реестры ФНС (флаги риска)
    in_debt_registry: bool = False           # Реестр01: есть взыскиваемая задолженность >1000₽
    in_no_reporting_registry: bool = False   # Реестр02: не сдаёт отчётность >1 года
    address_invalid: bool = False            # СвНедАдресЮЛ: адрес признан недостоверным

    # Статистика судов (СудыСтатистика — может быть в card)
    courts_total: int = 0

    # Госконтракты (ЗакупкиСтат)
    contracts_supplier_count: int = 0
    contracts_supplier_sum: float = 0.0
    contracts_customer_count: int = 0
    contracts_customer_sum: float = 0.0

    # Налоговые правонарушения и недоимки
    inspections_count: int = 0          # количество проверок (поле Проверки)
    tax_violations_sum: float = 0.0     # НалогПравонаруш — сумма штрафов
    tax_debt_sum: float = 0.0           # сумма недоимки/задолженности (СуммНедоимЗадолж)

    # Лицензии
    licenses_count: int = 0

    # Массовые директора/учредители (по первому руководителю)
    director_is_mass_leader: bool = False
    director_namesake_count: int = 0   # сколько тёзок-директоров с такими же ФИО
    # Текущий руководитель (для рендера в истории и связях)
    director_name: str = ""             # ФИО
    director_inn: str = ""              # ИНН ФЛ
    director_position: str = ""         # «ДИРЕКТОР», «ГЕНЕРАЛЬНЫЙ ДИРЕКТОР» и т.п.
    director_started_at: str = ""       # ISO-дата вступления в должность
    founder_is_mass: bool = False

    # Сотрудники, фонд, ЗП
    employees_count: int = 0
    payroll_fund: float = 0.0
    avg_salary: float = 0.0

    # Финансы (для дополнения DaData/FNS если те отдают подозрительные значения)
    capital: float = 0.0           # СумКап — уставный капитал
    revenue_last_year: float = 0.0   # ОсновПоказОтчетн.СумДоход (для УСН)
                                     # или последний год из ФО{YYYY}.ВЫРУЧКА
    profit_last_year: float = 0.0    # доход - расход (для УСН)
                                     # или ФО{YYYY}.ПРИБЫЛЬ

    # Признак недобросовестного поставщика
    is_unreliable_supplier: bool = False

    # Учредители (полный список с долями)
    founders: List[FounderInfo] = field(default_factory=list)
    # Сумма уставного капитала по СвУчредит.sumCap (для расчёта процентов)
    founders_sum_cap: float = 0.0

    # Категория МСП (Микро / Малое / Среднее предприятие; пусто для крупных)
    msp_category: str = ""

    # Налоговый режим (ОСНО, УСН, Патент, ЕНВД...)
    tax_regime: str = ""

    # Финансы по годам — для тренда. Одно значение в revenue/profit на год.
    finance_history: List[YearFinance] = field(default_factory=list)
    # История штрафов по годам (год → сумма)
    tax_violations_history: List[tuple] = field(default_factory=list)
    # Подробности налоговых недоимок (по налогам)
    tax_debt_items: List[TaxDebtItem] = field(default_factory=list)

    # История записей в ЕГРЮЛ (СвЗапЕГРЮЛ)
    egrul_records: List[EgrulRecord] = field(default_factory=list)


class ZchbClient:
    """Клиент API ЗЧБ с файловым кешем.

    Кеш ставится по паре (метод, ИНН/ОГРН). TTL общий для всех методов
    — обычно 24 часа. Чтобы инвалидировать раньше, удалите файл
    .cache/zchb.json или поднимите перезапись.
    """

    def __init__(self) -> None:
        self.api_key = os.getenv("ZCHB_API_KEY", "")
        self.timeout = float(os.getenv("ZCHB_TIMEOUT", "15"))
        self.base_url = os.getenv("ZCHB_BASE_URL", ZCHB_BASE_URL)
        ttl = float(os.getenv("ZCHB_CACHE_TTL", str(24 * 3600)))
        self._cache = FileTTLCache("zchb", ttl=ttl)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _cache_key(self, method: str, identifier: str) -> str:
        return f"{method}:{identifier}"

    def _cache_get(self, method: str, identifier: str, cls):
        raw = self._cache.get(self._cache_key(method, identifier))
        if raw is None or not isinstance(raw, dict):
            return None
        try:
            return _restore_dataclass(cls, raw)
        except Exception as exc:
            logger.warning("ZCHB cache restore failed for %s:%s: %s",
                           method, identifier, exc)
            return None

    def _cache_set(self, method: str, identifier: str, value) -> None:
        if value is None:
            return
        if not is_dataclass(value):
            return
        self._cache.set(self._cache_key(method, identifier), asdict(value))

    # ────────────────────────────────────────────────────────────────
    # Арбитражные дела
    # ────────────────────────────────────────────────────────────────

    async def get_arbitration(self, inn_or_ogrn: str) -> Optional[ArbitrationSummary]:
        """Получить сводку по арбитражным делам.

        Возвращает None если ключ не настроен. Возвращает пустой
        ArbitrationSummary если дел нет (статус 220/221) или произошла
        обрабатываемая ошибка API. None — только при отсутствии ключа.
        """
        if not self.enabled:
            logger.debug("ZCHB_API_KEY not set, skipping ZCHB")
            return None

        cached = self._cache_get("arbitration", inn_or_ogrn, ArbitrationSummary)
        if cached is not None:
            return cached

        raw = await asyncio.to_thread(self._call_arbitration, inn_or_ogrn)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_NO_CASES or status in ZCHB_STATUS_NO_DATA:
            return ArbitrationSummary()
        if status == ZCHB_STATUS_RATE_LIMIT or status == ZCHB_STATUS_QUOTA:
            logger.warning("ZCHB rate/quota: status=%s message=%s",
                           status, raw.get("message", ""))
            return ArbitrationSummary()
        if status in ZCHB_STATUS_KEY_INVALID:
            logger.error("ZCHB key invalid: %s", raw.get("message", ""))
            return None
        if status != "200":
            logger.warning("ZCHB unexpected status=%s message=%s",
                           status, raw.get("message", ""))
            return ArbitrationSummary()

        body = raw.get("body")
        if not isinstance(body, dict):
            return ArbitrationSummary()

        # ЗЧБ возвращает три разных формата:
        # 1) Поиск по ОГРН: body = {"точно": {...}, "неточно": {...}}
        # 2) Поиск по ИНН: body = {"total": N, "docs": [{"точно": ..., "неточно": ...}, ...]}
        # 3) Старый/нестандартный: body = {"0": {...}, "1": {...}}
        #
        # Нормализуем все варианты к формату 1.
        docs = body.get("docs")
        if isinstance(docs, list):
            if not docs:
                summary = ArbitrationSummary()
            else:
                summary = self._parse_multi_docs(docs, our_inn=inn_or_ogrn)
        elif body.keys() and all(k.isdigit() for k in body.keys()):
            summary = self._parse_multi_docs(
                list(body.values()), our_inn=inn_or_ogrn,
            )
        else:
            summary = self._parse_arbitration(body, our_inn=inn_or_ogrn)

        self._cache_set("arbitration", inn_or_ogrn, summary)
        return summary

    @staticmethod
    def _parse_multi_docs(
        docs: List[Any], our_inn: str,
    ) -> ArbitrationSummary:
        """Объединяет несколько секций (по компаниям) в одну сводку."""
        merged = ArbitrationSummary()
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            sub = ZchbClient._parse_arbitration(doc, our_inn=our_inn)
            merged.total_exact += sub.total_exact
            merged.total_fuzzy += sub.total_fuzzy
            merged.as_plaintiff_count += sub.as_plaintiff_count
            merged.as_defendant_count += sub.as_defendant_count
            merged.total_claim_sum += sub.total_claim_sum
            merged.plaintiff_claim_sum += sub.plaintiff_claim_sum
            merged.defendant_claim_sum += sub.defendant_claim_sum
            merged.cases.extend(sub.cases)
        return merged

    def _call_arbitration(self, inn_or_ogrn: str) -> Optional[Dict[str, Any]]:
        try:
            get_quota().check("zchb")
        except ApiQuotaExhausted as exc:
            logger.warning("ZCHB arbitration skipped: %s", exc)
            return None
        url = f"{self.base_url}/court-arbitration"
        params = {
            "id": inn_or_ogrn,
            "api_key": self.api_key,
            "_format": "json",
        }
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                get_quota().record("zchb")
                return resp.json()
            logger.warning("ZCHB HTTP %s: %s", resp.status_code, resp.text[:300])
        except requests.RequestException as exc:
            logger.warning("ZCHB request failed: %s", exc)
        except ValueError as exc:
            # JSONDecodeError extends ValueError
            logger.warning("ZCHB invalid JSON: %s", exc)
        return None

    @staticmethod
    def _parse_arbitration(
        body: Dict[str, Any], our_inn: str
    ) -> ArbitrationSummary:
        """Парсит ответ метода court-arbitration в сводку."""
        result = ArbitrationSummary()

        for accuracy_key, accuracy_label in (("точно", "exact"), ("неточно", "fuzzy")):
            section = body.get(accuracy_key)
            if not isinstance(section, dict):
                continue
            total = int(section.get("всего", 0) or 0)
            if accuracy_label == "exact":
                result.total_exact = total
            else:
                result.total_fuzzy = total

            cases_dict = section.get("дела")
            if not isinstance(cases_dict, dict):
                continue

            for uuid, case in cases_dict.items():
                if not isinstance(case, dict):
                    continue
                parsed = ZchbClient._parse_case(uuid, case, our_inn, accuracy_label)
                if parsed is None:
                    continue
                result.cases.append(parsed)
                if accuracy_label != "exact":
                    continue
                if parsed.role == "истец":
                    result.as_plaintiff_count += 1
                    result.plaintiff_claim_sum += parsed.sum_rub
                elif parsed.role == "ответчик":
                    result.as_defendant_count += 1
                    result.defendant_claim_sum += parsed.sum_rub
                result.total_claim_sum += parsed.sum_rub

        return result

    @staticmethod
    def _parse_case(
        uuid: str, raw: Dict[str, Any], our_inn: str, accuracy: str,
    ) -> Optional[ArbitrationCase]:
        """Извлекает наши данные из одного дела. Определяет роль по ИНН."""
        case_number = str(raw.get("НомерДела", "")).strip()
        if not case_number:
            return None

        sum_rub = 0
        sum_raw = raw.get("СуммаИска", 0)
        try:
            sum_rub = int(sum_raw or 0)
        except (TypeError, ValueError):
            sum_rub = 0

        role = ""
        counterparty_name = ""
        counterparty_inn = ""

        for role_key, role_label in (
            ("Истец", "истец"),
            ("Ответчик", "ответчик"),
            ("Третье лицо", "третье лицо"),
        ):
            participants = raw.get(role_key)
            if not isinstance(participants, list):
                continue
            for p in participants:
                if not isinstance(p, dict):
                    continue
                inn = str(p.get("ИНН") or "").strip()
                if inn == our_inn:
                    role = role_label
                else:
                    # Запоминаем первого «не нас» как контрагента
                    if not counterparty_name:
                        counterparty_name = str(p.get("Наименование") or "").strip()
                        counterparty_inn = inn

        # Если по ИНН не нашли (например, fuzzy-секция) — попытаемся
        # выставить роль эвристикой: если у нас есть истец, считаем нас
        # стороной, противоположной первому участнику в роли
        if not role and accuracy == "exact":
            # Запасной случай — оставим пустым, чтобы не врать
            role = ""

        return ArbitrationCase(
            case_number=case_number,
            case_uuid=uuid,
            role=role,
            sum_rub=sum_rub,
            started_at=str(raw.get("СтартДата", "")),
            counterparty_name=counterparty_name,
            counterparty_inn=counterparty_inn,
            accuracy=accuracy,
        )

    # ────────────────────────────────────────────────────────────────
    # Rating — Индекс компании + налоговые риски
    # ────────────────────────────────────────────────────────────────

    async def get_rating(self, inn_or_ogrn: str) -> Optional[RatingResult]:
        """Возвращает Индекс ЗЧБ + уровень налоговых рисков.
        В документации сказано: «По факту id должен быть ОГРН» — но на
        практике часто принимает и ИНН. Дёшево (1 запрос)."""
        if not self.enabled:
            return None

        cached = self._cache_get("rating", inn_or_ogrn, RatingResult)
        if cached is not None:
            return cached

        raw = await asyncio.to_thread(self._simple_call, "rating", inn_or_ogrn)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_KEY_INVALID:
            logger.error("ZCHB key invalid: %s", raw.get("message", ""))
            return None
        if status != "200":
            logger.warning("ZCHB rating status=%s message=%s",
                           status, raw.get("message", ""))
            return RatingResult()

        body = raw.get("body") or {}
        if not isinstance(body, dict):
            return RatingResult()
        result = RatingResult(
            rating_category=str(body.get("rating_category") or ""),
            risk_level=str(body.get("risk_level") or ""),
        )
        self._cache_set("rating", inn_or_ogrn, result)
        return result

    # ────────────────────────────────────────────────────────────────
    # FNS-card — полная карточка ФНС (10 запросов!) с СвЗапЕГРЮЛ
    # ────────────────────────────────────────────────────────────────

    async def get_fns_card_egrul(self, ogrn: str) -> Optional[List[EgrulRecord]]:
        """Полная карточка ФНС — для извлечения СвЗапЕГРЮЛ (история записей).
        ВНИМАНИЕ: тарифицируется как 10 запросов. Идентификатор — ОГРН/ОГРНИП."""
        if not self.enabled or not ogrn:
            return None

        cached_dict = self._cache.get(self._cache_key("fns_card_egrul", ogrn))
        if isinstance(cached_dict, list):
            try:
                return [_restore_dataclass(EgrulRecord, item) for item in cached_dict]
            except Exception:
                pass

        raw = await asyncio.to_thread(self._simple_call, "fns-card", ogrn)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_KEY_INVALID:
            return None
        if status != "200":
            logger.warning("ZCHB fns-card status=%s message=%s",
                           status, raw.get("message", ""))
            return []

        body = raw.get("body") or {}
        if isinstance(body, list):
            body = body[0] if body else {}
        if not isinstance(body, dict):
            return []

        records: List[EgrulRecord] = []
        egrul = body.get("СвЗапЕГРЮЛ")
        if isinstance(egrul, list):
            for item in egrul:
                if not isinstance(item, dict):
                    continue
                attrs = item.get("@attributes") or {}
                vid = item.get("ВидЗап") or {}
                vid_attrs = (vid.get("@attributes") if isinstance(vid, dict) else {}) or {}
                regorg = item.get("СвРегОрг") or {}
                ro_attrs = (regorg.get("@attributes") if isinstance(regorg, dict) else {}) or {}
                records.append(EgrulRecord(
                    grn=str(attrs.get("ГРН") or ""),
                    record_id=str(attrs.get("ИдЗап") or ""),
                    date=str(attrs.get("ДатаЗап") or ""),
                    type_code=str(vid_attrs.get("КодСПВЗ") or ""),
                    type_name=str(vid_attrs.get("НаимВидЗап") or ""),
                    authority_code=str(ro_attrs.get("КодНО") or ""),
                    authority_name=str(ro_attrs.get("НаимНО") or ""),
                ))

        self._cache.set(
            self._cache_key("fns_card_egrul", ogrn),
            [asdict(r) for r in records],
        )
        return records

    # ────────────────────────────────────────────────────────────────
    # Proverki — Единый Реестр Проверок
    # ────────────────────────────────────────────────────────────────

    async def get_inspections(self, inn_or_ogrn: str) -> Optional[List[InspectionRecord]]:
        """История проверок из ЕРП.
        Принимает ИНН/ОГРН/ИННФЛ/ОГРНИП."""
        if not self.enabled:
            return None

        cached_dict = self._cache.get(self._cache_key("inspections", inn_or_ogrn))
        if isinstance(cached_dict, list):
            try:
                return [_restore_dataclass(InspectionRecord, item) for item in cached_dict]
            except Exception:
                pass  # упадём на свежий запрос

        raw = await asyncio.to_thread(self._simple_call, "proverki", inn_or_ogrn)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_KEY_INVALID:
            return None
        if status != "200":
            logger.warning("ZCHB proverki status=%s message=%s",
                           status, raw.get("message", ""))
            return []

        body = raw.get("body")
        # ЗЧБ для proverki возвращает массив проверок прямо в body
        records: List[InspectionRecord] = []
        if isinstance(body, list):
            for inspection in body:
                rec = self._parse_inspection(inspection)
                if rec is not None:
                    records.append(rec)
        elif isinstance(body, dict):
            # Иногда обёрнуто в {"item": [...]} или один объект
            items = body.get("item") or body.get("docs") or [body]
            if isinstance(items, list):
                for inspection in items:
                    rec = self._parse_inspection(inspection)
                    if rec is not None:
                        records.append(rec)

        # Кешируем как list[asdict]
        self._cache.set(
            self._cache_key("inspections", inn_or_ogrn),
            [asdict(r) for r in records],
        )
        return records

    @staticmethod
    def _parse_inspection(raw: Dict[str, Any]) -> Optional[InspectionRecord]:
        """Парсит одну запись из ответа метода proverki.

        Реальная структура ЗЧБ (XML→JSON): все поля в @attributes блоках.
        Пример record["@attributes"]["ERPID"], record["I_AUTHORITY"]["@attributes"]["FRGU_ORG_NAME"].
        """
        if not isinstance(raw, dict):
            return None

        attrs = raw.get("@attributes")
        if not isinstance(attrs, dict):
            attrs = {}

        rec = InspectionRecord(
            erp_id=str(attrs.get("ERPID") or ""),
            inspection_type=str(attrs.get("ITYPE_NAME") or ""),
            fz=str(attrs.get("FZ_NAME") or ""),
            prosecutor=str(attrs.get("PROSEC_NAME") or ""),
            start_date=str(attrs.get("START_DATE") or ""),
            status=str(attrs.get("STATUS") or ""),
        )

        def _attrs(node: Any) -> Dict[str, Any]:
            """Достаёт @attributes из dict, либо пустой dict."""
            if isinstance(node, dict):
                a = node.get("@attributes")
                if isinstance(a, dict):
                    return a
            return {}

        # I_AUTHORITY — орган контроля
        auth = _attrs(raw.get("I_AUTHORITY"))
        rec.authority = str(auth.get("FRGU_ORG_NAME") or "")

        # I_CLASSIFICATION — форма проведения и категория риска
        cls_attrs = _attrs(raw.get("I_CLASSIFICATION"))
        rec.carryout_form = str(cls_attrs.get("ICARRYOUT_TYPE_NAME") or "")
        rec.risk_category = str(cls_attrs.get("IRISK_NAME") or "")
        # Если форма не указана — берём вид надзора (полезный контекст)
        if not rec.carryout_form:
            rec.carryout_form = str(cls_attrs.get("ISUPERVISION_NAME") or "")

        # I_OBJECT может быть dict или list. Обходим всё.
        objects = raw.get("I_OBJECT")
        if isinstance(objects, dict):
            objects = [objects]
        if isinstance(objects, list):
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                # I_RESULT тоже может быть dict или list
                res_block = obj.get("I_RESULT")
                if isinstance(res_block, dict):
                    res_block = [res_block]
                if isinstance(res_block, list):
                    for res in res_block:
                        if not isinstance(res, dict):
                            continue
                        res_attrs = _attrs(res)
                        end = str(res_attrs.get("ACT_DATE_CREATE") or "")
                        if end and not rec.end_date:
                            rec.end_date = end
                        # I_VIOLATION есть всегда, но поля null если нарушений нет
                        viol_attrs = _attrs(res.get("I_VIOLATION"))
                        if (viol_attrs.get("VIOLATION_NOTE")
                                or viol_attrs.get("IVIOLATION_TYPE_NAME")):
                            rec.has_violations = True

        return rec

    # ────────────────────────────────────────────────────────────────
    # Diffs — лента изменений компании в ЕГРЮЛ
    # ────────────────────────────────────────────────────────────────

    # Карта интересных нод -> тип события
    _DIFF_FIELD_TYPES = {
        "СвНаимЮЛ":      "name",
        "СведДолжнФЛ":   "director",
        "Руководители":  "director",
        "СвУчредит":     "founders",
        "УчрФЛ":         "founders",
        "СвАдресЮЛ":     "address",
        "АдресРФ":       "address",
        "СвОКВЭД":       "okved",
        "СвОКВЭДОсн":    "okved_main",
        "СвОКВЭДДоп":    "okved_extra",
        "СвУстКап":      "capital",
        "СвРеорг":       "reorganization",
    }

    async def get_diffs(self, ogrn: str) -> Optional[List[CompanyChangeEvent]]:
        """Лента всех изменений компании в ЕГРЮЛ через метод diffs ЗЧБ.
        Принимает ТОЛЬКО ОГРН/ОГРНИП.

        Возвращает упорядоченный по дате убывания список значимых
        изменений (директор, учредители, адрес, ОКВЭД, наименование,
        уставный капитал)."""
        if not self.enabled or not ogrn:
            return None

        cached_dict = self._cache.get(self._cache_key("diffs", ogrn))
        if isinstance(cached_dict, list):
            try:
                return [_restore_dataclass(CompanyChangeEvent, item)
                        for item in cached_dict]
            except Exception:
                pass

        raw = await asyncio.to_thread(self._simple_call, "diffs", ogrn)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_KEY_INVALID:
            return None
        if status != "200":
            logger.warning("ZCHB diffs status=%s message=%s",
                           status, raw.get("message", ""))
            return []

        body = raw.get("body")
        if not isinstance(body, dict):
            return []

        events: List[CompanyChangeEvent] = []
        for ts_str, day_events in body.items():
            try:
                ts = int(ts_str)
            except (TypeError, ValueError):
                ts = 0
            if not isinstance(day_events, list):
                continue
            for event in day_events:
                events.extend(self._extract_change_events(event, ts))

        # Дедупликация (одинаковые события за один и тот же timestamp)
        seen = set()
        unique: List[CompanyChangeEvent] = []
        for ev in events:
            key = (ev.timestamp, ev.field_type, ev.summary, ev.person_inn)
            if key in seen:
                continue
            seen.add(key)
            unique.append(ev)

        unique.sort(key=lambda e: e.timestamp, reverse=True)
        self._cache.set(
            self._cache_key("diffs", ogrn),
            [asdict(e) for e in unique],
        )
        return unique

    def _extract_change_events(
        self, event: Any, ts: int,
    ) -> List[CompanyChangeEvent]:
        """Извлекает CompanyChangeEvent из одной записи diffs.

        ZCHB шлёт два вида источников:
        - basicData: готовый человекочитаемый text
        - egrul_diff_runtime: структурированный diff с ins/del/upd
        """
        if not isinstance(event, dict):
            return []
        source = event.get("source", "")
        data = event.get("data")
        if source == "basicData":
            return self._parse_basic_data(data, ts)
        if source == "egrul_diff_runtime":
            return self._parse_egrul_diff(data, ts)
        return []

    @staticmethod
    def _parse_basic_data(data: Any, ts: int) -> List[CompanyChangeEvent]:
        """Парсит basicData — список текстовых описаний.
        Фильтруем только значимые типы изменений по ключевым словам."""
        if not isinstance(data, list):
            return []
        events: List[CompanyChangeEvent] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            tlow = text.lower()
            field_type = "other"
            # Ключевые слова → тип события
            if "руководител" in tlow or "директор" in tlow or "глав" in tlow:
                field_type = "director"
            elif "учредител" in tlow or "участник" in tlow or "доля" in tlow:
                field_type = "founders"
            elif "адрес" in tlow:
                field_type = "address"
            elif "оквэд" in tlow or "вид деятельност" in tlow:
                field_type = "okved"
            elif "наименован" in tlow or "название" in tlow:
                field_type = "name"
            elif "капитал" in tlow:
                field_type = "capital"
            elif "реорганиз" in tlow or "ликвид" in tlow:
                field_type = "reorganization"
            else:
                continue  # пропускаем мусорные изменения (товарные знаки, проверки и т.п.)

            events.append(CompanyChangeEvent(
                timestamp=ts,
                field_type=field_type,
                summary=text[:500],
            ))
        return events

    def _parse_egrul_diff(
        self, data: Any, ts: int,
    ) -> List[CompanyChangeEvent]:
        """Парсит egrul_diff_runtime — структурированный diff."""
        if not isinstance(data, dict):
            return []
        node = data.get("node", "")
        diff = data.get("diff")
        if not isinstance(diff, dict):
            return []

        field_type = self._DIFF_FIELD_TYPES.get(node, "other")
        if field_type == "other":
            return []

        events: List[CompanyChangeEvent] = []
        # Рекурсивно собираем все ins/upd-блоки с @attributes
        for ins_block, grn_date in self._collect_ins_blocks(diff):
            ev = self._build_event_from_attrs(
                field_type, ins_block, grn_date, ts,
            )
            if ev is not None:
                events.append(ev)
        return events

    @staticmethod
    def _collect_ins_blocks(node: Any, depth: int = 0) -> List[tuple]:
        """Рекурсивно собирает все блоки с @attributes под ins/upd-вложенностью.
        Возвращает список (attrs_dict, grn_date_str)."""
        results: List[tuple] = []
        if depth > 8:  # защита от бесконечной рекурсии
            return results
        if isinstance(node, dict):
            # Если есть ins — это новое значение, парсим его
            ins = node.get("ins")
            if isinstance(ins, dict):
                attrs = ins.get("@attributes")
                if isinstance(attrs, dict):
                    grn_date = ""
                    grn = ins.get("ГРНДата")
                    if isinstance(grn, dict):
                        grn_attrs = grn.get("@attributes") or {}
                        grn_date = str(grn_attrs.get("ДатаЗаписи") or "")
                    results.append((attrs, grn_date))
                # Идём ещё глубже в ins (там тоже могут быть вложенности)
                results.extend(
                    ZchbClient._collect_ins_blocks(ins, depth + 1)
                )
            # Также ищем в upd рекурсивно
            upd = node.get("upd")
            if isinstance(upd, dict):
                results.extend(
                    ZchbClient._collect_ins_blocks(upd, depth + 1)
                )
            # Прочие ключи могут содержать вложенности
            for k, v in node.items():
                if k in ("ins", "upd", "del", "@attributes", "ГРНДата"):
                    continue
                if isinstance(v, (dict, list)):
                    results.extend(
                        ZchbClient._collect_ins_blocks(v, depth + 1)
                    )
        elif isinstance(node, list):
            for item in node:
                results.extend(
                    ZchbClient._collect_ins_blocks(item, depth + 1)
                )
        return results

    @staticmethod
    def _build_event_from_attrs(
        field_type: str, attrs: Dict[str, Any], grn_date: str, ts: int,
    ) -> Optional[CompanyChangeEvent]:
        """Строит CompanyChangeEvent из @attributes ins-блока."""
        if not isinstance(attrs, dict):
            return None

        ev = CompanyChangeEvent(
            timestamp=ts, date_iso=grn_date, field_type=field_type,
        )

        if field_type == "director":
            fio_parts = [
                attrs.get("Фамилия"), attrs.get("Имя"), attrs.get("Отчество"),
            ]
            fio = " ".join(p for p in fio_parts if p).strip()
            if not fio:
                return None
            ev.person_name = fio
            ev.person_inn = str(attrs.get("ИННФЛ") or "").strip()
            position = (attrs.get("НаимДолжн") or
                        attrs.get("НаимВидДолжн") or "")
            ev.extra = str(position).strip()
            ev.summary = fio + (f" — {ev.extra}" if ev.extra else "")
            return ev

        if field_type == "founders":
            # Проверяем что это блок учредителя ФЛ
            fio_parts = [
                attrs.get("Фамилия"), attrs.get("Имя"), attrs.get("Отчество"),
            ]
            fio = " ".join(p for p in fio_parts if p).strip()
            if fio:
                ev.person_name = fio
                ev.person_inn = str(attrs.get("ИННФЛ") or "").strip()
                ev.summary = fio
                return ev
            # ЮЛ-учредитель
            name = attrs.get("НаимЮЛПолн") or attrs.get("НаимЮЛСокр")
            if name:
                ev.person_name = str(name)
                ev.person_inn = str(attrs.get("ИНН") or "").strip()
                ev.summary = str(name)
                return ev
            return None

        if field_type == "address":
            parts = [
                attrs.get("Индекс"),
                attrs.get("НаимРегион") and f"{attrs.get('ТипРегион') or ''} {attrs.get('НаимРегион')}".strip(),
                attrs.get("НаимГород") and f"{attrs.get('ТипГород') or ''} {attrs.get('НаимГород')}".strip(),
                attrs.get("НаимУлица") and f"{attrs.get('ТипУлица') or ''} {attrs.get('НаимУлица')}".strip(),
                attrs.get("Дом") and f"д. {attrs.get('Дом')}",
                attrs.get("Корпус") and f"корп. {attrs.get('Корпус')}",
                attrs.get("Кварт") and f"кв./офис {attrs.get('Кварт')}",
            ]
            address = ", ".join(p for p in parts if p)
            if not address.strip():
                return None
            ev.summary = address
            return ev

        if field_type in ("okved", "okved_main", "okved_extra"):
            code = attrs.get("КодОКВЭД")
            name = attrs.get("НаимОКВЭД")
            if not code:
                return None
            ev.summary = f"{code}" + (f" — {name}" if name else "")
            return ev

        if field_type == "capital":
            cap = attrs.get("СумКап")
            kind = attrs.get("НаимВидКап")
            if cap is None:
                return None
            try:
                cap_n = float(cap)
                cap_str = f"{int(cap_n):,}".replace(",", " ") + " ₽"
            except (TypeError, ValueError):
                cap_str = str(cap)
            ev.summary = cap_str
            ev.extra = str(kind or "")
            return ev

        if field_type == "name":
            full = attrs.get("НаимЮЛПолн") or ""
            short = attrs.get("НаимЮЛСокр") or ""
            display = short or full
            if not display:
                return None
            ev.summary = str(display)
            return ev

        if field_type == "reorganization":
            status_name = attrs.get("НаимСтатусЮЛ") or attrs.get("СостЮЛпосле")
            if not status_name:
                return None
            ev.summary = str(status_name)
            return ev

        return None

    # ────────────────────────────────────────────────────────────────
    # FL-card — карточка физлица (директор/учредитель)
    # ────────────────────────────────────────────────────────────────

    async def get_fl_card(self, inn_fl: str) -> Optional[FlCard]:
        """Получить карточку физлица. inn_fl — 12-значный ИНН ФЛ.
        Возвращает все компании где этот человек руководит/учредитель/ИП."""
        if not self.enabled or not inn_fl:
            return None

        cached = self._cache_get("fl_card", inn_fl, FlCard)
        if cached is not None:
            return cached

        raw = await asyncio.to_thread(self._simple_call, "fl-card", inn_fl)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_KEY_INVALID:
            return None
        if status in {"248", "249"}:
            # «Введен неверный ИННФЛ» / «По данному ИННФЛ ничего не найдено»
            empty = FlCard(inn_fl=inn_fl)
            self._cache_set("fl_card", inn_fl, empty)
            return empty
        if status != "200":
            logger.warning("ZCHB fl-card status=%s message=%s",
                           status, raw.get("message", ""))
            return FlCard(inn_fl=inn_fl)

        body = raw.get("body") or {}
        if not isinstance(body, dict):
            return FlCard(inn_fl=inn_fl)

        result = FlCard(
            inn_fl=str(body.get("ИННФЛ") or inn_fl),
            full_name=str(body.get("ФИО") or ""),
            region_inn=str(body.get("РегионПолучИНН") or ""),
            region_business=str(body.get("РегионВедБизнеса") or ""),
            is_mass_leader=bool(body.get("МассРуководитель")),
            is_mass_founder=bool(body.get("МассУчредитель")),
        )
        for key, target in (
            ("Руководитель", result.leads),
            ("Учредитель", result.founds),
            ("ИП", result.sole_props),
        ):
            items = body.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                target.append(FlCompanyLink(
                    ogrn=str(item.get("ОГРН") or ""),
                    inn=str(item.get("ИНН") or ""),
                    name_short=str(item.get("НаимЮЛСокр") or ""),
                    name_full=str(item.get("НаимЮЛПолн") or ""),
                    address=str(item.get("Адрес") or ""),
                    reg_date=str(item.get("ДатаРег") or ""),
                    is_active=("действ" in str(item.get("Активность") or "").lower()),
                ))

        self._cache_set("fl_card", inn_fl, result)
        return result

    # ────────────────────────────────────────────────────────────────
    # FSSP — исполнительные производства
    # ────────────────────────────────────────────────────────────────

    async def get_fssp(self, ogrn: str) -> Optional[FsspSummary]:
        """Список исполнительных производств. Принимает ТОЛЬКО ОГРН.
        Использует fssp-list — он отдаёт total + docs (для пагинации).
        """
        if not self.enabled:
            return None

        cached = self._cache_get("fssp", ogrn, FsspSummary)
        if cached is not None:
            return cached

        raw = await asyncio.to_thread(self._simple_call, "fssp-list", ogrn)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_KEY_INVALID:
            return None
        if status == "235":
            # «По данному ОГРН не найдено исполнительных производств»
            empty = FsspSummary()
            self._cache_set("fssp", ogrn, empty)
            return empty
        if status != "200":
            logger.warning("ZCHB fssp status=%s message=%s",
                           status, raw.get("message", ""))
            return FsspSummary()

        body = raw.get("body") or {}
        if not isinstance(body, dict):
            return FsspSummary()
        total = int(body.get("total", 0) or 0)
        docs = body.get("docs") or []
        summary = FsspSummary(total=total)
        if isinstance(docs, list):
            for item in docs:
                if not isinstance(item, dict):
                    continue
                proc = self._parse_fssp_item(item)
                summary.proceedings.append(proc)
                summary.total_debt += proc.debt_total
                summary.total_remaining += proc.debt_remaining
        self._cache_set("fssp", ogrn, summary)
        return summary

    @staticmethod
    def _parse_fssp_item(raw: Dict[str, Any]) -> FsspProceeding:
        def _money(v):
            try:
                return float(str(v).replace(",", ".").replace(" ", "") or 0)
            except (TypeError, ValueError):
                return 0.0

        def _ts(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        return FsspProceeding(
            case_number=str(raw.get("НомИспПроизв") or ""),
            started_at=_ts(raw.get("ДатаВозбуждения")),
            doc_type=str(raw.get("ТипИспДок") or ""),
            subject=str(raw.get("ПредметИсп") or ""),
            debt_total=_money(raw.get("СуммаДолга")),
            debt_remaining=_money(raw.get("ОстатокДолга")),
            department=str(raw.get("ОтделСудебПрист") or ""),
        )

    # ────────────────────────────────────────────────────────────────
    # Card — расширенная карточка компании
    # ────────────────────────────────────────────────────────────────

    async def get_card(self, inn_or_ogrn: str) -> Optional[CardSummary]:
        """Расширенная карточка ЗЧБ. 1 запрос, но даёт сразу много полей.

        При поиске по ИНН ответ имеет уровень body[0/1/...] (если несколько
        компаний под ИНН). Берём первую запись.
        """
        if not self.enabled:
            return None

        cached = self._cache_get("card", inn_or_ogrn, CardSummary)
        if cached is not None:
            return cached

        raw = await asyncio.to_thread(self._simple_call, "card", inn_or_ogrn)
        if raw is None:
            return None

        status = str(raw.get("status", ""))
        if status in ZCHB_STATUS_KEY_INVALID:
            return None
        if status != "200":
            logger.warning("ZCHB card status=%s message=%s",
                           status, raw.get("message", ""))
            return CardSummary()

        body = raw.get("body")
        if not isinstance(body, dict):
            return CardSummary()
        # ЗЧБ может возвращать обёртки трёх форматов (как и в arbitration):
        # 1) Прямые поля компании
        # 2) {"total": N, "docs": [{...}]} при поиске по ИНН
        # 3) {"0": {...}, "1": {...}} — старый/нестандартный
        docs = body.get("docs")
        if isinstance(docs, list):
            if not docs or not isinstance(docs[0], dict):
                return CardSummary()
            body = docs[0]
        elif body.keys() and all(k.isdigit() for k in body.keys()):
            first = next(iter(body.values()), None)
            if isinstance(first, dict):
                body = first
            else:
                return CardSummary()

        result = self._parse_card(body)
        # Диагностика: если ИНН/ОГРН в результате пустые — значит парсер
        # не нашёл нужных полей. Логируем чтобы было видно.
        if not result.inn and not result.ogrn:
            logger.warning(
                "ZCHB card returned no inn/ogrn for %s; top-level keys: %s",
                inn_or_ogrn, list(body.keys())[:10],
            )
        self._cache_set("card", inn_or_ogrn, result)
        return result

    @staticmethod
    def _parse_card(body: Dict[str, Any]) -> CardSummary:
        def _money(v):
            if v is None:
                return 0.0
            if isinstance(v, (int, float)):
                return float(v)
            try:
                cleaned = str(v).replace(",", ".").replace(" ", "")
                return float(cleaned or 0)
            except (TypeError, ValueError):
                return 0.0

        def _int(v, default=0):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return default

        result = CardSummary(
            inn=str(body.get("ИНН") or ""),
            ogrn=str(body.get("ОГРН") or ""),
            name_full=str(body.get("НаимЮЛПолн") or ""),
            name_short=str(body.get("НаимЮЛСокр") or ""),
            status=str(body.get("Активность") or ""),
            in_debt_registry=str(body.get("Реестр01") or "0") == "1",
            in_no_reporting_registry=str(body.get("Реестр02") or "0") == "1",
            address_invalid=bool(body.get("СвНедАдресЮЛ")),
            licenses_count=_int(body.get("СвЛицензия")),
            inspections_count=_int(body.get("Проверки")),
            employees_count=_int(body.get("ЧислСотруд")),
            payroll_fund=_money(body.get("ФондОплТруда")),
            avg_salary=_money(body.get("СредЗП")),
            tax_violations_sum=_money(body.get("НалогПравонаруш")),
            is_unreliable_supplier=bool(body.get("НедобросовПостав")),
            capital=_money(body.get("СумКап")),
        )

        # Финансы: пробуем сначала ОсновПоказОтчетн (УСН-формат), потом
        # перебираем ФО{год} и берём самый свежий с ненулевыми значениями.
        op = body.get("ОсновПоказОтчетн")
        if isinstance(op, list) and op:
            first_op = op[0] if isinstance(op[0], dict) else None
            if first_op:
                income = _money(first_op.get("СумДоход"))
                expense = _money(first_op.get("СумРасход"))
                if income > 0:
                    result.revenue_last_year = income
                    result.profit_last_year = income - expense

        if result.revenue_last_year == 0:
            # Пройдёмся по ФО{год} с YYYY от 2024 до 2010 включительно
            for year in range(2024, 2009, -1):
                fo = body.get(f"ФО{year}")
                if not isinstance(fo, dict):
                    continue
                rev = _money(fo.get("ВЫРУЧКА"))
                if rev > 0:
                    result.revenue_last_year = rev
                    result.profit_last_year = _money(fo.get("ПРИБЫЛЬ"))
                    break

        # СудыСтатистика — может приходить разными ключами
        suds = body.get("СудыСтатистика")
        if isinstance(suds, dict):
            result.courts_total = _int(suds.get("всего") or suds.get("total"))

        # Госконтракты
        zakup = body.get("ЗакупкиСтат")
        if isinstance(zakup, dict):
            result.contracts_supplier_count = _int(zakup.get("КонтрПоставщКолв"))
            result.contracts_supplier_sum = _money(zakup.get("КонтрПоставщСум"))
            result.contracts_customer_count = _int(zakup.get("КонтрЗакупщКолв"))
            result.contracts_customer_sum = _money(zakup.get("КонтрЗакупщСум"))

        # Налоговые недоимки — суммируем по списку
        debt = body.get("СуммНедоимЗадолж")
        if isinstance(debt, list):
            for item in debt:
                if isinstance(item, dict):
                    result.tax_debt_sum += _money(item.get("ОбщСумНедоим"))

        # Первый руководитель — флаги массовости и тёзок + сохраняем
        # ФИО/ИНН/должность для отображения текущего директора в отчётах.
        leaders = body.get("Руководители")
        if isinstance(leaders, list) and leaders:
            first = leaders[0]
            if isinstance(first, dict):
                result.director_is_mass_leader = (
                    str(first.get("mass_leaders") or "0") == "1"
                )
                aff = first.get("aff") or {}
                if isinstance(aff, dict):
                    boss = aff.get("boss") or {}
                    if isinstance(boss, dict):
                        result.director_namesake_count = _int(boss.get("namesake"))
                # Текущий директор
                result.director_name = str(first.get("fl") or "").strip()
                result.director_inn = str(first.get("inn") or "").strip()
                result.director_position = str(first.get("post") or "").strip()
                result.director_started_at = str(first.get("date") or "").strip()

        # Учредители — полный список с долями + флаг массовости первого
        founders_block = body.get("СвУчредит") or {}
        if isinstance(founders_block, dict):
            sum_cap = _money(founders_block.get("sumCap")) or result.capital
            result.founders_sum_cap = sum_cap
            all_list = founders_block.get("all") or []
            if isinstance(all_list, list):
                for idx, item in enumerate(all_list):
                    if not isinstance(item, dict):
                        continue
                    abs_share = _money(item.get("dol_abs"))
                    pct = (
                        round(abs_share * 100 / sum_cap, 2)
                        if sum_cap > 0 and abs_share > 0 else 0.0
                    )
                    f_info = FounderInfo(
                        name=str(item.get("name") or "").strip(),
                        inn=str(item.get("inn") or "").strip(),
                        type=str(item.get("type") or ""),
                        share_abs=abs_share,
                        share_pct=pct,
                        is_mass=str(item.get("mass_founders") or "0") == "1",
                        started_at=str(item.get("date") or ""),
                    )
                    result.founders.append(f_info)
                    if idx == 0:
                        result.founder_is_mass = f_info.is_mass

        # Категория МСП — приходит как dict {1: "Малое предприятие", ...}
        msp = body.get("КатСубМСП")
        if isinstance(msp, dict):
            # Берём первое строковое значение — это название категории
            for v in msp.values():
                if isinstance(v, str) and v.strip():
                    result.msp_category = v.strip()
                    break

        # Налоговый режим
        result.tax_regime = str(body.get("НалогРежим") or "").strip()

        # История финансов: ОсновПоказОтчетнИст (УСН) + ФО{год}
        op_hist = body.get("ОсновПоказОтчетнИст")
        if isinstance(op_hist, list):
            for item in op_hist:
                if not isinstance(item, dict):
                    continue
                year = _int(item.get("Год"))
                if not year:
                    continue
                income = _money(item.get("СумДоход"))
                expense = _money(item.get("СумРасход"))
                result.finance_history.append(YearFinance(
                    year=year,
                    income=income,
                    expense=expense,
                    revenue=income,           # для УСН выручка ≈ доход
                    profit=income - expense,
                ))

        # ФО{год} — для ОСНО (выручка/прибыль за год)
        for year in range(2024, 2009, -1):
            fo = body.get(f"ФО{year}")
            if not isinstance(fo, dict):
                continue
            rev = _money(fo.get("ВЫРУЧКА"))
            prof = _money(fo.get("ПРИБЫЛЬ"))
            if rev > 0 or prof != 0:
                # Не дублируем, если уже есть из ОсновПоказОтчетнИст
                if not any(f.year == year for f in result.finance_history):
                    result.finance_history.append(YearFinance(
                        year=year, revenue=rev, profit=prof,
                    ))

        # Сортировка по году убыванию (последние сверху)
        result.finance_history.sort(key=lambda f: f.year, reverse=True)

        # История штрафов: НалогПравонарушИст
        nph_hist = body.get("НалогПравонарушИст")
        if isinstance(nph_hist, list):
            for item in nph_hist:
                if not isinstance(item, dict):
                    continue
                year = _int(item.get("Год"))
                summ = _money(item.get("Сумма"))
                if year:
                    result.tax_violations_history.append((year, summ))
            result.tax_violations_history.sort(key=lambda t: t[0], reverse=True)

        # Подробности недоимок: СуммНедоимЗадолж (список)
        debt_list = body.get("СуммНедоимЗадолж")
        if isinstance(debt_list, list):
            for item in debt_list:
                if not isinstance(item, dict):
                    continue
                result.tax_debt_items.append(TaxDebtItem(
                    tax_name=str(item.get("НаимНалог") or ""),
                    debt=_money(item.get("СумНедНалог")),
                    fines=_money(item.get("СумПени")),
                    penalties=_money(item.get("СумШтраф")),
                    total=_money(item.get("ОбщСумНедоим")),
                ))

        # СвЗапЕГРЮЛ — может быть в card (обычно нет, но fns-card точно).
        egrul = body.get("СвЗапЕГРЮЛ")
        if isinstance(egrul, list):
            for item in egrul:
                if not isinstance(item, dict):
                    continue
                # У записей ЕГРЮЛ свой формат — могут быть @attributes/ВидЗап/СвРегОрг
                attrs = item.get("@attributes") or {}
                vid = item.get("ВидЗап") or {}
                vid_attrs = (vid.get("@attributes") if isinstance(vid, dict) else {}) or {}
                regorg = item.get("СвРегОрг") or {}
                ro_attrs = (regorg.get("@attributes") if isinstance(regorg, dict) else {}) or {}
                result.egrul_records.append(EgrulRecord(
                    grn=str(attrs.get("ГРН") or ""),
                    record_id=str(attrs.get("ИдЗап") or ""),
                    date=str(attrs.get("ДатаЗап") or ""),
                    type_code=str(vid_attrs.get("КодСПВЗ") or ""),
                    type_name=str(vid_attrs.get("НаимВидЗап") or ""),
                    authority_code=str(ro_attrs.get("КодНО") or ""),
                    authority_name=str(ro_attrs.get("НаимНО") or ""),
                ))

        return result

    # ────────────────────────────────────────────────────────────────
    # Общий низкоуровневый вызов
    # ────────────────────────────────────────────────────────────────

    def _simple_call(self, method: str, identifier: str) -> Optional[Dict[str, Any]]:
        """Простой GET-запрос к ZCHB по имени метода и id."""
        try:
            get_quota().check("zchb")
        except ApiQuotaExhausted as exc:
            logger.warning("ZCHB %s skipped: %s", method, exc)
            return None
        url = f"{self.base_url}/{method}"
        params = {
            "id": identifier,
            "api_key": self.api_key,
            "_format": "json",
        }
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                get_quota().record("zchb")
                return resp.json()
            logger.warning("ZCHB %s HTTP %s: %s",
                           method, resp.status_code, resp.text[:300])
        except requests.RequestException as exc:
            logger.warning("ZCHB %s request failed: %s", method, exc)
        except ValueError as exc:
            logger.warning("ZCHB %s invalid JSON: %s", method, exc)
        return None

    async def get_stats(self) -> Optional[Dict[str, Any]]:
        """Возвращает статистику использования ключа: остаток запросов,
        сумму использованных, дату окончания тарифа.
        Используется для админ-отчёта.
        Структура ответа: {stats, end_date, sum_request, rem_request}.
        """
        if not self.enabled:
            return None
        url = f"{self.base_url}/stats"
        params = {"api_key": self.api_key, "_format": "json"}

        def _call():
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
                if resp.status_code != 200:
                    logger.warning("ZCHB stats HTTP %s: %s",
                                   resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                if str(data.get("status", "")) != "200":
                    return None
                return data.get("body")
            except (requests.RequestException, ValueError) as exc:
                logger.warning("ZCHB stats failed: %s", exc)
                return None

        return await asyncio.to_thread(_call)
