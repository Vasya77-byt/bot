from typing import List, Optional

from pydantic import BaseModel


class Contract(BaseModel):
    """Контракт / договор с контрагентом."""
    contract_id: Optional[str] = None
    inn: Optional[str] = None
    counterparty: Optional[str] = None
    subject: Optional[str] = None
    total_amount: Optional[float] = None
    paid_amount: Optional[float] = None
    remaining_amount: Optional[float] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = None  # active / completed / cancelled
    year: Optional[int] = None
    notes: Optional[str] = None


class Debt(BaseModel):
    """Задолженность (дебиторская или кредиторская)."""
    debt_id: Optional[str] = None
    inn: Optional[str] = None
    counterparty: Optional[str] = None
    direction: Optional[str] = None  # receivable (нам должны) / payable (мы должны)
    amount: Optional[float] = None
    due_date: Optional[str] = None
    origin_year: Optional[int] = None
    description: Optional[str] = None
    status: Optional[str] = None  # outstanding / settled / overdue


class CompanyData(BaseModel):
    inn: Optional[str] = None
    name: Optional[str] = None
    ogrn: Optional[str] = None
    region: Optional[str] = None
    reg_date: Optional[str] = None
    age_years: Optional[int] = None
    okved_main: Optional[str] = None
    employees_count: Optional[int] = None
    revenue_last_year: Optional[float] = None
    profit_last_year: Optional[float] = None
    licenses: Optional[List[str]] = None

    class Config:
        frozen = True


class CarryoverSummary(BaseModel):
    """Сводка переносимых данных при переходе на новый учётный год."""
    year_from: int
    year_to: int
    active_contracts: List[Contract] = []
    outstanding_debts: List[Debt] = []
    total_receivables: float = 0.0
    total_payables: float = 0.0
    notes: Optional[str] = None


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

