"""Тесты CompanyService — агрегация DaData + FNS + SBIS."""
from typing import Optional

import pytest

from company_service import CompanyService
from schemas import CompanyData


class FakeClient:
    """Подменяет любой из источников. Поведение задаётся per-test."""

    def __init__(self, result: Optional[CompanyData] = None,
                 exception: Optional[Exception] = None):
        self.result = result
        self.exception = exception
        self.call_count = 0
        self.last_inn: Optional[str] = None

    async def fetch_company(self, inn: str) -> Optional[CompanyData]:
        return await self._call(inn)

    async def fetch_company_data(self, inn: str) -> Optional[CompanyData]:
        return await self._call(inn)

    async def _call(self, inn: str) -> Optional[CompanyData]:
        self.call_count += 1
        self.last_inn = inn
        if self.exception:
            raise self.exception
        return self.result


@pytest.fixture
def service(tmp_path, monkeypatch):
    # Не позволяем реальным клиентам инициализироваться с настоящими env
    monkeypatch.delenv("DADATA_API_KEY", raising=False)
    monkeypatch.delenv("FNS_API_KEY", raising=False)
    monkeypatch.delenv("SBIS_LOGIN", raising=False)
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    return CompanyService()


class TestMergeSingleSource:
    def test_single_source_passes_through(self):
        data = CompanyData(inn="123", name="Acme", source="dadata")
        merged = CompanyService._merge([data], inn="123")
        assert merged.inn == "123"
        assert merged.name == "Acme"
        assert merged.source == "dadata"

    def test_inn_fallback_when_source_empty(self):
        data = CompanyData(name="X", source="dadata")  # inn пустой
        merged = CompanyService._merge([data], inn="FALLBACK_INN")
        assert merged.inn == "FALLBACK_INN"

    def test_empty_results_uses_passed_inn(self):
        merged = CompanyService._merge([], inn="888")
        assert merged.inn == "888"
        assert merged.name is None
        assert merged.source is None


class TestMergeMultipleSources:
    def test_first_non_empty_wins(self):
        primary = CompanyData(inn="1", name="From DaData", source="dadata")
        secondary = CompanyData(inn="1", name="From FNS", source="fns")
        merged = CompanyService._merge([primary, secondary], inn="1")
        assert merged.name == "From DaData"

    def test_secondary_fills_gaps(self):
        primary = CompanyData(inn="1", name="Acme", source="dadata")  # без ОГРН
        secondary = CompanyData(inn="1", ogrn="1027700132195", source="fns")
        merged = CompanyService._merge([primary, secondary], inn="1")
        assert merged.name == "Acme"
        assert merged.ogrn == "1027700132195"

    def test_unspecified_sentinel_treated_as_empty(self):
        # Источник вернул "не указано" — должно проигнорироваться,
        # следующий источник имеет шанс заполнить поле
        primary = CompanyData(inn="1", name="не указано", source="dadata")
        secondary = CompanyData(inn="1", name="Acme Real", source="fns")
        merged = CompanyService._merge([primary, secondary], inn="1")
        assert merged.name == "Acme Real"

    def test_empty_string_treated_as_empty(self):
        primary = CompanyData(inn="1", region="", source="dadata")
        secondary = CompanyData(inn="1", region="Москва", source="fns")
        merged = CompanyService._merge([primary, secondary], inn="1")
        assert merged.region == "Москва"

    def test_zero_age_kept(self):
        # 0 — валидное значение для age_years (юная компания), не должно
        # подмениваться на следующий источник
        primary = CompanyData(inn="1", age_years=0, source="dadata")
        secondary = CompanyData(inn="1", age_years=10, source="fns")
        merged = CompanyService._merge([primary, secondary], inn="1")
        assert merged.age_years == 0

    def test_sources_joined_with_plus(self):
        a = CompanyData(inn="1", source="dadata")
        b = CompanyData(inn="1", source="fns")
        c = CompanyData(inn="1", source="sbis")
        merged = CompanyService._merge([a, b, c], inn="1")
        assert merged.source == "dadata+fns+sbis"

    def test_sources_skips_empty(self):
        a = CompanyData(inn="1", source="dadata")
        b = CompanyData(inn="1", source=None)
        merged = CompanyService._merge([a, b], inn="1")
        assert merged.source == "dadata"

    def test_three_way_merge_field_priority(self):
        # DaData: name+address; FNS: ogrn+kpp; SBIS: revenue
        d = CompanyData(inn="1", name="Acme", address="Адрес 1", source="dadata")
        f = CompanyData(inn="1", ogrn="OGRN-1", kpp="KPP-1", source="fns")
        s = CompanyData(inn="1", revenue_last_year=1_000_000.0,
                        profit_last_year=200_000.0, source="sbis")
        merged = CompanyService._merge([d, f, s], inn="1")
        assert merged.name == "Acme"
        assert merged.address == "Адрес 1"
        assert merged.ogrn == "OGRN-1"
        assert merged.kpp == "KPP-1"
        assert merged.revenue_last_year == 1_000_000.0
        assert merged.profit_last_year == 200_000.0
        assert merged.source == "dadata+fns+sbis"


