"""Тесты security_check: SecurityResult.calculate_risk, FsspChecker, SecurityService."""
import time
from typing import Any, Dict, Optional

import pytest
import requests

import security_check
from security_check import FsspChecker, SecurityResult, SecurityService


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None,
                 text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def disable_sleeps(monkeypatch):
    """Срезаем все задержки в тестах FSSP."""
    async def fast_async_sleep(_seconds):
        return None

    # Подменяем asyncio.sleep локально в модуле security_check
    class FakeAsyncio:
        sleep = staticmethod(fast_async_sleep)
        # to_thread оставляем настоящим — нужен для запуска _call в треде
        from asyncio import to_thread  # noqa: F401

    monkeypatch.setattr(security_check, "asyncio",
                        type("A", (), {
                            "sleep": fast_async_sleep,
                            "to_thread": __import__("asyncio").to_thread,
                            "create_task": __import__("asyncio").create_task,
                        }))
    monkeypatch.setattr(time, "sleep", lambda _s: None)


class TestSecurityResultFields:
    def test_inspections_default_zero(self):
        r = SecurityResult()
        assert r.inspections_count == 0
        assert r.inspections_violations_count == 0

    def test_inspections_fields_assignable(self):
        r = SecurityResult(
            inspections_count=5,
            inspections_violations_count=2,
        )
        assert r.inspections_count == 5
        assert r.inspections_violations_count == 2


class TestCalculateRisk:
    def test_no_enforcement_low(self):
        r = SecurityResult()
        r.calculate_risk()
        assert r.risk_level == "low"

    def test_few_records_below_thresholds_low(self):
        r = SecurityResult(enforcement_count=2, enforcement_total_sum=500_000)
        r.calculate_risk()
        assert r.risk_level == "low"

    def test_count_above_3_medium(self):
        r = SecurityResult(enforcement_count=4, enforcement_total_sum=0)
        r.calculate_risk()
        assert r.risk_level == "medium"

    def test_sum_above_1m_medium(self):
        r = SecurityResult(enforcement_count=0, enforcement_total_sum=1_500_000)
        r.calculate_risk()
        assert r.risk_level == "medium"

    def test_count_above_10_high(self):
        r = SecurityResult(enforcement_count=11, enforcement_total_sum=0)
        r.calculate_risk()
        assert r.risk_level == "high"

    def test_sum_above_10m_high(self):
        r = SecurityResult(enforcement_count=0, enforcement_total_sum=15_000_000)
        r.calculate_risk()
        assert r.risk_level == "high"

    def test_count_above_20_critical(self):
        r = SecurityResult(enforcement_count=25, enforcement_total_sum=0)
        r.calculate_risk()
        assert r.risk_level == "critical"

    def test_sum_above_50m_critical(self):
        r = SecurityResult(enforcement_count=0, enforcement_total_sum=60_000_000)
        r.calculate_risk()
        assert r.risk_level == "critical"

    def test_critical_takes_precedence_over_high(self):
        # high по count, но critical по sum → должно быть critical
        r = SecurityResult(enforcement_count=15, enforcement_total_sum=70_000_000)
        r.calculate_risk()
        assert r.risk_level == "critical"

    def test_boundary_exactly_3_count_stays_low(self):
        # Условие — > 3, не >=
        r = SecurityResult(enforcement_count=3, enforcement_total_sum=0)
        r.calculate_risk()
        assert r.risk_level == "low"

    def test_boundary_exactly_1m_sum_stays_low(self):
        r = SecurityResult(enforcement_count=0, enforcement_total_sum=1_000_000)
        r.calculate_risk()
        assert r.risk_level == "low"


