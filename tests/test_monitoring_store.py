"""Тесты MonitoringStore — стор подписок на мониторинг ИНН."""
import json

import pytest

from monitoring_store import MonitoringStore, MonitoringSubscription


@pytest.fixture
def store(tmp_path):
    return MonitoringStore(filepath=str(tmp_path / "monitoring.json"))


class TestAdd:
    def test_creates_new_subscription(self, store):
        sub = store.add(user_id=1, inn="7707083893", name="ООО Ромашка",
                        snapshot={"name": "ООО Ромашка"})
        assert isinstance(sub, MonitoringSubscription)
        assert sub.user_id == 1
        assert sub.inn == "7707083893"
        assert sub.name == "ООО Ромашка"
        assert sub.snapshot == {"name": "ООО Ромашка"}
        assert sub.created_at  # выставлен timestamp
        assert sub.last_checked

    def test_persists_to_file(self, tmp_path):
        path = tmp_path / "m.json"
        store = MonitoringStore(filepath=str(path))
        store.add(1, "123", "X")
        with open(path) as f:
            data = json.load(f)
        assert "1:123" in data

    def test_duplicate_add_updates_in_place(self, store):
        first = store.add(1, "123", name="ООО Старая")
        # Повторный вызов с другим именем — не создаёт дубль
        second = store.add(1, "123", name="ООО Новая",
                           snapshot={"name": "ООО Новая"})
        assert store.count_for_user(1) == 1
        assert second.name == "ООО Новая"
        assert second.snapshot == {"name": "ООО Новая"}
        # created_at не поменялся
        assert second.created_at == first.created_at

    def test_duplicate_add_without_name_keeps_old(self, store):
        store.add(1, "123", name="ООО Первая")
        # Если в повторном add не передали name — старое не затирается
        result = store.add(1, "123", name="")
        assert result.name == "ООО Первая"


class TestRemove:
    def test_removes_existing(self, store):
        store.add(1, "123", "X")
        assert store.remove(1, "123") is True
        assert store.get(1, "123") is None

    def test_remove_missing_returns_false(self, store):
        assert store.remove(1, "999") is False

    def test_remove_does_not_affect_other_users(self, store):
        store.add(1, "123", "A")
        store.add(2, "123", "B")
        store.remove(1, "123")
        assert store.get(2, "123") is not None


class TestGet:
    def test_get_existing(self, store):
        store.add(1, "123", "X")
        sub = store.get(1, "123")
        assert sub is not None
        assert sub.inn == "123"

    def test_get_missing(self, store):
        assert store.get(1, "999") is None


class TestListForUser:
    def test_returns_only_users_subs(self, store):
        store.add(1, "111", "A")
        store.add(1, "222", "B")
        store.add(2, "333", "C")
        result = store.list_for_user(1)
        inns = sorted(s.inn for s in result)
        assert inns == ["111", "222"]

    def test_empty_for_unknown_user(self, store):
        assert store.list_for_user(999) == []


class TestCountForUser:
    def test_counts_only_users_subs(self, store):
        store.add(1, "111")
        store.add(1, "222")
        store.add(2, "333")
        assert store.count_for_user(1) == 2
        assert store.count_for_user(2) == 1
        assert store.count_for_user(999) == 0


class TestIterAll:
    def test_iterates_all_subscriptions(self, store):
        store.add(1, "111", "A")
        store.add(2, "222", "B")
        store.add(3, "333", "C")
        all_subs = list(store.iter_all())
        assert len(all_subs) == 3
        inns = sorted(s.inn for s in all_subs)
        assert inns == ["111", "222", "333"]


class TestUpdateSnapshot:
    def test_updates_existing(self, store):
        store.add(1, "123", "X", snapshot={"status": "Действующая"})
        result = store.update_snapshot(1, "123", {"status": "Ликвидируется"})
        assert result is not None
        assert result.snapshot == {"status": "Ликвидируется"}
        # last_checked обновлён
        assert result.last_checked

    def test_unknown_subscription_returns_none(self, store):
        assert store.update_snapshot(1, "missing", {}) is None

    def test_persists_after_update(self, tmp_path):
        path = tmp_path / "m.json"
        store = MonitoringStore(filepath=str(path))
        store.add(1, "123", "X", snapshot={"v": 1})
        store.update_snapshot(1, "123", {"v": 2})
        # Перечитываем
        store2 = MonitoringStore(filepath=str(path))
        assert store2.get(1, "123").snapshot == {"v": 2}


class TestPersistenceLoad:
    def test_corrupt_file_yields_empty(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text("not json {{{")
        store = MonitoringStore(filepath=str(path))
        assert list(store.iter_all()) == []

    def test_unknown_fields_ignored_for_back_compat(self, tmp_path):
        path = tmp_path / "m.json"
        with open(path, "w") as f:
            json.dump({
                "1:123": {
                    "user_id": 1, "inn": "123", "name": "X",
                    "old_legacy_field": "should be ignored",
                }
            }, f)
        store = MonitoringStore(filepath=str(path))
        sub = store.get(1, "123")
        assert sub is not None
        assert sub.inn == "123"
