"""Тесты renderers — формирование текста отчёта пользователю.

Главная цель — гарантировать, что пустые/None-поля показываются как
«не указано» / «—» / «?», а не как 0 или мусорные значения. Это прямая
реализация правила «не выдумывать данные».
"""
from datetime import date, datetime, timedelta, timezone


from parsers import ParseResult
from renderers import (
    _fmt_money,
    _risk_reasons,
    _security_block,
    _short_analysis,
    render_client_proposal,
    render_comparison,
    render_internal_analysis,
    render_mixed,
    render_profile,
    render_proposal,
    render_request,
    render_response,
)
from schemas import CompanyData
from security_check import SecurityResult
from user_store import UserProfile


def _company(**overrides) -> CompanyData:
    """Заполненная компания, в тесте можно переопределить любое поле."""
    base = dict(
        inn="7707083893",
        name="ООО Ромашка",
        ogrn="1027700132195",
        kpp="770701001",
        region="Москва",
        address="г. Москва, ул. Пушкина, д. 1",
        reg_date="2010-01-01",
        age_years=15,
        okved_main="62.01",
        okved_name="Разработка ПО",
        employees_count=50,
        revenue_last_year=120_000_000.0,
        profit_last_year=18_000_000.0,
        director="Генеральный директор: Иванов И.И.",
        status="Действующая",
        capital=100_000.0,
        licenses=["Лицензия №1"],
        source="dadata+fns",
    )
    base.update(overrides)
    return CompanyData(**base)


class TestFmtMoney:
    def test_none_returns_unspecified(self):
        assert _fmt_money(None) == "не указано"

    def test_zero(self):
        assert _fmt_money(0) == "0 ₽"

    def test_below_thousand(self):
        assert _fmt_money(500) == "500 ₽"

    def test_under_thousand_boundary(self):
        # 999 < 1000 — отображается в рублях
        assert _fmt_money(999) == "999 ₽"

    def test_exactly_thousand(self):
        assert _fmt_money(1_000) == "1 тыс ₽"

    def test_thousands(self):
        assert _fmt_money(50_000) == "50 тыс ₽"

    def test_under_million_boundary(self):
        # 999_999 < 1_000_000 — всё ещё тыс
        assert _fmt_money(999_999) == "1000 тыс ₽"

    def test_exactly_million(self):
        assert _fmt_money(1_000_000) == "1.0 млн ₽"

    def test_millions(self):
        assert _fmt_money(120_000_000) == "120.0 млн ₽"

    def test_under_billion_boundary(self):
        assert _fmt_money(999_999_999) == "1000.0 млн ₽"

    def test_exactly_billion(self):
        assert _fmt_money(1_000_000_000) == "1.0 млрд ₽"

    def test_billions(self):
        assert _fmt_money(2_500_000_000) == "2.5 млрд ₽"


class TestRiskReasons:
    def test_no_reasons_for_clean_company(self):
        assert _risk_reasons(_company()) == []

    def test_minimal_capital_flagged(self):
        reasons = _risk_reasons(_company(capital=10_000))
        assert len(reasons) == 1
        assert "Уставный капитал" in reasons[0]
        assert "🟡" in reasons[0]

    def test_capital_above_threshold_not_flagged(self):
        # 10_001 > 10_000 — порог = <=
        assert _risk_reasons(_company(capital=10_001)) == []

    def test_no_capital_does_not_crash(self):
        assert _risk_reasons(_company(capital=None)) == []

    def test_status_liquidating_red_flag(self):
        reasons = _risk_reasons(_company(status="Ликвидируется"))
        assert any("🔴" in r and "Ликвидируется" in r for r in reasons)

    def test_status_reorganizing_yellow_flag(self):
        reasons = _risk_reasons(_company(status="В реорганизации"))
        assert any("🟡" in r and "реорганизации" in r.lower() for r in reasons)

    def test_status_active_no_flag(self):
        assert _risk_reasons(_company(status="Действующая")) == []

    def test_fssp_low_count_yellow(self):
        sec = SecurityResult(has_enforcement=True, enforcement_count=5)
        reasons = _risk_reasons(_company(), security=sec)
        assert len(reasons) == 1
        assert "🟡" in reasons[0]
        assert "5" in reasons[0]

    def test_fssp_high_count_red(self):
        sec = SecurityResult(has_enforcement=True, enforcement_count=11)
        reasons = _risk_reasons(_company(), security=sec)
        assert "🔴" in reasons[0]

    def test_no_security_no_fssp_flag(self):
        assert _risk_reasons(_company(), security=None) == []

    def test_security_without_enforcement_no_flag(self):
        sec = SecurityResult(has_enforcement=False, enforcement_count=0)
        assert _risk_reasons(_company(), security=sec) == []

    def test_multiple_reasons_combined(self):
        sec = SecurityResult(has_enforcement=True, enforcement_count=15)
        reasons = _risk_reasons(_company(
            capital=5_000, status="Ликвидируется",
        ), security=sec)
        assert len(reasons) == 3


