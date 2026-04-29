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


# ────────────────────────────────────────────────────────────────────
# Факторы из ZCHB CardSummary (https://zachestnyibiznesapi.ru/docs)
# ────────────────────────────────────────────────────────────────────


def _card(security: Optional[SecurityResult]):
    """Извлекает CardSummary из SecurityResult, если он там есть."""
    if security is None:
        return None
    return getattr(security, "zchb_card", None)


def _card_debt_registry_factor(security) -> Optional[ScoreFactor]:
    """Реестр01 ФНС — есть взыскиваемая судебными приставами задолженность."""
    card = _card(security)
    if card and getattr(card, "in_debt_registry", False):
        return ScoreFactor("В реестре ФНС: взыскиваемая задолженность", 25)
    return None


def _card_no_reporting_factor(security) -> Optional[ScoreFactor]:
    """Реестр02 ФНС — компания не сдаёт отчётность более года.
    Сильный признак фиктивно-живой компании."""
    card = _card(security)
    if card and getattr(card, "in_no_reporting_registry", False):
        return ScoreFactor("Не сдаёт налоговую отчётность >1 года", 35)
    return None


def _card_address_invalid_factor(security) -> Optional[ScoreFactor]:
    """Признак недостоверности адреса (запись в ЕГРЮЛ от ФНС)."""
    card = _card(security)
    if card and getattr(card, "address_invalid", False):
        return ScoreFactor("Адрес признан недостоверным (ФНС)", 25)
    return None


def _card_unreliable_supplier_factor(security) -> Optional[ScoreFactor]:
    """РНП ФАС — реестр недобросовестных поставщиков."""
    card = _card(security)
    if card and getattr(card, "is_unreliable_supplier", False):
        return ScoreFactor("В реестре недобросовестных поставщиков (ФАС)", 30)
    return None


def _card_mass_director_factor(security) -> Optional[ScoreFactor]:
    """Директор фигурирует в реестре массовых руководителей."""
    card = _card(security)
    if card and getattr(card, "director_is_mass_leader", False):
        return ScoreFactor("Директор — массовый руководитель", 20)
    return None


def _card_director_namesakes_factor(security) -> Optional[ScoreFactor]:
    """Большое число тёзок-директоров с такими же ФИО — слабый сигнал
    «директор-номинал». Берём только при значительном количестве."""
    card = _card(security)
    if not card:
        return None
    n = getattr(card, "director_namesake_count", 0) or 0
    if n >= 50:
        return ScoreFactor(f"Директор: {n} однофамильцев-руководителей", 10)
    return None


def _card_mass_founder_factor(security) -> Optional[ScoreFactor]:
    """Учредитель в реестре массовых учредителей."""
    card = _card(security)
    if card and getattr(card, "founder_is_mass", False):
        return ScoreFactor("Учредитель — массовый", 15)
    return None


def _card_tax_debt_factor(security) -> Optional[ScoreFactor]:
    """Сумма недоимки и задолженности по налогам (по данным ФНС)."""
    card = _card(security)
    if not card:
        return None
    debt = getattr(card, "tax_debt_sum", 0) or 0
    if debt > 10_000_000:
        return ScoreFactor("Налоговая задолженность > 10 млн ₽", 25)
    if debt > 1_000_000:
        return ScoreFactor("Налоговая задолженность > 1 млн ₽", 15)
    if debt > 100_000:
        return ScoreFactor("Налоговая задолженность > 100 тыс ₽", 5)
    return None


def _card_courts_factor(security) -> Optional[ScoreFactor]:
    """Большое количество судебных дел — сигнал нестабильности.
    Учитываем мягко — крупные компании всегда судятся, но крайние
    значения должны иметь вес."""
    card = _card(security)
    if not card:
        return None
    n = getattr(card, "courts_total", 0) or 0
    if n > 1000:
        return ScoreFactor(f"Очень много судебных дел: {n}", 15)
    if n > 100:
        return ScoreFactor(f"Много судебных дел: {n}", 5)
    return None


# ────────────────────────────────────────────────────────────────────
# Положительные факторы (снимают баллы) пока не вводим — модель
# «факторы только повышают риск» проще и предсказуемее. Сильный игрок
# (миллиарды госконтрактов, лицензии) всё равно получит low по итогу.

_FACTOR_FUNCTIONS = (
    _status_factor,
    _age_factor,
    _capital_factor,
    _finance_factor,
    _fssp_count_factor,
    _fssp_sum_factor,
    _inspections_factor,
    _card_debt_registry_factor,
    _card_no_reporting_factor,
    _card_address_invalid_factor,
    _card_unreliable_supplier_factor,
    _card_mass_director_factor,
    _card_director_namesakes_factor,
    _card_mass_founder_factor,
    _card_tax_debt_factor,
    _card_courts_factor,
)


_CARD_FACTOR_FUNCTIONS = (
    _card_debt_registry_factor,
    _card_no_reporting_factor,
    _card_address_invalid_factor,
    _card_unreliable_supplier_factor,
    _card_mass_director_factor,
    _card_director_namesakes_factor,
    _card_mass_founder_factor,
    _card_tax_debt_factor,
    _card_courts_factor,
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
        for fn in _CARD_FACTOR_FUNCTIONS:
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
