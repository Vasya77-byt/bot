import asyncio
import os
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from cache import FileTTLCache
from schemas import CompanyData
from sbis_mock import mock_company


class SbisClient:
    def __init__(self) -> None:
        self.login = os.getenv("SBIS_LOGIN")
        self.password = os.getenv("SBIS_PASSWORD")
        self.api_key = os.getenv("SBIS_API_KEY")
        self.client_id = os.getenv("SBIS_CLIENT_ID")
        self.base_url = os.getenv(
            "SBIS_BASE_URL",
            "https://online.sbis.ru/service/?class=crm_ClaimsService&method=get_organization_info",
        )
        self.timeout = float(os.getenv("SBIS_TIMEOUT", "10"))
        self.retries = int(os.getenv("SBIS_RETRIES", "2"))
        self.retry_delay = float(os.getenv("SBIS_RETRY_DELAY", "1.0"))
        self.cache_ttl = float(os.getenv("SBIS_CACHE_TTL", "300"))
        self._cache: Dict[str, Tuple[float, CompanyData]] = {}
        self._file_cache = FileTTLCache("sbis_cache", ttl=self.cache_ttl)

    async def fetch_company_data(self, inn: str) -> Optional[CompanyData]:
        if os.getenv("SBIS_MOCK", "").lower() in {"1", "true", "yes"}:
            return mock_company(inn)

        cached = self._cache_get(inn)
        if cached:
            return cached
        cached_file = self._file_cache.get(inn)
        if cached_file:
            try:
                return CompanyData.model_validate(cached_file)
            except Exception:
                pass

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
            for attempt in range(self.retries + 1):
                try:
                    resp = requests.post(
                        self.base_url,
                        json=payload,
                        timeout=self.timeout,
                    )
                    if resp.status_code == 200:
                        return resp.json()
                except Exception:
                    pass
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
            return None

        raw = await asyncio.to_thread(_call)
        if not raw:
            return None

        company = self._normalize(raw, inn)
        if company:
            self._cache_put(inn, company)
            self._file_cache.set(inn, company.model_dump())
        return company

    def _normalize(self, data: Dict[str, Any], inn: str) -> CompanyData:
        """
        Map SBIS response to CompanyData.
        Handles shapes:
        - {"result": {"Organization": {...}}}
        - {"Organization": {...}}
        - {"answer": {"data": {...}}}
        - flat dict with Name/OGRN/Region/etc.
        """

        org = self._extract_org(data)

        def pick(keys: Iterable[str]) -> Optional[Any]:
            for key in keys:
                val = org.get(key)
                if val:
                    return val
            return None

        licenses = pick(["licenses", "Licenses", "License"])
        if isinstance(licenses, str):
            licenses = [licenses]

        age = pick(["age_years", "AgeYears"])
        try:
            age = int(age) if age is not None else None
        except Exception:
            age = None

        return CompanyData(
            inn=inn or pick(["inn", "INN"]),
            name=pick(["name", "Name", "Наименование"]),
            ogrn=pick(["ogrn", "OGRN"]),
            region=pick(["region", "Region"]),
            reg_date=pick(["reg_date", "RegDate", "RegistrationDate"]),
            age_years=age,
            okved_main=pick(["okved_main", "Okved", "OKVED"]),
            employees_count=pick(["employees_count", "Staff", "Employees"]),
            revenue_last_year=pick(["revenue_last_year", "Revenue"]),
            profit_last_year=pick(["profit_last_year", "Profit"]),
            licenses=licenses if licenses else None,
        )

    @staticmethod
    def _extract_org(data: Dict[str, Any]) -> Dict[str, Any]:
        candidates = [
            data.get("result", {}).get("Organization", {}),
            data.get("result", {}),
            data.get("Organization", {}),
            data.get("answer", {}).get("data", {}),
            data,
        ]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate:
                return candidate
        return {}

    def _cache_get(self, inn: str) -> Optional[CompanyData]:
        entry = self._cache.get(inn)
        if not entry:
            return None
        ts, value = entry
        if time.time() - ts > self.cache_ttl:
            self._cache.pop(inn, None)
            return None
        return value

    def _cache_put(self, inn: str, company: CompanyData) -> None:
        self._cache[inn] = (time.time(), company)

