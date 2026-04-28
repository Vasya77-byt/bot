import pytest

from schemas import CompanyData, empty_company


class TestEmptyCompany:
    def test_default_inn_is_none(self):
        company = empty_company()
        assert company.inn is None

    def test_inn_passed_through(self):
        company = empty_company(inn="7707083893")
        assert company.inn == "7707083893"

    def test_text_fields_marked_unspecified(self):
        company = empty_company()
        assert company.name == "не указано"
        assert company.ogrn == "не указано"
        assert company.region == "не указано"
        assert company.reg_date == "не указано"
        assert company.okved_main == "не указано"

    def test_numeric_fields_are_none(self):
        company = empty_company()
        assert company.age_years is None
        assert company.employees_count is None
        assert company.revenue_last_year is None
        assert company.profit_last_year is None

    def test_licenses_is_none(self):
        assert empty_company().licenses is None


class TestCompanyDataModel:
    def test_is_frozen(self):
        company = CompanyData(inn="123")
        with pytest.raises((TypeError, ValueError, Exception)):
            company.inn = "456"  # type: ignore[misc]

    def test_all_fields_optional(self):
        # все поля Optional — модель должна создаваться без аргументов
        company = CompanyData()
        assert company.inn is None
        assert company.name is None

    def test_source_field(self):
        company = CompanyData(source="dadata")
        assert company.source == "dadata"
