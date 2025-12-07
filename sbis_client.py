import asyncio
import os
from typing import Any, Dict, Iterable, Optional

from schemas import CompanyData
from sbis_mock import mock_company


class SbisClient:
    def __init__(self) -> None:
        self.login = os.getenv("SBIS_LOGIN")
        self.password = os.getenv("SBIS_PASSWORD")
        self.api_key = os.getenv("SBIS_API_KEY")
        self.client_id = os.getenv("SBIS_CLIENT_ID")

    async def fetch_company_data(self, inn: str) -> Optional[CompanyData]:
        if os.getenv("SBIS_MOCK", "").lower() in {"1", "true", "yes"}:
            return mock_company(inn)

        if not all([self.login, self.password, self.api_key, self.client_id]):
            return None

        payload = {
            "login": self.login,
            "password": self.password,
            "API_KEY": self.api_key,
            "client_id": self.client_id,
            "inn": inn,
        }

        def _call() -> Optional[Dict[str, Any]]:
            try:
                import requests
            except ImportError:
                return None
            try:
                resp = requests.post(
                    "https://online.sbis.ru/service/?class=crm_ClaimsService&method=get_organization_info",
                    json=payload,
                    timeout=10,
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
            except Exception:
                return None

        raw = await asyncio.to_thread(_call)
        if not raw:
            return None

        return self._normalize(raw, inn)

    def _normalize(self, data: Dict[str, Any], inn: str) -> CompanyData:
        # Expected mapping should be adjusted to real SBIS response.
        def pick(keys: Iterable[str]) -> Optional[Any]:
            for key in keys:
                if key in data and data[key]:
                    return data[key]
            return None

        return CompanyData(
            inn=inn,
            name=pick(["name", "Name"]),
            ogrn=pick(["ogrn", "OGRN"]),
            region=pick(["region", "Region"]),
            reg_date=pick(["reg_date", "RegDate"]),
            age_years=pick(["age_years"]),
            okved_main=pick(["okved_main", "Okved"]),
            employees_count=pick(["employees_count", "Staff"]),
            revenue_last_year=pick(["revenue_last_year", "Revenue"]),
            profit_last_year=pick(["profit_last_year", "Profit"]),
            licenses=pick(["licenses", "Licenses"]),
        )

