"""Тесты api_quota: глобальный учёт расхода API + auto-degradation."""
from __future__ import annotations

from datetime import date

import pytest

from api_quota import ApiQuota, ApiQuotaExhausted, get_quota, reset_singleton


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Чистим singleton между тестами — _isolated_cache_dir подменяет
    CACHE_DIR, нам нужно пересоздать ApiQuota чтобы он указывал на
    новый файл."""
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture
def quota(tmp_path):
    return ApiQuota(filepath=str(tmp_path / "quota.json"))


class TestUsageAndLimits:
    def test_initial_usage_is_zero(self, quota):
        assert quota.usage_today("dadata_fetch") == 0

    def test_record_increments_today(self, quota):
        quota.record("dadata_fetch")
        quota.record("dadata_fetch")
        assert quota.usage_today("dadata_fetch") == 2

    def test_record_with_count(self, quota):
        quota.record("zchb", count=5)
        assert quota.usage_today("zchb") == 5

    def test_record_count_zero_noop(self, quota):
        quota.record("zchb", count=0)
        quota.record("zchb", count=-3)
        assert quota.usage_today("zchb") == 0

    def test_default_limit_returned(self, quota):
        # Дефолт DaData fetch — 200 (зашит в _DEFAULT_LIMITS)
        assert quota.daily_limit("dadata_fetch") == 200

    def test_env_overrides_limit(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "42")
        assert quota.daily_limit("fns") == 42

    def test_env_zero_means_unlimited(self, quota, monkeypatch):
        monkeypatch.setenv("DADATA_FETCH_DAILY_LIMIT", "0")
        assert quota.daily_limit("dadata_fetch") is None

    def test_env_invalid_falls_back_to_default(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "not-a-number")
        assert quota.daily_limit("fns") == 80  # default

    def test_unknown_api_no_limit(self, quota):
        assert quota.daily_limit("unknown_xyz") is None
        assert quota.is_near_limit("unknown_xyz") is False
        assert quota.is_exhausted("unknown_xyz") is False

    def test_remaining_when_unlimited(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "0")
        quota.record("fns", 10)
        assert quota.remaining("fns") is None

    def test_remaining_decreases(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "10")
        quota.record("fns", 3)
        assert quota.remaining("fns") == 7

    def test_remaining_clamped_at_zero(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "5")
        quota.record("fns", 100)
        assert quota.remaining("fns") == 0


class TestNearAndExhaustedThresholds:
    def test_below_80_not_near(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        quota.record("fns", 79)
        assert quota.is_near_limit("fns") is False

    def test_at_80_near(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        quota.record("fns", 80)
        assert quota.is_near_limit("fns") is True
        assert quota.is_exhausted("fns") is False

    def test_at_95_exhausted(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        quota.record("fns", 95)
        assert quota.is_exhausted("fns") is True

    def test_check_raises_when_exhausted(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        quota.record("fns", 95)
        with pytest.raises(ApiQuotaExhausted) as exc_info:
            quota.check("fns")
        assert "fns" in str(exc_info.value)

    def test_check_silent_when_below_exhausted(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "100")
        quota.record("fns", 50)
        # Не должно бросать
        quota.check("fns")

    def test_check_unlimited_never_raises(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "0")
        quota.record("fns", 1_000_000)
        quota.check("fns")  # silent


class TestPersistence:
    def test_save_and_reload(self, tmp_path):
        path = str(tmp_path / "quota.json")
        q1 = ApiQuota(filepath=path)
        q1.record("dadata_fetch", 5)
        q2 = ApiQuota(filepath=path)
        assert q2.usage_today("dadata_fetch") == 5

    def test_old_dates_garbage_collected(self, tmp_path, monkeypatch):
        path = str(tmp_path / "quota.json")
        q = ApiQuota(filepath=path)
        # Имитируем «вчерашнюю» запись напрямую в _data
        q._data["dadata_fetch"] = {"2020-01-01": 999, date.today().isoformat(): 1}
        q.record("dadata_fetch")  # запись подчищает старые ключи
        assert "2020-01-01" not in q._data["dadata_fetch"]
        assert q.usage_today("dadata_fetch") == 2

    def test_corrupt_file_does_not_crash(self, tmp_path):
        path = tmp_path / "quota.json"
        path.write_text("not-a-valid-json", encoding="utf-8")
        q = ApiQuota(filepath=str(path))
        # Загрузка молча провалилась, начинаем с пустого состояния
        assert q.usage_today("dadata_fetch") == 0


class TestSnapshot:
    def test_includes_all_default_apis(self, quota):
        snap = quota.snapshot()
        for api in ("dadata_fetch", "dadata_suggest", "fns", "zchb",
                    "sbis", "gigachat"):
            assert api in snap, f"{api} отсутствует в snapshot"

    def test_snapshot_fields(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "10")
        quota.record("fns", 8)
        info = quota.snapshot()["fns"]
        assert info["used"] == 8
        assert info["limit"] == 10
        assert info["remaining"] == 2
        assert info["percent"] == 80.0
        assert info["near"] is True
        assert info["exhausted"] is False

    def test_snapshot_unlimited_fields(self, quota, monkeypatch):
        monkeypatch.setenv("FNS_DAILY_LIMIT", "0")
        quota.record("fns", 50)
        info = quota.snapshot()["fns"]
        assert info["limit"] is None
        assert info["remaining"] is None


class TestSingleton:
    def test_get_quota_returns_same_instance(self):
        q1 = get_quota()
        q2 = get_quota()
        assert q1 is q2

    def test_reset_creates_new(self):
        q1 = get_quota()
        reset_singleton()
        q2 = get_quota()
        assert q1 is not q2


class TestIntegrationWithDaDataClient:
    """Проверяем что DaDataClient действительно дёргает api_quota."""

    @pytest.mark.asyncio
    async def test_dadata_records_on_success(self, monkeypatch, tmp_path):
        from dadata_client import DaDataClient
        import dadata_client

        # Подмена requests.post с успешным ответом
        class FakeResp:
            status_code = 200
            text = "ok"
            def json(self):
                return {"suggestions": [{"value": "X", "data": {"inn": "1"}}]}

        monkeypatch.setenv("DADATA_API_KEY", "k")
        monkeypatch.setattr(dadata_client.requests, "post",
                            lambda *a, **kw: FakeResp())

        client = DaDataClient()
        await client.fetch_company("7707083893")
        assert get_quota().usage_today("dadata_fetch") == 1

    @pytest.mark.asyncio
    async def test_dadata_skipped_when_quota_exhausted(self, monkeypatch):
        from dadata_client import DaDataClient
        import dadata_client

        monkeypatch.setenv("DADATA_API_KEY", "k")
        monkeypatch.setenv("DADATA_FETCH_DAILY_LIMIT", "10")
        # Забиваем счётчик до auto-degradation (95%)
        get_quota().record("dadata_fetch", 10)

        called = {"n": 0}

        def fake_post(*a, **kw):
            called["n"] += 1
            class R:
                status_code = 200
                text = ""
                def json(self):
                    return {"suggestions": []}
            return R()

        monkeypatch.setattr(dadata_client.requests, "post", fake_post)

        client = DaDataClient()
        # На auto-degradation — None, без сетевого вызова
        result = await client.fetch_company("9999999999")
        assert result is None
        assert called["n"] == 0  # реального HTTP не было
