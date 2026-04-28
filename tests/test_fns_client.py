"""Тесты FnsClient: парсеры ЮЛ/ИП и HTTP-слой через monkeypatch."""
from datetime import datetime
from typing import Any, Dict, Optional

import pytest

import fns_client
from fns_client import FnsClient
from schemas import CompanyData


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None,
                 text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


def _ul(**overrides: Any) -> Dict[str, Any]:
    """Минимальный валидный блок ЮЛ от API-ФНС."""
    base: Dict[str, Any] = {
        "ИНН": "7707083893",
        "ОГРН": "1027700132195",
        "КПП": "770701001",
        "НаимПолнЮЛ": 'Публичное акционерное общество "Сбербанк"',
        "НаимСокрЮЛ": "ПАО Сбербанк",
        "ДатаРег": "1991-03-20",
        "Адрес": {
            "АдресПолн": "117997, г. Москва, ул. Вавилова, д. 19",
            "Регион": "77",
        },
        "ОснВидДеят": {"Код": "64.19", "Текст": "Денежное посредничество"},
        "Руководитель": {"ФИОПолн": "Греф Г.О.", "Должн": "Президент"},
        "УстКап": {"Сум": "67760844000"},
        "Статус": {"Наим": "Действующее"},
    }
    base.update(overrides)
    return base


def _ip(**overrides: Any) -> Dict[str, Any]:
    """Минимальный валидный блок ИП."""
    base: Dict[str, Any] = {
        "ИНН": "123456789012",
        "ОГРНИП": "304500116000157",
        "Фамилия": "Иванов",
        "Имя": "Иван",
        "Отчество": "Иванович",
        "ДатаРег": "2010-05-15",
        "КодОКВЭД": "47.11",
        "Статус": {"Наим": "Действующий"},
    }
    base.update(overrides)
    return base


class TestFnsParseDispatch:
    def test_no_items_returns_none(self):
        assert FnsClient._parse({"items": []}, "1") is None

    def test_missing_items_key_returns_none(self):
        assert FnsClient._parse({}, "1") is None

    def test_dispatches_to_ul(self):
        result = FnsClient._parse({"items": [{"ЮЛ": _ul()}]}, "1")
        assert isinstance(result, CompanyData)
        assert result.source == "fns"
        assert result.ogrn == "1027700132195"

    def test_dispatches_to_ip(self):
        result = FnsClient._parse({"items": [{"ИП": _ip()}]}, "1")
        assert isinstance(result, CompanyData)
        assert result.source == "fns"
        assert result.ogrn == "304500116000157"

    def test_neither_ul_nor_ip_returns_none(self):
        assert FnsClient._parse({"items": [{"НечтоИное": {}}]}, "1") is None


class TestFnsParseUL:
    def test_core_fields(self):
        result = FnsClient._parse_ul(_ul(), "X")
        assert result.inn == "7707083893"
        assert result.ogrn == "1027700132195"
        assert result.kpp == "770701001"
        assert result.source == "fns"

    def test_name_full_preferred(self):
        ul = _ul()
        result = FnsClient._parse_ul(ul, "X")
        assert result.name == 'Публичное акционерное общество "Сбербанк"'

    def test_name_falls_back_to_short(self):
        ul = _ul(НаимПолнЮЛ=None)
        result = FnsClient._parse_ul(ul, "X")
        assert result.name == "ПАО Сбербанк"

    def test_address_polnyy_preferred(self):
        result = FnsClient._parse_ul(_ul(), "X")
        assert result.address == "117997, г. Москва, ул. Вавилова, д. 19"

    def test_address_assembled_from_parts(self):
        ul = _ul()
        ul["Адрес"] = {
            "Индекс": "117997",
            "Регион": "77",
            "Город": "Москва",
            "Улица": "Вавилова",
            "Дом": "19",
        }
        result = FnsClient._parse_ul(ul, "X")
        assert result.address == "117997, 77, Москва, Вавилова, 19"

    def test_okved_from_dict(self):
        result = FnsClient._parse_ul(_ul(), "X")
        assert result.okved_main == "64.19"
        assert result.okved_name == "Денежное посредничество"

    def test_okved_fallback_to_flat_field(self):
        ul = _ul(ОснВидДеят=None, КодОКВЭД="62.01")
        result = FnsClient._parse_ul(ul, "X")
        assert result.okved_main == "62.01"

    def test_director_with_post(self):
        result = FnsClient._parse_ul(_ul(), "X")
        assert result.director == "Президент: Греф Г.О."

    def test_director_assembled_from_parts(self):
        ul = _ul()
        ul["Руководитель"] = {
            "Фамилия": "Иванов",
            "Имя": "Иван",
            "Отчество": "Иванович",
            "Должн": "Директор",
        }
        result = FnsClient._parse_ul(ul, "X")
        assert result.director == "Директор: Иванов Иван Иванович"

    def test_director_without_post_keeps_fio(self):
        ul = _ul()
        ul["Руководитель"] = {"ФИОПолн": "Сидоров С.С."}
        result = FnsClient._parse_ul(ul, "X")
        assert result.director == "Сидоров С.С."

    def test_director_missing(self):
        ul = _ul(Руководитель={})
        result = FnsClient._parse_ul(ul, "X")
        assert result.director is None

    def test_capital_parsed(self):
        result = FnsClient._parse_ul(_ul(), "X")
        assert result.capital == 67760844000.0

    def test_capital_invalid_left_zero(self):
        # Текущая реализация делает float(... or 0) и попадает в except
        # только при ValueError; "abc" даёт ValueError и оставит None.
        ul = _ul()
        ul["УстКап"] = {"Сум": "abc"}
        result = FnsClient._parse_ul(ul, "X")
        assert result.capital is None

    def test_status_from_dict_naim(self):
        result = FnsClient._parse_ul(_ul(), "X")
        assert result.status == "Действующее"

    def test_status_as_plain_string(self):
        ul = _ul(Статус="В стадии ликвидации")
        result = FnsClient._parse_ul(ul, "X")
        assert result.status == "В стадии ликвидации"

    def test_age_years_from_reg_date(self):
        ul = _ul(ДатаРег="2020-01-01")
        result = FnsClient._parse_ul(ul, "X")
        expected = (datetime.now() - datetime(2020, 1, 1)).days // 365
        assert result.age_years == expected

    def test_invalid_reg_date_does_not_crash(self):
        ul = _ul(ДатаРег="not-a-date")
        result = FnsClient._parse_ul(ul, "X")
        assert result.age_years is None

    def test_inn_fallback_to_passed(self):
        ul = _ul(ИНН=None)
        result = FnsClient._parse_ul(ul, "FALLBACK")
        assert result.inn == "FALLBACK"


