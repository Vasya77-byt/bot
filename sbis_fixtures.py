"""Тестовые данные для SBIS-клиента.

Вынесено из sbis_mock.py, чтобы production-клиент SbisClient мог
импортировать mock_company без подтягивания FastAPI/uvicorn (которые
нужны только для запуска отдельного мок-сервера).
"""
from typing import Optional

from schemas import CompanyData


def mock_company(inn: Optional[str]) -> CompanyData:
    return CompanyData(
        inn=inn or "0000000000",
        name="ООО «Мокап»",
        ogrn="0000000000000",
        region="Москва",
        reg_date="2019-01-01",
        age_years=5,
        okved_main="62.01 Разработка ПО",
        employees_count=25,
        revenue_last_year=120_000_000,
        profit_last_year=18_000_000,
        licenses=["нет лицензий"],
    )
