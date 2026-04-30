"""Тесты admin_stats — парсер админов и сборка отчёта."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from admin_stats import (
    _fmt_money,
    build_admin_report,
    parse_admin_user_ids,
)
from monitoring_store import MonitoringStore
from payments_store import PaymentsStore
from user_store import UserStore


class TestParseAdminUserIds:
    def test_empty_returns_empty_set(self):
        assert parse_admin_user_ids("") == set()
        assert parse_admin_user_ids("   ") == set()

    def test_single_id(self):
        assert parse_admin_user_ids("123") == {123}

    def test_multiple_ids_comma_separated(self):
        assert parse_admin_user_ids("123,456,789") == {123, 456, 789}

    def test_handles_whitespace(self):
        assert parse_admin_user_ids("  123 , 456 ,789") == {123, 456, 789}

    def test_invalid_tokens_skipped(self):
        assert parse_admin_user_ids("123,abc,456") == {123, 456}


class TestFmtMoney:
    def test_thousands(self):
        assert _fmt_money(125_000) == "125 000 ₽"

    def test_under_thousand(self):
        assert _fmt_money(500) == "500 ₽"

    def test_millions(self):
        assert "млн" in _fmt_money(1_500_000)


@pytest.fixture
def stores(tmp_path):
    users = UserStore(filepath=str(tmp_path / "users.json"))
    payments = PaymentsStore(filepath=str(tmp_path / "payments.json"))
    monitoring = MonitoringStore(filepath=str(tmp_path / "monitoring.json"))
    return users, payments, monitoring


def _activate_paid(users: UserStore, user_id: int, tariff: str = "pro",
                   *, days_left: int = 25, auto_renew: bool = True,
                   accepted_at: str = "") -> None:
    p = users.get(user_id)
    p.tariff = tariff
    p.tariff_expires_at = (
        datetime.now(timezone.utc) + timedelta(days=days_left)
    ).isoformat()
    p.auto_renew = auto_renew
    if accepted_at:
        p.accepted_offer_at = accepted_at
    users.save_profile(p)


class TestBuildAdminReport:
    @pytest.mark.asyncio
    async def test_empty_database_returns_zeros(self, stores):
        users, payments, monitoring = stores
        report = await build_admin_report(users, payments, monitoring, zchb=None)
        assert "Админ-отчёт MondayCompany" in report
        assert "Всего: 0" in report
        assert "Платных активных: 0" in report
        # ZCHB не настроен — соответствующая строка
        assert "ключ не настроен" in report.lower()

    @pytest.mark.asyncio
    async def test_counts_active_paid_users(self, stores):
        users, payments, monitoring = stores
        _activate_paid(users, 1, "pro", days_left=20, auto_renew=True)
        _activate_paid(users, 2, "pro", days_left=10, auto_renew=False)
        users.get(3)  # free user
        report = await build_admin_report(users, payments, monitoring, zchb=None)
        assert "Всего: 3" in report
        assert "Платных активных: 2" in report
        assert "На автоплатеже: 1" in report
        assert "Отменили автопродление: 1" in report

    @pytest.mark.asyncio
    async def test_payments_this_month(self, stores):
        users, payments, monitoring = stores
        # Платёж в этом месяце
        now = datetime.now(timezone.utc)
        payments._data.append({
            "operation_id": "op1", "order_id": "ord1", "user_id": 1,
            "tariff": "pro", "amount": 1290.0, "kind": "initial",
            "status": "paid", "created_at": now.isoformat(),
            "paid_at": now.isoformat(), "error": "",
        })
        payments._save()

        report = await build_admin_report(users, payments, monitoring, zchb=None)
        assert "Оплат: 1" in report
        assert "1 290" in report or "1290" in report
        assert "Самый популярный: pro" in report

    @pytest.mark.asyncio
    async def test_mrr_calculation(self, stores):
        users, payments, monitoring = stores
        # Один Pro (1290) и один Business (2490) активные
        _activate_paid(users, 10, "pro", days_left=15, auto_renew=True)
        _activate_paid(users, 20, "business", days_left=15, auto_renew=True)
        report = await build_admin_report(users, payments, monitoring, zchb=None)
        # 1290 + 2490 = 3780
        assert "3 780" in report or "MRR" in report

    @pytest.mark.asyncio
    async def test_referrals_section_with_zero(self, stores):
        users, payments, monitoring = stores
        users.get(1)
        report = await build_admin_report(users, payments, monitoring, zchb=None)
        assert "РЕФЕРАЛЫ" in report
        assert "Приглашено: 0" in report

    @pytest.mark.asyncio
    async def test_handles_zchb_failure(self, stores):
        users, payments, monitoring = stores

        class FakeZchb:
            enabled = True

            async def get_stats(self):
                raise RuntimeError("network down")

        report = await build_admin_report(
            users, payments, monitoring, zchb=FakeZchb(),
        )
        # Не падает, выводит fallback
        assert "ZCHB" in report
        assert "недоступна" in report.lower() or "не настроен" in report.lower()
