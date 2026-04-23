"""Модуль проверки безопасности компании.

Источники:
1. ФССП — исполнительные производства (долги)
2. ЗаЧестныйБизнес — комплексная проверка контрагента (TODO)
3. Контур.Фокус — проверка по стоп-листам (TODO)
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

import requests

logger = logging.getLogger("financial-architect")


@dataclass
class SecurityResult:
    """Результат проверки безопасности."""
    # ФССП
    has_enforcement: bool = False
    enforcement_count: int = 0
    enforcement_total_sum: float = 0.0
    enforcement_details: List[str] = field(default_factory=list)

    # ЗаЧестныйБизнес (TODO)
    zchb_risk_level: Optional[str] = None
    zchb_details: Optional[str] = None

    # Контур.Фокус (TODO)
    focus_risk_level: Optional[str] = None
    focus_details: Optional[str] = None

    # Общий статус
    risk_level: str = "low"  # low, medium, high, critical

    def calculate_risk(self) -> None:
        """Рассчитать уровень риска."""
        if self.enforcement_count > 20 or self.enforcement_total_sum > 50_000_000:
            self.risk_level = "critical"
        elif self.enforcement_count > 10 or self.enforcement_total_sum > 10_000_000:
            self.risk_level = "high"
        elif self.enforcement_count > 3 or self.enforcement_total_sum > 1_000_000:
            self.risk_level = "medium"
        else:
            self.risk_level = "low"


class FsspChecker:
    """Проверка исполнительных производств через ФССП API."""

    BASE_URL = "https://api-ip.fssp.gov.ru/api/v1.0"

    def __init__(self) -> None:
        self.token = os.getenv("FSSP_API_KEY", "")
        self.timeout = float(os.getenv("FSSP_TIMEOUT", "15"))

    async def check(self, inn: str, name: Optional[str] = None, region: Optional[str] = None) -> dict:
        """Проверить наличие исполнительных производств."""
        result = {
            "has_enforcement": False,
            "count": 0,
            "total_sum": 0.0,
            "details": [],
        }

        if not self.token:
            logger.warning("FSSP_API_KEY not set, skipping FSSP check")
            return result

        if not name:
            logger.info("FSSP: company name required for search, skipping")
            return result

        # Шаг 1: запрос на поиск
        task_id = await self._start_search(name, region)
        if not task_id:
            return result

        # Шаг 2: ожидание и получение результатов
        await asyncio.sleep(2)  # ФССП требует задержку
        results = await self._get_results(task_id)
        if not results:
            return result

        # Парсинг результатов
        for item in results:
            subject = item.get("name", "")
            exe_production = item.get("exe_production", "")
            details_text = item.get("details", "")
            subject_text = f"{subject}: {exe_production}" if exe_production else subject

            # Извлекаем сумму долга
            debt_sum = 0.0
            if isinstance(item.get("ip_end"), dict):
                debt_sum = float(item["ip_end"].get("debt_remainder", 0) or 0)
            elif "сумма" in str(details_text).lower():
                try:
                    match = re.search(r"(\d[\d\s]*[\.,]\d{2})", str(details_text))
                    if match:
                        debt_sum = float(match.group(1).replace(" ", "").replace(",", "."))
                except Exception:
                    pass

            result["total_sum"] += debt_sum
            result["count"] += 1
            result["details"].append(subject_text[:100])

        result["has_enforcement"] = result["count"] > 0
        return result

    async def _start_search(self, name: str, region: Optional[str]) -> Optional[str]:
        """Запустить поиск в ФССП."""
        def _call() -> Optional[str]:
            try:
                params = {
                    "token": self.token,
                    "region": region or "",
                    "name": name,
                }
                resp = requests.get(
                    f"{self.BASE_URL}/search/legal",
                    params=params,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        return data.get("response", {}).get("task")
                else:
                    logger.warning("FSSP search returned status %s", resp.status_code)
            except Exception as exc:
                logger.warning("FSSP search failed: %s", exc)
            return None

        return await asyncio.to_thread(_call)

    async def _get_results(self, task_id: str) -> Optional[list]:
        """Получить результаты поиска ФССП."""
        def _call() -> Optional[list]:
            for attempt in range(3):
                try:
                    params = {"token": self.token, "task": task_id}
                    resp = requests.get(
                        f"{self.BASE_URL}/result",
                        params=params,
                        timeout=self.timeout,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        status = data.get("status")
                        if status == "success":
                            response = data.get("response", {})
                            result_list = response.get("result", [])
                            if result_list and isinstance(result_list, list):
                                all_results = []
                                for group in result_list:
                                    if isinstance(group, dict) and group.get("result"):
                                        all_results.extend(group["result"])
                                return all_results
                            return []
                        elif status == "wait":
                            import time
                            time.sleep(3)
                            continue
                except Exception as exc:
                    logger.warning("FSSP result fetch failed: %s", exc)
            return None

        return await asyncio.to_thread(_call)


class SecurityService:
    """Единый сервис проверки безопасности."""

    def __init__(self) -> None:
        self.fssp = FsspChecker()

    async def check(
        self,
        inn: str,
        name: Optional[str] = None,
        okved: Optional[str] = None,
        region: Optional[str] = None,
    ) -> SecurityResult:
        """Полная проверка безопасности компании."""
        result = SecurityResult()

        # ФССП
        try:
            fssp_result = await self.fssp.check(inn, name, region)
            if isinstance(fssp_result, dict):
                result.has_enforcement = fssp_result.get("has_enforcement", False)
                result.enforcement_count = fssp_result.get("count", 0)
                result.enforcement_total_sum = fssp_result.get("total_sum", 0.0)
                result.enforcement_details = fssp_result.get("details", [])
        except Exception as exc:
            logger.warning("FSSP check error: %s", exc)

        # TODO: ЗаЧестныйБизнес
        # TODO: Контур.Фокус

        result.calculate_risk()
        return result
