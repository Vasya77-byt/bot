"""Тесты bulk_check — массовая проверка контрагентов."""
from __future__ import annotations

from typing import Optional

import pytest

from api_quota import get_quota, reset_singleton
from bulk_check import (
    BulkPreflightError,
    BulkResult,
    parse_csv_inns,
    preflight_check,
    process_batch,
)
from schemas import CompanyData


@pytest.fixture(autouse=True)
def _quota_reset():
    reset_singleton()
    yield
    reset_singleton()


class TestParseCsvInns:
    def test_simple_text_with_one_inn(self):
        assert parse_csv_inns(b"7707083893") == ["7707083893"]

    def test_csv_with_header(self):
        csv = b"INN;Name\n7707083893;Sber\n7728168971;Lukoil\n"
        assert parse_csv_inns(csv) == ["7707083893", "7728168971"]

    def test_dedupes(self):
        # Один и тот же ИНН несколько раз — берём только первый
        csv = b"7707083893\n7707083893\n7707083893\n"
        assert parse_csv_inns(csv) == ["7707083893"]

    def test_extracts_both_lengths(self):
        # 10 и 12 цифр — оба валидны (юрлица и ИП)
        result = parse_csv_inns(b"7707083893, 123456789012")
        assert "7707083893" in result
        assert "123456789012" in result

    def test_ignores_non_inn_numbers(self):
        # 9 цифр (короткий), 13 (слишком длинный — но 12 в начале)
        # Регулярка \b(\d{12}|\d{10})\b: 13-значное "9999999999999"
        # внутри не имеет \b на границе после 12-й цифры, fallback на 10.
        result = parse_csv_inns(b"phone: 123456789, code: 9999999")
        # 9 и 7 цифр не должны попасть
        assert "123456789" not in result
        assert "9999999" not in result

    def test_empty_input(self):
        assert parse_csv_inns(b"") == []
        assert parse_csv_inns(b"no numbers here") == []

    def test_respects_max_count(self):
        # 5 разных ИНН, max=2 — должны взять только первые 2
        csv = b"7707083893\n7728168971\n7710140679\n7705512995\n7703683430\n"
        result = parse_csv_inns(csv, max_count=2)
        assert len(result) == 2
        assert result == ["7707083893", "7728168971"]

    def test_handles_cp1251_encoding(self):
        """Excel из 1С/Windows часто экспортирует в cp1251."""
        text = "ИНН;Название\n7707083893;ООО Ромашка\n"
        content = text.encode("cp1251")
        result = parse_csv_inns(content)
        assert result == ["7707083893"]

    def test_handles_utf8_bom(self):
        """Excel может ставить BOM перед UTF-8."""
        content = b"\xef\xbb\xbf7707083893\n7728168971\n"
        result = parse_csv_inns(content)
        assert result == ["7707083893", "7728168971"]


