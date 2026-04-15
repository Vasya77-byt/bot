"""Модуль проверки безопасности компании.

Источники:
1. Росфинмониторинг — список террористов/экстремистов
2. ФССП — исполнительные производства (долги)
3. ЦБ РФ — проверка статуса кредитных организаций
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("financial-architect")


@dataclass
class SecurityResult:
    """Результат проверки безопасности."""
    # Росфинмониторинг
    in_terrorist_list: bool = False
    terrorist_details: Optional[str] = None

    # ФССП
    has_enforcement: bool = False
    enforcement_count: int = 0
    enforcement_total_sum: float = 0.0
    enforcement_details: List[str] = field(default_factory=list)

    # ЦБ РФ (для банков)
    is_bank: bool = False
    bank_license_active: bool = True
    bank_details: Optional[str] = None

    # Общий статус
    risk_level: str = "low"  # low, medium, high, critical

    def calculate_risk(self) -> None:
        """Рассчитать уровень риска."""
        if self.in_terrorist_list:
            self.risk_level = "critical"
        elif not self.bank_license_active and self.is_bank:
            self.risk_level = "critical"
        elif self.enforcement_count > 10 or self.enforcement_total_sum > 10_000_000:
            self.risk_level = "high"
        elif self.enforcement_count > 3 or self.enforcement_total_sum > 1_000_000:
            self.risk_level = "medium"
        else:
            self.risk_level = "low"


class RosfinmonitoringChecker:
    """Проверка по списку Росфинмониторинга (террористы/экстремисты)."""

    SEARCH_URL = "https://www.fedsfm.ru/documents/terr-list"

    async def check(self, inn: str, name: Optional[str] = None) -> dict:
        """Проверить ИНН/название в списке Росфинмониторинга."""
        result = {"in_list": False, "details": None}

        def _call() -> Optional[Dict[str, Any]]:
            try:
                # Используем API fedsfm.ru для поиска
                resp = requests.get(
                    "https://www.fedsfm.ru/documents/terr-list",
                    timeout=15,
                )
                if resp.status_code == 200:
                    text = resp.text.lower()
                    # Проверяем наличие ИНН в списке
                    if inn and inn in text:
                        return {"found": True, "by": "inn"}
                    # Проверяем по названию
                    if name and name.lower() in text:
                        return {"found": True, "by": "name"}
                    return {"found": False}
            except Exception as exc:
                logger.warning("Rosfinmonitoring check failed: %s", exc)
            return None

        raw = await asyncio.to_thread(_call)
        if raw and raw.get("found"):
            result["in_list"] = True
            result["details"] = f"Найден в списке Росфинмониторинга (по {raw.get('by', '?')})"
            logger.warning("INN %s found in Rosfinmonitoring list!", inn)
        else:
            logger.info("INN %s not found in Rosfinmonitoring list", inn)

        return result


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
                    import re
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
                                # Собираем все записи
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


class CbrChecker:
    """Проверка по данным ЦБ РФ — статус кредитных организаций."""

    # ЦБ РФ API для списка кредитных организаций
    LICENSED_URL = "https://www.cbr.ru/banking_sector/credit/QuantityOfCreditInstitutions/"

    async def check(self, inn: str, okved: Optional[str] = None) -> dict:
        """Проверить статус в ЦБ РФ (для банков/финорганизаций)."""
        result = {
            "is_bank": False,
            "license_active": True,
            "details": None,
        }

        # Проверяем только если ОКВЭД связан с финансами (64.xx)
        is_financial = False
        if okved and okved.startswith("64"):
            is_financial = True
        # Или если в ИНН есть признаки банка (необязательно)

        if not is_financial:
            return result

        result["is_bank"] = True

        def _call() -> Optional[Dict[str, Any]]:
            try:
                # Проверяем через API ЦБ РФ
                resp = requests.get(
                    f"https://www.cbr.ru/api/dc/search?query={inn}&type=credit",
                    timeout=15,
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    return resp.json()
            except Exception as exc:
                logger.warning("CBR check failed: %s", exc)
            return None

        raw = await asyncio.to_thread(_call)
        if raw:
            items = raw if isinstance(raw, list) else raw.get("items", raw.get("result", []))
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        status = item.get("status") or item.get("licensestatus") or ""
                        if "отозвана" in str(status).lower() or "ликвидир" in str(status).lower():
                            result["license_active"] = False
                            result["details"] = f"Лицензия: {status}"
                            break

        return result


class SecurityService:
    """Единый сервис проверки безопасности."""

    def __init__(self) -> None:
        self.rosfin = RosfinmonitoringChecker()
        self.fssp = FsspChecker()
        self.cbr = CbrChecker()

    async def check(
        self,
        inn: str,
        name: Optional[str] = None,
        okved: Optional[str] = None,
        region: Optional[str] = None,
    ) -> SecurityResult:
        """Полная проверка безопасности компании."""
        result = SecurityResult()

        # Запускаем все проверки параллельно
        rosfin_task = self.rosfin.check(inn, name)
        fssp_task = self.fssp.check(inn, name, region)
        cbr_task = self.cbr.check(inn, okved)

        rosfin_result, fssp_result, cbr_result = await asyncio.gather(
            rosfin_task, fssp_task, cbr_task,
            return_exceptions=True,
        )

        # Росфинмониторинг
        if isinstance(rosfin_result, dict):
            result.in_terrorist_list = rosfin_result.get("in_list", False)
            result.terrorist_details = rosfin_result.get("details")
        elif isinstance(rosfin_result, Exception):
            logger.warning("Rosfinmonitoring check error: %s", rosfin_result)

        # ФССП
        if isinstance(fssp_result, dict):
            result.has_enforcement = fssp_result.get("has_enforcement", False)
            result.enforcement_count = fssp_result.get("count", 0)
            result.enforcement_total_sum = fssp_result.get("total_sum", 0.0)
            result.enforcement_details = fssp_result.get("details", [])
        elif isinstance(fssp_result, Exception):
            logger.warning("FSSP check error: %s", fssp_result)

        # ЦБ РФ
        if isinstance(cbr_result, dict):
            result.is_bank = cbr_result.get("is_bank", False)
            result.bank_license_active = cbr_result.get("license_active", True)
            result.bank_details = cbr_result.get("details")
        elif isinstance(cbr_result, Exception):
            logger.warning("CBR check error: %s", cbr_result)

        result.calculate_risk()
        return result


def render_security_report(result: SecurityResult, company_name: Optional[str] = None) -> str:
    """Форматирование отчёта безопасности для Telegram."""
    risk_emoji = {
        "low": "🟢",
        "medium": "🟡",
        "high": "🟠",
        "critical": "🔴",
    }
    risk_label = {
        "low": "Низкий",
        "medium": "Средний",
        "high": "Высокий",
        "critical": "Критический",
    }

    lines = []
    header = f"🔒 Проверка безопасности"
    if company_name:
        header += f": {company_name}"
    lines.append(header)
    lines.append("")

    # Уровень риска
    emoji = risk_emoji.get(result.risk_level, "⚪")
    label = risk_label.get(result.risk_level, "Неизвестен")
    lines.append(f"{emoji} Уровень риска: {label}")
    lines.append("")

    # Росфинмониторинг
    lines.append("1️⃣ Росфинмониторинг (террор/экстремизм):")
    if result.in_terrorist_list:
        lines.append(f"   🔴 ВНИМАНИЕ: {result.terrorist_details}")
    else:
        lines.append("   ✅ Не найден в списках")

    # ФССП
    lines.append("")
    lines.append("2️⃣ ФССП (исполнительные производства):")
    if result.has_enforcement:
        lines.append(f"   ⚠️ Найдено производств: {result.enforcement_count}")
        if result.enforcement_total_sum > 0:
            lines.append(f"   💰 Общая сумма: {_fmt_money(result.enforcement_total_sum)}")
        for detail in result.enforcement_details[:5]:
            lines.append(f"   • {detail}")
        if len(result.enforcement_details) > 5:
            lines.append(f"   ... и ещё {len(result.enforcement_details) - 5}")
    else:
        lines.append("   ✅ Исполнительных производств не найдено")

    # ЦБ РФ
    if result.is_bank:
        lines.append("")
        lines.append("3️⃣ ЦБ РФ (лицензия кредитной организации):")
        if result.bank_license_active:
            lines.append("   ✅ Лицензия действует")
        else:
            lines.append(f"   🔴 {result.bank_details or 'Лицензия отозвана/ликвидирована'}")

    return "\n".join(lines)


def _fmt_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} млрд ₽"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽"
    if value >= 1_000:
        return f"{value / 1_000:.0f} тыс ₽"
    return f"{value:.0f} ₽"