class TestSecurityBlock:
    def test_low_risk_green_emoji(self):
        block = _security_block(SecurityResult(risk_level="low"))
        assert "🟢" in block
        assert "Низкий" in block

    def test_medium_risk_yellow(self):
        block = _security_block(SecurityResult(risk_level="medium"))
        assert "🟡" in block
        assert "Средний" in block

    def test_high_risk_orange(self):
        block = _security_block(SecurityResult(risk_level="high"))
        assert "🟠" in block
        assert "Высокий" in block

    def test_critical_risk_red(self):
        block = _security_block(SecurityResult(risk_level="critical"))
        assert "🔴" in block
        assert "Критический" in block

    def test_unknown_risk_white_circle(self):
        block = _security_block(SecurityResult(risk_level="unknown"))
        assert "⚪" in block
        assert "Неизвестен" in block

    def test_no_enforcement_clean_message(self):
        block = _security_block(SecurityResult(has_enforcement=False))
        assert "не найдено" in block

    def test_enforcement_with_sum_shown(self):
        block = _security_block(SecurityResult(
            has_enforcement=True,
            enforcement_count=3,
            enforcement_total_sum=2_500_000,
            enforcement_details=["A", "B", "C"],
        ))
        assert "Найдено производств: 3" in block
        assert "2.5 млн" in block

    def test_zero_total_sum_not_shown(self):
        block = _security_block(SecurityResult(
            has_enforcement=True,
            enforcement_count=1,
            enforcement_total_sum=0.0,
            enforcement_details=["X"],
        ))
        assert "Общая сумма" not in block

    def test_details_capped_at_5(self):
        details = [f"item-{i}" for i in range(10)]
        block = _security_block(SecurityResult(
            has_enforcement=True,
            enforcement_count=10,
            enforcement_details=details,
        ))
        # Первые 5 показаны, остальные подсчитаны
        assert "item-0" in block
        assert "item-4" in block
        assert "item-5" not in block
        assert "и ещё 5" in block

    def test_zchb_block_when_set(self):
        block = _security_block(SecurityResult(zchb_details="Высокий риск"))
        assert "ЗаЧестныйБизнес" in block
        assert "Высокий риск" in block

    def test_zchb_block_skipped_when_empty(self):
        block = _security_block(SecurityResult(zchb_details=None))
        assert "ЗаЧестныйБизнес" not in block

    def test_focus_block_when_set(self):
        block = _security_block(SecurityResult(focus_details="Стоп-лист"))
        assert "Контур.Фокус" in block

    def test_focus_block_skipped_when_empty(self):
        block = _security_block(SecurityResult(focus_details=None))
        assert "Контур.Фокус" not in block


class TestRenderRequest:
    def test_filled_company_values_shown(self):
        text = render_request(_company())
        assert "ООО Ромашка" in text
        assert "7707083893" in text
        assert "1027700132195" in text
        assert "62.01" in text

    def test_empty_company_shows_unspecified_everywhere(self):
        empty = CompanyData()
        text = render_request(empty)
        # Каждая строка с None показывает «не указано»
        assert text.count("не указано") >= 8
        # Не должно быть "None" в пользовательском выводе
        assert "None" not in text

    def test_address_falls_back_to_region(self):
        c = _company(address=None, region="Санкт-Петербург")
        text = render_request(c)
        assert "Санкт-Петербург" in text


class TestRenderProposal:
    def test_filled_company(self):
        text = render_proposal(_company())
        assert "ООО Ромашка" in text
        assert "120.0 млн" in text  # выручка через _fmt_money
        assert "18.0 млн" in text  # прибыль

    def test_empty_company_no_zeros_for_money(self):
        # Если revenue/profit None — должно быть «не указано», не «0 ₽»
        text = render_proposal(CompanyData(inn="X"))
        assert "Выручка: не указано" in text
        assert "Прибыль: не указано" in text
        assert "0 ₽" not in text

    def test_zero_revenue_renders_as_zero_rubles(self):
        # 0 — это ВАЛИДНОЕ значение (отчётный 0, не пропуск).
        # Тест документирует что _fmt_money не путает 0 и None.
        text = render_proposal(_company(revenue_last_year=0.0))
        assert "Выручка: 0 ₽" in text


