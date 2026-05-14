"""Тесты PaymentsStore: журнал операций оплаты."""
import json

import pytest

from payments_store import PaymentRecord, PaymentsStore


@pytest.fixture
def store(tmp_path):
    return PaymentsStore(filepath=str(tmp_path / "payments.json"))


class TestRecordCreated:
    def test_creates_record_with_status_created(self, store):
        rec = store.record_created(
            operation_id="op-1", order_id="sub_1_pro_x",
            user_id=1, tariff="pro", amount=1290.0,
        )
        assert isinstance(rec, PaymentRecord)
        assert rec.status == "created"
        assert rec.operation_id == "op-1"
        assert rec.amount == 1290.0
        assert rec.kind == "initial"
        assert rec.created_at  # ISO timestamp заполнен
        assert rec.paid_at == ""

    def test_default_kind_is_initial(self, store):
        rec = store.record_created(
            operation_id="op", order_id="o", user_id=1,
            tariff="pro", amount=1.0,
        )
        assert rec.kind == "initial"

    def test_kind_recurring_explicit(self, store):
        rec = store.record_created(
            operation_id="op", order_id="o", user_id=1,
            tariff="pro", amount=1.0, kind="recurring",
        )
        assert rec.kind == "recurring"

    def test_persists_to_file(self, tmp_path):
        path = tmp_path / "p.json"
        store = PaymentsStore(filepath=str(path))
        store.record_created(
            operation_id="op", order_id="o", user_id=42,
            tariff="pro", amount=1290.0,
        )
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["operation_id"] == "op"


class TestMarkPaid:
    def test_marks_existing_record(self, store):
        store.record_created(
            operation_id="op-1", order_id="o", user_id=1,
            tariff="pro", amount=1.0,
        )
        rec = store.mark_paid("op-1")
        assert rec is not None
        assert rec.status == "paid"
        assert rec.paid_at  # выставлен timestamp

    def test_persists_status_change(self, tmp_path):
        path = tmp_path / "p.json"
        store = PaymentsStore(filepath=str(path))
        store.record_created(
            operation_id="op-1", order_id="o", user_id=1,
            tariff="pro", amount=1.0,
        )
        store.mark_paid("op-1")
        store2 = PaymentsStore(filepath=str(path))
        assert store2.find_by_operation("op-1").status == "paid"

    def test_unknown_operation_returns_none(self, store):
        assert store.mark_paid("nope") is None


class TestMarkFailed:
    def test_marks_with_error(self, store):
        store.record_created(
            operation_id="op-2", order_id="o", user_id=1,
            tariff="pro", amount=1.0,
        )
        rec = store.mark_failed("op-2", error="card declined")
        assert rec is not None
        assert rec.status == "failed"
        assert rec.error == "card declined"

    def test_unknown_operation_returns_none(self, store):
        assert store.mark_failed("nope") is None


class TestFinders:
    def test_find_by_operation(self, store):
        store.record_created(
            operation_id="op-find", order_id="ord", user_id=1,
            tariff="pro", amount=1.0,
        )
        assert store.find_by_operation("op-find").operation_id == "op-find"

    def test_find_by_operation_missing(self, store):
        assert store.find_by_operation("none") is None

    def test_find_by_order(self, store):
        store.record_created(
            operation_id="op", order_id="ord-find", user_id=1,
            tariff="pro", amount=1.0,
        )
        assert store.find_by_order("ord-find").order_id == "ord-find"

    def test_find_by_order_missing(self, store):
        assert store.find_by_order("none") is None


class TestUserPayments:
    def test_filters_by_user(self, store):
        store.record_created(operation_id="a", order_id="oa", user_id=1,
                             tariff="pro", amount=1.0)
        store.record_created(operation_id="b", order_id="ob", user_id=2,
                             tariff="pro", amount=2.0)
        store.record_created(operation_id="c", order_id="oc", user_id=1,
                             tariff="pro", amount=3.0)

        u1 = store.user_payments(1)
        assert len(u1) == 2
        assert {r.operation_id for r in u1} == {"a", "c"}

    def test_no_payments_for_user(self, store):
        assert store.user_payments(999) == []


class TestTotalRevenue:
    def test_only_paid_counts(self, store):
        store.record_created(operation_id="a", order_id="oa", user_id=1,
                             tariff="pro", amount=100.0)
        store.record_created(operation_id="b", order_id="ob", user_id=1,
                             tariff="pro", amount=200.0)
        store.record_created(operation_id="c", order_id="oc", user_id=1,
                             tariff="pro", amount=400.0)
        store.mark_paid("a")
        store.mark_paid("b")
        store.mark_failed("c", "x")

        assert store.total_revenue() == 300.0

    def test_empty_store_zero(self, store):
        assert store.total_revenue() == 0


class TestIterPending:
    def test_returns_only_created(self, store):
        store.record_created(operation_id="a", order_id="oa", user_id=1,
                             tariff="pro", amount=1.0)
        store.record_created(operation_id="b", order_id="ob", user_id=1,
                             tariff="pro", amount=1.0)
        store.mark_paid("b")
        result = store.iter_pending()
        ids = {r.operation_id for r in result}
        assert ids == {"a"}

    def test_returns_only_failed_excluded(self, store):
        store.record_created(operation_id="x", order_id="o", user_id=1,
                             tariff="pro", amount=1.0)
        store.mark_failed("x", "declined")
        assert store.iter_pending() == []

    def test_older_than_filter(self, store):
        # Только что созданная — не попадает в "old"
        store.record_created(operation_id="fresh", order_id="o", user_id=1,
                             tariff="pro", amount=1.0)
        assert store.iter_pending(older_than_seconds=10) == []
        # А с порогом 0 — попадает
        assert len(store.iter_pending(older_than_seconds=0)) == 1

    def test_max_age_filter_excludes_ancient(self, store, monkeypatch):
        # Создаём запись, потом подменяем created_at на старое значение
        store.record_created(operation_id="old", order_id="o", user_id=1,
                             tariff="pro", amount=1.0)
        # 2 дня назад
        from datetime import datetime, timezone, timedelta
        ancient = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        store._data[0]["created_at"] = ancient
        # max_age=24 часа — должен пропустить
        result = store.iter_pending(max_age_seconds=86400)
        assert result == []
        # Без max_age — попадает
        result_all = store.iter_pending()
        assert len(result_all) == 1

    def test_invalid_created_at_skipped(self, store):
        store.record_created(operation_id="x", order_id="o", user_id=1,
                             tariff="pro", amount=1.0)
        store._data[0]["created_at"] = "not-iso"
        # Не должно бросить
        assert store.iter_pending() == []


class TestPersistenceLoad:
    def test_load_existing_file(self, tmp_path):
        path = tmp_path / "p.json"
        with open(path, "w") as f:
            json.dump([{
                "operation_id": "op-old", "order_id": "o", "user_id": 1,
                "tariff": "pro", "amount": 1290.0, "kind": "initial",
                "status": "paid", "created_at": "2024-01-01T00:00:00+00:00",
                "paid_at": "2024-01-01T00:01:00+00:00", "error": "",
            }], f)
        store = PaymentsStore(filepath=str(path))
        rec = store.find_by_operation("op-old")
        assert rec.status == "paid"

    def test_corrupt_file_yields_empty(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text("not json")
        store = PaymentsStore(filepath=str(path))
        assert store.user_payments(1) == []
        assert store.total_revenue() == 0
