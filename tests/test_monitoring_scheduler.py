"""Тесты monitoring_scheduler — фоновое перечитывание подписок."""
import asyncio
from typing import Optional

import pytest

import monitoring_scheduler
from monitoring_scheduler import _check_once, run_monitoring_loop
from monitoring_store import MonitoringStore
from schemas import CompanyData
from security_check import SecurityResult


class FakeCompanyService:
    def __init__(self, results: dict[str, Optional[CompanyData]] = None):
        self.results = results or {}
        self.exceptions: dict[str, Exception] = {}
        self.calls: list[str] = []

    async def fetch(self, inn: str) -> Optional[CompanyData]:
        self.calls.append(inn)
        if inn in self.exceptions:
            raise self.exceptions[inn]
        return self.results.get(inn)


class FakeSecurityService:
    def __init__(self, results: dict[str, SecurityResult] = None):
        self.results = results or {}
        self.exceptions: dict[str, Exception] = {}
        self.calls: list[dict] = []

    async def check(self, **kw) -> SecurityResult:
        self.calls.append(kw)
        inn = kw.get("inn")
        if inn in self.exceptions:
            raise self.exceptions[inn]
        return self.results.get(inn, SecurityResult(risk_level="low"))


@pytest.fixture
def store(tmp_path):
    return MonitoringStore(filepath=str(tmp_path / "m.json"))


class TestCheckOnce:
    @pytest.mark.asyncio
    async def test_no_subscriptions_no_calls(self, store):
        company = FakeCompanyService()
        security = FakeSecurityService()
        notifications: list[tuple[int, str]] = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _check_once(store, company, security, notify)
        assert company.calls == []
        assert notifications == []

    @pytest.mark.asyncio
    async def test_first_check_no_notification(self, store):
        # Первый прогон при пустом snapshot не считается изменением
        store.add(1, "123", "ООО А", snapshot={})
        company = FakeCompanyService(
            {"123": CompanyData(inn="123", name="ООО А", status="Действующая")}
        )
        security = FakeSecurityService()
        notifications = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _check_once(store, company, security, notify)
        assert notifications == []
        # Snapshot обновлён
        sub = store.get(1, "123")
        assert sub.snapshot.get("status") == "Действующая"

    @pytest.mark.asyncio
    async def test_change_triggers_notification(self, store):
        # Сценарий: подписка → 1-я проверка (без diff) → данные меняются →
        # 2-я проверка → notify
        store.add(1, "123", "ООО А")  # пустой snapshot
        company = FakeCompanyService(
            {"123": CompanyData(inn="123", name="ООО А", status="Действующая")}
        )
        security = FakeSecurityService()

        # Первый прогон: первичный замер, уведомления нет
        await _check_once(store, company, security, None)

        # Имитируем изменение в источнике
        company.results["123"] = CompanyData(
            inn="123", name="ООО А", status="Ликвидируется",
        )
        notifications = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _check_once(store, company, security, notify)
        assert len(notifications) == 1
        uid, text = notifications[0]
        assert uid == 1
        assert "Ликвидируется" in text
        assert "Действующая" in text
        assert "ООО А" in text

    @pytest.mark.asyncio
    async def test_fetch_error_skips_subscription(self, store):
        store.add(1, "123", "ООО А")
        company = FakeCompanyService()
        company.exceptions["123"] = RuntimeError("DaData down")
        security = FakeSecurityService()

        # Не должно бросить, просто пропустит подписку
        await _check_once(store, company, security, None)
        # Следующая итерация ожидает security_service не вызывался
        assert security.calls == []

    @pytest.mark.asyncio
    async def test_security_error_continues_with_company_only(self, store):
        store.add(1, "123", "ООО А")
        company = FakeCompanyService(
            {"123": CompanyData(inn="123", name="ООО А", status="Действующая")}
        )
        security = FakeSecurityService()
        security.exceptions["123"] = RuntimeError("FSSP down")

        # Первая проверка — snapshot обновляется по company, без security
        await _check_once(store, company, security, None)
        sub = store.get(1, "123")
        assert sub.snapshot["status"] == "Действующая"
        # FSSP-поля остались None
        assert sub.snapshot.get("fssp_count") is None

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_crash(self, store):
        # Первый замер для базы
        store.add(1, "123", "ООО А")
        company = FakeCompanyService(
            {"123": CompanyData(inn="123", name="ООО А", status="Действующая")}
        )
        await _check_once(store, company, FakeSecurityService(), None)

        # Вторая итерация — есть изменение, но notify падает
        company.results["123"] = CompanyData(inn="123", name="ООО А",
                                              status="Ликвидируется")

        async def angry_notify(uid, text):
            raise RuntimeError("telegram is down")

        # Не должно бросить
        await _check_once(store, company, FakeSecurityService(), angry_notify)
        # Snapshot всё равно обновлён, чтобы не дублировать на следующий раз
        assert store.get(1, "123").snapshot["status"] == "Ликвидируется"

    @pytest.mark.asyncio
    async def test_each_subscription_independent(self, store):
        store.add(1, "111", "A")
        store.add(2, "222", "B")
        company = FakeCompanyService()
        company.exceptions["111"] = RuntimeError("nope")
        company.results["222"] = CompanyData(inn="222", name="B", status="Действующая")

        await _check_once(store, company, FakeSecurityService(), None)
        # Первая упала — её snapshot пуст
        assert store.get(1, "111").snapshot == {}
        # Вторая — обновилась
        assert store.get(2, "222").snapshot.get("status") == "Действующая"

    @pytest.mark.asyncio
    async def test_no_notify_callback_does_not_crash(self, store):
        store.add(1, "123", "A")
        company = FakeCompanyService(
            {"123": CompanyData(inn="123", name="A", status="Действующая")}
        )
        # Дважды: первый — пустой snapshot, второй — с diff
        await _check_once(store, company, FakeSecurityService(), None)
        company.results["123"] = CompanyData(
            inn="123", name="A", status="Ликвидируется",
        )
        # notify=None — не должно упасть
        await _check_once(store, company, FakeSecurityService(), None)


def _patch_loop_sleep_with_limit(monkeypatch, max_iterations: int = 3):
    """Подменяет monitoring_scheduler.asyncio на фейк со счётчиком."""
    counter = {"n": 0}

    async def limited_sleep(seconds):
        counter["n"] += 1
        if counter["n"] >= max_iterations:
            raise asyncio.CancelledError()
        await asyncio.sleep(0)

    class FakeAsyncioModule:
        sleep = staticmethod(limited_sleep)

    monkeypatch.setattr(monitoring_scheduler, "asyncio", FakeAsyncioModule)
    return counter


class TestRunMonitoringLoop:
    @pytest.mark.asyncio
    async def test_loop_runs_check_once_per_iteration(self, monkeypatch, store):
        _patch_loop_sleep_with_limit(monkeypatch, max_iterations=3)
        company = FakeCompanyService()
        security = FakeSecurityService()

        with pytest.raises(asyncio.CancelledError):
            await run_monitoring_loop(store, company, security, notify=None,
                                      interval=0)

    @pytest.mark.asyncio
    async def test_exception_in_iteration_does_not_kill_loop(
        self, monkeypatch, store
    ):
        _patch_loop_sleep_with_limit(monkeypatch, max_iterations=4)

        class BadStore:
            def iter_all(self):
                raise RuntimeError("DB down")

        with pytest.raises(asyncio.CancelledError):
            await run_monitoring_loop(
                BadStore(), FakeCompanyService(), FakeSecurityService(),
                notify=None, interval=0,
            )
