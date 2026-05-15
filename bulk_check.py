"""Bulk-проверка контрагентов через CSV-upload (Block C, Step C1).

Дизайн с фокусом на ЗАЩИТУ БЮДЖЕТА:
- Per-batch cap (BULK_MAX_PER_REQUEST, default 30): один батч не может
  выжечь больше N API-вызовов.
- Pre-flight check: если любой апстрим уже >70% дневной квоты — bulk
  отказывается с сообщением «высокая нагрузка».
- Throttled execution (BULK_THROTTLE_SECONDS, default 2.0): между ИНН
  пауза, чтобы другие пользователи не блокировались на минуты.
- Quick-уровень (CompanyService.fetch_quick): только DaData + ФНС-
  fallback; БЕЗ security/AI/ZCHB — в bulk это явный overkill.
- Cross-user кэш работает прозрачно — популярные ИНН возвращаются
  из кэша = 0 upstream-вызовов.

Per-user суточный лимит (TARIFF_BULK_LIMITS в user_store) гарантирует,
что один Business-юзер за день не больше 100 ИНН в bulk суммарно.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

from api_quota import get_quota
from company_service import CompanyService
from ocr import is_valid_inn
from schemas import CompanyData

logger = logging.getLogger("financial-architect")

# Максимум ИНН в одной bulk-загрузке. Защита от «вставил CSV на 5000 строк
# и выжег всю квоту за раз». Защита № 1 — это per-user суточный лимит,
# защита № 2 — этот батч-cap.
BULK_MAX_PER_REQUEST = int(os.getenv("BULK_MAX_PER_REQUEST", "30"))

# Пауза между API-вызовами в bulk. Балансирует throughput и нагрузку
# на апстримы. 2 сек × 30 ИНН = 1 минута на максимальный батч —
# приемлемо для UX, не блокирует других юзеров.
BULK_THROTTLE_SECONDS = float(os.getenv("BULK_THROTTLE_SECONDS", "2.0"))

# Если ЛЮБОЙ апстрим уже использовал >70% дневной квоты — bulk
# отказывается стартовать. Защищает остальных пользователей от
# деградации сервиса.
BULK_PREFLIGHT_QUOTA_THRESHOLD = float(
    os.getenv("BULK_PREFLIGHT_QUOTA_THRESHOLD", "0.70"),
)

# Регулярка ИНН: 10 цифр (юрлица) или 12 цифр (ИП).
_INN_RE = re.compile(r"\b(\d{12}|\d{10})\b")


@dataclass
class BulkResult:
    """Одна строка bulk-отчёта."""
    inn: str
    name: Optional[str] = None
    ogrn: Optional[str] = None
    status: Optional[str] = None
    director: Optional[str] = None
    region: Optional[str] = None
    okved_main: Optional[str] = None
    okved_name: Optional[str] = None
    reg_date: Optional[str] = None
    age_years: Optional[int] = None
    error: Optional[str] = None  # "not_found" | "quota_exhausted" | None


class BulkPreflightError(Exception):
    """Bulk не может стартовать — глобальные квоты слишком высокие.
    Содержит имя API и текущий процент для информативного сообщения."""

    def __init__(self, api: str, percent: float) -> None:
        self.api = api
        self.percent = percent
        super().__init__(
            f"Высокая нагрузка на {api} ({percent:.0f}% дневной квоты). "
            "Попробуйте через 1-2 часа.",
        )


def parse_csv_inns(content: bytes, max_count: int = BULK_MAX_PER_REQUEST) -> List[str]:
    """Извлекает уникальные ИНН (10/12 цифр) из текстового файла.

    Принимает:
    - CSV с любым числом колонок: ищем 10/12-значные подстроки
    - TXT с ИНН по одному в строке или через запятые/пробелы
    - Кодировка: пробуем UTF-8, потом cp1251 (для экспортов из 1С/Excel)

    Дедупликация — по точному совпадению. Порядок сохраняется (первое
    вхождение). Обрезается до max_count.
    """
    text = ""
    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for match in _INN_RE.finditer(text):
        inn = match.group(1)
        if inn in seen:
            continue
        # Контрольная цифра ИНН отсеивает мусор (артикулы, телефоны,
        # серии документов), которые случайно совпали по длине 10/12.
        if not is_valid_inn(inn):
            continue
        seen.add(inn)
        out.append(inn)
        if len(out) >= max_count:
            break
    return out


def preflight_check() -> None:
    """Проверяет, что bulk МОЖНО стартовать сейчас.

    Бросает BulkPreflightError если любой апстрим уже использовал
    >BULK_PREFLIGHT_QUOTA_THRESHOLD дневной квоты. Это защищает
    остальных пользователей: bulk-операция, которая добьёт квоту до
    95% (auto-degradation), сделает обычные проверки деградированными
    на N часов до полуночи.
    """
    snap = get_quota().snapshot()
    for api, info in snap.items():
        limit = info.get("limit")
        if limit is None or limit == 0:
            continue
        used = info.get("used", 0)
        ratio = used / limit if limit > 0 else 0
        if ratio > BULK_PREFLIGHT_QUOTA_THRESHOLD:
            raise BulkPreflightError(api=api, percent=ratio * 100)


def _to_result(inn: str, company: Optional[CompanyData], error: Optional[str] = None) -> BulkResult:
    """Конвертирует CompanyData в строку отчёта (Quick-уровень)."""
    if error:
        return BulkResult(inn=inn, error=error)
    if company is None:
        return BulkResult(inn=inn, error="not_found")
    return BulkResult(
        inn=inn,
        name=company.name,
        ogrn=company.ogrn,
        status=company.status,
        director=company.director,
        region=company.region,
        okved_main=company.okved_main,
        okved_name=company.okved_name,
        reg_date=company.reg_date,
        age_years=company.age_years,
    )


async def process_batch(
    inns: Iterable[str],
    company_service: CompanyService,
    throttle_seconds: float = BULK_THROTTLE_SECONDS,
    progress_callback=None,
) -> List[BulkResult]:
    """Обрабатывает партию ИНН в Quick-режиме с троттлингом.

    Каждый ИНН → fetch_quick (DaData/ФНС, ~1 API-вызов). Между ИНН
    пауза `throttle_seconds` чтобы не забивать апстрим.
    Cross-user кэш делает повторные ИНН бесплатными.

    progress_callback(done, total) — опциональный коллбэк прогресса
    для UI (например, обновление сообщения «обработано 5/30»).

    Если в процессе сработает api_quota auto-degradation, конкретный
    ИНН вернётся с error="not_found" — bulk не прерывается.
    """
    inn_list = list(inns)
    results: List[BulkResult] = []
    total = len(inn_list)
    for i, inn in enumerate(inn_list, start=1):
        try:
            company = await company_service.fetch_quick(inn)
            results.append(_to_result(inn, company))
        except Exception as exc:
            logger.warning("Bulk: fetch_quick failed for INN %s: %s", inn, exc)
            results.append(BulkResult(inn=inn, error="error"))
        if progress_callback is not None:
            try:
                await progress_callback(i, total)
            except Exception:
                # Прогресс — best-effort, ошибки UI не должны ломать bulk
                pass
        if i < total and throttle_seconds > 0:
            await asyncio.sleep(throttle_seconds)
    return results
