"""Тесты renewal_scheduler — фоновое автопродление подписок."""
import asyncio
from typing import Any

import pytest

import renewal_scheduler
from renewal_scheduler import _renew_once, run_renewal_loop
from user_store import UserProfile


class FakeSubscription:
    """Подменяет SubscriptionService."""

    def __init__(self, candidates=None, renew_results=None):
        self._candidates = candidates or []
        # renew_results: list of (ok, msg) — возвращается по очереди
        self._renew_results = list(renew_results or [])
        self.expiring_soon_calls: list[int] = []
        self.try_renew_calls: list[UserProfile] = []

    def expiring_soon(self, days: int = 1):
        self.expiring_soon_calls.append(days)
        return list(self._candidates)

    async def try_renew(self, profile: UserProfile):
        self.try_renew_calls.append(profile)
        if not self._renew_results:
            raise AssertionError("FakeSubscription: no renew result configured")
        return self._renew_results.pop(0)


def _profile(uid: int = 1, tariff: str = "pro") -> UserProfile:
    return UserProfile(user_id=uid, tariff=tariff)


class TestRenewOnce:
    @pytest.mark.asyncio
    async def test_no_candidates_no_renew_calls(self):
        sub = FakeSubscription(candidates=[])
        notifications: list[tuple[int, str]] = []

        async def notify(uid, msg):
            notifications.append((uid, msg))

        await _renew_once(sub, notify)
        assert sub.try_renew_calls == []
        assert notifications == []

    @pytest.mark.asyncio
    async def test_renewed_triggers_success_notification(self):
        sub = FakeSubscription(
            candidates=[_profile(42)],
            renew_results=[(True, "renewed")],
        )
        notifications = []

        async def notify(uid, msg):
            notifications.append((uid, msg))

        await _renew_once(sub, notify)
        assert len(notifications) == 1
        uid, msg = notifications[0]
        assert uid == 42
        assert "продлена" in msg
        assert "PRO" in msg  # tariff.upper()

    @pytest.mark.asyncio
    async def test_pending_does_not_notify(self):
        sub = FakeSubscription(
            candidates=[_profile(42)],
            renew_results=[(True, "pending")],
        )
        notifications = []

        async def notify(uid, msg):
            notifications.append((uid, msg))

        await _renew_once(sub, notify)
        assert notifications == []

    @pytest.mark.asyncio
    async def test_failure_triggers_warning_notification(self):
        sub = FakeSubscription(
            candidates=[_profile(7)],
            renew_results=[(False, "card declined")],
        )
        notifications = []

        async def notify(uid, msg):
            notifications.append((uid, msg))

        await _renew_once(sub, notify)
        assert len(notifications) == 1
        uid, msg = notifications[0]
        assert uid == 7
        assert "Не удалось" in msg

    @pytest.mark.asyncio
    async def test_no_notify_callback_does_not_crash(self):
        sub = FakeSubscription(
            candidates=[_profile(1)],
            renew_results=[(True, "renewed")],
        )
        # notify=None — функция должна корректно отработать
        await _renew_once(sub, None)
        assert sub.try_renew_calls

    @pytest.mark.asyncio
    async def test_notify_exception_does_not_skip_remaining(self):
        sub = FakeSubscription(
            candidates=[_profile(1), _profile(2), _profile(3)],
            renew_results=[(True, "renewed"), (True, "renewed"), (True, "renewed")],
        )

        async def flaky_notify(uid, msg):
            if uid == 1:
                raise RuntimeError("telegram down")

        await _renew_once(sub, flaky_notify)
        # Все три кандидата должны быть обработаны, даже если notify упал на первом
        assert len(sub.try_renew_calls) == 3

    @pytest.mark.asyncio
    async def test_mixed_results_processed_independently(self):
        sub = FakeSubscription(
            candidates=[_profile(1), _profile(2), _profile(3)],
            renew_results=[
                (True, "renewed"),
                (False, "declined"),
                (True, "pending"),
            ],
        )
        notifications = []

        async def notify(uid, msg):
            notifications.append((uid, msg))

        await _renew_once(sub, notify)
        # 1 — renewed (notify), 2 — declined (notify), 3 — pending (no notify)
        assert len(notifications) == 2
        assert notifications[0][0] == 1
        assert "продлена" in notifications[0][1]
        assert notifications[1][0] == 2
        assert "Не удалось" in notifications[1][1]

    @pytest.mark.asyncio
    async def test_passes_lead_days_one_to_expiring_soon(self):
        sub = FakeSubscription(candidates=[])
        await _renew_once(sub, None)
        assert sub.expiring_soon_calls == [1]


def _patch_loop_sleep_with_limit(monkeypatch, max_iterations: int = 3):
    """Заменяет renewal_scheduler.asyncio.sleep на счётчик: после N
    итераций бросает CancelledError, чтобы цикл вышел. Работает только
    внутри renewal_scheduler — глобальный asyncio не трогается."""
    counter = {"n": 0}

    async def limited_sleep(seconds):
        counter["n"] += 1
        if counter["n"] >= max_iterations:
            raise asyncio.CancelledError()
        # Yield control, чтобы планировщик смог выполнить фоновые задачи
        await asyncio.sleep(0)

    class FakeAsyncioModule:
        sleep = staticmethod(limited_sleep)

    monkeypatch.setattr(renewal_scheduler, "asyncio", FakeAsyncioModule)
    return counter


class TestRunRenewalLoop:
    @pytest.mark.asyncio
    async def test_loop_runs_iterations_until_cancelled(self, monkeypatch):
        _patch_loop_sleep_with_limit(monkeypatch, max_iterations=3)

        sub = FakeSubscription(candidates=[])
        with pytest.raises(asyncio.CancelledError):
            await run_renewal_loop(sub, notify=None, interval=0)

        # _renew_once вызывался на каждой итерации до отмены
        assert len(sub.expiring_soon_calls) == 3

    @pytest.mark.asyncio
    async def test_exception_in_iteration_does_not_kill_loop(self, monkeypatch):
        _patch_loop_sleep_with_limit(monkeypatch, max_iterations=4)

        call_log: list[str] = []

        class BoomingSubscription:
            def expiring_soon(self, days=1):
                call_log.append("call")
                if len(call_log) == 1:
                    raise RuntimeError("DB down")
                return []  # последующие ОК

            async def try_renew(self, profile):
                raise AssertionError("not reached")

        sub = BoomingSubscription()
        with pytest.raises(asyncio.CancelledError):
            await run_renewal_loop(sub, notify=None, interval=0)

        # Цикл выжил после первой ошибки и продолжил вызывать expiring_soon
        assert len(call_log) >= 2
