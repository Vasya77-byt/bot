"""Сводный риск-балл компании на основе всех доступных данных.

Рассчитывает оценку 0-100 с цветным маркером 🟢🟡🔴.
Использует данные из CompanyData (DaData/ФНС/Руспрофайл) и SecurityResult (ФССП).
"""

from dataclasses import dataclass
from typing import List, Optional

from schemas import CompanyData
from security_check import SecurityResult


@dataclass
class RiskScore:
    score: int          # 0-100
    color: str          # 🟢🟡🔴
    label: str          # "Низкий риск" / "Средний риск" / "Высокий риск"
    reasons: List[str]  # причины снижения балла


def _classify(score: int) -> tuple[str, str]:
    if score >= 80:
        return "🟢", "Низкий риск"
    if score >= 50:
        return "🟡", "Средний риск"
    return "🔴", "Высокий риск"


def calculate(company: Optional[CompanyData], security: Optional[SecurityResult] = None) -> Optional[RiskScore]:
    """Возвращает риск-балл или None если данных недостаточно."""
    if not company or not company.name:
        return None

    score = 100
    reasons: List[str] = []

    # ── Статус компании (самый критичный фактор) ──
    status = (company.status or "").lower()
    if "ликвидирован" in status or "банкрот" in status:
        score = 0
        reasons.append(f"Компания {company.status} — работа невозможна")
        color, label = _classify(score)
        return RiskScore(score=score, color=color, label=label, reasons=reasons)
    if "ликвидац" in status:
        score -= 50
        reasons.append("Компания в процессе ликвидации")
    if "реорганизац" in status:
        score -= 20
        reasons.append("Компания реорганизуется")

    # ── Возраст компании ──
    age = company.age_years or 0
    if age < 1 and company.reg_date:
        score -= 15
        reasons.append("Компания зарегистрирована менее года назад")
    elif age < 3 and age >= 1:
        score -= 5
        reasons.append(f"Молодая компания ({age} г.)")

    # ── Надёжность от Руспрофайла ──
    rating = (company.reliability_rating or "").lower()
    if "низк" in rating:
        score -= 25
        reasons.append("Низкий рейтинг надёжности (Руспрофайл)")
    elif "средн" in rating:
        score -= 10
        reasons.append("Средний рейтинг надёжности (Руспрофайл)")

    # ── Признаки однодневки ──
    shell = (company.reliability_shell or "").lower()
    if shell and shell != "отсутствуют" and "нет" not in shell:
        score -= 30
        reasons.append(f"Признаки однодневки: {company.reliability_shell}")

    # ── Налоговые риски ──
    tax = (company.reliability_tax or "").lower()
    if "высок" in tax or "значительн" in tax:
        score -= 20
        reasons.append(f"Налоговые риски: {company.reliability_tax}")
    elif "средн" in tax:
        score -= 8
        reasons.append("Средние налоговые риски")

    # ── Риски неисполнения обязательств ──
    obligations = (company.reliability_obligations or "").lower()
    if "высок" in obligations or "значительн" in obligations:
        score -= 20
        reasons.append(f"Риски неисполнения обязательств: {company.reliability_obligations}")
    elif "средн" in obligations:
        score -= 8
        reasons.append("Средние риски неисполнения обязательств")

    # ── Финансовое положение ──
    fin = (company.reliability_financial or "").lower()
    if "неудовлетворит" in fin or "критич" in fin or "плох" in fin:
        score -= 20
        reasons.append(f"Финансовое положение: {company.reliability_financial}")
    elif "удовлетворит" in fin and "не" not in fin:
        score -= 5
        reasons.append("Финансовое положение удовлетворительное")

    # ── ФССП (исполнительные производства) ──
    if security and security.has_enforcement:
        count = security.enforcement_count or 0
        total = security.enforcement_total_sum or 0
        if total >= 1_000_000:
            score -= 20
            reasons.append(f"ФССП: {count} производств на сумму {total:,.0f} ₽")
        elif total >= 100_000:
            score -= 12
            reasons.append(f"ФССП: {count} производств на сумму {total:,.0f} ₽")
        else:
            score -= 5
            reasons.append(f"ФССП: {count} производств")

    # ── Капитал ──
    if company.capital is not None and company.capital < 50_000:
        score -= 5
        reasons.append("Минимальный уставный капитал (≤ 50 тыс ₽)")

    # ── Прибыль (отрицательная) ──
    if company.profit_last_year is not None and company.profit_last_year < 0:
        score -= 10
        reasons.append(f"Убыток за последний год: {company.profit_last_year:,.0f} ₽")

    score = max(0, min(100, score))
    color, label = _classify(score)
    return RiskScore(score=score, color=color, label=label, reasons=reasons)
