"""Массовая проверка компаний из файла (TXT/XLSX)."""

import io
import re
from dataclasses import dataclass
from typing import Callable, Coroutine, List, Optional

from parsers import _inn_valid
from risk_score import RiskScore, calculate as calculate_risk

# Лимиты по тарифам для массовой проверки (компаний за раз)
BULK_LIMITS = {
    "free": 0,
    "start": 10,
    "pro": 50,
    "business": 200,
}


@dataclass
class BulkResult:
    inn: str
    name: Optional[str]
    risk: Optional[RiskScore]
    error: Optional[str] = None


def parse_inns_from_txt(content: bytes) -> List[str]:
    """Извлекает ИНН из текстового файла."""
    text = content.decode("utf-8", errors="replace")
    found = re.findall(r'\b(\d{10}|\d{12})\b', text)
    seen: set[str] = set()
    result = []
    for inn in found:
        if inn not in seen and _inn_valid(inn):
            seen.add(inn)
            result.append(inn)
    return result


def parse_inns_from_xlsx(content: bytes) -> List[str]:
    """Извлекает ИНН из Excel-файла."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        seen: set[str] = set()
        result = []
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                if cell is None:
                    continue
                val = str(cell).strip().split(".")[0]  # убираем .0 из числовых ячеек
                if re.match(r'^\d{10}$|^\d{12}$', val) and val not in seen and _inn_valid(val):
                    seen.add(val)
                    result.append(val)
        wb.close()
        return result
    except Exception:
        return []


def parse_inns(filename: str, content: bytes) -> List[str]:
    """Определяет формат файла и извлекает ИНН."""
    name_lower = (filename or "").lower()
    if name_lower.endswith(".xlsx") or name_lower.endswith(".xls"):
        return parse_inns_from_xlsx(content)
    return parse_inns_from_txt(content)


async def check_companies(
    inns: List[str],
    company_service,
    security_service=None,
    progress_cb: Optional[Callable[[int, int], Coroutine]] = None,
) -> List[BulkResult]:
    """Последовательно проверяет список ИНН и возвращает результаты."""
    results: List[BulkResult] = []
    total = len(inns)

    for i, inn in enumerate(inns):
        try:
            company = await company_service.fetch(inn)
            if not company:
                results.append(BulkResult(inn=inn, name=None, risk=None, error="Не найдена"))
            else:
                sec = None
                if security_service:
                    try:
                        sec = await security_service.check(
                            inn=inn,
                            name=company.name,
                            okved=company.okved_main,
                        )
                    except Exception:
                        pass
                risk = calculate_risk(company, sec)
                results.append(BulkResult(inn=inn, name=company.name, risk=risk))
        except Exception as exc:
            results.append(BulkResult(inn=inn, name=None, risk=None, error=str(exc)[:80]))

        if progress_cb:
            await progress_cb(i + 1, total)

    return results


def format_bulk_results(results: List[BulkResult]) -> str:
    """Форматирует результаты в текстовый отчёт."""
    lines = [f"📊 Результаты массовой проверки: {len(results)} компаний\n"]

    for i, r in enumerate(results, 1):
        if r.error:
            lines.append(f"{i}. ❌ {r.inn} — {r.error}")
        elif r.risk:
            name = (r.name or r.inn)[:50]
            lines.append(
                f"{i}. {r.risk.color} {name}\n"
                f"   ИНН: {r.inn} | {r.risk.score}/100 — {r.risk.label}"
            )
        else:
            name = (r.name or r.inn)[:50]
            lines.append(f"{i}. ⚪ {name}\n   ИНН: {r.inn} | Оценка недоступна")

    low = sum(1 for r in results if r.risk and r.risk.score >= 80)
    mid = sum(1 for r in results if r.risk and 50 <= r.risk.score < 80)
    high = sum(1 for r in results if r.risk and r.risk.score < 50)
    errors = sum(1 for r in results if r.error)

    lines.append("\n── Итого ──")
    lines.append(f"🟢 Низкий риск: {low}")
    lines.append(f"🟡 Средний риск: {mid}")
    lines.append(f"🔴 Высокий риск: {high}")
    if errors:
        lines.append(f"❌ Не найдено/ошибки: {errors}")

    return "\n".join(lines)
