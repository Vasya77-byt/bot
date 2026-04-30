"""Тесты report_renderer — генерация HTML веб-отчёта."""
from __future__ import annotations

import pytest

from report_renderer import _collect_factors, _fmt_money, render_report
from schemas import CompanyData
from security_check import SecurityResult
from zchb_client import (
    ArbitrationSummary,
    CardSummary,
    FounderInfo,
    YearFinance,
)


# ────────────────────────────────────────────────────────────────────
# _fmt_money
# ────────────────────────────────────────────────────────────────────


class TestFmtMoney:
    def test_zero(self):
        assert _fmt_money(0) == "0 ₽"

    def test_none_treated_as_zero(self):
        assert _fmt_money(None) == "0 ₽"

    def test_invalid_returns_zero(self):
        assert _fmt_money("not a number") == "0 ₽"

    def test_thousands(self):
        assert _fmt_money(125_000) == "125 тыс ₽"

    def test_millions(self):
        assert "млн" in _fmt_money(2_500_000)

    def test_billions(self):
        assert "млрд" in _fmt_money(7_000_000_000)


# ────────────────────────────────────────────────────────────────────
# _collect_factors
# ────────────────────────────────────────────────────────────────────


class TestCollectFactors:
    def test_no_data_all_factors_in_positive(self):
        neg, pos = _collect_factors(card=None, security=None)
        assert neg == []
        # Все известные фактор-карточки + плейсхолдеры идут в зелёную колонку
        assert len(pos) > 10

    def test_debt_registry_flag_lands_in_negative(self):
        card = CardSummary(in_debt_registry=True)
        sec = SecurityResult()
        neg, pos = _collect_factors(card=card, security=sec)
        labels = [f["label"] for f in neg]
        assert any("задолженности ФНС" in l for l in labels)

    def test_fssp_count_positive(self):
        card = CardSummary()
        sec = SecurityResult(enforcement_count=5)
        neg, _ = _collect_factors(card=card, security=sec)
        labels = [f["label"] for f in neg]
        assert any("ФССП" in l for l in labels)
        # detail должен содержать число
        fssp = next(f for f in neg if "ФССП" in f["label"])
        assert "5" in fssp["detail"]

    def test_liquidated_status_marked(self):
        card = CardSummary(status="Ликвидировано")
        sec = SecurityResult()
        neg, _ = _collect_factors(card=card, security=sec)
        labels = [f["label"] for f in neg]
        assert any("ликвид" in l.lower() for l in labels)

    def test_status_in_reorg(self):
        card = CardSummary(status="В процессе реорганизации")
        sec = SecurityResult()
        neg, _ = _collect_factors(card=card, security=sec)
        labels = [f["label"] for f in neg]
        assert any("реорганиз" in l.lower() for l in labels)

    def test_clean_card_has_no_negative(self):
        card = CardSummary()  # всё False по дефолту
        sec = SecurityResult()
        neg, pos = _collect_factors(card=card, security=sec)
        # Явных красных факторов быть не должно
        assert neg == []
        assert len(pos) >= 10


# ────────────────────────────────────────────────────────────────────
# render_report — full HTML
# ────────────────────────────────────────────────────────────────────


def _build_company(**overrides) -> CompanyData:
    base = dict(
        inn="7707083893",
        name="ПАО СБЕРБАНК",
        ogrn="1027700132195",
        address="г. Москва, ул. Вавилова, 19",
        status="Действующее",
        capital=67_760_844_000.0,
    )
    base.update(overrides)
    return CompanyData(**base)


def _build_card(**overrides) -> CardSummary:
    base = dict(
        ogrn="1027700132195",
        name_short="ПАО СБЕРБАНК",
        status="Действующее",
        director_name="Греф Г. О.",
        director_inn="773601015849",
        capital=67_760_844_000.0,
        finance_history=[
            YearFinance(year=2023, revenue=3_500_000_000_000.0,
                        profit=1_500_000_000_000.0),
            YearFinance(year=2022, revenue=3_000_000_000_000.0,
                        profit=1_200_000_000_000.0),
        ],
        founders=[
            FounderInfo(name="Российская Федерация", inn="7710168360",
                        type="ul", share_pct=50.0),
        ],
    )
    base.update(overrides)
    return CardSummary(**base)


def test_render_returns_bytes_html():
    html = render_report(
        inn="7707083893",
        company=_build_company(),
        card=_build_card(),
        security=SecurityResult(),
    )
    assert isinstance(html, bytes)
    text = html.decode("utf-8")
    assert text.startswith("<!DOCTYPE html>") or text.startswith("<!doctype")
    assert "</html>" in text


def test_render_contains_company_info():
    html = render_report(
        inn="7707083893",
        company=_build_company(),
        card=_build_card(),
        security=SecurityResult(),
    )
    text = html.decode("utf-8")
    assert "СБЕРБАНК" in text
    assert "7707083893" in text
    assert "1027700132195" in text
    assert "Греф" in text


def test_render_with_no_card_falls_back_to_inn():
    html = render_report(
        inn="9999999999",
        company=None,
        card=None,
        security=None,
    )
    text = html.decode("utf-8")
    assert "9999999999" in text


def test_render_finance_years_serialized():
    html = render_report(
        inn="7707083893",
        company=_build_company(),
        card=_build_card(),
        security=SecurityResult(),
    )
    text = html.decode("utf-8")
    # JSON массив для year switcher
    assert "2023" in text
    assert "2022" in text


def test_render_arbitration_block():
    arb = ArbitrationSummary(
        as_plaintiff_count=10,
        as_defendant_count=5,
        plaintiff_claim_sum=1_000_000,
        defendant_claim_sum=500_000,
    )
    sec = SecurityResult(zchb_arbitration=arb)
    html = render_report(
        inn="7707083893",
        company=_build_company(),
        card=_build_card(),
        security=sec,
    )
    text = html.decode("utf-8")
    assert "10" in text
    assert "5" in text


def test_render_status_active_for_acting_company():
    html = render_report(
        inn="7707083893",
        company=_build_company(status="Действующее"),
        card=_build_card(status="Действующее"),
        security=SecurityResult(),
    )
    text = html.decode("utf-8")
    # Зелёный pill для действующих — символ 🟢 или класс pill-pos
    assert "Действующее" in text


def test_render_with_negative_factors_visible():
    card = _build_card(
        in_debt_registry=True,
        director_is_mass_leader=True,
    )
    html = render_report(
        inn="7707083893",
        company=_build_company(),
        card=card,
        security=SecurityResult(enforcement_count=3),
    )
    text = html.decode("utf-8")
    # Сработавшие факторы попадают в HTML как label
    assert "задолженности ФНС" in text
    assert "Массовый руководитель" in text
    assert "ФССП" in text


def test_render_handles_minimum_data():
    """Не падает при пустой карточке — все данные опциональны."""
    html = render_report(
        inn="1234567890",
        company=None,
        card=None,
        security=None,
    )
    assert isinstance(html, bytes)
    assert b"</html>" in html
