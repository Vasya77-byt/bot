import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from schemas import CarryoverSummary, Contract, Debt


class Ledger:
    """
    Реестр контрактов и задолженностей с разделением по учётным годам.
    Данные хранятся в JSON-файлах: ledger/contracts_YYYY.json, ledger/debts_YYYY.json.
    """

    def __init__(self, base_dir: Optional[str] = None, year: Optional[int] = None) -> None:
        self.base_dir = Path(base_dir or os.getenv("LEDGER_DIR", "ledger"))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.year = year or datetime.now().year

    # ── Пути к файлам ──

    def _contracts_path(self, year: Optional[int] = None) -> Path:
        return self.base_dir / f"contracts_{year or self.year}.json"

    def _debts_path(self, year: Optional[int] = None) -> Path:
        return self.base_dir / f"debts_{year or self.year}.json"

    def _carryover_path(self, year_from: int, year_to: int) -> Path:
        return self.base_dir / f"carryover_{year_from}_to_{year_to}.json"

    # ── Контракты ──

    def list_contracts(self, year: Optional[int] = None) -> List[Contract]:
        data = self._read_json(self._contracts_path(year))
        return [Contract(**c) for c in data]

    def add_contract(self, contract: Contract) -> Contract:
        if not contract.contract_id:
            contract = contract.copy(update={"contract_id": _new_id()})
        if not contract.year:
            contract = contract.copy(update={"year": self.year})
        contracts = self._read_json(self._contracts_path())
        contracts.append(contract.dict())
        self._write_json(self._contracts_path(), contracts)
        return contract

    def get_active_contracts(self, year: Optional[int] = None) -> List[Contract]:
        return [c for c in self.list_contracts(year) if c.status == "active"]

    # ── Долги ──

    def list_debts(self, year: Optional[int] = None) -> List[Debt]:
        data = self._read_json(self._debts_path(year))
        return [Debt(**d) for d in data]

    def add_debt(self, debt: Debt) -> Debt:
        if not debt.debt_id:
            debt = debt.copy(update={"debt_id": _new_id()})
        if not debt.origin_year:
            debt = debt.copy(update={"origin_year": self.year})
        debts = self._read_json(self._debts_path())
        debts.append(debt.dict())
        self._write_json(self._debts_path(), debts)
        return debt

    def get_outstanding_debts(self, year: Optional[int] = None) -> List[Debt]:
        return [d for d in self.list_debts(year) if d.status in ("outstanding", "overdue")]

    # ── Сводки ──

    def total_receivables(self, year: Optional[int] = None) -> float:
        return sum(
            d.amount or 0.0
            for d in self.get_outstanding_debts(year)
            if d.direction == "receivable"
        )

    def total_payables(self, year: Optional[int] = None) -> float:
        return sum(
            d.amount or 0.0
            for d in self.get_outstanding_debts(year)
            if d.direction == "payable"
        )

    def contracts_remaining_total(self, year: Optional[int] = None) -> float:
        return sum(c.remaining_amount or 0.0 for c in self.get_active_contracts(year))

    # ── Миграция на новый год ──

    def migrate_to_new_year(self, year_from: int, year_to: int) -> CarryoverSummary:
        """
        Переносит активные контракты и непогашенные долги из year_from в year_to.
        Создаёт файл carryover-сводки и новые файлы реестра для year_to.
        """
        active_contracts = self.get_active_contracts(year_from)
        outstanding_debts = self.get_outstanding_debts(year_from)

        # Переносим контракты с обновлённым годом
        migrated_contracts: List[Contract] = []
        for c in active_contracts:
            migrated = c.copy(update={
                "year": year_to,
                "notes": f"Перенос из {year_from}. {c.notes or ''}".strip(),
            })
            migrated_contracts.append(migrated)

        # Переносим долги с обновлённым годом
        migrated_debts: List[Debt] = []
        for d in outstanding_debts:
            migrated = d.copy(update={
                "origin_year": year_to,
                "description": f"Перенос из {year_from}. {d.description or ''}".strip(),
            })
            migrated_debts.append(migrated)

        # Записываем в файлы нового года
        self._write_json(
            self._contracts_path(year_to),
            [c.dict() for c in migrated_contracts],
        )
        self._write_json(
            self._debts_path(year_to),
            [d.dict() for d in migrated_debts],
        )

        total_recv = sum(
            d.amount or 0.0 for d in migrated_debts if d.direction == "receivable"
        )
        total_pay = sum(
            d.amount or 0.0 for d in migrated_debts if d.direction == "payable"
        )

        summary = CarryoverSummary(
            year_from=year_from,
            year_to=year_to,
            active_contracts=migrated_contracts,
            outstanding_debts=migrated_debts,
            total_receivables=total_recv,
            total_payables=total_pay,
            notes=f"Миграция выполнена {datetime.now().isoformat()}",
        )

        self._write_json(
            self._carryover_path(year_from, year_to),
            summary.dict(),
        )

        return summary

    def get_carryover_summary(self, year_from: int, year_to: int) -> Optional[CarryoverSummary]:
        path = self._carryover_path(year_from, year_to)
        if not path.exists():
            return None
        data = self._read_json_obj(path)
        if not data:
            return None
        return CarryoverSummary(**data)

    # ── I/O ──

    def _read_json(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _read_json_obj(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_json(self, path: Path, data: Any) -> None:
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _new_id() -> str:
    return uuid.uuid4().hex[:12]