class TestPreflightCheck:
    def test_passes_with_clean_quotas(self, monkeypatch):
        # Лимит 100, использовано 0 → проходит
        monkeypatch.setenv("DADATA_FETCH_DAILY_LIMIT", "100")
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        monkeypatch.setenv("ZCHB_DAILY_LIMIT", "100")
        preflight_check()  # не должно бросать

    def test_fails_at_71_percent(self, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        get_quota().record("fns", 71)  # 71% > 70%
        with pytest.raises(BulkPreflightError) as exc_info:
            preflight_check()
        assert exc_info.value.api == "fns"
        assert exc_info.value.percent >= 70

    def test_passes_at_70_percent(self, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        get_quota().record("fns", 70)  # ровно 70%, не выше
        preflight_check()  # не должно бросать

    def test_message_is_informative(self, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "10")
        get_quota().record("fns", 8)  # 80%
        with pytest.raises(BulkPreflightError) as exc_info:
            preflight_check()
        msg = str(exc_info.value)
        assert "fns" in msg
        assert "80" in msg or "80.0" in msg

    def test_unlimited_api_never_triggers(self, monkeypatch):
        monkeypatch.setenv("DADATA_FETCH_DAILY_LIMIT", "0")
        # Никакой счётчик не вызовет preflight при безлимите
        get_quota().record("dadata_fetch", 1000000)
        preflight_check()


class FakeCompanyService:
    """Заглушка для CompanyService — конфигурируется per-test."""

    def __init__(self, by_inn: Optional[dict] = None,
                 raise_on: Optional[set] = None):
        self.by_inn = by_inn or {}
        self.raise_on = raise_on or set()
        self.call_log: list[str] = []

    async def fetch_quick(self, inn: str):
        self.call_log.append(inn)
        if inn in self.raise_on:
            raise RuntimeError("simulated")
        return self.by_inn.get(inn)


class TestProcessBatch:
    @pytest.mark.asyncio
    async def test_returns_one_result_per_inn(self):
        svc = FakeCompanyService(by_inn={
            "7707083893": CompanyData(inn="7707083893", name="Sber", source="dadata"),
            "7728168971": CompanyData(inn="7728168971", name="Lukoil", source="dadata"),
        })
        results = await process_batch(
            ["7707083893", "7728168971"], svc, throttle_seconds=0,
        )
        assert len(results) == 2
        assert results[0].name == "Sber"
        assert results[1].name == "Lukoil"
        assert all(r.error is None for r in results)

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self):
        svc = FakeCompanyService(by_inn={})  # ничего не возвращает
        results = await process_batch(["9999999999"], svc, throttle_seconds=0)
        assert results[0].error == "not_found"
        assert results[0].name is None

    @pytest.mark.asyncio
    async def test_exception_does_not_break_batch(self):
        svc = FakeCompanyService(
            by_inn={"7707083893": CompanyData(inn="7707083893", name="OK")},
            raise_on={"7728168971"},
        )
        results = await process_batch(
            ["7707083893", "7728168971", "7707083893"],
            svc, throttle_seconds=0,
        )
        assert len(results) == 3
        # Первый — OK
        assert results[0].name == "OK"
        # Второй — ошибка, не прерывает обработку
        assert results[1].error == "error"
        # Третий — снова OK
        assert results[2].name == "OK"

    @pytest.mark.asyncio
    async def test_progress_callback_called(self):
        svc = FakeCompanyService(by_inn={
            "7707083893": CompanyData(inn="7707083893", name="X"),
            "7728168971": CompanyData(inn="7728168971", name="Y"),
        })
        progress_log = []

        async def cb(done, total):
            progress_log.append((done, total))

        await process_batch(
            ["7707083893", "7728168971"], svc,
            throttle_seconds=0, progress_callback=cb,
        )
        assert progress_log == [(1, 2), (2, 2)]

    @pytest.mark.asyncio
    async def test_progress_callback_exception_is_swallowed(self):
        """Сломавшийся progress callback не должен ронять bulk."""
        svc = FakeCompanyService(by_inn={
            "7707083893": CompanyData(inn="7707083893", name="X"),
        })

        async def broken_cb(done, total):
            raise RuntimeError("ui error")

        results = await process_batch(
            ["7707083893"], svc,
            throttle_seconds=0, progress_callback=broken_cb,
        )
        assert len(results) == 1
        assert results[0].name == "X"


class TestBuildBulkXlsx:
    def test_returns_bytes_with_results(self):
        from exports import build_bulk_xlsx
        results = [
            BulkResult(
                inn="7707083893", name="Sber", status="Действующая",
                director="И.И. Иванов",
            ),
            BulkResult(inn="9999999999", error="not_found"),
        ]
        data = build_bulk_xlsx(results)
        assert isinstance(data, bytes)
        # Excel-файл должен начинаться с ZIP-magic (xlsx это zip)
        # ИЛИ с UTF-8 BOM если fallback на CSV
        assert data[:2] in (b"PK", b"\xef\xbb")

    def test_works_with_empty_results(self):
        from exports import build_bulk_xlsx
        data = build_bulk_xlsx([])
        assert isinstance(data, bytes)
        assert len(data) > 0  # хотя бы заголовок есть


class TestUserStoreBulkLimits:
    def test_default_bulk_today_is_zero(self):
        from user_store import UserProfile
        p = UserProfile(user_id=1)
        assert p.bulk_today == 0

    def test_pro_bulk_limit_30(self):
        from datetime import datetime, timedelta, timezone
        from user_store import UserProfile
        future = datetime.now(timezone.utc) + timedelta(days=30)
        p = UserProfile(
            user_id=1, tariff="pro", tariff_expires_at=future.isoformat(),
        )
        assert p.remaining_bulk() == 30
        assert p.can_bulk(count=30) is True
        assert p.can_bulk(count=31) is False

    def test_business_bulk_limit_100(self):
        from datetime import datetime, timedelta, timezone
        from user_store import UserProfile
        future = datetime.now(timezone.utc) + timedelta(days=30)
        p = UserProfile(
            user_id=1, tariff="business", tariff_expires_at=future.isoformat(),
        )
        assert p.remaining_bulk() == 100

    def test_free_and_start_no_bulk(self):
        from user_store import UserProfile
        p_free = UserProfile(user_id=1, tariff="free")
        assert p_free.remaining_bulk() == 0
        assert p_free.can_bulk(count=1) is False

    def test_increment_bulk_consumes_quota(self):
        from datetime import datetime, timedelta, timezone
        from user_store import UserProfile
        future = datetime.now(timezone.utc) + timedelta(days=30)
        p = UserProfile(
            user_id=1, tariff="business", tariff_expires_at=future.isoformat(),
        )
        p.increment_bulk(count=25)
        assert p.bulk_today == 25
        assert p.remaining_bulk() == 75
        assert p.can_bulk(count=75) is True
        assert p.can_bulk(count=76) is False

    def test_new_day_resets_bulk(self):
        from user_store import UserProfile
        p = UserProfile(
            user_id=1, tariff="business",
            bulk_today=50, checks_date="2020-01-01",
        )
        p.reset_if_new_day()
        assert p.bulk_today == 0