class TestFsspCheckerEntryPoint:
    @pytest.mark.asyncio
    async def test_no_token_returns_empty(self, monkeypatch):
        monkeypatch.delenv("FSSP_API_KEY", raising=False)
        checker = FsspChecker()
        result = await checker.check("123", "ООО Acme", "77")
        assert result == {
            "has_enforcement": False,
            "count": 0,
            "total_sum": 0.0,
            "details": [],
        }

    @pytest.mark.asyncio
    async def test_no_company_name_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()
        result = await checker.check("123", name=None)
        assert result["count"] == 0
        assert result["has_enforcement"] is False

    @pytest.mark.asyncio
    async def test_search_returns_no_task_yields_empty(self, monkeypatch):
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()

        # search вернул не-success
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **kw: FakeResponse(200, {"status": "error"}),
        )
        result = await checker.check("123", "ООО Acme")
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_full_pipeline_with_results(self, monkeypatch):
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()

        responses_iter = iter([
            # _start_search
            FakeResponse(200, {
                "status": "success",
                "response": {"task": "task-123"},
            }),
            # _get_results
            FakeResponse(200, {
                "status": "success",
                "response": {
                    "result": [{
                        "result": [
                            {
                                "name": "ООО Acme",
                                "exe_production": "12345/24",
                                "ip_end": {"debt_remainder": 100000},
                                "details": "",
                            },
                            {
                                "name": "ООО Acme",
                                "exe_production": "67890/24",
                                "ip_end": {"debt_remainder": 50000.50},
                                "details": "",
                            },
                        ],
                    }],
                },
            }),
        ])

        def fake_get(*args, **kwargs):
            return next(responses_iter)

        monkeypatch.setattr(requests, "get", fake_get)
        result = await checker.check("123", "ООО Acme", region="77")

        assert result["has_enforcement"] is True
        assert result["count"] == 2
        assert result["total_sum"] == 150000.50
        assert len(result["details"]) == 2
        assert "12345/24" in result["details"][0]

    @pytest.mark.asyncio
    async def test_debt_extraction_from_text(self, monkeypatch):
        """Если ip_end отсутствует, сумма парсится regex из details."""
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()

        responses_iter = iter([
            FakeResponse(200, {"status": "success", "response": {"task": "t"}}),
            FakeResponse(200, {
                "status": "success",
                "response": {
                    "result": [{
                        "result": [{
                            "name": "ООО Acme",
                            "exe_production": "12345/24",
                            "details": "Сумма долга: 25 000,75",
                        }],
                    }],
                },
            }),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: next(responses_iter))

        result = await checker.check("123", "ООО Acme")
        assert result["count"] == 1
        assert result["total_sum"] == 25000.75

    @pytest.mark.asyncio
    async def test_empty_results_yields_zero(self, monkeypatch):
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()

        responses_iter = iter([
            FakeResponse(200, {"status": "success", "response": {"task": "t"}}),
            FakeResponse(200, {"status": "success", "response": {"result": []}}),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: next(responses_iter))
        result = await checker.check("123", "ООО Acme")
        assert result["count"] == 0
        assert result["has_enforcement"] is False

    @pytest.mark.asyncio
    async def test_search_http_exception_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()

        def boom(*a, **kw):
            raise ConnectionError("dns failure")

        monkeypatch.setattr(requests, "get", boom)
        result = await checker.check("123", "ООО Acme")
        assert result["count"] == 0
        assert result["has_enforcement"] is False

    @pytest.mark.asyncio
    async def test_search_non_200_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **kw: FakeResponse(500, {}, text="server error"),
        )
        result = await checker.check("123", "ООО Acme")
        assert result["count"] == 0


class TestFsspGetResultsWaitRetry:
    @pytest.mark.asyncio
    async def test_wait_status_retries_then_succeeds(self, monkeypatch):
        """status=wait → ретрай; затем success."""
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()

        responses_iter = iter([
            # _start_search
            FakeResponse(200, {"status": "success", "response": {"task": "t"}}),
            # _get_results: первая попытка — wait
            FakeResponse(200, {"status": "wait"}),
            # вторая попытка — success
            FakeResponse(200, {
                "status": "success",
                "response": {"result": [{"result": [
                    {"name": "X", "exe_production": "1/24",
                     "ip_end": {"debt_remainder": 1000}, "details": ""},
                ]}]},
            }),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: next(responses_iter))

        result = await checker.check("1", "X")
        assert result["count"] == 1
        assert result["total_sum"] == 1000

    @pytest.mark.asyncio
    async def test_wait_three_times_returns_none(self, monkeypatch):
        """Если все 3 попытки в wait — _get_results вернёт None, check
        выдаст пустой результат."""
        monkeypatch.setenv("FSSP_API_KEY", "tok")
        checker = FsspChecker()

        responses_iter = iter([
            FakeResponse(200, {"status": "success", "response": {"task": "t"}}),
            FakeResponse(200, {"status": "wait"}),
            FakeResponse(200, {"status": "wait"}),
            FakeResponse(200, {"status": "wait"}),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: next(responses_iter))

        result = await checker.check("1", "X")
        assert result["count"] == 0


class TestSecurityService:
    @pytest.mark.asyncio
    async def test_aggregates_fssp_into_result(self, monkeypatch):
        service = SecurityService()

        class FakeFssp:
            async def check(self, inn, name=None, region=None):
                return {
                    "has_enforcement": True,
                    "count": 12,
                    "total_sum": 5_000_000,
                    "details": ["item1", "item2"],
                }

        service.fssp = FakeFssp()
        result = await service.check("123", name="X")

        assert result.has_enforcement is True
        assert result.enforcement_count == 12
        assert result.enforcement_total_sum == 5_000_000
        assert result.enforcement_details == ["item1", "item2"]
        # 12 > 10 → high
        assert result.risk_level == "high"

    @pytest.mark.asyncio
    async def test_fssp_exception_yields_low_risk(self, monkeypatch):
        service = SecurityService()

        class BoomFssp:
            async def check(self, inn, name=None, region=None):
                raise RuntimeError("network")

        service.fssp = BoomFssp()
        result = await service.check("123", name="X")
        # Ошибка не должна валить весь сервис; risk_level остаётся low
        assert result.risk_level == "low"
        assert result.enforcement_count == 0

    @pytest.mark.asyncio
    async def test_fssp_returns_non_dict_handled(self, monkeypatch):
        service = SecurityService()

        class WeirdFssp:
            async def check(self, inn, name=None, region=None):
                return None  # некорректное значение, но не исключение

        service.fssp = WeirdFssp()
        result = await service.check("123", name="X")
        assert result.risk_level == "low"
        assert result.enforcement_count == 0

    @pytest.mark.asyncio
    async def test_calculate_risk_invoked(self, monkeypatch):
        service = SecurityService()

        class FakeFssp:
            async def check(self, inn, name=None, region=None):
                return {
                    "has_enforcement": True,
                    "count": 25,
                    "total_sum": 0,
                    "details": [],
                }

        service.fssp = FakeFssp()
        result = await service.check("123", name="X")
        # 25 > 20 → critical
        assert result.risk_level == "critical"