class TestRenderProfile:
    def test_free_user(self):
        p = UserProfile(user_id=1, tariff="free", checks_today=2,
                        checks_total=10, checks_date=date.today().isoformat())
        text = render_profile(p)
        assert "Free" in text
        assert "Проверок сегодня: 2/3" in text  # free лимит = 3
        assert "Осталось: 1" in text
        assert "Всего проверок: 10" in text

    def test_business_unlimited_shown_as_infinity(self):
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        p = UserProfile(user_id=1, tariff="business",
                        tariff_expires_at=future,
                        checks_today=99, checks_date=date.today().isoformat())
        text = render_profile(p)
        assert "∞" in text  # лимит ∞
        assert "Проверок сегодня: 99/∞" in text

    def test_features_listed_with_marks(self):
        p = UserProfile(user_id=1, tariff="pro")
        text = render_profile(p)
        # Pro имеет ИИ-анализ, не имеет API
        assert "✅ 🤖 ИИ-анализ" in text
        assert "❌ 🔌 API доступ" in text

    def test_unknown_tariff_falls_back_to_raw(self):
        p = UserProfile(user_id=1, tariff="enterprise")
        text = render_profile(p)
        # tariff label fallback на raw имя
        assert "enterprise" in text


class TestRenderComparison:
    def test_match_marked_with_check(self):
        c1 = _company(region="Москва")
        c2 = _company(region="Москва")
        text = render_comparison(c1, "1", c2, "2")
        assert "✅" in text  # совпавшие поля
        # Несовпавших полей при одинаковых компаниях нет
        # (но эмодзи ↔️ может появиться, если что-то отличается; для одинаковых — нет)

    def test_mismatch_marked_with_swap(self):
        c1 = _company(region="Москва")
        c2 = _company(region="Санкт-Петербург")
        text = render_comparison(c1, "1", c2, "2")
        assert "↔️" in text
        assert "Москва" in text
        assert "Санкт-Петербург" in text

    def test_none_company_uses_inn_for_title(self):
        text = render_comparison(None, "1111111111", None, "2222222222")
        assert "1111111111" in text
        assert "2222222222" in text

    def test_dash_for_empty_value(self):
        c1 = _company(ogrn=None)
        c2 = _company(ogrn="999")
        text = render_comparison(c1, "1", c2, "2")
        # Пустое поле отрисовано как "—", а не "None"
        assert "—" in text
        assert "None" not in text


class TestShortAnalysis:
    def test_active_status_no_warning(self):
        text = _short_analysis(_company(status="Действующая"))
        assert "⚠️" not in text

    def test_non_active_status_warns(self):
        text = _short_analysis(_company(status="Ликвидирована"))
        assert "⚠️ Статус: Ликвидирована" in text

    def test_okved_name_appended(self):
        text = _short_analysis(_company(okved_main="62.01", okved_name="Разработка ПО"))
        assert "62.01 (Разработка ПО)" in text

    def test_licenses_listed(self):
        text = _short_analysis(_company(licenses=["A", "B"]))
        assert "Лицензии: A, B" in text

    def test_unknown_age_shown_as_question(self):
        text = _short_analysis(_company(age_years=None))
        assert "? лет" in text


