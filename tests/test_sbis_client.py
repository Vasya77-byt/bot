"""Тесты SbisClient: _extract_org/_normalize, in-memory cache, retry, mock-режим."""
import importlib
import sys
import time
from typing import Any, Dict, Optional

import pytest
import requests

from sbis_client import SbisClient
from schemas import CompanyData


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None,
                 text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    """SbisClient с кешем в tmp_path и заполненными credentials."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("SBIS_LOGIN", "u")
    monkeypatch.setenv("SBIS_PASSWORD", "p")
    monkeypatch.setenv("SBIS_API_KEY", "k")
    monkeypatch.setenv("SBIS_CLIENT_ID", "c")
    monkeypatch.setenv("SBIS_RETRIES", "2")
    monkeypatch.setenv("SBIS_RETRY_DELAY", "0")  # без задержек в тестах
    monkeypatch.setenv("SBIS_CACHE_TTL", "60")
    monkeypatch.delenv("SBIS_MOCK", raising=False)
    return SbisClient()


class TestExtractOrg:
    def test_result_organization(self):
        data = {"result": {"Organization": {"name": "X"}}}
        assert SbisClient._extract_org(data) == {"name": "X"}

    def test_organization_at_root(self):
        data = {"Organization": {"Name": "Y"}}
        assert SbisClient._extract_org(data) == {"Name": "Y"}

    def test_answer_data(self):
        data = {"answer": {"data": {"name": "Z"}}}
        assert SbisClient._extract_org(data) == {"name": "Z"}

    def test_flat_dict_fallback(self):
        data = {"name": "Flat"}
        assert SbisClient._extract_org(data) == {"name": "Flat"}

    def test_empty_returns_empty_dict(self):
        assert SbisClient._extract_org({}) == {}

    def test_non_dict_returns_empty(self):
        assert SbisClient._extract_org("not a dict") == {}

    def test_result_dict_without_organization_used_as_org(self):
        # result — dict, но без Organization. Тогда fallback идёт на сам result.
        data = {"result": {"name": "FromResult"}}
        assert SbisClient._extract_org(data) == {"name": "FromResult"}


class TestNormalize:
    def test_basic_lowercase_keys(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "name": "ООО Тест",
            "ogrn": "1234567890123",
            "region": "Москва",
        }}, inn="7707083893")
        assert company.name == "ООО Тест"
        assert company.ogrn == "1234567890123"
        assert company.region == "Москва"
        assert company.inn == "7707083893"

    def test_capitalized_keys(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "Name": "ООО Бета",
            "OGRN": "999",
            "Region": "СПб",
        }}, inn="0")
        assert company.name == "ООО Бета"
        assert company.ogrn == "999"
        assert company.region == "СПб"

    def test_russian_name_key(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "Наименование": "ООО Гамма",
        }}, inn="0")
        assert company.name == "ООО Гамма"

    def test_licenses_string_wrapped_to_list(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "licenses": "Лицензия №1",
        }}, inn="0")
        assert company.licenses == ["Лицензия №1"]

    def test_licenses_list_passthrough(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "licenses": ["L1", "L2"],
        }}, inn="0")
        assert company.licenses == ["L1", "L2"]

    def test_age_int_conversion(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "age_years": "5",
        }}, inn="0")
        assert company.age_years == 5

    def test_age_invalid_left_none(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "age_years": "много",
        }}, inn="0")
        assert company.age_years is None

    def test_inn_falls_back_to_payload_when_arg_empty(self, isolated_client):
        company = isolated_client._normalize({"Organization": {
            "INN": "from-payload",
        }}, inn="")
        assert company.inn == "from-payload"


class TestSafeGet:
    def test_dict_returns_value(self):
        assert SbisClient._safe_get({"a": 1}, "a") == 1

    def test_dict_missing_returns_default(self):
        assert SbisClient._safe_get({}, "x", "default") == "default"

    def test_non_dict_returns_default(self):
        assert SbisClient._safe_get(None, "x", "fallback") == "fallback"
        assert SbisClient._safe_get("string", "x", 0) == 0


class TestInMemoryCache:
    def test_set_then_get(self, isolated_client):
        company = CompanyData(inn="1", name="X")
        isolated_client._cache_put("1", company)
        assert isolated_client._cache_get("1") == company

    def test_miss_returns_none(self, isolated_client):
        assert isolated_client._cache_get("missing") is None

    def test_expired_evicted(self, isolated_client):
        isolated_client.cache_ttl = 0.01
        company = CompanyData(inn="1")
        isolated_client._cache_put("1", company)
        time.sleep(0.05)
        assert isolated_client._cache_get("1") is None
        assert "1" not in isolated_client._cache


class TestFetchCompanyData:
    @pytest.mark.asyncio
    async def test_mock_mode_returns_mock(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("SBIS_MOCK", "true")
        client = SbisClient()
        result = await client.fetch_company_data("1234567890")
        assert result is not None
        assert result.inn == "1234567890"
        assert result.name == "ООО «Мокап»"

    @pytest.mark.asyncio
    async def test_missing_creds_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CACHE_DIR", str(tmp_path))
        for var in ("SBIS_LOGIN", "SBIS_PASSWORD", "SBIS_API_KEY", "SBIS_CLIENT_ID", "SBIS_MOCK"):
            monkeypatch.delenv(var, raising=False)
        client = SbisClient()
        assert await client.fetch_company_data("1") is None

    @pytest.mark.asyncio
    async def test_successful_http_caches_result(self, isolated_client, monkeypatch):
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(200, {"Organization": {"name": "ООО А"}}),
        )
        result = await isolated_client.fetch_company_data("777")
        assert result.name == "ООО А"
        # повторный вызов — должен попасть в in-memory cache, без HTTP
        def boom(*a, **kw):
            raise AssertionError("HTTP must not be called on cache hit")
        monkeypatch.setattr(requests, "post", boom)
        result2 = await isolated_client.fetch_company_data("777")
        assert result2.name == "ООО А"

    @pytest.mark.asyncio
    async def test_file_cache_used_after_new_instance(self, tmp_path, monkeypatch):
        # Первый клиент кладёт в файловый кеш
        monkeypatch.setenv("CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("SBIS_LOGIN", "u")
        monkeypatch.setenv("SBIS_PASSWORD", "p")
        monkeypatch.setenv("SBIS_API_KEY", "k")
        monkeypatch.setenv("SBIS_CLIENT_ID", "c")
        monkeypatch.setenv("SBIS_RETRIES", "0")
        monkeypatch.setenv("SBIS_RETRY_DELAY", "0")
        monkeypatch.setenv("SBIS_CACHE_TTL", "60")
        monkeypatch.delenv("SBIS_MOCK", raising=False)

        client1 = SbisClient()
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(200, {"Organization": {"name": "ООО Кеш"}}),
        )
        await client1.fetch_company_data("888")

        # Новый клиент: HTTP не должен дёргаться
        def fail(*a, **kw):
            raise AssertionError("HTTP must not be called when file cache is hot")

        client2 = SbisClient()
        monkeypatch.setattr(requests, "post", fail)
        result = await client2.fetch_company_data("888")
        assert result.name == "ООО Кеш"

    @pytest.mark.asyncio
    async def test_retries_on_exception(self, isolated_client, monkeypatch):
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("temp fail")
            return FakeResponse(200, {"Organization": {"name": "ООО Ретрай"}})

        monkeypatch.setattr(requests, "post", flaky)
        result = await isolated_client.fetch_company_data("999")
        assert result.name == "ООО Ретрай"
        assert calls["n"] == 3  # 1 первичный + 2 ретрая

    @pytest.mark.asyncio
    async def test_all_retries_fail_returns_none(self, isolated_client, monkeypatch):
        def always_fail(*a, **kw):
            raise ConnectionError("down")

        monkeypatch.setattr(requests, "post", always_fail)
        result = await isolated_client.fetch_company_data("0")
        assert result is None

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, isolated_client, monkeypatch):
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(500, {}, text="server error"),
        )
        result = await isolated_client.fetch_company_data("0")
        assert result is None


class TestSbisClientHasNoServerDeps:
    """Регрессия: sbis_client не должен тянуть FastAPI/uvicorn в прод —
    они нужны только для standalone-сервера sbis_mock.py."""

    def test_imports_without_fastapi_or_uvicorn(self, monkeypatch):
        # Блокируем fastapi и uvicorn перед чистым реимпортом sbis_client
        monkeypatch.setitem(sys.modules, "fastapi", None)
        monkeypatch.setitem(sys.modules, "uvicorn", None)
        # Сбрасываем кешированные версии sbis_client и его зависимостей
        for mod in ("sbis_client", "sbis_fixtures"):
            sys.modules.pop(mod, None)

        # Импорт должен пройти без ImportError
        reimported = importlib.import_module("sbis_client")
        assert reimported.SbisClient is not None
        # mock_company доступна через sbis_fixtures, не через sbis_mock
        from sbis_fixtures import mock_company
        company = mock_company("123")
        assert company.name == "ООО «Мокап»"
