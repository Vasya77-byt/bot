"""
Счётчик запросов счетов — хранит в SQLite (потокобезопасно).
"""
import sqlite3
import os
from datetime import date

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "counters.db")


def _ensure_db():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS counters (
            name TEXT PRIMARY KEY,
            total INTEGER DEFAULT 0,
            today_date TEXT,
            today_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO counters (name, total, today_date, today_count)
        VALUES ('invoices', 0, ?, 0)
    """, (str(date.today()),))
    conn.commit()
    conn.close()


_ensure_db()


def next_invoice() -> dict:
    """
    Увеличивает счётчики атомарно и возвращает:
    {"number": 13, "today": 4}
    """
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    today = str(date.today())
    try:
        conn.execute("""
            UPDATE counters SET
                total = total + 1,
                today_count = CASE WHEN today_date = ? THEN today_count + 1 ELSE 1 END,
                today_date = ?
            WHERE name = 'invoices'
        """, (today, today))
        conn.commit()
        row = conn.execute("SELECT total, today_count FROM counters WHERE name = 'invoices'").fetchone()
        return {"number": row["total"], "today": row["today_count"]}
    finally:
        conn.close()


def get_invoice_stats() -> dict:
    """Текущая статистика без изменения счётчиков."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    today = str(date.today())
    try:
        row = conn.execute("SELECT total, today_date, today_count FROM counters WHERE name = 'invoices'").fetchone()
        if not row:
            return {"total": 0, "today": 0, "today_date": today}
        today_count = row["today_count"] if row["today_date"] == today else 0
        return {"total": row["total"], "today": today_count, "today_date": today}
    finally:
        conn.close()
