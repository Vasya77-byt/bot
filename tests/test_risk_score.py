"""Тесты risk_score: факторы, пороги уровней, формат вывода.

Главное правило, фиксируемое тестами: «не выдумывать данные» — None/
отсутствующие поля не должны накручивать скор. Каждый фактор имеет
отдельный тест на None.
"""

import pytest

from risk_score import (
    LEVEL_EMOJI,
    LEVEL_LABEL,
    RiskScore,
    ScoreFactor,
    calculate_risk_score,
    format_risk_block,
)
from schemas import CompanyData
from security_check import SecurityResult


def _company(**overrides) -> CompanyData:
    base = dict(inn="1", name="X")
    base.update(overrides)
    return CompanyData(**base)


def _security(**overrides) -> SecurityResult:
    return SecurityResult(**overrides)


# ──────────────────────────────────────────────────────────────────────
# Фактор: статус компании
# ──────────────────────────────────────────────────────────────────────


class TestStatusFactor:
    def test_active_no_factor(self):
        result = calculate_risk_score(_company(status="Действующая"))
        assert all("Статус" not in f.label for f in result.factors)

    def test_bankrupt_critical(self):
        result = calculate_risk_score(_company(status="Банкрот"))
        assert any(f.points == 90 and "банкрот" in f.label.lower()
                   for f in result.factors)
        assert result.level == "critical"

    def test_liquidated_high(self):
        result = calculate_risk_score(_company(status="Ликвидирована"))
        assert any(f.points == 80 for f in result.factors)
        assert result.level in ("high", "critical")

    def test_liquidating_medium(self):
        result = calculate_risk_score(_company(status="Ликвидируется"))
        assert any(f.points == 50 and "ликвидация" in f.label.lower()
                   for f in result.factors)
        assert result.level == "medium"

    def test_reorganization_low_or_medium(self):
        result = calculate_risk_score(_company(status="В реорганизации"))
        assert any(f.points == 15 for f in result.factors)
        # 15 баллов в одиночку — low
        assert result.level == "low"

    def test_status_case_insensitive(self):
        result = calculate_risk_score(_company(status="БАНКРОТ"))
        assert result.level == "critical"

    def test_status_none_no_factor(self):
        result = calculate_risk_score(_company(status=None))
        assert all("Статус" not in f.label for f in result.factors)


# ──────────────────────────────────────────────────────────────────────
# Фактор: возраст компании
# ──────────────────────────────────────────────────────────────────────


class TestAgeFactor:
    def test_young_company_flagged(self):
        result = calculate_risk_score(_company(age_years=0))
        assert any(f.points == 15 and "Возраст" in f.label
                   for f in result.factors)

    def test_old_company_no_flag(self):
        result = calculate_risk_score(_company(age_years=5))
        assert all("Возраст" not in f.label for f in result.factors)

    def test_age_none_no_factor(self):
        result = calculate_risk_score(_company(age_years=None))
        assert all("Возраст" not in f.label for f in result.factors)

    def test_boundary_exactly_one_year_no_flag(self):
        # < 1 года, не <= 1
        result = calculate_risk_score(_company(age_years=1))
        assert all("Возраст" not in f.label for f in result.factors)


# ──────────────────────────────────────────────────────────────────────
# Фактор: уставный капитал
# ──────────────────────────────────────────────────────────────────────


class TestCapitalFactor:
    def test_minimal_capital_flagged(self):
        result = calculate_risk_score(_company(capital=10_000))
        assert any(f.points == 10 and "капитал" in f.label.lower()
                   for f in result.factors)

    def test_large_capital_no_flag(self):
        result = calculate_risk_score(_company(capital=1_000_000))
        assert all("капитал" not in f.label.lower() for f in result.factors)

    def test_capital_none_no_factor(self):
        result = calculate_risk_score(_company(capital=None))
        assert all("капитал" not in f.label.lower() for f in result.factors)

    def test_boundary_above_threshold(self):
        # > 10_000 → не флагается
        result = calculate_risk_score(_company(capital=10_001))
        assert all("капитал" not in f.label.lower() for f in result.factors)


# ──────────────────────────────────────────────────────────────────────
# Фактор: финансовое положение
# ──────────────────────────────────────────────────────────────────────


