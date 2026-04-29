"""Тесты zchb_client — парсинг ответа court-arbitration / fssp / rating / card + кеш."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from zchb_client import (
    ArbitrationSummary,
    CardSummary,
    FsspSummary,
    RatingResult,
    ZchbClient,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ZCHB_API_KEY", "test-key")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    return ZchbClient()


@pytest.fixture
def no_key_client(tmp_path, monkeypatch):
    monkeypatch.delenv("ZCHB_API_KEY", raising=False)
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
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
    async def test_inn_response_with_docs_array(self, client):
        """Реальный формат ЗЧБ при поиске по ИНН:
        body = {"total": N, "docs": [{"точно": ..., "неточно": ...}]}"""
        inner = _arbitration_body(exact_total=2, our_inn="7707083893")
        body = {"total": 1, "docs": [inner]}
        with patch.object(client, "_call_arbitration",
                          return_value={"status": "200",
                                        "message": "ok",
                                        "body": body}):
            result = await client.get_arbitration("7707083893")
        assert result.total_exact == 2
        assert result.as_plaintiff_count == 1
        assert result.as_defendant_count == 1
        assert len(result.cases) == 2

    @pytest.mark.asyncio
    async def test_empty_docs_array(self, client):
        with patch.object(client, "_call_arbitration",
                          return_value={"status": "200",
                                        "message": "ok",
                                        "body": {"total": 0, "docs": []}}):
            result = await client.get_arbitration("7707083893")
        assert isinstance(result, ArbitrationSummary)
        assert not result.has_cases

    @pytest.mark.asyncio
    async def test_request_failure_returns_none(self, client):
        with patch.object(client, "_call_arbitration", return_value=None):
            result = await client.get_arbitration("123")
        assert result is None


class TestCache:
    @pytest.mark.asyncio
    async def test_arbitration_uses_cache_on_second_call(self, client):
        body = _arbitration_body(exact_total=1, our_inn="7707083893")
        call_count = {"n": 0}

        def fake_call(inn):
            call_count["n"] += 1
            return {"status": "200", "message": "ok",
                    "body": {"total": 1, "docs": [body]}}

        with patch.object(client, "_call_arbitration", side_effect=fake_call):
            r1 = await client.get_arbitration("7707083893")
            r2 = await client.get_arbitration("7707083893")
        assert call_count["n"] == 1
        assert r1.total_exact == r2.total_exact == 1

    @pytest.mark.asyncio
    async def test_cache_isolated_per_id(self, client):
        body1 = _arbitration_body(exact_total=1, our_inn="111")
        body2 = _arbitration_body(exact_total=2, our_inn="222")
        responses = {
            "111": {"status": "200", "body": {"total": 1, "docs": [body1]}},
            "222": {"status": "200", "body": {"total": 1, "docs": [body2]}},
        }
        with patch.object(client, "_call_arbitration",
                          side_effect=lambda inn: responses[inn]):
            r1 = await client.get_arbitration("111")
            r2 = await client.get_arbitration("222")
        assert r1.total_exact == 1
        assert r2.total_exact == 2


class TestRating:
    @pytest.mark.asyncio
    async def test_no_key(self, no_key_client):
        assert await no_key_client.get_rating("123") is None

    @pytest.mark.asyncio
    async def test_normal_response(self, client):
        with patch.object(client, "_simple_call", return_value={
                "status": "200", "message": "ok",
                "body": {"rating_category": "высокий", "risk_level": "низкий"}}):
            r = await client.get_rating("123")
        assert r.rating_category == "высокий"
        assert r.risk_level == "низкий"

    @pytest.mark.asyncio
    async def test_rating_uses_cache(self, client):
        call_count = {"n": 0}

        def fake_call(method, ident):
            call_count["n"] += 1
            return {"status": "200", "body": {"rating_category": "x"}}

        with patch.object(client, "_simple_call", side_effect=fake_call):
            await client.get_rating("123")
            await client.get_rating("123")
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_invalid_key_returns_none(self, client):
        with patch.object(client, "_simple_call", return_value={
                "status": "215", "message": "Задан неверный ключ"}):
            assert await client.get_rating("123") is None


class TestFssp:
    @pytest.mark.asyncio
    async def test_no_key(self, no_key_client):
        assert await no_key_client.get_fssp("ОГРН") is None

    @pytest.mark.asyncio
    async def test_no_proceedings_status_235(self, client):
        with patch.object(client, "_simple_call", return_value={
                "status": "235", "message": "не найдено"}):
            r = await client.get_fssp("ОГРН")
        assert isinstance(r, FsspSummary)
        assert r.total == 0
        assert not r.has_proceedings

    @pytest.mark.asyncio
    async def test_normal_response(self, client):
        body = {
            "total": 2,
            "docs": [
                {
                    "Должник": "ПАО СБЕРБАНК",
                    "НомИспПроизв": "26967/18/77021-ИП",
                    "ДатаВозбуждения": 1522962000,
                    "ТипИспДок": "Исполнительный лист",
                    "ПредметИсп": "Госпошлина",
                    "СуммаДолга": "0",
                    "ОстатокДолга": "400",
                    "ОтделСудебПрист": "Перовский РОСП",
                },
                {
                    "Должник": "ПАО СБЕРБАНК",
                    "НомИспПроизв": "174314/18/77058-ИП",
                    "ДатаВозбуждения": 1519160400,
                    "ТипИспДок": "Акт по делу об АП",
                    "ПредметИсп": "Штраф",
                    "СуммаДолга": "6000",
                    "ОстатокДолга": "6000",
                    "ОтделСудебПрист": "МОСП по ВАШ №7",
                },
            ],
        }
        with patch.object(client, "_simple_call", return_value={
                "status": "200", "body": body}):
            r = await client.get_fssp("1027700132195")
        assert r.total == 2
        assert len(r.proceedings) == 2
        assert r.total_remaining == 6400  # 400 + 6000
        assert r.proceedings[0].case_number == "26967/18/77021-ИП"
        assert r.proceedings[1].debt_total == 6000.0


class TestCard:
    @pytest.mark.asyncio
    async def test_no_key(self, no_key_client):
        assert await no_key_client.get_card("123") is None

    @pytest.mark.asyncio
    async def test_normal_response(self, client):
        body = {
            "ИНН": "7707083893",
            "ОГРН": "1027700132195",
            "НаимЮЛПолн": "ПАО СБЕРБАНК",
            "НаимЮЛСокр": "СБЕР",
            "Активность": "Действующее",
            "Реестр01": "0",
            "Реестр02": "0",
            "СвНедАдресЮЛ": None,
            "СвЛицензия": 5,
            "Проверки": 12,
            "ЧислСотруд": 100000,
            "ФондОплТруда": 1500000000,
            "СредЗП": 150000,
            "НалогПравонаруш": "2492.00",
            "НедобросовПостав": False,
            "СудыСтатистика": {"всего": 167016},
            "ЗакупкиСтат": {
                "КонтрПоставщКолв": 34,
                "КонтрПоставщСум": 988117735,
                "КонтрЗакупщКолв": 51,
                "КонтрЗакупщСум": 75751826,
            },
            "СуммНедоимЗадолж": [
                {"ОбщСумНедоим": "35657.66"},
                {"ОбщСумНедоим": "100.00"},
            ],
            "Руководители": [{
                "fl": "Греф",
                "mass_leaders": "0",
                "aff": {"boss": {"inn": 1, "namesake": 50}},
            }],
            "СвУчредит": {
                "all": [{"name": "ЦБ РФ", "mass_founders": "0"}],
            },
        }
        with patch.object(client, "_simple_call", return_value={
                "status": "200", "body": body}):
            r = await client.get_card("7707083893")

        assert r.inn == "7707083893"
        assert r.ogrn == "1027700132195"
        assert r.name_short == "СБЕР"
        assert r.licenses_count == 5
        assert r.inspections_count == 12
        assert r.employees_count == 100000
        assert r.payroll_fund == 1500000000
        assert r.tax_violations_sum == 2492.0
        assert r.courts_total == 167016
        assert r.contracts_supplier_count == 34
        assert r.contracts_supplier_sum == 988117735.0
        assert r.tax_debt_sum == 35757.66  # 35657.66 + 100.00
        assert r.director_namesake_count == 50
        assert r.director_is_mass_leader is False
        assert r.in_debt_registry is False
        assert r.is_unreliable_supplier is False

    @pytest.mark.asyncio
    async def test_card_with_nested_inn_structure(self, client):
        """body имеет цифровые ключи — берём первый."""
        inner = {"ИНН": "111", "ОГРН": "1", "Активность": "Действующее"}
        with patch.object(client, "_simple_call", return_value={
                "status": "200", "body": {"0": inner}}):
            r = await client.get_card("111")
        assert r.inn == "111"

    @pytest.mark.asyncio
    async def test_card_uses_cache(self, client):
        call_count = {"n": 0}

        def fake_call(method, ident):
            call_count["n"] += 1
            return {"status": "200", "body": {"ИНН": "111", "Активность": "x"}}

        with patch.object(client, "_simple_call", side_effect=fake_call):
            await client.get_card("111")
            await client.get_card("111")
        assert call_count["n"] == 1