class TestFetch:
    @pytest.mark.asyncio
    async def test_no_data_from_any_source_returns_none(self, service):
        service.dadata = FakeClient(result=None)
        service.fns = FakeClient(result=None)
        service.sbis = FakeClient(result=None)
        result = await service.fetch("7707083893")
        assert result is None

    @pytest.mark.asyncio
    async def test_only_dadata_returns_dadata_data(self, service):
        service.dadata = FakeClient(result=CompanyData(
            inn="123", name="Only DaData", source="dadata",
        ))
        service.fns = FakeClient(result=None)
        service.sbis = FakeClient(result=None)
        result = await service.fetch("123")
        assert result.name == "Only DaData"
        assert result.source == "dadata"

    @pytest.mark.asyncio
    async def test_all_sources_merged(self, service):
        service.dadata = FakeClient(result=CompanyData(
            inn="1", name="Acme", address="Адрес", source="dadata",
        ))
        service.fns = FakeClient(result=CompanyData(
            inn="1", ogrn="OGRN-1", source="fns",
        ))
        service.sbis = FakeClient(result=CompanyData(
            inn="1", revenue_last_year=500_000.0, source="sbis",
        ))
        result = await service.fetch("1")
        assert result.name == "Acme"
        assert result.ogrn == "OGRN-1"
        assert result.revenue_last_year == 500_000.0
        assert result.source == "dadata+fns+sbis"

    @pytest.mark.asyncio
    async def test_dadata_exception_does_not_break_others(self, service):
        service.dadata = FakeClient(exception=RuntimeError("dadata down"))
        service.fns = FakeClient(result=CompanyData(
            inn="1", name="From FNS", source="fns",
        ))
        service.sbis = FakeClient(result=None)
        result = await service.fetch("1")
        assert result is not None
        assert result.name == "From FNS"
        assert result.source == "fns"

    @pytest.mark.asyncio
    async def test_all_clients_exception_returns_none(self, service):
        service.dadata = FakeClient(exception=RuntimeError("a"))
        service.fns = FakeClient(exception=RuntimeError("b"))
        service.sbis = FakeClient(exception=RuntimeError("c"))
        result = await service.fetch("1")
        assert result is None

    @pytest.mark.asyncio
    async def test_each_client_called_once_with_inn(self, service):
        service.dadata = FakeClient(result=None)
        service.fns = FakeClient(result=None)
        service.sbis = FakeClient(result=None)
        await service.fetch("7707083893")
        assert service.dadata.call_count == 1
        assert service.fns.call_count == 1
        assert service.sbis.call_count == 1
        assert service.dadata.last_inn == "7707083893"
        assert service.fns.last_inn == "7707083893"
        assert service.sbis.last_inn == "7707083893"

    @pytest.mark.asyncio
    async def test_only_fns_works_returns_fns_data(self, service):
        service.dadata = FakeClient(result=None)
        service.fns = FakeClient(result=CompanyData(
            inn="1", name="FNS only", source="fns",
        ))
        service.sbis = FakeClient(result=None)
        result = await service.fetch("1")
        assert result.name == "FNS only"
        # Один источник — суффикс без "+"
        assert result.source == "fns"