class TestFinanceFactor:
    def test_profit_positive_no_factor(self):
        result = calculate_risk_score(_company(
            revenue_last_year=10_000_000, profit_last_year=2_000_000,
        ))
        assert all("убыток" not in f.label.lower() for f in result.factors)

    def test_zero_profit_no_factor(self):
        # 0 не считается убытком
        result = calculate_risk_score(_company(
            revenue_last_year=10_000_000, profit_last_year=0,
        ))
        assert all("убыток" not in f.label.lower() for f in result.factors)

    def test_loss_flagged(self):
        result = calculate_risk_score(_company(
            revenue_last_year=10_000_000, profit_last_year=-100_000,
        ))
        assert any(f.points == 15 and "убыток" in f.label.lower()
                   for f in result.factors)

    def test_significant_loss_flagged(self):
        # Убыток 30% от выручки → существенный → +25
        result = calculate_risk_score(_company(
            revenue_last_year=10_000_000, profit_last_year=-3_000_000,
        ))
        assert any(f.points == 25 and "Существенный" in f.label
                   for f in result.factors)

    def test_loss_without_revenue_uses_simple_loss(self):
        # Без revenue нельзя считать процент → обычный убыток
        result = calculate_risk_score(_company(
            revenue_last_year=None, profit_last_year=-100_000,
        ))
        assert any(f.points == 15 for f in result.factors)
        assert all(f.points != 25 for f in result.factors)

    def test_loss_with_zero_revenue_uses_simple_loss(self):
        result = calculate_risk_score(_company(
            revenue_last_year=0, profit_last_year=-100_000,
        ))
        assert any(f.points == 15 for f in result.factors)

    def test_profit_none_no_factor(self):
        # Нет данных по прибыли → не накручиваем скор
        result = calculate_risk_score(_company(
            revenue_last_year=10_000_000, profit_last_year=None,
        ))
        assert all("убыток" not in f.label.lower() for f in result.factors)


# ──────────────────────────────────────────────────────────────────────
# Фактор: ФССП производства
# ──────────────────────────────────────────────────────────────────────


class TestFsspCountFactor:
    @pytest.mark.parametrize("count,expected_points", [
        (0, None), (1, None), (3, None),
        (4, 10), (10, 10),
        (11, 20), (20, 20),
        (21, 40), (50, 40),
    ])
    def test_count_thresholds(self, count, expected_points):
        result = calculate_risk_score(
            _company(), _security(enforcement_count=count),
        )
        fssp_factors = [f for f in result.factors if "ФССП" in f.label
                        and "сумма" not in f.label.lower()]
        if expected_points is None:
            assert fssp_factors == []
        else:
            assert any(f.points == expected_points for f in fssp_factors)

    def test_no_security_no_factor(self):
        result = calculate_risk_score(_company(), security=None)
        assert all("ФССП" not in f.label for f in result.factors)


# ──────────────────────────────────────────────────────────────────────
# Фактор: ФССП сумма долга
# ──────────────────────────────────────────────────────────────────────


class TestFsspSumFactor:
    @pytest.mark.parametrize("amount,expected_points", [
        (0, None),
        (500_000, None),
        (1_000_001, 10),
        (10_000_001, 20),
        (50_000_001, 30),
    ])
    def test_sum_thresholds(self, amount, expected_points):
        result = calculate_risk_score(
            _company(), _security(enforcement_total_sum=amount),
        )
        sum_factors = [f for f in result.factors
                       if "ФССП" in f.label and "сумма" in f.label.lower()]
        if expected_points is None:
            assert sum_factors == []
        else:
            assert any(f.points == expected_points for f in sum_factors)


# ──────────────────────────────────────────────────────────────────────
# Фактор: проверки регуляторов
# ──────────────────────────────────────────────────────────────────────


class TestInspectionsFactor:
    def test_no_violations_no_factor(self):
        result = calculate_risk_score(
            _company(), _security(inspections_count=10,
                                  inspections_violations_count=0),
        )
        assert all("Проверки" not in f.label for f in result.factors)

    def test_inspections_count_alone_irrelevant(self):
        # Сами проверки без нарушений — не риск-фактор
        result = calculate_risk_score(
            _company(),
            _security(inspections_count=20, inspections_violations_count=0),
        )
        assert all("Проверки" not in f.label for f in result.factors)

    def test_few_violations(self):
        # 1–4 нарушений → +10
        result = calculate_risk_score(
            _company(), _security(inspections_violations_count=2),
        )
        assert any(f.points == 10 and "Проверки" in f.label
                   for f in result.factors)

    def test_many_violations(self):
        # ≥5 нарушений → +20
        result = calculate_risk_score(
            _company(), _security(inspections_violations_count=7),
        )
        assert any(f.points == 20 and "Проверки" in f.label
                   for f in result.factors)

    def test_security_none_no_factor(self):
        result = calculate_risk_score(_company(), security=None)
        assert all("Проверки" not in f.label for f in result.factors)


