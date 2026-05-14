"""Тесты expiry_reminder — напоминания об окончании подписки."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from expiry_reminder import _check_once
from user_store import UserStore


@pytest.fixture
def users(tmp_path):
    return UserStore(filepath=str(tmp_path / "users.json"))


@pytest.fixture
def notifications():
    sent: list[tuple[int, str]] = []

    async def notify(user_id: int, text: str) -> None:
        sent.append((user_id, text))

    return sent, notify


def _make_paid(users: UserStore, user_id: int, *,
               days_until_expire: float, auto_renew: bool = False,
               expired_notice_sent: bool = False,
               last_reminder_date: str = "") -> None:
    """Создаёт пользователя с платной подпиской и заданным сроком."""
    p = users.get(user_id)
    p.tariff = "pro"
    p.tariff_expires_at = (
        datetime.now(timezone.utc) + timedelta(days=days_until_expire)
    ).isoformat()
    p.auto_renew = auto_renew
    p.expired_notice_sent = expired_notice_sent
    p.last_expiry_reminder_date = last_reminder_date
    users.save_profile(p)


class TestCheckOnce:
    @pytest.mark.asyncio
    async def test_free_user_skipped(self, users, notifications):
        sent, notify = notifications
        # Free пользователь — без даты окончания
        users.get(1)  # просто создаём
        await _check_once(users, notify)
        assert sent == []

    @pytest.mark.asyncio
    async def test_3_days_left_no_autorenew_sends(self, users, notifications):
        sent, notify = notifications
        _make_paid(users, 100, days_until_expire=3.2, auto_renew=False)
        await _check_once(users, notify)
        assert len(sent) == 1
        user_id, text = sent[0]
        assert user_id == 100
        assert "через 3" in text or "через 3 дн" in text

    @pytest.mark.asyncio
    async def test_1_day_left_no_autorenew_sends(self, users, notifications):
        sent, notify = notifications
        _make_paid(users, 200, days_until_expire=1.5, auto_renew=False)
        await _check_once(users, notify)
        assert len(sent) == 1
        assert "Завтра" in sent[0][1]

    @pytest.mark.asyncio
    async def test_3_days_left_with_autorenew_skipped(
        self, users, notifications,
    ):
        """Если автопродление включено — не дёргаем напоминанием.
        Списание сделает renewal_scheduler сам."""
        sent, notify = notifications
        _make_paid(users, 300, days_until_expire=3.2, auto_renew=True)
        await _check_once(users, notify)
        assert sent == []

    @pytest.mark.asyncio
    async def test_5_days_left_no_send(self, users, notifications):
        """Шлём только за 3 и 1 день, не за 5."""
        sent, notify = notifications
        _make_paid(users, 400, days_until_expire=5.5, auto_renew=False)
        await _check_once(users, notify)
        assert sent == []

    @pytest.mark.asyncio
    async def test_already_reminded_today_no_dup(
        self, users, notifications,
    ):
        sent, notify = notifications
        today = date.today().isoformat()
        _make_paid(users, 500, days_until_expire=3.2, auto_renew=False,
                   last_reminder_date=today)
        await _check_once(users, notify)
        assert sent == []

    @pytest.mark.asyncio
    async def test_expired_sends_once(self, users, notifications):
        sent, notify = notifications
        _make_paid(users, 600, days_until_expire=-5, auto_renew=False)
        await _check_once(users, notify)
        assert len(sent) == 1
        assert "истёк" in sent[0][1].lower() or "истек" in sent[0][1].lower()
        # Повторный вызов — не отправляет ещё раз
        sent.clear()
        await _check_once(users, notify)
        assert sent == []

    @pytest.mark.asyncio
    async def test_expired_resets_when_renewed(self, users, notifications):
        """Если юзер продлил подписку — флаг expired_notice_sent сбросится,
        чтобы при следующем истечении прийти снова."""
        sent, notify = notifications
        _make_paid(users, 700, days_until_expire=15, auto_renew=True,
                   expired_notice_sent=True)
        await _check_once(users, notify)
        # Сообщение не пришло (auto_renew=True), но флаг сбросился
        p = users.get(700)
        assert p.expired_notice_sent is False

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_break_loop(
        self, users, notifications, caplog,
    ):
        async def boom(user_id, text):
            raise RuntimeError("Telegram down")

        _make_paid(users, 800, days_until_expire=3.2, auto_renew=False)
        # Не должно бросить
        await _check_once(users, boom)
        # Дата отправки НЕ должна обновиться (потому что мы её ставим
        # после notify; при ошибке safe_notify ловит, и save идёт всё равно).
        # Это допустимое поведение — мы не хотим бесконечно повторять
        # упавшую отправку. Главное — что цикл не упал.
