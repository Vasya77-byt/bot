"""Логика снимков и diff'ов для мониторинга изменений по ИНН.

Снимок — плоский dict со значимыми полями компании и проверки
безопасности. Diff возвращает список изменений с парами «было/стало»,
чтобы в уведомлении пользователю показать конкретику, а не «что-то
изменилось».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from schemas import CompanyData
from security_check import SecurityResult


# Поля, изменения которых считаются значимыми и попадают в diff.
# Порядок важен — он определяет порядок строк в уведомлении.
TRACKED_FIELDS: List[tuple[str, str]] = [
    ("name",         "Название"),
    ("status",       "Статус"),
    ("director",     "Руководитель"),
    ("address",      "Адрес"),
    ("ogrn",         "ОГРН"),
    ("kpp",          "КПП"),
    ("okved_main",   "ОКВЭД"),
    ("capital",      "Уставный капитал"),
    ("employees_count", "Штат"),
    ("fssp_count",   "Производств ФССП"),
    ("fssp_total_sum", "Сумма по ФССП"),
    ("risk_level",   "Уровень риска"),
]


@dataclass(frozen=True)
class FieldChange:
    field: str        # внутренний ключ
    label: str        # человекочитаемая подпись
    old: Any
    new: Any


def make_snapshot(
    company: Optional[CompanyData],
    security: Optional[SecurityResult] = None,
) -> Dict[str, Any]:
    """Собирает снимок значимых полей. None-значения сохраняются явно —
    важно отличать «не было данных» от «не проверяли»."""
    snapshot: Dict[str, Any] = {}

    if company is not None:
        snapshot["name"] = company.name
        snapshot["status"] = company.status
        snapshot["director"] = company.director
        snapshot["address"] = company.address
        snapshot["ogrn"] = company.ogrn
        snapshot["kpp"] = company.kpp
        snapshot["okved_main"] = company.okved_main
        snapshot["capital"] = company.capital
        snapshot["employees_count"] = company.employees_count
    else:
        for k in (
            "name", "status", "director", "address", "ogrn",
            "kpp", "okved_main", "capital", "employees_count",
        ):
            snapshot[k] = None

    if security is not None:
        snapshot["fssp_count"] = security.enforcement_count
        snapshot["fssp_total_sum"] = security.enforcement_total_sum
        snapshot["risk_level"] = security.risk_level
    else:
        snapshot["fssp_count"] = None
        snapshot["fssp_total_sum"] = None
        snapshot["risk_level"] = None

    return snapshot


def diff_snapshots(
    old: Optional[Dict[str, Any]], new: Dict[str, Any]
) -> List[FieldChange]:
    """Возвращает список реальных изменений по TRACKED_FIELDS.

    Если старого снимка нет (первая проверка) — список пуст: первичный
    замер не считается изменением.

    Поля, отсутствующие в новом снимке (например, не запрашивали FSSP),
    из diff'а исключаются — иначе появятся ложные «удалили данные».
    """
    if not old:
        return []

    changes: List[FieldChange] = []
    for key, label in TRACKED_FIELDS:
        if key not in new:
            continue  # нет новых данных по полю — пропускаем
        old_val = old.get(key)
        new_val = new[key]
        if old_val == new_val:
            continue
        # Игнорируем переход None → None (на случай странных стейтов)
        if old_val is None and new_val is None:
            continue
        changes.append(FieldChange(field=key, label=label, old=old_val, new=new_val))
    return changes


def format_change_message(inn: str, name: str, changes: List[FieldChange]) -> str:
    """Форматирует уведомление пользователю об изменениях."""
    if not changes:
        return ""
    title = name.strip() or inn
    lines = [
        f"🔔 Изменения по компании {title}",
        f"ИНН: {inn}",
        "",
    ]
    for ch in changes:
        old = _fmt_value(ch.old)
        new = _fmt_value(ch.new)
        lines.append(f"• {ch.label}: {old} → {new}")
    return "\n".join(lines)


def _fmt_value(value: Any) -> str:
    """Человекочитаемое представление поля для diff-уведомления."""
    if value is None:
        return "не было данных"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        # ФССП-суммы и капитал — оставим простую запись без округлений
        return f"{value:g}"
    return str(value)
