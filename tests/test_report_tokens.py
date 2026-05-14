"""Тесты ReportTokenStore — JSON-стор одноразовых ссылок на веб-отчёт."""
from __future__ import annotations

import json
import time

import pytest

from report_tokens import ReportTokenStore


@pytest.fixture
def store(tmp_path):
    return ReportTokenStore(filepath=str(tmp_path / "tokens.json"))


def test_create_returns_unique_hex(store):
    t1 = store.create(user_id=1, inn="7700000001")
    t2 = store.create(user_id=1, inn="7700000001")
    assert t1 != t2
    assert len(t1) == 32
    assert all(c in "0123456789abcdef" for c in t1)


def test_resolve_valid_token(store):
    token = store.create(user_id=42, inn="7700000001")
    info = store.resolve(token)
    assert info is not None
    assert info["user_id"] == 42
    assert info["inn"] == "7700000001"
    assert "expires_at" in info


def test_resolve_unknown_returns_none(store):
    assert store.resolve("deadbeef" * 4) is None


def test_resolve_empty_returns_none(store):
    assert store.resolve("") is None
    assert store.resolve(None) is None  # type: ignore[arg-type]


def test_expired_token_is_rejected_and_removed(store, tmp_path):
    token = store.create(user_id=1, inn="7700000001", ttl=-10)
    assert store.resolve(token) is None
    # После resolve просроченная запись удалена из файла
    raw = json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))
    assert token not in raw


def test_create_cleans_up_expired_entries(store, tmp_path):
    expired = store.create(user_id=1, inn="111", ttl=-100)
    fresh = store.create(user_id=2, inn="222", ttl=3600)
    raw = json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))
    assert expired not in raw
    assert fresh in raw


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "tokens.json")
    s1 = ReportTokenStore(filepath=path)
    token = s1.create(user_id=7, inn="7700000007")
    s2 = ReportTokenStore(filepath=path)
    info = s2.resolve(token)
    assert info is not None
    assert info["user_id"] == 7


def test_handles_corrupt_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("not json", encoding="utf-8")
    s = ReportTokenStore(filepath=str(path))
    # Не падает, начинает с пустого состояния
    token = s.create(user_id=1, inn="7700000001")
    assert s.resolve(token) is not None
