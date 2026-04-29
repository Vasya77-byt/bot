"""Тесты zchb_client — парсинг ответа court-arbitration."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from zchb_client import (
    ArbitrationSummary,
    ZchbClient,
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ZCHB_API_KEY", "test-key")
    return ZchbClient()


@pytest.fixture
def no_key_client(monkeypatch):
    monkeypatch.delenv("ZCHB_API_KEY", raising=False)
    return ZchbClient()


def _arbitration_body(*, exact_total=2, fuzzy_total=0, our_inn="7712040126"):
    """Собирает body как у документации ЗЧБ."""
    cases = {
        "UUID-1": {
            "Истец": [{
                "ИНН": our_inn,
                "ОГРН": "1027700092661",
                "Наименование": "ПАО Аэрофлот",
            }],
            "Ответчик": [{
                "ИНН": "9102065317",
                "ОГРН": "1149102173109",
                "Наименование": "АО Симферополь",
            }],
            "НомерДела": "А83-12738/2017",
            "СуммаИска": 1_558_718,
            "СтартДата": "24.08.2017",
        },
        "UUID-2": {
            "Ответчик": [{
                "ИНН": our_inn,
                "ОГРН": "1027700092661",
                "Наименование": "ПАО Аэрофлот",
            }],
            "Истец": [{
                "ИНН": "7703796452",
                "ОГРН": "1137746804469",
                "Наименование": "ООО Спецтех",
            }],
            "НомерДела": "А40-178019/2016",
            "СуммаИска": 9_723_732,
            "СтартДата": "26.08.2016",
        },
    }
    body = {
        "точно": {
            "всего": exact_total,
            "дела": {k: cases[k] for k in list(cases.keys())[:exact_total]},
        }
    }
    if fuzzy_total:
        body["неточно"] = {"всего": fuzzy_total, "дела": {}}
    return body


class TestParseArbitration:
    def test_role_detected_from_inn(self):
        body = _arbitration_body(exact_total=2, our_inn="7712040126")
        s = ZchbClient._parse_arbitration(body, our_inn="7712040126")
        assert s.total_exact == 2
        assert s.as_plaintiff_count == 1
        assert s.as_defendant_count == 1
        assert s.plaintiff_claim_sum == 1_558_718
        assert s.defendant_claim_sum == 9_723_732
        assert s.total_claim_sum == 1_558_718 + 9_723_732

    def test_counterparty_filled(self):
        body = _arbitration_body(exact_total=2, our_inn="7712040126")
        s = ZchbClient._parse_arbitration(body, our_inn="7712040126")
        plaintiff_case = next(c for c in s.cases if c.role == "истец")
        assert plaintiff_case.counterparty_inn == "9102065317"
        assert "Симферополь" in plaintiff_case.counterparty_name

    def test_case_uuid_preserved(self):
        body = _arbitration_body(exact_total=2, our_inn="7712040126")
        s = ZchbClient._parse_arbitration(body, our_inn="7712040126")
        uuids = {c.case_uuid for c in s.cases}
        assert uuids == {"UUID-1", "UUID-2"}

    def test_empty_body(self):
        s = ZchbClient._parse_arbitration({}, our_inn="123")
        assert s.total_exact == 0
        assert s.total_fuzzy == 0
        assert s.cases == []
        assert not s.has_cases

    def test_fuzzy_section_doesnt_affect_role_counters(self):
        body = {
            "неточно": {
                "всего": 1,
                "дела": {
                    "U1": {
                        "Истец": [{"ИНН": None, "Наименование": "ООО"}],
                        "Ответчик": [{"ИНН": None, "Наименование": "ПАО"}],
                        "НомерДела": "А40-1/2020",
                        "СуммаИска": 100,
                        "СтартДата": "01.01.2020",
                    }
                },
            }
        }
        s = ZchbClient._parse_arbitration(body, our_inn="7712040126")
        assert s.total_fuzzy == 1
        assert s.as_plaintiff_count == 0
        assert s.as_defendant_count == 0
        assert s.total_claim_sum == 0  # не считаем по fuzzy
        assert len(s.cases) == 1
        assert s.cases[0].accuracy == "fuzzy"

    def test_invalid_sum_falls_back_to_zero(self):
        body = {
            "точно": {
                "всего": 1,
                "дела": {
                    "U1": {
                        "Истец": [{"ИНН": "7712040126"}],
                        "Ответчик": [{"ИНН": "9999999999"}],
                        "НомерДела": "А40-1",
                        "СуммаИска": "не число",
                        "СтартДата": "01.01.2020",
                    }
                },
            }
        }
        s = ZchbClient._parse_arbitration(body, our_inn="7712040126")
        assert s.cases[0].sum_rub == 0


class TestGetArbitration:
    @pytest.mark.asyncio
    async def test_no_key_returns_none(self, no_key_client):
        result = await no_key_client.get_arbitration("123")
        assert result is None

    @pytest.mark.asyncio
    async def test_status_220_returns_empty_summary(self, client):
        with patch.object(client, "_call_arbitration",
                          return_value={"status": "220",
                                        "message": "не найдено"}):
            result = await client.get_arbitration("123")
        assert isinstance(result, ArbitrationSummary)
        assert result.total_exact == 0
        assert not result.has_cases

    @pytest.mark.asyncio
    async def test_invalid_key_returns_none(self, client):
        with patch.object(client, "_call_arbitration",
                          return_value={"status": "215",
                                        "message": "Задан неверный ключ"}):
            result = await client.get_arbitration("123")
        assert result is None

    @pytest.mark.asyncio
    async def test_rate_limit_returns_empty(self, client):
        with patch.object(client, "_call_arbitration",
                          return_value={"status": "239",
                                        "message": "слишком частые"}):
            result = await client.get_arbitration("123")
        assert isinstance(result, ArbitrationSummary)
        assert not result.has_cases

    @pytest.mark.asyncio
    async def test_normal_response(self, client):
        body = _arbitration_body(exact_total=2, our_inn="7712040126")
        with patch.object(client, "_call_arbitration",
                          return_value={"status": "200",
                                        "message": "ok",
                                        "body": body}):
            result = await client.get_arbitration("7712040126")
        assert result.total_exact == 2
        assert result.as_plaintiff_count == 1
        assert result.as_defendant_count == 1

    @pytest.mark.asyncio
    async def test_nested_inn_response(self, client):
        """ЗЧБ при поиске по ИНН может вернуть body[0]={...},
        body[1]={...} (несколько компаний)."""
        inner = _arbitration_body(exact_total=1, our_inn="7712040126")
        nested = {"0": inner, "1": _arbitration_body(exact_total=0)}
        with patch.object(client, "_call_arbitration",
                          return_value={"status": "200",
                                        "message": "ok",
                                        "body": nested}):
            result = await client.get_arbitration("7712040126")
        assert result.total_exact == 1

    @pytest.mark.asyncio
    async def test_request_failure_returns_none(self, client):
        with patch.object(client, "_call_arbitration", return_value=None):
            result = await client.get_arbitration("123")
        assert result is None
