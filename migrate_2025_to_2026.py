#!/usr/bin/env python3
"""
Скрипт миграции данных учёта 2025 → 2026.

Выполняет:
1. Архивирование метаданных КП за 2025 год
2. Перенос активных контрактов из 2025 в 2026
3. Перенос непогашенных задолженностей (дебиторская/кредиторская)
4. Очистка устаревшего кеша SBIS
5. Генерация сводки переноса (carryover summary)

Запуск:
    python migrate_2025_to_2026.py [--dry-run]
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from ledger import Ledger
from metadata_store import MetadataStore


YEAR_FROM = 2025
YEAR_TO = 2026


def migrate(dry_run: bool = False) -> None:
    print(f"{'[DRY RUN] ' if dry_run else ''}Миграция данных: {YEAR_FROM} → {YEAR_TO}")
    print("=" * 60)

    # 1. Архивация метаданных КП
    print("\n1. Архивация метаданных КП за 2025 год...")
    meta = MetadataStore(year=YEAR_FROM)
    records_2025 = meta.read_records(YEAR_FROM)
    print(f"   Записей метаданных за {YEAR_FROM}: {len(records_2025)}")
    if not dry_run:
        archived = meta.archive_year(YEAR_FROM)
        if archived:
            print(f"   Архив создан: {archived}")
        else:
            print("   Нет данных для архивации (или уже заархивировано)")

    # 2. Перенос контрактов и долгов
    print(f"\n2. Перенос данных реестра из {YEAR_FROM} в {YEAR_TO}...")
    ledger = Ledger()

    contracts_2025 = ledger.list_contracts(YEAR_FROM)
    active_contracts = ledger.get_active_contracts(YEAR_FROM)
    debts_2025 = ledger.list_debts(YEAR_FROM)
    outstanding_debts = ledger.get_outstanding_debts(YEAR_FROM)

    print(f"   Контрактов за {YEAR_FROM}: {len(contracts_2025)} (активных: {len(active_contracts)})")
    print(f"   Задолженностей за {YEAR_FROM}: {len(debts_2025)} (непогашенных: {len(outstanding_debts)})")

    if active_contracts:
        print(f"\n   Активные контракты для переноса:")
        for c in active_contracts:
            remaining = f"{c.remaining_amount:,.0f}" if c.remaining_amount else "—"
            print(f"     • {c.counterparty} (ИНН {c.inn}) — остаток: {remaining}")

    if outstanding_debts:
        print(f"\n   Непогашенные задолженности для переноса:")
        for d in outstanding_debts:
            direction = "нам должны" if d.direction == "receivable" else "мы должны"
            print(f"     • {d.counterparty} (ИНН {d.inn}) — {d.amount:,.0f} ({direction})")

    if not dry_run:
        summary = ledger.migrate_to_new_year(YEAR_FROM, YEAR_TO)
        print(f"\n   Перенесено контрактов: {len(summary.active_contracts)}")
        print(f"   Перенесено задолженностей: {len(summary.outstanding_debts)}")
        print(f"   Дебиторская задолженность: {summary.total_receivables:,.0f}")
        print(f"   Кредиторская задолженность: {summary.total_payables:,.0f}")

    # 3. Очистка кеша SBIS
    print(f"\n3. Очистка кеша SBIS...")
    cache_path = Path(".cache/sbis_cache.json")
    if cache_path.exists():
        if not dry_run:
            archive_cache = Path(f".cache/sbis_cache_{YEAR_FROM}_archive.json")
            shutil.copy2(str(cache_path), str(archive_cache))
            cache_path.write_text("{}", encoding="utf-8")
            print(f"   Кеш заархивирован в {archive_cache} и очищен")
        else:
            print(f"   Кеш будет заархивирован и очищен")
    else:
        print("   Кеш не найден — пропускаем")

    # 4. Создание чистых директорий для 2026
    print(f"\n4. Подготовка директорий для {YEAR_TO}...")
    storage_2026 = Path(f"storage/{YEAR_TO}")
    if not dry_run:
        storage_2026.mkdir(parents=True, exist_ok=True)
        print(f"   Создана директория: {storage_2026}")
    else:
        print(f"   Будет создана директория: {storage_2026}")

    # Итог
    print("\n" + "=" * 60)
    if dry_run:
        print("[DRY RUN] Миграция не выполнена. Запустите без --dry-run для применения.")
    else:
        print(f"Миграция {YEAR_FROM} → {YEAR_TO} завершена успешно!")
        print(f"Дата выполнения: {datetime.now().isoformat()}")
        print(f"\nСистема готова к учёту за {YEAR_TO} год.")
        print("Новые команды бота:")
        print("  /ledger — сводка учёта")
        print("  /contracts — список контрактов")
        print("  /debts — задолженности")
        print("  /add_contract — добавить контракт")
        print("  /add_debt — добавить задолженность")
        print("  /carryover — сводка переноса из прошлого года")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"Миграция учёта {YEAR_FROM} → {YEAR_TO}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать что будет сделано, без реального выполнения",
    )
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
