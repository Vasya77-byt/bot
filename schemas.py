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
    reliability_rating: Optional[str] = None       # Высокая / Средняя / Низкая
    reliability_obligations: Optional[str] = None  # Риски неисполнения обязательств
    reliability_shell: Optional[str] = None        # Признаки однодневки
    reliability_tax: Optional[str] = None          # Налоговые риски
    reliability_financial: Optional[str] = None    # Финансовое положение
    source: Optional[str] = None

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