class TestRenderInternalAnalysis:
    def test_active_status_check_emoji(self):
        text = render_internal_analysis(_company(status="Действующая"), risk=set())
        assert "✅ Действующая" in text

    def test_non_active_status_warn_emoji(self):
        text = render_internal_analysis(_company(status="Банкрот"), risk=set())
        assert "⚠️ Банкрот" in text

    def test_unknown_status_default(self):
        text = render_internal_analysis(_company(status=None), risk=set())
        assert "неизвестно" in text

    def test_security_clean_shown(self):
        sec = SecurityResult(has_enforcement=False)
        text = render_internal_analysis(_company(), risk=set(), security=sec)
        assert "ФССП: чисто" in text
        assert "ФССП: нет ✅" in text

    def test_security_with_enforcement(self):
        sec = SecurityResult(
            has_enforcement=True, enforcement_count=5,
            enforcement_total_sum=1_500_000,
        )
        text = render_internal_analysis(_company(), risk=set(), security=sec)
        assert "5 производств" in text
        assert "1.5 млн" in text

    def test_finances_block_skipped_when_no_data(self):
        c = _company(revenue_last_year=None, profit_last_year=None)
        text = render_internal_analysis(c, risk=set())
        assert "Финансы" not in text

    def test_finances_block_shown_with_partial_data(self):
        c = _company(revenue_last_year=10_000_000, profit_last_year=None)
        text = render_internal_analysis(c, risk=set())
        assert "Финансы" in text

    def test_risk_score_block_at_top(self):
        # Риск-скор — это первый блок отчёта, не где-то внизу
        text = render_internal_analysis(_company(), risk=set())
        assert text.startswith("🟢")  # эмодзи уровня low — первая строка
        assert "Риск-скор" in text.split("\n")[0]

    def test_clean_company_no_factors_block(self):
        # У чистой компании в скоре нет факторов
        text = render_internal_analysis(_company(), risk=set())
        assert "Факторов риска не обнаружено" in text

    def test_risk_factors_listed_when_risky(self):
        risky_text = render_internal_analysis(
            _company(status="Ликвидируется"), risk=set(),
        )
        assert "Факторы:" in risky_text
        assert "Статус: ликвидация" in risky_text
        # Уровень medium при единственном факторе ликвидации
        assert "Средний" in risky_text or "🟡" in risky_text


class TestRenderClientProposal:
    def test_includes_proposal_text_and_legal_note_for_risky(self):
        text = render_client_proposal(_company(), risk={"обнал"})
        assert "Коммерческое предложение" in text
        assert "обнал" in text  # legal_note включает упомянутые сленги

    def test_no_legal_note_for_clean(self):
        text = render_client_proposal(_company(), risk=set())
        assert "обнал" not in text


class TestRenderMixed:
    def test_includes_analysis_and_mini_kp(self):
        text = render_mixed(_company(), risk=set())
        assert "Анализ компании" in text
        assert "Мини-КП" in text

    def test_no_legal_note_when_clean(self):
        text = render_mixed(_company(), risk=set())
        assert "легальном поле" not in text

    def test_legal_note_when_risky(self):
        text = render_mixed(_company(), risk={"обнал"})
        assert "легальном" in text


class TestRenderResponseDispatcher:
    def _parsed(self, **kwargs) -> ParseResult:
        defaults = dict(
            raw_text="", inn=None, mode=None,
            is_request=False, is_proposal=False, company_data=None,
        )
        defaults.update(kwargs)
        return ParseResult(**defaults)

    def test_dispatches_to_request(self):
        parsed = self._parsed(is_request=True, inn="123")
        text = render_response(parsed, _company(), risk=set())
        assert "ЗАЯВКА НА ОБСЛУЖИВАНИЕ" in text

    def test_dispatches_to_proposal(self):
        parsed = self._parsed(is_proposal=True, inn="123")
        text = render_response(parsed, _company(), risk=set())
        assert "ПРЕДЛОЖЕНИЕ ДЛЯ КОМПАНИИ" in text

    def test_dispatches_to_internal_analysis_mode(self):
        parsed = self._parsed(mode="internal_analysis")
        text = render_response(parsed, _company(), risk=set())
        # internal_analysis имеет блок "Стоп-листы"
        assert "Стоп-листы" in text

    def test_dispatches_to_client_proposal_mode(self):
        parsed = self._parsed(mode="client_proposal")
        text = render_response(parsed, _company(), risk=set())
        assert "Коммерческое предложение" in text

    def test_falls_back_to_mixed(self):
        parsed = self._parsed(mode=None)
        text = render_response(parsed, _company(), risk=set())
        # mixed = analysis + mini_kp
        assert "Анализ компании" in text
        assert "Мини-КП" in text

    def test_none_company_replaced_by_empty(self):
        parsed = self._parsed(is_request=True, inn="123")
        text = render_response(parsed, None, risk=set())
        # empty_company('123') заполняет inn, остальные — "не указано"
        assert "123" in text
        assert "не указано" in text

    def test_mode_is_case_insensitive(self):
        parsed = self._parsed(mode="INTERNAL_ANALYSIS")
        text = render_response(parsed, _company(), risk=set())
        assert "Стоп-листы" in text
