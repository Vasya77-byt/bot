"""Журнал платежей и рекуррентных списаний.

Храним все операции для отладки, возвратов и бухгалтерии.
Формат: JSON-массив записей.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("financial-architect")

STORAGE_FILE = os.getenv("PAYMENTS_FILE", "payments.json")


@dataclass
class PaymentRecord:
    operation_id: str
    order_id: str
    user_id: int
    tariff: str
    amount: float
    kind: str            # "initial" | "recurring"
    status: str          # "created" | "paid" | "failed" | "refunded"
    created_at: str
    paid_at: str = ""
    error: str = ""


class PaymentsStore:
    def __init__(self, filepath: str = STORAGE_FILE) -> None:
        self.filepath = filepath
        self._data: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as exc:
                logger.warning("PaymentsStore: failed to load %s: %s", self.filepath, exc)
                self._data = []

    def _save(self) -> None:
        try:
            dir_ = os.path.dirname(os.path.abspath(self.filepath))
            with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False,
                                            suffix=".tmp", encoding="utf-8") as tf:
                json.dump(self._data, tf, ensure_ascii=False, indent=2)
                tmp_path = tf.name
            os.replace(tmp_path, self.filepath)
        except Exception as exc:
            logger.error("PaymentsStore: failed to save %s: %s", self.filepath, exc)

    def record_created(
        self,
        *,
        operation_id: str,
        order_id: str,
        user_id: int,
        tariff: str,
        amount: float,
        kind: str = "initial",
    ) -> PaymentRecord:
        rec = PaymentRecord(
            operation_id=operation_id,
            order_id=order_id,
            user_id=user_id,
            tariff=tariff,
            amount=amount,
            kind=kind,
            status="created",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._data.append(asdict(rec))
        self._save()
        return rec

    def mark_paid(self, operation_id: str) -> Optional[PaymentRecord]:
        for rec in self._data:
            if rec.get("operation_id") == operation_id:
                rec["status"] = "paid"
                rec["paid_at"] = datetime.now(timezone.utc).isoformat()
                self._save()
                return PaymentRecord(**rec)
        return None

    def mark_failed(self, operation_id: str, error: str = "") -> Optional[PaymentRecord]:
        for rec in self._data:
            if rec.get("operation_id") == operation_id:
                rec["status"] = "failed"
                rec["error"] = error
                self._save()
                return PaymentRecord(**rec)
        return None

    def find_by_operation(self, operation_id: str) -> Optional[PaymentRecord]:
        for rec in self._data:
            if rec.get("operation_id") == operation_id:
                return PaymentRecord(**rec)
        return None

    def find_by_order(self, order_id: str) -> Optional[PaymentRecord]:
        for rec in self._data:
            if rec.get("order_id") == order_id:
                return PaymentRecord(**rec)
        return None

    def user_payments(self, user_id: int) -> list[PaymentRecord]:
        return [PaymentRecord(**r) for r in self._data if r.get("user_id") == user_id]

    def total_revenue(self) -> float:
        return sum(r.get("amount", 0) for r in self._data if r.get("status") == "paid")
