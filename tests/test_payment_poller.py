"""Тесты payment_poller — фоновый цикл проверки pending-платежей."""
import asyncio
from typing import Optional

import pytest

import payment_poller
from payment_poller import _poll_once, run_payment_poller


class FakeRecord:
    def __init__(self, operation_id, user_id, tariff):
        self.operation_id = operation_id
        self.user_id = user_id
        self.tariff = tariff
        self.order_id = f"sub_{user_id}_{tariff}_x"


class FakePayments:
    def __init__(self):
        self.records: dict[str, FakeRecord] = {}

    def find_by_operation(self, op_id):
        return self.records.get(op_id)


class FakeSubscription:
    def __init__(self, poll_results=None):
        self.poll_results: list[tuple[str, str]] = poll_results or []
        self.poll_calls: list[dict] = []
        self.payments = FakePayments()
        self.poll_exception: Optional[Exception] = None

    async def poll_pending_payments(self, **kwargs):
        self.poll_calls.append(kwargs)
        if self.poll_exception:
            raise self.poll_exception
        return list(self.poll_results)


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_no_results_no_notify(self):
        sub = FakeSubscription(poll_results=[])
        notifications = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _poll_once(
            sub, notify,
            older_than_seconds=300, max_age_seconds=86400,
        )
        assert notifications == []

    @pytest.mark.asyncio
    async def test_activated_triggers_notification(self):
        sub = FakeSubscription(poll_results=[("op-1", "activated")])
        sub.payments.records["op-1"] = FakeRecord("op-1", 42, "pro")
        notifications = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _poll_once(
            sub, notify,
            older_than_seconds=300, max_age_seconds=86400,
        )
        assert len(notifications) == 1
        uid, text = notifications[0]
        assert uid == 42
        assert "Оплата подтверждена" in text
        assert "PRO" in text

    @pytest.mark.asyncio
    async def test_failed_does_not_trigger_user_notification(self):
        # У failed-платежей webhook от Точки уже отработал статус,
        # поллер не должен дублировать уведомление пользователю
        sub = FakeSubscription(poll_results=[("op-2", "failed")])
        notifications = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _poll_once(
            sub, notify,
            older_than_seconds=300, max_age_seconds=86400,
        )
        assert notifications == []

    @pytest.mark.asyncio
    async def test_still_pending_no_notification(self):
        sub = FakeSubscription(poll_results=[("op-3", "still_pending")])
        notifications = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _poll_once(
            sub, notify,
            older_than_seconds=300, max_age_seconds=86400,
        )
        assert notifications == []

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_crash(self):
        sub = FakeSubscription(poll_results=[("op-1", "activated")])
        sub.payments.records["op-1"] = FakeRecord("op-1", 1, "pro")

        async def angry_notify(uid, text):
            raise RuntimeError("telegram down")

        # Не должно бросить
        await _poll_once(
            sub, angry_notify,
            older_than_seconds=300, max_age_seconds=86400,
        )

    @pytest.mark.asyncio
    async def test_no_notify_callback_does_not_crash(self):
        sub = FakeSubscription(poll_results=[("op-1", "activated")])
        sub.payments.records["op-1"] = FakeRecord("op-1", 1, "pro")
        await _poll_once(
            sub, None,
            older_than_seconds=300, max_age_seconds=86400,
        )

    @pytest.mark.asyncio
    async def test_record_not_found_in_store_skips_notify(self):
        # Поллер сообщил activated, но запись в стор не найдена
        sub = FakeSubscription(poll_results=[("op-ghost", "activated")])
        # records пуст, find_by_operation вернёт None
        notifications = []

        async def notify(uid, text):
            notifications.append((uid, text))

        await _poll_once(
            sub, notify,
            older_than_seconds=300, max_age_seconds=86400,
        )
        assert notifications == []

    @pytest.mark.asyncio
    async def test_passes_thresholds_to_subscription(self):
        sub = FakeSubscription()
        await _poll_once(
            sub, None,
            older_than_seconds=600, max_age_seconds=3600,
        )
        assert sub.poll_calls == [{
            "older_than_seconds": 600, "max_age_seconds": 3600,
        }]


def _patch_loop_sleep_with_limit(monkeypatch, max_iterations: int = 3):
    counter = {"n": 0}

    async def limited_sleep(seconds):
        counter["n"] += 1
        if counter["n"] >= max_iterations:
            raise asyncio.CancelledError()
        await asyncio.sleep(0)

    class FakeAsyncioModule:
        sleep = staticmethod(limited_sleep)

    monkeypatch.setattr(payment_poller, "asyncio", FakeAsyncioModule)
    return counter


class TestRunPaymentPoller:
    @pytest.mark.asyncio
    async def test_loop_calls_poll_each_iteration(self, monkeypatch):
        _patch_loop_sleep_with_limit(monkeypatch, max_iterations=3)
        sub = FakeSubscription(poll_results=[])

        with pytest.raises(asyncio.CancelledError):
            await run_payment_poller(sub, notify=None, interval=0)

        # Первая итерация _poll_once вызывает poll → 1 вызов
        # До CancelledError на 3-м sleep успевает 3 итерации
        assert len(sub.poll_calls) == 3

    @pytest.mark.asyncio
    async def test_exception_does_not_kill_loop(self, monkeypatch):
        _patch_loop_sleep_with_limit(monkeypatch, max_iterations=4)
        sub = FakeSubscription(poll_results=[])
        # Первая итерация падает, последующие — ОК
        call_log = []
        original = sub.poll_pending_payments

        async def flaky_poll(**kw):
            call_log.append(1)
            if len(call_log) == 1:
                raise RuntimeError("DB down")
            return await original(**kw)

        sub.poll_pending_payments = flaky_poll

        with pytest.raises(asyncio.CancelledError):
            await run_payment_poller(sub, notify=None, interval=0)

        # Loop пережил исключение и продолжил вызывать poll
        assert len(call_log) >= 2
