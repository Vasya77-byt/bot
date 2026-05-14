"""Тесты Reputation Score 0–100.

Reputation = 100 − риски, разбит по 4 категориям с бюджетами (30+35+20+15).
Каждая категория ограничена своим бюджетом — один сильный фактор не
обнуляет общий рейтинг, а только свою категорию.
"""
from __future__ import annotations

import pytest

from reputation_score import (
    CATEGORIES,
    ReputationScore,
    calculate_reputation,
    format_reputation_block,
)
from schemas import CompanyData
from security_check import SecurityResult


def _company(**kw) -> CompanyData:
    """Builder для CompanyData с дефолтами 'чистая компания'."""
    defaults = dict(
        inn="7707083893",
        name="ООО Тест",
        status="Действующая",
        age_years=10,
        capital=1_000_000.0,
        profit_last_year=5_000_000.0,
        revenue_last_year=50_000_000.0,
    )
    defaults.update(kw)
    return CompanyData(**defaults)


def _security(**kw) -> SecurityResult:
    return SecurityResult(**kw)


class TestBudgetsSumTo100:
    def test_total_budget_is_exactly_100(self):
        """Сумма бюджетов всех категорий = 100 (контракт API)."""
        assert sum(c.budget for c in CATEGORIES) == 100


class TestCleanCompany:
    def test_no_data_at_all_returns_100(self):
        """Никаких данных, никаких факторов риска → 100/100."""
        r = calculate_reputation(None, None)
        assert r.score == 100
        assert r.level == "excellent"

    def test_clean_company_no_security_returns_100(self):
        """Чистая компания, без проверки рисков → 100."""
        r = calculate_reputation(_company(), None)
        assert r.score == 100
        assert r.level == "excellent"

    def test_clean_company_clean_security_returns_100(self):
        r = calculate_reputation(_company(), _security())
        assert r.score == 100
        assert r.level == "excellent"


class TestStatus:
    def test_bankrupt_company_score_drops(self):
        """Банкротство = -90 в категории 'Регистрация' (бюджет 20),
        capped к 20 → категория = 0, остальные полные → 80."""
        r = calculate_reputation(_company(status="Признана банкротом"), None)
        # Финансы (30) + Обязательства (35) + Регистрация (0) + Репутация (15)
        assert r.score == 80
        # Несмотря на банкротство — overall пока excellent, потому что
        # одна категория не топит весь рейтинг. Это by design: банкротство
        # подсвечивается явно в "Что повлияло" и в категории = 0/20.
        assert r.level == "excellent"

    def test_liquidated_status(self):
        r = calculate_reputation(_company(status="Ликвидирована"), None)
        assert r.score == 80  # категория capped, остальные ок

    def test_young_company(self):
        r = calculate_reputation(_company(age_years=0), None)
        # -15 в "Регистрация" (бюджет 20) → категория 5; итог 30+35+5+15 = 85
        assert r.score == 85


class TestFinancialCategory:
    def test_loss_drops_finance(self):
        c = _company(profit_last_year=-1_000_000.0, revenue_last_year=10_000_000.0)
        r = calculate_reputation(c, None)
        # Убыток -15 (но не >20% выручки) → "Финансы": 30-15=15
        assert r.score == 100 - 15
        fin = next(c for c in r.categories if c.name == "Финансовое состояние")
        assert fin.score == 15

    def test_huge_loss_capped_to_budget(self):
        """Существенный убыток (-25) + минимальный капитал (-10) = -35,
        но категория Финансы capped к бюджету 30 → даёт 0."""
        c = _company(
            profit_last_year=-3_000_000.0,
            revenue_last_year=10_000_000.0,
            capital=10_000.0,
        )
        r = calculate_reputation(c, None)
        fin = next(c for c in r.categories if c.name == "Финансовое состояние")
        assert fin.score == 0
        # Итог: 0 + 35 + 20 + 15 = 70
        assert r.score == 70


class TestObligationsCategory:
    def test_minor_fssp_some_penalty(self):
        s = _security(enforcement_count=5, enforcement_total_sum=500_000.0)
        r = calculate_reputation(_company(), s)
        # 5 производств → -10; <1M суммы → 0
        # Категория Обязательства: 35-10 = 25
        ob = next(c for c in r.categories if c.name == "Обязательства и долги")
        assert ob.score == 25
        assert r.score == 90

    def test_massive_fssp_caps_obligations_to_zero(self):
        """Много производств + большая сумма → категория capped к 0."""
        s = _security(enforcement_count=50, enforcement_total_sum=100_000_000.0)
        r = calculate_reputation(_company(), s)
        ob = next(c for c in r.categories if c.name == "Обязательства и долги")
        assert ob.score == 0
        # Итог: 30 + 0 + 20 + 15 = 65
        assert r.score == 65


class TestLevels:
    def test_excellent_threshold(self):
        # Чистая → 100 → excellent
        assert calculate_reputation(_company(), None).level == "excellent"

    def test_risky_when_everything_bad(self):
        """Все категории capped → итог низкий."""
        c = _company(
            status="Признана банкротом",
            age_years=0,
            capital=1_000.0,
            profit_last_year=-10_000_000.0,
            revenue_last_year=10_000_000.0,
        )
        s = _security(
            enforcement_count=50,
            enforcement_total_sum=100_000_000.0,
        )
        r = calculate_reputation(c, s)
        # Финансы 0 + Обязательства 0 + Регистрация 0 + Репутация 15 = 15
        assert r.score <= 24
        assert r.level == "risky"


class TestRendering:
    def test_format_block_clean_company(self):
        r = calculate_reputation(_company(), None)
        block = format_reputation_block(r)
        assert "100/100" in block
        assert "Благонадёжна" in block
        assert "🟢" in block
        # Чистая компания — нет "Что повлияло"
        assert "Что повлияло" not in block

    def test_format_block_brief_mode(self):
        """detailed=False — только одна строка (для Free-тарифа)."""
        r = calculate_reputation(_company(), None)
        block = format_reputation_block(r, detailed=False)
        assert "100/100" in block
        # В кратком режиме нет разбивки по категориям
        assert "Финансовое состояние" not in block

    def test_format_block_with_factors(self):
        r = calculate_reputation(
            _company(status="Признана банкротом"), None,
        )
        block = format_reputation_block(r)
        assert "Что повлияло" in block
        assert "банкрот" in block.lower()
        # Разбивка по категориям присутствует
        assert "Регистрация и статус" in block

    def test_emoji_changes_by_level(self):
        clean = calculate_reputation(_company(), None)
        risky = calculate_reputation(
            _company(
                status="Признана банкротом",
                age_years=0,
                capital=1_000.0,
                profit_last_year=-10_000_000.0,
                revenue_last_year=10_000_000.0,
            ),
            _security(enforcement_count=50, enforcement_total_sum=100_000_000.0),
        )
        assert "🟢" in format_reputation_block(clean)
        assert "🔴" in format_reputation_block(risky)
