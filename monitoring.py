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


CRITICAL_FIELDS = {"status", "risk_level"}


# ── D3: Категоризация изменений для богатых уведомлений ────────────────
# Каждое изменение классифицируется по «событию» — банкротство, смена
# директора, новые иски ФССП и т.д. Это позволяет:
# - использовать правильный эмодзи и заголовок секции
# - сортировать по серьёзности (critical → warning → info)
# - расширять список событий по мере добавления полей в TRACKED_FIELDS


class EventCategory:
    """Тип события мониторинга. Не Enum: dataclass-проще в матчинге
    и сериализации через .value-строку."""
    BANKRUPTCY       = "bankruptcy"
    LIQUIDATION      = "liquidation"
    REORG            = "reorganization"
    DIRECTOR_CHANGE  = "director_change"
    NEW_LAWSUITS     = "new_lawsuits"
    LAWSUITS_DECREASE = "lawsuits_decrease"  # позитивное событие
    RISK_INCREASE    = "risk_increase"
    RISK_DECREASE    = "risk_decrease"
    CAPITAL_DECREASE = "capital_decrease"
    CAPITAL_INCREASE = "capital_increase"
    ADDRESS_CHANGE   = "address_change"
    NAME_CHANGE      = "name_change"
    OKVED_CHANGE     = "okved_change"
    KPP_CHANGE       = "kpp_change"
    STAFF_CHANGE     = "staff_change"
    OTHER            = "other"


# severity → визуальный приоритет:
# critical: красный 🚨 — реакция нужна сразу (банкротство/ликвидация/CRIT-риск)
# warning:  жёлтый ⚠️ — стоит проверить (новые иски, смена директора)
# info:     синий  🔔 — для информации (новое имя, ОКВЭД)
# positive: зелёный ✅ — улучшение состояния
_CATEGORY_META: Dict[str, Dict[str, str]] = {
    EventCategory.BANKRUPTCY: {
        "emoji": "🚨", "severity": "critical",
        "title": "Банкротство",
    },
    EventCategory.LIQUIDATION: {
        "emoji": "🚨", "severity": "critical",
        "title": "Ликвидация",
    },
    EventCategory.REORG: {
        "emoji": "⚠️", "severity": "warning",
        "title": "Реорганизация",
    },
    EventCategory.DIRECTOR_CHANGE: {
        "emoji": "👤", "severity": "warning",
        "title": "Смена руководителя",
    },
    EventCategory.NEW_LAWSUITS: {
        "emoji": "⚖️", "severity": "warning",
        "title": "Новые производства ФССП",
    },
    EventCategory.LAWSUITS_DECREASE: {
        "emoji": "✅", "severity": "positive",
        "title": "Производств ФССП стало меньше",
    },
    EventCategory.RISK_INCREASE: {
        "emoji": "📈", "severity": "critical",
        "title": "Уровень риска вырос",
    },
    EventCategory.RISK_DECREASE: {
        "emoji": "📉", "severity": "positive",
        "title": "Уровень риска снизился",
    },
    EventCategory.CAPITAL_DECREASE: {
        "emoji": "💰", "severity": "warning",
        "title": "Уставный капитал уменьшился",
    },
    EventCategory.CAPITAL_INCREASE: {
        "emoji": "💰", "severity": "info",
        "title": "Уставный капитал увеличился",
    },
    EventCategory.ADDRESS_CHANGE: {
        "emoji": "📍", "severity": "info",
        "title": "Смена юр. адреса",
    },
    EventCategory.NAME_CHANGE: {
        "emoji": "🏢", "severity": "info",
        "title": "Смена названия",
    },
    EventCategory.OKVED_CHANGE: {
        "emoji": "💼", "severity": "info",
        "title": "Смена основного ОКВЭД",
    },
    EventCategory.KPP_CHANGE: {
        "emoji": "📋", "severity": "info",
        "title": "Смена КПП",
    },
    EventCategory.STAFF_CHANGE: {
        "emoji": "👥", "severity": "info",
        "title": "Изменение штата",
    },
    EventCategory.OTHER: {
        "emoji": "🔔", "severity": "info",
        "title": "Прочие изменения",
    },
}

# Ранг уровней риска для сравнения risk_level → risk_level.
_RISK_RANK: Dict[str, int] = {
    "low": 1, "низкий": 1,
    "medium": 2, "средний": 2, "med": 2,
    "high": 3, "высокий": 3,
    "critical": 4, "критический": 4, "crit": 4,
}


