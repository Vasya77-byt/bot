"""Reputation Score 0–100: единый рейтинг благонадёжности для клиента.

Архитектура — обратная risk_score.py:
    risk_score:        0 = чисто, 100 = критично (для внутренней логики,
                       мониторинга, алёртов).
    reputation_score:  100 = благонадёжна, 0 = высокий риск (для UX в
                       отчёте — как кредитный рейтинг).

Расчёт прозрачный, без ML:
    1. Каждая категория имеет «бюджет» баллов (макс. вклад в итог).
    2. Внутри категории — те же risk-факторы что в risk_score.py, но
       суммарные штрафы clamp'нуты к бюджету.
    3. Категория даёт = budget − capped_risk. Итог = сумма по категориям.
    4. Если категория без данных (ни один фактор не дал None и не дал
       срабатывания) — отображаем «—», но в итоге она засчитывается
       как полный бюджет (отсутствие плохих новостей = плюс).

Сумма бюджетов = 100. Уровни:
    🟢 excellent 80–100    компания благонадёжна
    🟡 good      50–79     есть нюансы
    🟠 medium    25–49     повышенные риски
    🔴 risky     0–24      высокий риск, осторожно
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from risk_score import (
    ScoreFactor,
    _age_factor,
    _capital_factor,
    _card_address_invalid_factor,
    _card_courts_factor,
    _card_debt_registry_factor,
    _card_director_namesakes_factor,
    _card_mass_director_factor,
    _card_mass_founder_factor,
    _card_no_reporting_factor,
    _card_tax_debt_factor,
    _card_unreliable_supplier_factor,
    _finance_factor,
    _fssp_count_factor,
    _fssp_sum_factor,
    _inspections_factor,
    _status_factor,
)
from schemas import CompanyData
from security_check import SecurityResult


# ──────────────────────────────────────────────────────────────────────
# Категории и веса (бюджеты)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CategoryDef:
    name: str
    budget: int
    company_factors: Sequence[Callable] = ()
    security_factors: Sequence[Callable] = ()


CATEGORIES: tuple[CategoryDef, ...] = (
    CategoryDef(
        name="Финансовое состояние",
        budget=30,
        company_factors=(_capital_factor, _finance_factor),
    ),
    CategoryDef(
        name="Обязательства и долги",
        budget=35,
        security_factors=(
            _fssp_count_factor,
            _fssp_sum_factor,
            _card_tax_debt_factor,
            _card_debt_registry_factor,
        ),
    ),
    CategoryDef(
        name="Регистрация и статус",
        budget=20,
        company_factors=(_status_factor, _age_factor),
        security_factors=(
            _inspections_factor,
            _card_no_reporting_factor,
            _card_address_invalid_factor,
        ),
    ),
    CategoryDef(
        name="Репутация",
        budget=15,
        security_factors=(
            _card_unreliable_supplier_factor,
            _card_mass_director_factor,
            _card_director_namesakes_factor,
            _card_mass_founder_factor,
            _card_courts_factor,
        ),
    ),
)


# ──────────────────────────────────────────────────────────────────────
# Уровни итоговой репутации
# ──────────────────────────────────────────────────────────────────────


# Порядок важен: первое совпадение от верха
LEVEL_THRESHOLDS = [
    (80, "excellent"),
    (50, "good"),
    (25, "medium"),
    (0,  "risky"),
]

LEVEL_EMOJI = {
    "excellent": "🟢",
    "good":      "🟡",
    "medium":    "🟠",
    "risky":     "🔴",
}

LEVEL_LABEL = {
    "excellent": "Благонадёжна",
    "good":      "Есть нюансы",
    "medium":    "Повышенные риски",
    "risky":     "Высокий риск",
}


def _score_to_level(score: int) -> str:
    for threshold, level in LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return "risky"


# ──────────────────────────────────────────────────────────────────────
# Результаты
# ──────────────────────────────────────────────────────────────────────


@dataclass
class CategoryResult:
    name: str
    score: int            # фактическая «оставшаяся» репутация в категории
    budget: int           # максимум, который могла бы дать категория
    factors: List[ScoreFactor] = field(default_factory=list)


@dataclass
class ReputationScore:
    score: int                            # 0..100
    level: str                            # excellent / good / medium / risky
    categories: List[CategoryResult] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# Расчёт
# ──────────────────────────────────────────────────────────────────────


def _eval_category(
    cat: CategoryDef,
    company: Optional[CompanyData],
    security: Optional[SecurityResult],
) -> CategoryResult:
    factors: List[ScoreFactor] = []
    if company is not None:
        for fn in cat.company_factors:
            f = fn(company)
            if f is not None:
                factors.append(f)
    if security is not None:
        for fn in cat.security_factors:
            f = fn(security)
            if f is not None:
                factors.append(f)
    raw_risk = sum(f.points for f in factors)
    # Штрафы внутри категории ограничены её бюджетом — одна категория
    # не может «потопить» весь рейтинг ниже своего веса.
    capped_risk = min(raw_risk, cat.budget)
    score = cat.budget - capped_risk
    return CategoryResult(
        name=cat.name,
        score=score,
        budget=cat.budget,
        factors=factors,
    )


def calculate_reputation(
    company: Optional[CompanyData],
    security: Optional[SecurityResult] = None,
) -> ReputationScore:
    """Считает Reputation Score 0–100 с разбивкой по категориям."""
    categories = [_eval_category(cat, company, security) for cat in CATEGORIES]
    total = sum(c.score for c in categories)
    level = _score_to_level(total)
    return ReputationScore(score=total, level=level, categories=categories)


# ──────────────────────────────────────────────────────────────────────
# Рендер для Telegram
# ──────────────────────────────────────────────────────────────────────


def format_reputation_block(result: ReputationScore, *, detailed: bool = True) -> str:
    """Форматирует блок рейтинга для вставки в начало отчёта.

    detailed=False — только заголовок и общий уровень (Free-тариф).
    detailed=True  — заголовок + разбивка по категориям + ключевые факторы.
    """
    emoji = LEVEL_EMOJI.get(result.level, "⚪")
    label = LEVEL_LABEL.get(result.level, "Неизвестно")
    lines = [
        f"{emoji} Рейтинг благонадёжности: {result.score}/100 — {label}",
    ]
    if not detailed:
        return "\n".join(lines)

    lines.append("")
    for cat in result.categories:
        lines.append(f"  {cat.name}: {cat.score}/{cat.budget}")

    # Показываем сработавшие факторы (что снизило рейтинг)
    all_factors: List[ScoreFactor] = []
    for cat in result.categories:
        all_factors.extend(cat.factors)
    if all_factors:
        lines.append("")
        lines.append("Что повлияло:")
        for f in all_factors:
            lines.append(f"  • {f.label} (−{f.points})")
    return "\n".join(lines)
