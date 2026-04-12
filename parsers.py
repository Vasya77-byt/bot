import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from schemas import CompanyData, empty_company

INN_PATTERN = re.compile(r"\b(\d{10}|\d{12})\b")


@dataclass
class ParseResult:
    raw_text: str
    inn: Optional[str]
    mode: Optional[str]
    is_request: bool
    is_proposal: bool
    company_data: Optional[CompanyData]


def parse_message(text: str) -> ParseResult:
    inn = extract_inn(text)
    mode = extract_mode(text)
    is_request = bool(re.search(r"дай\s+заявку\s+\d{10}", text, re.IGNORECASE))
    is_proposal = bool(re.search(r"дай\s+предложение\s+\d{10}", text, re.IGNORECASE))

    company_data = parse_company_json(text)
    if company_data is None and inn:
        company_data = None  # will be fetched

    return ParseResult(
        raw_text=text,
        inn=inn,
        mode=mode,
        is_request=is_request,
        is_proposal=is_proposal,
        company_data=company_data,
    )


def extract_inn(text: str) -> Optional[str]:
    match = INN_PATTERN.search(text)
    return match.group(1) if match else None


def extract_mode(text: str) -> Optional[str]:
    match = re.search(r"mode\s*=\s*(\w+)", text, re.IGNORECASE)
    return match.group(1) if match else None


def parse_company_json(text: str) -> Optional[CompanyData]:
    try:
        blob = json.loads(text)
        if isinstance(blob, dict):
            return to_company(blob)
    except Exception:
        pass
    return None


def to_company(data: Dict[str, Any]) -> CompanyData:
    return CompanyData(
        inn=data.get("inn"),
        name=data.get("name"),
        ogrn=data.get("ogrn"),
        region=data.get("region"),
        reg_date=data.get("reg_date"),
        age_years=data.get("age_years"),
        okved_main=data.get("okved_main"),
        employees_count=data.get("employees_count"),
        revenue_last_year=data.get("revenue_last_year"),
        profit_last_year=data.get("profit_last_year"),
        licenses=data.get("licenses"),
    ) if data else empty_company()

