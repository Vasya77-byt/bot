"""Клиент API ЗаЧестныйБизнес (zachestnyibiznesapi.ru).

Покрывает один метод: court-arbitration — арбитражные дела по ИНН/ОГРН.
Это аналог kad.arbitr.ru. В будущем сюда же прирастёт fssp, rating, card.

Тариф/лимиты не зашиты — клиент просто отдаёт ошибки, если ключ не валиден
или превышен лимит. Вызывающий код решает, что делать.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("financial-architect")

ZCHB_BASE_URL = "https://zachestnyibiznesapi.ru/paid/data"

# Статусы из docs (answer-code-status-list). Перечислены те, что нам важны
# отдельно от просто success/failure.
ZCHB_STATUS_NO_CASES = {"220", "221"}     # «по данному ИНН не найдено судебных дел»
ZCHB_STATUS_NO_DATA = {"223", "224"}      # «не найдено информации»
ZCHB_STATUS_RATE_LIMIT = "239"            # «слишком частые запросы»
ZCHB_STATUS_QUOTA = "212"                 # лимит тарифа исчерпан
ZCHB_STATUS_KEY_INVALID = {"211", "215"}


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


class ZchbClient:
    """Клиент API ЗЧБ. Пока умеет только court-arbitration."""

    def __init__(self) -> None:
        self.api_key = os.getenv("ZCHB_API_KEY", "")
        self.timeout = float(os.getenv("ZCHB_TIMEOUT", "15"))
        self.base_url = os.getenv("ZCHB_BASE_URL", ZCHB_BASE_URL)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

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

        # При запросе по ИНН в теории body может быть `{"0": {...}, "1": {...}}`
        # (несколько компаний под одним ИНН). На практике ИНН уникален, но
        # обработаем оба варианта — берём первую вложенную, если такая структура.
        if all(k.isdigit() for k in body.keys()):
            sub = next(iter(body.values()), None)
            if isinstance(sub, dict):
                body = sub
            else:
                return ArbitrationSummary()

        return self._parse_arbitration(body, our_inn=inn_or_ogrn)

    def _call_arbitration(self, inn_or_ogrn: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/court-arbitration"
        params = {
            "id": inn_or_ogrn,
            "api_key": self.api_key,
            "_format": "json",
        }
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
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
