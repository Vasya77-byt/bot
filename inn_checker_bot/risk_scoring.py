"""
Rule-based скоринговая модель для оценки риска контрагента.

Каждый фактор получает баллы риска (0 = нет риска, чем больше — тем хуже).
Суммарный балл определяет цвет светофора:
  0–15  → 🟢 Зелёный (низкий риск)
  16–35 → 🟡 Жёлтый (средний риск, ручная проверка)
  36+   → 🔴 Красный (высокий риск)
"""

from typing import Any

from config import RISK_GREEN_MAX, RISK_YELLOW_MAX


def calculate_risk_score(fields: dict[str, Any]) -> dict[str, Any]:
    """
    Считает суммарный risk score и возвращает:
    {
        "total_score": int,
        "color": "green" | "yellow" | "red",
        "emoji": "🟢" | "🟡" | "🔴",
        "label": "Низкий риск" | ...,
        "factors": [{"name": ..., "score": ..., "comment": ...}, ...]
    }
    """
    factors: list[dict[str, Any]] = []

    # --- 1. Статус компании (вес: до 50) ---
    status = fields.get("status")
    if status == "ACTIVE":
        factors.append({"name": "Статус компании", "score": 0, "comment": "Действующая"})
    elif status == "LIQUIDATING":
        factors.append({"name": "Статус компании", "score": 30, "comment": "В процессе ликвидации"})
    elif status == "LIQUIDATED":
        factors.append({"name": "Статус компании", "score": 50, "comment": "Ликвидирована"})
    elif status == "BANKRUPT":
        factors.append({"name": "Статус компании", "score": 50, "comment": "Банкротство"})
    elif status == "REORGANIZING":
        factors.append({"name": "Статус компании", "score": 15, "comment": "Реорганизация"})
    elif status is None:
        factors.append({"name": "Статус компании", "score": 10, "comment": "Статус неизвестен"})
    else:
        factors.append({"name": "Статус компании", "score": 10, "comment": f"Нетипичный статус: {status}"})

    # --- 2. Налоговая задолженность (вес: до 30) ---
    debt = fields.get("debt")
    if debt is not None:
        if debt == 0:
            factors.append({"name": "Налоговая задолженность", "score": 0, "comment": "Нет задолженности"})
        elif debt <= 100_000:
            factors.append({"name": "Налоговая задолженность", "score": 10, "comment": f"Долг: {_fmt_money(debt)}"})
        elif debt <= 1_000_000:
            factors.append({"name": "Налоговая задолженность", "score": 20, "comment": f"Долг: {_fmt_money(debt)}"})
        else:
            factors.append({"name": "Налоговая задолженность", "score": 30, "comment": f"Крупный долг: {_fmt_money(debt)}"})
    else:
        factors.append({"name": "Налоговая задолженность", "score": 5, "comment": "Нет данных"})

    # --- 3. Штрафы/пени (вес: до 15) ---
    penalty = fields.get("penalty")
    if penalty is not None:
        if penalty == 0:
            factors.append({"name": "Штрафы/пени", "score": 0, "comment": "Нет штрафов"})
        elif penalty <= 50_000:
            factors.append({"name": "Штрафы/пени", "score": 8, "comment": f"Штрафы: {_fmt_money(penalty)}"})
        else:
            factors.append({"name": "Штрафы/пени", "score": 15, "comment": f"Значительные штрафы: {_fmt_money(penalty)}"})
    # Если данных нет — не штрафуем, т.к. часто просто не заполнено

    # --- 4. Уставный капитал (вес: до 15) ---
    capital = fields.get("capital_value")
    if capital is not None:
        if capital >= 100_000:
            factors.append({"name": "Уставный капитал", "score": 0, "comment": _fmt_money(capital)})
        elif capital > 10_000:
            factors.append({"name": "Уставный капитал", "score": 5, "comment": f"Невысокий: {_fmt_money(capital)}"})
        elif capital == 10_000:
            factors.append({"name": "Уставный капитал", "score": 10, "comment": "Минимальный (10 000 ₽)"})
        else:
            factors.append({"name": "Уставный капитал", "score": 15, "comment": f"Подозрительно низкий: {_fmt_money(capital)}"})
    else:
        # Для некоторых ОПФ капитал не требуется
        factors.append({"name": "Уставный капитал", "score": 5, "comment": "Нет данных"})

    # --- 5. Возраст компании (вес: до 10) ---
    age = fields.get("company_age_years")
    if age is not None:
        if age >= 3:
            factors.append({"name": "Возраст компании", "score": 0, "comment": f"{age} лет"})
        elif age >= 1:
            factors.append({"name": "Возраст компании", "score": 5, "comment": f"{age} лет (молодая)"})
        else:
            factors.append({"name": "Возраст компании", "score": 10, "comment": f"{age} лет (очень молодая)"})
    else:
        factors.append({"name": "Возраст компании", "score": 5, "comment": "Нет данных о дате регистрации"})

    # --- 6. Штат сотрудников (вес: до 10) ---
    employees = fields.get("employee_count")
    if employees is not None:
        if employees > 5:
            factors.append({"name": "Штат сотрудников", "score": 0, "comment": f"{employees} чел."})
        elif employees >= 1:
            factors.append({"name": "Штат сотрудников", "score": 5, "comment": f"{employees} чел. (мало)"})
        else:
            factors.append({"name": "Штат сотрудников", "score": 10, "comment": "0 сотрудников"})
    else:
        factors.append({"name": "Штат сотрудников", "score": 3, "comment": "Нет данных"})

    # --- 7. Выручка (вес: до 10) ---
    income = fields.get("income")
    if income is not None:
        if income > 1_000_000:
            factors.append({"name": "Выручка", "score": 0, "comment": _fmt_money(income)})
        elif income > 100_000:
            factors.append({"name": "Выручка", "score": 3, "comment": f"Невысокая: {_fmt_money(income)}"})
        elif income > 0:
            factors.append({"name": "Выручка", "score": 7, "comment": f"Очень низкая: {_fmt_money(income)}"})
        else:
            factors.append({"name": "Выручка", "score": 10, "comment": "Нулевая выручка"})
    else:
        factors.append({"name": "Выручка", "score": 5, "comment": "Нет данных"})

    # --- 8. Адрес (вес: до 10) ---
    address = fields.get("address")
    if not address:
        factors.append({"name": "Юридический адрес", "score": 10, "comment": "Адрес не указан"})

    # Итоговый расчёт
    total_score = sum(f["score"] for f in factors)

    if total_score <= RISK_GREEN_MAX:
        color, emoji, label = "green", "🟢", "Низкий риск"
    elif total_score <= RISK_YELLOW_MAX:
        color, emoji, label = "yellow", "🟡", "Средний риск"
    else:
        color, emoji, label = "red", "🔴", "Высокий риск"

    return {
        "total_score": total_score,
        "color": color,
        "emoji": emoji,
        "label": label,
        "factors": factors,
    }


def _fmt_money(value: float | int | None) -> str:
    """Форматирует сумму в рубли."""
    if value is None:
        return "нет данных"
    if isinstance(value, float):
        return f"{value:,.0f} ₽".replace(",", " ")
    return f"{value:,} ₽".replace(",", " ")
