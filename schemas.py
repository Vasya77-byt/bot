from typing import List, Optional

from pydantic import BaseModel


class CompanyData(BaseModel):
    inn: Optional[str] = None
    name: Optional[str] = None
    ogrn: Optional[str] = None
    region: Optional[str] = None
    address: Optional[str] = None
    reg_date: Optional[str] = None
    age_years: Optional[int] = None
    okved_main: Optional[str] = None
    okved_name: Optional[str] = None
    employees_count: Optional[int] = None
    revenue_last_year: Optional[float] = None
    profit_last_year: Optional[float] = None
    licenses: Optional[List[str]] = None
    director: Optional[str] = None
    status: Optional[str] = None
    kpp: Optional[str] = None
    capital: Optional[float] = None
    source: Optional[str] = None  # откуда данные: dadata, fns, sbis, mock
    # Данные Rusprofile
    courts_plaintiff: Optional[int] = None   # арбитраж: истец
    courts_defendant: Optional[int] = None   # арбитраж: ответчик
    courts_total: Optional[int] = None       # арбитраж: всего дел
    gov_contracts_count: Optional[int] = None    # госконтракты: количество
    gov_contracts_amount: Optional[float] = None # госконтракты: сумма
    founders: Optional[List[str]] = None     # учредители

    model_config = {"frozen": True}


def empty_company(inn: Optional[str] = None) -> CompanyData:
    return CompanyData(
        inn=inn,
        name="не указано",
        ogrn="не указано",
        region="не указано",
        reg_date="не указано",
        age_years=None,
        okved_main="не указано",
        employees_count=None,
        revenue_last_year=None,
        profit_last_year=None,
        licenses=None,
    )