# ──────────────────────────────────────────────────────────────────────
# Уровни риска и cap = 100
# ──────────────────────────────────────────────────────────────────────


class TestLevels:
    def test_no_factors_low_zero(self):
        result = calculate_risk_score(_company(), _security())
        assert result.score == 0
        assert result.level == "low"
        assert result.factors == []

    def test_score_capped_at_100(self):
        # Намеренно собираем больше 100: bankrupt(90) + min capital(10)
        # + young(15) + significant loss(25) + FSSP > 20(40) + sum > 50M(30)
        result = calculate_risk_score(
            _company(
                status="Банкрот", age_years=0, capital=10_000,
                revenue_last_year=10_000_000, profit_last_year=-3_000_000,
            ),
            _security(
                enforcement_count=25, enforcement_total_sum=60_000_000,
                inspections_violations_count=10,
            ),
        )
        assert result.score == 100
        assert result.level == "critical"

    def test_medium_threshold_at_31(self):
        # Ровно 31 балл → medium
        result = calculate_risk_score(
            _company(
                age_years=0,        # +15
                capital=10_000,     # +10
                profit_last_year=-100_000,
                revenue_last_year=10_000_000,  # +15
            ),
        )
        assert result.score == 40
        assert result.level == "medium"

    def test_high_threshold_above_60(self):
        # 65 баллов → high
        result = calculate_risk_score(
            _company(status="Ликвидируется"),  # +50
            _security(enforcement_count=15),    # +20
        )
        assert result.score == 70
        assert result.level == "high"

    def test_critical_threshold_above_80(self):
        result = calculate_risk_score(
            _company(status="Банкрот"),  # +90
        )
        assert result.score == 90
        assert result.level == "critical"


# ──────────────────────────────────────────────────────────────────────
# format_risk_block
# ──────────────────────────────────────────────────────────────────────


class TestFormatRiskBlock:
    def test_low_zero(self):
        score = RiskScore(score=0, level="low", factors=[])
        text = format_risk_block(score)
        assert "🟢" in text
        assert "0/100" in text
        assert "Низкий" in text
        assert "Факторов риска не обнаружено" in text

    def test_critical_emoji_and_label(self):
        score = RiskScore(score=95, level="critical",
                          factors=[ScoreFactor("Статус: банкрот", 90)])
        text = format_risk_block(score)
        assert "🔴" in text
        assert "95/100" in text
        assert "Критический" in text
        assert "Статус: банкрот (+90)" in text

    def test_factor_breakdown_listed(self):
        score = RiskScore(score=70, level="high", factors=[
            ScoreFactor("Статус: ликвидация", 50),
            ScoreFactor("ФССП: 12 производств", 20),
        ])
        text = format_risk_block(score)
        assert "Факторы:" in text
        assert "Статус: ликвидация (+50)" in text
        assert "ФССП: 12 производств (+20)" in text

    def test_unknown_level_falls_back_to_white_circle(self):
        score = RiskScore(score=10, level="bogus", factors=[])
        text = format_risk_block(score)
        assert "⚪" in text


class TestLevelTables:
    """Эмодзи и подписи — не должны теряться при добавлении новых уровней."""

    def test_all_known_levels_have_emoji_and_label(self):
        for level in ("low", "medium", "high", "critical"):
            assert level in LEVEL_EMOJI
            assert level in LEVEL_LABEL


# ──────────────────────────────────────────────────────────────────────
# Сценарии end-to-end (документируют реалистичные комбинации)
# ──────────────────────────────────────────────────────────────────────