class TestFnsParseIP:
    def test_core_fields(self):
        result = FnsClient._parse_ip(_ip(), "X")
        assert result.inn == "123456789012"
        assert result.ogrn == "304500116000157"
        assert result.source == "fns"

    def test_name_assembled_from_fio(self):
        result = FnsClient._parse_ip(_ip(), "X")
        assert result.name == "ИП Иванов Иван Иванович"

    def test_name_with_partial_fio(self):
        ip = _ip(Отчество=None)
        result = FnsClient._parse_ip(ip, "X")
        assert result.name == "ИП Иванов Иван"

    def test_no_fio_yields_none_name(self):
        ip = _ip(Фамилия=None, Имя=None, Отчество=None)
        result = FnsClient._parse_ip(ip, "X")
        assert result.name is None

    def test_okved_from_flat_field(self):
        result = FnsClient._parse_ip(_ip(), "X")
        assert result.okved_main == "47.11"

    def test_age_years_from_reg_date(self):
        result = FnsClient._parse_ip(_ip(), "X")
        assert result.age_years is not None
        assert result.age_years > 0

    def test_status_from_dict(self):
        result = FnsClient._parse_ip(_ip(), "X")
        assert result.status == "Действующий"

    def test_inn_fallback_to_passed(self):
        ip = _ip(ИНН=None)
        result = FnsClient._parse_ip(ip, "FALLBACK")
        assert result.inn == "FALLBACK"


class TestFnsFetchCompany:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("FNS_API_KEY", raising=False)
        client = FnsClient()
        assert await client.fetch_company("123") is None

    @pytest.mark.asyncio
    async def test_successful_response_parsed(self, monkeypatch):
        monkeypatch.setenv("FNS_API_KEY", "test-key")
        client = FnsClient()

        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse(200, {"items": [{"ЮЛ": _ul()}]})

        monkeypatch.setattr(fns_client.requests, "get", fake_get)
        result = await client.fetch_company("7707083893")

        assert isinstance(result, CompanyData)
        assert result.ogrn == "1027700132195"
        assert captured["params"] == {"req": "7707083893", "key": "test-key"}

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, monkeypatch):
        monkeypatch.setenv("FNS_API_KEY", "test-key")
        client = FnsClient()
        monkeypatch.setattr(
            fns_client.requests, "get",
            lambda *a, **kw: FakeResponse(503, {}, text="unavailable"),
        )
        assert await client.fetch_company("123") is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, monkeypatch):
        monkeypatch.setenv("FNS_API_KEY", "test-key")
        client = FnsClient()

        def boom(*a, **kw):
            raise ConnectionError("dns failure")

        monkeypatch.setattr(fns_client.requests, "get", boom)
        assert await client.fetch_company("123") is None

    @pytest.mark.asyncio
    async def test_ip_response_parsed(self, monkeypatch):
        monkeypatch.setenv("FNS_API_KEY", "test-key")
        client = FnsClient()
        monkeypatch.setattr(
            fns_client.requests, "get",
            lambda *a, **kw: FakeResponse(200, {"items": [{"ИП": _ip()}]}),
        )
        result = await client.fetch_company("123456789012")
        assert result.name == "ИП Иванов Иван Иванович"
