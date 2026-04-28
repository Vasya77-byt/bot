import json

from parsers import (
    extract_inn,
    extract_mode,
    parse_company_json,
    parse_message,
    to_company,
)
from schemas import CompanyData


class TestExtractInn:
    def test_extracts_10_digit_inn(self):
        assert extract_inn("ИНН 7707083893 Сбербанк") == "7707083893"

    def test_extracts_12_digit_inn(self):
        assert extract_inn("ИП ИНН 123456789012") == "123456789012"

    def test_returns_none_when_no_inn(self):
        assert extract_inn("нет цифр здесь") is None

    def test_returns_none_for_short_number(self):
        assert extract_inn("номер 12345") is None

    def test_returns_none_for_11_digits(self):
        assert extract_inn("число 12345678901") is None

    def test_returns_first_inn_when_multiple(self):
        assert extract_inn("ИНН 7707083893 и ИНН 1234567890") == "7707083893"

    def test_extracts_inn_at_start(self):
        assert extract_inn("7707083893 — это ИНН") == "7707083893"

    def test_extracts_inn_at_end(self):
        assert extract_inn("ИНН компании: 7707083893") == "7707083893"


class TestExtractMode:
    def test_extracts_internal_analysis(self):
        assert extract_mode("mode=internal_analysis") == "internal_analysis"

    def test_extracts_client_proposal(self):
        assert extract_mode("текст mode=client_proposal продолжение") == "client_proposal"

    def test_case_insensitive(self):
        assert extract_mode("MODE=internal_analysis") == "internal_analysis"

    def test_with_spaces_around_equals(self):
        assert extract_mode("mode = client_proposal") == "client_proposal"

    def test_returns_none_when_no_mode(self):
        assert extract_mode("обычный текст без режима") is None


class TestParseMessage:
    def test_request_trigger(self):
        result = parse_message("дай заявку 7707083893")
        assert result.is_request is True
        assert result.is_proposal is False
        assert result.inn == "7707083893"

    def test_proposal_trigger(self):
        result = parse_message("дай предложение 7707083893")
        assert result.is_proposal is True
        assert result.is_request is False
        assert result.inn == "7707083893"

    def test_request_case_insensitive(self):
        result = parse_message("ДАЙ ЗАЯВКУ 7707083893")
        assert result.is_request is True

    def test_plain_inn_no_triggers(self):
        result = parse_message("просто 7707083893")
        assert result.inn == "7707083893"
        assert result.is_request is False
        assert result.is_proposal is False

    def test_no_inn_no_triggers(self):
        result = parse_message("привет")
        assert result.inn is None
        assert result.is_request is False
        assert result.is_proposal is False
        assert result.company_data is None

    def test_extracts_mode_alongside(self):
        result = parse_message("mode=internal_analysis 7707083893")
        assert result.mode == "internal_analysis"
        assert result.inn == "7707083893"

    def test_company_json_populates_company_data(self):
        payload = json.dumps({"inn": "7707083893", "name": "Acme"})
        result = parse_message(payload)
        assert isinstance(result.company_data, CompanyData)
        assert result.company_data.inn == "7707083893"
        assert result.company_data.name == "Acme"


class TestParseCompanyJson:
    def test_valid_json_dict(self):
        company = parse_company_json('{"inn": "7707083893", "name": "Acme"}')
        assert isinstance(company, CompanyData)
        assert company.inn == "7707083893"

    def test_invalid_json_returns_none(self):
        assert parse_company_json("not a json {") is None

    def test_json_list_returns_none(self):
        assert parse_company_json('[{"inn": "123"}]') is None

    def test_empty_string_returns_none(self):
        assert parse_company_json("") is None

    def test_json_string_returns_none(self):
        assert parse_company_json('"just a string"') is None


class TestToCompany:
    def test_empty_dict_returns_empty_company(self):
        company = to_company({})
        # empty_company() defaults: name/ogrn/region/reg_date/okved_main = "не указано"
        assert company.name == "не указано"
        assert company.ogrn == "не указано"
        assert company.inn is None

    def test_populates_known_fields(self):
        data = {
            "inn": "7707083893",
            "name": "Сбербанк",
            "ogrn": "1027700132195",
            "region": "Москва",
            "okved_main": "64.19",
            "employees_count": 250000,
        }
        company = to_company(data)
        assert company.inn == "7707083893"
        assert company.name == "Сбербанк"
        assert company.ogrn == "1027700132195"
        assert company.region == "Москва"
        assert company.okved_main == "64.19"
        assert company.employees_count == 250000

    def test_missing_fields_default_to_none(self):
        company = to_company({"inn": "7707083893"})
        assert company.inn == "7707083893"
        assert company.name is None
        assert company.ogrn is None