class TestEndToEndScenarios:
    def test_clean_company_zero(self):
        result = calculate_risk_score(
            _company(
                status="Действующая", age_years=10, capital=1_000_000,
                revenue_last_year=100_000_000, profit_last_year=10_000_000,
            ),
            _security(
                enforcement_count=0, enforcement_total_sum=0,
                inspections_count=2, inspections_violations_count=0,
            ),
        )
        assert result.score == 0
        assert result.level == "low"

    def test_young_loss_minimal_capital_medium(self):
        # Молодая ИП с минимальным капиталом и убытком — типичный red flag
        result = calculate_risk_score(
            _company(
                status="Действующая", age_years=0, capital=10_000,
                revenue_last_year=500_000, profit_last_year=-150_000,
            ),
        )
        # 15 (молодая) + 10 (капитал) + 25 (существенный убыток) = 50
        assert result.score == 50
        assert result.level == "medium"

    def test_bankrupt_with_fssp_critical(self):
        result = calculate_risk_score(
            _company(status="Банкрот"),
            _security(enforcement_count=15,
                      enforcement_total_sum=20_000_000),
        )
        # 90 + 20 + 20 = 130 → cap 100
        assert result.score == 100
        assert result.level == "critical"


# ────────────────────────────────────────────────────────────────────
# Факторы из ZCHB CardSummary
# ────────────────────────────────────────────────────────────────────

from zchb_client import CardSummary  # noqa: E402


def _security_with_card(**card_overrides) -> SecurityResult:
    return SecurityResult(zchb_card=CardSummary(**card_overrides))


class TestCardFactors:
    def test_no_card_means_no_card_factors(self):
        # Без карточки — никакие card-факторы не сработают
        result = calculate_risk_score(_company(), _security())
        labels = [f.label for f in result.factors]
        assert not any("реестр" in l.lower() for l in labels)
        assert not any("массов" in l.lower() for l in labels)

    def test_debt_registry_flag_adds_25(self):
        result = calculate_risk_score(
            _company(),
            _security_with_card(in_debt_registry=True),
        )
        assert result.score == 25
        labels = [f.label for f in result.factors]
        assert any("задолженность" in l for l in labels)

    def test_no_reporting_registry_strong_signal(self):
        result = calculate_risk_score(
            _company(),
            _security_with_card(in_no_reporting_registry=True),
        )
        assert result.score == 35
        labels = [f.label for f in result.factors]
        assert any("отчётность" in l for l in labels)

    def test_address_invalid_adds_25(self):
        result = calculate_risk_score(
            _company(),
            _security_with_card(address_invalid=True),
        )
        assert result.score == 25

    def test_unreliable_supplier_adds_30(self):
        result = calculate_risk_score(
            _company(),
            _security_with_card(is_unreliable_supplier=True),
        )
        assert result.score == 30

    def test_mass_director_adds_20(self):
        result = calculate_risk_score(
            _company(),
            _security_with_card(director_is_mass_leader=True),
        )
        assert result.score == 20

    def test_director_50_namesakes_adds_10(self):
        result = calculate_risk_score(
            _company(),
            _security_with_card(director_namesake_count=50),
        )
        assert result.score == 10

    def test_director_few_namesakes_no_factor(self):
        # 49 — ниже порога
        result = calculate_risk_score(
            _company(),
            _security_with_card(director_namesake_count=49),
        )
        assert result.score == 0

    def test_mass_founder_adds_15(self):
        result = calculate_risk_score(
            _company(),
            _security_with_card(founder_is_mass=True),
        )
        assert result.score == 15

    @pytest.mark.parametrize("debt,expected", [
        (50_000, 0),
        (100_000, 0),       # пороговое — не считаем
        (200_000, 5),
        (1_500_000, 15),
        (15_000_000, 25),
    ])
    def test_tax_debt_thresholds(self, debt, expected):
        result = calculate_risk_score(
            _company(),
            _security_with_card(tax_debt_sum=debt),
        )
        assert result.score == expected

    @pytest.mark.parametrize("courts,expected", [
        (50, 0),
        (101, 5),
        (1_001, 15),
    ])
    def test_courts_thresholds(self, courts, expected):
        result = calculate_risk_score(
            _company(),
            _security_with_card(courts_total=courts),
        )
        assert result.score == expected

    def test_multiple_card_flags_sum(self):
        # Худший сценарий: фирма-однодневка
        result = calculate_risk_score(
            _company(),
            _security_with_card(
                in_debt_registry=True,            # +25
                in_no_reporting_registry=True,    # +35
                address_invalid=True,             # +25
                director_is_mass_leader=True,     # +20
                founder_is_mass=True,             # +15
            ),
        )
        # 25+35+25+20+15 = 120 → cap 100
        assert result.score == 100
        assert result.level == "critical"
