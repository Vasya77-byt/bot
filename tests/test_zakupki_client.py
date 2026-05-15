"""Тесты zakupki_client — клиент к данным госзакупок."""
from __future__ import annotations

from typing import Optional

import pytest

import zakupki_client
from api_quota import get_quota, reset_singleton
from zakupki_client import (
    Contract,
    ZakupkiClient,
    ZakupkiStats,
    format_stats_message,
)


@pytest.fixture(autouse=True)
def _quota_reset():
    reset_singleton()
    yield
    reset_singleton()


class FakeResp:
    def __init__(self, status: int = 200, payload: Optional[dict] = None,
                 text: str = ""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class TestEnabledFlag:
    def test_enabled_when_url_set(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com/api")
        client = ZakupkiClient()
        assert client.enabled is True

    def test_disabled_without_url(self, monkeypatch):
        monkeypatch.delenv("ZAKUPKI_API_URL", raising=False)
        client = ZakupkiClient()
        assert client.enabled is False


class TestGetStats:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ZAKUPKI_API_URL", raising=False)
        client = ZakupkiClient()
        stats = await client.get_stats("7707083893")
        assert stats.total_count == 0
        assert stats.contracts == []
        assert stats.is_in_rnp is None

    @pytest.mark.asyncio
    async def test_returns_parsed_response(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com/api")
        client = ZakupkiClient()

        payload = {
            "total_count": 42,
            "total_sum": 15_000_000.5,
            "is_in_rnp": False,
            "contracts": [
                {
                    "reg_number": "0173100000824000123",
                    "customer_name": "Минобороны",
                    "customer_inn": "7704252261",
                    "sum_rub": 5_000_000,
                    "signed_at": "2024-09-15",
                    "status": "исполнен",
                    "law": "44",
                    "subject": "Поставка канцтоваров",
                },
            ],
        }

        monkeypatch.setattr(
            zakupki_client.requests, "get",
            lambda *a, **kw: FakeResp(200, payload),
        )
        stats = await client.get_stats("7707083893")
        assert stats.total_count == 42
        assert stats.total_sum == 15_000_000.5
        assert stats.is_in_rnp is False
        assert len(stats.contracts) == 1
        c = stats.contracts[0]
        assert c.reg_number == "0173100000824000123"
        assert c.customer_name == "Минобороны"
        assert c.sum_rub == 5_000_000.0
        # Закон нормализован «44» → «44-ФЗ»
        assert c.law == "44-ФЗ"

    @pytest.mark.asyncio
    async def test_caches_response(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com/api")
        client = ZakupkiClient()
        call_count = {"n": 0}

        def fake_get(*a, **kw):
            call_count["n"] += 1
            return FakeResp(200, {
                "total_count": 5,
                "total_sum": 1000.0,
                "is_in_rnp": False,
                "contracts": [],
            })

        monkeypatch.setattr(zakupki_client.requests, "get", fake_get)
        await client.get_stats("7707083893")
        await client.get_stats("7707083893")
        await client.get_stats("7707083893")
        assert call_count["n"] == 1  # cross-user cache

    @pytest.mark.asyncio
    async def test_records_quota_on_success(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com/api")
        client = ZakupkiClient()
        monkeypatch.setattr(
            zakupki_client.requests, "get",
            lambda *a, **kw: FakeResp(200, {"total_count": 1, "contracts": []}),
        )
        await client.get_stats("7707083893")
        assert get_quota().usage_today("zakupki") == 1

    @pytest.mark.asyncio
    async def test_failed_response_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com/api")
        client = ZakupkiClient()
        monkeypatch.setattr(
            zakupki_client.requests, "get",
            lambda *a, **kw: FakeResp(503, {}),
        )
        stats = await client.get_stats("7707083893")
        assert stats.total_count == 0
        assert stats.contracts == []

    @pytest.mark.asyncio
    async def test_skipped_when_quota_exhausted(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com/api")
        monkeypatch.setenv("ZAKUPKI_DAILY_LIMIT", "10")
        get_quota().record("zakupki", 10)  # 100% — auto-degradation

        client = ZakupkiClient()
        called = {"n": 0}

        def fake_get(*a, **kw):
            called["n"] += 1
            return FakeResp(200, {})

        monkeypatch.setattr(zakupki_client.requests, "get", fake_get)
        stats = await client.get_stats("7707083893")
        assert called["n"] == 0  # реального вызова не было
        assert stats.total_count == 0


class TestParseStats:
    @pytest.mark.asyncio
    async def test_law_normalization_44_223(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com")
        client = ZakupkiClient()
        payload = {
            "contracts": [
                {"law": "44"},
                {"law": "223"},
                {"law": "44-ФЗ"},   # уже нормализован
                {"law": "unknown"},
            ],
        }
        monkeypatch.setattr(
            zakupki_client.requests, "get",
            lambda *a, **kw: FakeResp(200, payload),
        )
        stats = await client.get_stats("1")
        laws = [c.law for c in stats.contracts]
        assert laws == ["44-ФЗ", "223-ФЗ", "44-ФЗ", "unknown"]

    @pytest.mark.asyncio
    async def test_handles_invalid_sum(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com")
        client = ZakupkiClient()
        payload = {
            "contracts": [{"sum_rub": "not-a-number"}],
            "total_sum": "garbage",
        }
        monkeypatch.setattr(
            zakupki_client.requests, "get",
            lambda *a, **kw: FakeResp(200, payload),
        )
        stats = await client.get_stats("1")
        assert stats.contracts[0].sum_rub == 0.0
        assert stats.total_sum == 0.0

    @pytest.mark.asyncio
    async def test_respects_top_n(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com")
        client = ZakupkiClient()
        # API возвращает 10 контрактов
        payload = {
            "contracts": [{"reg_number": f"C-{i}"} for i in range(10)],
        }
        monkeypatch.setattr(
            zakupki_client.requests, "get",
            lambda *a, **kw: FakeResp(200, payload),
        )
        stats = await client.get_stats("1", top_n=3)
        assert len(stats.contracts) == 3

    @pytest.mark.asyncio
    async def test_rnp_none_when_missing(self, monkeypatch):
        monkeypatch.setenv("ZAKUPKI_API_URL", "https://example.com")
        client = ZakupkiClient()
        payload = {"total_count": 1, "contracts": []}  # is_in_rnp отсутствует
        monkeypatch.setattr(
            zakupki_client.requests, "get",
            lambda *a, **kw: FakeResp(200, payload),
        )
        stats = await client.get_stats("1")
        assert stats.is_in_rnp is None  # «не проверяли», не False!


class TestFormatStatsMessage:
    def test_empty_stats_shows_unavailable(self):
        text = format_stats_message(ZakupkiStats(), "7707083893")
        assert "временно недоступны" in text or "не найдено" in text

    def test_full_stats_shows_count_sum_rnp_top(self):
        stats = ZakupkiStats(
            total_count=42,
            total_sum=15_000_000.0,
            is_in_rnp=False,
            contracts=[
                Contract(
                    reg_number="0173100000824000123",
                    customer_name="Минобороны",
                    sum_rub=5_000_000,
                    signed_at="2024-09-15",
                    status="исполнен",
                    law="44-ФЗ",
                ),
            ],
        )
        text = format_stats_message(stats, "7707083893")
        assert "42" in text
        assert "15 000 000" in text
        assert "✅ В РНП не значится" in text
        assert "Минобороны" in text
        assert "2024-09-15" in text
        assert "44-ФЗ" in text

    def test_rnp_critical_marker(self):
        stats = ZakupkiStats(
            total_count=2, is_in_rnp=True,
            rnp_reason="Уклонение от заключения контракта",
        )
        text = format_stats_message(stats, "1")
        assert "🔴" in text
        assert "Уклонение от заключения" in text

    def test_rnp_unknown_omitted(self):
        """is_in_rnp=None — не показывать ни ✅ ни 🔴, чтобы не вводить
        в заблуждение «проверено / не проверено»."""
        stats = ZakupkiStats(total_count=2, is_in_rnp=None)
        text = format_stats_message(stats, "1")
        assert "✅ В РНП" not in text
        assert "🔴 В реестре" not in text


class TestCacheSerialization:
    def test_roundtrip(self):
        from zakupki_client import _stats_from_dict, _stats_to_dict
        original = ZakupkiStats(
            total_count=5,
            total_sum=1234.5,
            is_in_rnp=True,
            rnp_reason="reason",
            contracts=[
                Contract(reg_number="A", sum_rub=100.0, law="44-ФЗ"),
                Contract(reg_number="B", customer_name="X", sum_rub=200.0),
            ],
        )
        restored = _stats_from_dict(_stats_to_dict(original))
        assert restored.total_count == 5
        assert restored.total_sum == 1234.5
        assert restored.is_in_rnp is True
        assert restored.rnp_reason == "reason"
        assert len(restored.contracts) == 2
        assert restored.contracts[0].reg_number == "A"
