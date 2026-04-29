"""Сводный риск-скор компании 0–100 с прозрачной разбивкой.

Архитектура:
- Каждый фактор риска описан явно: лейбл и количество добавляемых баллов.
- Скор = сумма баллов всех сработавших факторов, обрезанная до 100.
- Уровень риска (low/medium/high/critical) определяется порогами.
- Клиенту показывается полный breakdown — никаких «магических цифр».

Правило «не выдумывать данные»: если поле None или отсутствует, фактор
просто не срабатывает. Скор не пытается угадывать значения.

Факторы покрывают:
- Статус компании (банкрот, ликвидация, реорганизация)
- Возраст (< 1 года — повышенный риск молодой компании)
- Уставный капитал (минимальный)
- Финансовое положение (убыток, существенный убыток)
- ФССП (производства и сумма)
- Регуляторные проверки (нарушения)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from schemas import CompanyData
from security_check import SecurityResult


# Пороги уровней риска по итоговому скору
LEVEL_THRESHOLDS = [
    (80, "critical"),
    (60, "high"),
    (30, "medium"),
    (0,  "low"),
]


@dataclass(frozen=True)
class ScoreFactor:
    """Один сработавший фактор риска."""
    label: str
    points: int


@dataclass
class RiskScore:
    score: int  # 0..100
    level: str  # low / medium / high / critical
    factors: List[ScoreFactor] = field(default_factory=list)


def _score_to_level(score: int) -> str:
    for threshold, level in LEVEL_THRESHOLDS:
        if score > threshold:
            return level
    return "low"


def _status_factor(company: Optional[CompanyData]) -> Optional[ScoreFactor]:
    """Серьёзность статуса: банкротство и ликвидация должны выдавать
    minimum medium-уровень даже без других факторов."""
    if not company or not company.status:
        return None
    s = company.status.lower()
    if "банкрот" in s:
        return ScoreFactor("Статус: банкрот", 90)
    if "ликвидирована" in s or "ликвидирован" in s:
        return ScoreFactor("Статус: ликвидирована", 80)
    if "ликвид" in s:
        return ScoreFactor("Статус: ликвидация", 50)
    if "реорган" in s:
        return ScoreFactor("Статус: реорганизация", 15)
    return None


def _age_factor(company: Optional[CompanyData]) -> Optional[ScoreFactor]:
    if not company or company.age_years is None:
        return None
    if company.age_years < 1:
        return ScoreFactor("Возраст компании < 1 года", 15)
    return None


def _capital_factor(company: Optional[CompanyData]) -> Optional[ScoreFactor]:
    if not company or company.capital is None:
        return None
    if company.capital <= 10_000:
        return ScoreFactor("Минимальный уставный капитал", 10)
    return None


def _finance_factor(company: Optional[CompanyData]) -> Optional[ScoreFactor]:
    """Сигнал по финансовому положению.
    - Убыток в прошлом году → +15
    - Существенный убыток (> 20% выручки) → +25
    """
    if not company or company.profit_last_year is None:
        return None
    profit = company.profit_last_year
    if profit >= 0:
        return None

    revenue = company.revenue_last_year
    if revenue and revenue > 0 and abs(profit) > revenue * 0.2:
        return ScoreFactor("Существенный убыток (>20% выручки)", 25)
    return ScoreFactor("Убыток в прошлом году", 15)


def _fssp_count_factor(security: Optional[SecurityResult]) -> Optional[ScoreFactor]:
    if not security:
        return None
    n = security.enforcement_count
    if n > 20:
        return ScoreFactor(f"ФССП: {n} производств", 40)
    if n > 10:
        return ScoreFactor(f"ФССП: {n} производств", 20)
    if n > 3:
        return ScoreFactor(f"ФССП: {n} производств", 10)
    return None


def _fssp_sum_factor(security: Optional[SecurityResult]) -> Optional[ScoreFactor]:
    if not security:
        return None
    s = security.enforcement_total_sum
    if s > 50_000_000:
        return ScoreFactor("ФССП: сумма долга > 50 млн ₽", 30)
    if s > 10_000_000:
        return ScoreFactor("ФССП: сумма долга > 10 млн ₽", 20)
    if s > 1_000_000:
        return ScoreFactor("ФССП: сумма долга > 1 млн ₽", 10)
    return None


def _inspections_factor(security: Optional[SecurityResult]) -> Optional[ScoreFactor]:
    """Регуляторные проверки с нарушениями.
    Само по себе наличие проверок без нарушений — нейтрально/положительно
    (компания публична, проверяется), поэтому в скор идут только нарушения.
    """
    if not security:
        return None
    v = getattr(security, "inspections_violations_count", 0) or 0
    if v >= 5:
        return ScoreFactor(f"Проверки: {v} нарушений", 20)
    if v >= 1:
        return ScoreFactor(f"Проверки: {v} нарушений", 10)
    return None


_FACTOR_FUNCTIONS = (
    _status_factor,
    _age_factor,
    _capital_factor,
    _finance_factor,
    _fssp_count_factor,
    _fssp_sum_factor,
    _inspections_factor,
)


def calculate_risk_score(
    company: Optional[CompanyData],
    security: Optional[SecurityResult] = None,
) -> RiskScore:
    """Вычисляет сводный риск-скор и список сработавших факторов."""
    factors: List[ScoreFactor] = []

    if company is not None:
        for fn in (_status_factor, _age_factor, _capital_factor, _finance_factor):
            f = fn(company)
            if f is not None:
                factors.append(f)

    if security is not None:
        for fn in (_fssp_count_factor, _fssp_sum_factor, _inspections_factor):
            f = fn(security)
            if f is not None:
                factors.append(f)

    raw = sum(f.points for f in factors)
    score = max(0, min(raw, 100))
    level = _score_to_level(score)
    return RiskScore(score=score, level=level, factors=factors)


# Эмодзи и подписи уровней — единое место правды для рендера
LEVEL_EMOJI = {
    "low":      "🟢",
    "medium":   "🟡",
    "high":     "🟠",
    "critical": "🔴",
}

LEVEL_LABEL = {
    "low":      "Низкий",
    "medium":   "Средний",
    "high":     "Высокий",
    "critical": "Критический",
}


def format_risk_block(result: RiskScore) -> str:
    """Форматирует блок риск-скора для вставки в начале отчёта."""
    emoji = LEVEL_EMOJI.get(result.level, "⚪")
    label = LEVEL_LABEL.get(result.level, "Неизвестен")
    lines = [
        f"{emoji} Риск-скор: {result.score}/100 — {label}",
    ]
    if result.factors:
        lines.append("")
        lines.append("Факторы:")
        for f in result.factors:
            lines.append(f"• {f.label} (+{f.points})")
    else:
        lines.append("")
        lines.append("Факторов риска не обнаружено.")
    return "\n".join(lines)
