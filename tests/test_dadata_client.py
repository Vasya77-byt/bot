"""Тесты DaDataClient: pure-парсер _parse и HTTP-слой через monkeypatch."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

import dadata_client
from dadata_client import DaDataClient
from schemas import CompanyData


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None,
                 text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


def _suggestion(**overrides: Any) -> Dict[str, Any]:
    """Минимальный валидный suggestion от DaData."""
    base: Dict[str, Any] = {
        "value": "ООО Ромашка",
        "data": {
            "inn": "7707083893",
            "ogrn": "1027700132195",
            "kpp": "770701001",
            "name": {"full_with_opf": 'ООО "Ромашка"'},
            "address": {
                "value": "Москва, ул. Пушкина",
                "unrestricted_value": "г. Москва, ул. Пушкина, д. 1",
                "data": {"region_with_type": "г Москва"},
            },
            "okved": "62.01",
            "okved_type2": "Разработка ПО",
            "state": {"status": "ACTIVE"},
            "management": {"name": "Иванов И.И.", "post": "Генеральный директор"},
            "capital": {"value": 100000.0},
            "employee_count": "25",
            "ogrn_date": 1262304000000,  # 2010-01-01 UTC
        },
    }
    base["data"].update(overrides.pop("data_overrides", {}))
    base.update(overrides)
    return base


class TestDaDataParse:
    def test_empty_suggestions_returns_none(self):
        assert DaDataClient._parse({"suggestions": []}, "123") is None

    def test_missing_suggestions_key_returns_none(self):
        assert DaDataClient._parse({}, "123") is None

    def test_minimal_response_parses_core_fields(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "7707083893")
        assert isinstance(result, CompanyData)
        assert result.inn == "7707083893"
        assert result.ogrn == "1027700132195"
        assert result.kpp == "770701001"
        assert result.source == "dadata"

    def test_name_from_value(self):
        sugg = _suggestion()
        sugg["value"] = "ООО Ромашка"
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.name == "ООО Ромашка"

    def test_name_falls_back_to_full_with_opf(self):
        sugg = _suggestion()
        sugg["value"] = None
        sugg["data"]["name"] = {"full_with_opf": 'ООО "Запас"'}
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.name == 'ООО "Запас"'

    def test_address_unrestricted_preferred(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        assert result.address == "г. Москва, ул. Пушкина, д. 1"

    def test_address_falls_back_to_value(self):
        sugg = _suggestion()
        sugg["data"]["address"]["unrestricted_value"] = None
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.address == "Москва, ул. Пушкина"

    def test_region_from_address_data(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        assert result.region == "г Москва"

    def test_okved_main_and_name(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        assert result.okved_main == "62.01"
        assert result.okved_name == "Разработка ПО"

    def test_status_mapping_active(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        assert result.status == "Действующая"

    def test_status_mapping_liquidated(self):
        sugg = _suggestion()
        sugg["data"]["state"] = {"status": "LIQUIDATED"}
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.status == "Ликвидирована"

    def test_status_mapping_bankrupt(self):
        sugg = _suggestion()
        sugg["data"]["state"] = {"status": "BANKRUPT"}
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.status == "Банкрот"

    def test_status_unknown_returned_as_is(self):
        sugg = _suggestion()
        sugg["data"]["state"] = {"status": "WEIRD_STATUS"}
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.status == "WEIRD_STATUS"

    def test_director_with_post(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        assert result.director == "Генеральный директор: Иванов И.И."

    def test_director_without_post(self):
        sugg = _suggestion()
        sugg["data"]["management"] = {"name": "Петров П.П.", "post": None}
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.director == "Петров П.П."

    def test_director_missing(self):
        sugg = _suggestion()
        sugg["data"]["management"] = {}
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.director is None

    def test_reg_date_from_ogrn_date(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        # 1262304000000 ms = 2010-01-01 UTC
        assert result.reg_date == "2010-01-01"

    def test_age_years_calculated(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        expected = (datetime.now(tz=timezone.utc)
                    - datetime(2010, 1, 1, tzinfo=timezone.utc)).days // 365
        assert result.age_years == expected

    def test_invalid_ogrn_date_does_not_crash(self):
        sugg = _suggestion()
        sugg["data"]["ogrn_date"] = "not-a-number"
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.reg_date is None
        assert result.age_years is None

    def test_capital_from_value(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        assert result.capital == 100000.0

    def test_capital_missing(self):
        sugg = _suggestion()
        sugg["data"]["capital"] = None
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.capital is None

    def test_employees_string_to_int(self):
        result = DaDataClient._parse({"suggestions": [_suggestion()]}, "1")
        assert result.employees_count == 25

    def test_employees_invalid_left_none(self):
        sugg = _suggestion()
        sugg["data"]["employee_count"] = "много"
        result = DaDataClient._parse({"suggestions": [sugg]}, "1")
        assert result.employees_count is None

    def test_inn_fallback_to_passed(self):
        sugg = _suggestion()
        sugg["data"]["inn"] = None
        result = DaDataClient._parse({"suggestions": [sugg]}, "FALLBACK_INN")
        assert result.inn == "FALLBACK_INN"


class TestDaDataFetchCompany:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("DADATA_API_KEY", raising=False)
        client = DaDataClient()
        result = await client.fetch_company("7707083893")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_response_parsed(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "test-key")
        client = DaDataClient()

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse(200, {"suggestions": [_suggestion()]})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)
        result = await client.fetch_company("7707083893")

        assert isinstance(result, CompanyData)
        assert result.inn == "7707083893"
        assert captured["json"] == {"query": "7707083893", "count": 1}
        assert captured["headers"]["Authorization"] == "Token test-key"

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "test-key")
        client = DaDataClient()
        monkeypatch.setattr(
            dadata_client.requests, "post",
            lambda *a, **kw: FakeResponse(500, {}, text="server error"),
        )
        assert await client.fetch_company("123") is None

    @pytest.mark.asyncio
    async def test_exception_in_request_returns_none(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "test-key")
        client = DaDataClient()

        def boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(dadata_client.requests, "post", boom)
        assert await client.fetch_company("123") is None

    @pytest.mark.asyncio
    async def test_empty_suggestions_returns_none(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "test-key")
        client = DaDataClient()
        monkeypatch.setattr(
            dadata_client.requests, "post",
            lambda *a, **kw: FakeResponse(200, {"suggestions": []}),
        )
        assert await client.fetch_company("123") is None


class TestDaDataSuggestByName:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty(self, monkeypatch):
        monkeypatch.delenv("DADATA_API_KEY", raising=False)
        client = DaDataClient()
        assert await client.suggest_by_name("Сбер") == []

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        assert await client.suggest_by_name("") == []
        assert await client.suggest_by_name("   ") == []

    @pytest.mark.asyncio
    async def test_returns_list_of_companies(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()

        sug1 = _suggestion()
        sug2 = _suggestion()
        sug2["data"]["inn"] = "9999999999"
        sug2["data"]["name"] = {"full_with_opf": "ООО Альфа"}
        sug2["value"] = "ООО Альфа"

        monkeypatch.setattr(
            dadata_client.requests, "post",
            lambda *a, **kw: FakeResponse(200, {"suggestions": [sug1, sug2]}),
        )
        result = await client.suggest_by_name("Сбер")
        assert len(result) == 2
        assert isinstance(result[0], CompanyData)
        assert result[0].source == "dadata"
        assert result[1].name == "ООО Альфа"
        assert result[1].inn == "9999999999"

    @pytest.mark.asyncio
    async def test_passes_query_and_count_to_api(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(200, {"suggestions": []})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)
        await client.suggest_by_name("Сбер", count=7)

        assert captured["url"].endswith("/suggest/party")
        assert captured["json"] == {"query": "Сбер", "count": 7}

    @pytest.mark.asyncio
    async def test_count_clamped_to_max_20(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return FakeResponse(200, {"suggestions": []})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)
        await client.suggest_by_name("Сбер", count=100)
        assert captured["json"]["count"] == 20

    @pytest.mark.asyncio
    async def test_count_clamped_to_min_1(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return FakeResponse(200, {"suggestions": []})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)
        await client.suggest_by_name("Сбер", count=0)
        assert captured["json"]["count"] == 1

    @pytest.mark.asyncio
    async def test_query_trimmed(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return FakeResponse(200, {"suggestions": []})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)
        await client.suggest_by_name("  Сбер  ")
        assert captured["json"]["query"] == "Сбер"

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        monkeypatch.setattr(
            dadata_client.requests, "post",
            lambda *a, **kw: FakeResponse(500, {}, text="server error"),
        )
        assert await client.suggest_by_name("Сбер") == []

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()

        def boom(*a, **kw):
            raise ConnectionError("dns fail")

        monkeypatch.setattr(dadata_client.requests, "post", boom)
        assert await client.suggest_by_name("Сбер") == []

    @pytest.mark.asyncio
    async def test_authorization_header_passed(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "secret-token")
        client = DaDataClient()
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return FakeResponse(200, {"suggestions": []})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)
        await client.suggest_by_name("Сбер")
        assert captured["headers"]["Authorization"] == "Token secret-token"

    @pytest.mark.asyncio
    async def test_each_result_has_source_dadata(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        monkeypatch.setattr(
            dadata_client.requests, "post",
            lambda *a, **kw: FakeResponse(200, {"suggestions": [_suggestion(), _suggestion()]}),
        )
        result = await client.suggest_by_name("Сбер")
        assert all(c.source == "dadata" for c in result)


class TestDaDataCrossUserCache:
    """Cross-user persistent cache: один и тот же ИНН/query — один HTTP-вызов
    в окно TTL, независимо от того, сколько разных юзеров спросило."""

    @pytest.mark.asyncio
    async def test_fetch_company_caches_response(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        call_count = {"n": 0}

        def fake_post(*a, **kw):
            call_count["n"] += 1
            return FakeResponse(200, {"suggestions": [_suggestion()]})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)

        r1 = await client.fetch_company("7707083893")
        r2 = await client.fetch_company("7707083893")
        r3 = await client.fetch_company("7707083893")

        assert r1 is not None and r2 is not None and r3 is not None
        # 3 запроса юзера → 1 HTTP-вызов
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_fetch_company_different_inns_separate(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        call_count = {"n": 0}

        def fake_post(*a, **kw):
            call_count["n"] += 1
            return FakeResponse(200, {"suggestions": [_suggestion()]})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)

        await client.fetch_company("7707083893")
        await client.fetch_company("7728168971")
        # Разные ИНН — разные ключи кэша
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_failed_call_not_cached(self, monkeypatch):
        """Если запрос упал (None) — не кэшируем, чтобы при восстановлении
        связи следующий вызов реально проверил API."""
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        call_count = {"n": 0}

        def fake_post(*a, **kw):
            call_count["n"] += 1
            return FakeResponse(500, {})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)
        await client.fetch_company("7707083893")
        await client.fetch_company("7707083893")
        assert call_count["n"] == 2  # оба запроса прошли

    @pytest.mark.asyncio
    async def test_suggest_caches_by_normalized_query(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        call_count = {"n": 0}

        def fake_post(*a, **kw):
            call_count["n"] += 1
            return FakeResponse(200, {"suggestions": [_suggestion()]})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)

        await client.suggest_by_name("Сбер")
        await client.suggest_by_name("сбер")    # lowercase
        await client.suggest_by_name("  Сбер  ")  # с пробелами
        # Все три варианта приводятся к одному ключу
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_suggest_different_count_separate(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "k")
        client = DaDataClient()
        call_count = {"n": 0}

        def fake_post(*a, **kw):
            call_count["n"] += 1
            return FakeResponse(200, {"suggestions": [_suggestion()]})

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)

        await client.suggest_by_name("Сбер", count=5)
        await client.suggest_by_name("Сбер", count=10)
        # Разный count — разные ключи (для маленького count меньше данных)
        assert call_count["n"] == 2