def categorize_change(change: FieldChange) -> str:
    """Возвращает строку-категорию EventCategory для одного изменения.

    Логика по полю:
    - status: распознаём «банкрот», «ликвид», «реорганизац» по подстроке
    - director: всегда DIRECTOR_CHANGE
    - fssp_count: рост → NEW_LAWSUITS, падение → LAWSUITS_DECREASE
    - risk_level: сравнение по _RISK_RANK
    - capital: уменьшение → CAPITAL_DECREASE, увеличение → CAPITAL_INCREASE
    - address/name/okved_main/kpp/employees_count → каждый свой
    - всё прочее → OTHER
    """
    f = change.field
    if f == "status":
        new_lower = str(change.new or "").lower()
        if "банкрот" in new_lower:
            return EventCategory.BANKRUPTCY
        if "ликвид" in new_lower:
            return EventCategory.LIQUIDATION
        if "реорганиз" in new_lower:
            return EventCategory.REORG
        return EventCategory.OTHER
    if f == "director":
        return EventCategory.DIRECTOR_CHANGE
    if f == "fssp_count":
        try:
            old_n = int(change.old or 0)
            new_n = int(change.new or 0)
            if new_n > old_n:
                return EventCategory.NEW_LAWSUITS
            if new_n < old_n:
                return EventCategory.LAWSUITS_DECREASE
        except (TypeError, ValueError):
            pass
        return EventCategory.OTHER
    if f == "risk_level":
        old_rank = _RISK_RANK.get(str(change.old or "").lower(), 0)
        new_rank = _RISK_RANK.get(str(change.new or "").lower(), 0)
        if new_rank > old_rank:
            return EventCategory.RISK_INCREASE
        if new_rank < old_rank and old_rank > 0:
            return EventCategory.RISK_DECREASE
        return EventCategory.OTHER
    if f == "capital":
        try:
            old_v = float(change.old or 0)
            new_v = float(change.new or 0)
            if new_v < old_v:
                return EventCategory.CAPITAL_DECREASE
            if new_v > old_v:
                return EventCategory.CAPITAL_INCREASE
        except (TypeError, ValueError):
            pass
        return EventCategory.OTHER
    if f == "address":
        return EventCategory.ADDRESS_CHANGE
    if f == "name":
        return EventCategory.NAME_CHANGE
    if f == "okved_main":
        return EventCategory.OKVED_CHANGE
    if f == "kpp":
        return EventCategory.KPP_CHANGE
    if f == "employees_count":
        return EventCategory.STAFF_CHANGE
    return EventCategory.OTHER


# Порядок секций в уведомлении — по серьёзности.
_SEVERITY_ORDER: Dict[str, int] = {
    "critical": 0, "warning": 1, "info": 2, "positive": 3,
}


def format_change_message(inn: str, name: str, changes: List[FieldChange]) -> str:
    """Форматирует категоризированное уведомление об изменениях (D3).

    Изменения группируются по EventCategory, секции упорядочиваются
    по серьёзности (critical → warning → info → positive). Заголовок
    использует общий эмодзи самой серьёзной категории.

    Если изменений нет — пустая строка (вызывающий код не должен
    отправлять пустое уведомление).
    """
    if not changes:
        return ""
    title = name.strip() or inn

    # Группировка
    by_category: Dict[str, List[FieldChange]] = {}
    for ch in changes:
        cat = categorize_change(ch)
        by_category.setdefault(cat, []).append(ch)

    # Общий эмодзи и тон — по самой серьёзной категории
    severities = {_CATEGORY_META[c]["severity"] for c in by_category}
    if "critical" in severities:
        overall_emoji = "🚨"
        overall_word = "Серьёзное изменение"
    elif "warning" in severities:
        overall_emoji = "⚠️"
        overall_word = "Внимание: изменения"
    elif "positive" in severities:
        overall_emoji = "✅"
        overall_word = "Положительные изменения"
    else:
        overall_emoji = "🔔"
        overall_word = "Изменения"

    lines = [
        f"{overall_emoji} {overall_word} по компании {title}",
        f"ИНН: {inn}",
        "",
    ]

    # Сортируем категории по серьёзности (critical first), внутри — по
    # порядку в _CATEGORY_META (стабильно).
    sorted_cats = sorted(
        by_category.keys(),
        key=lambda c: _SEVERITY_ORDER.get(_CATEGORY_META[c]["severity"], 99),
    )

    for cat in sorted_cats:
        meta = _CATEGORY_META[cat]
        lines.append(f"{meta['emoji']} {meta['title']}")
        for ch in by_category[cat]:
            old = _fmt_value(ch.old)
            new = _fmt_value(ch.new)
            lines.append(f"  {ch.label}: {old} → {new}")
        lines.append("")

    # Подсказка для критичных событий — что делать
    if "critical" in severities:
        lines.append("⚡️ Рекомендуем проверить статус контракта и приостановить расчёты.")

    return "\n".join(lines).rstrip()


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
