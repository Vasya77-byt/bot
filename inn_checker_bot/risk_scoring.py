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


def calculate_risk_score(
    fields: dict[str, Any],
    zchb_data: dict[str, Any] | None = None,
    fns_data: dict[str, Any] | None = None,
    sanctions_data: dict[str, Any] | None = None,
    cbrf_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Собственный Risk Engine. Считает суммарный risk score из ВСЕХ источников.
    Возвращает светофор + список причин с баллами.
    """
    zchb = zchb_data or {}
    fns = fns_data or {}
    sanctions = sanctions_data or {}
    cbrf = cbrf_data or {}
    check = fns.get("check") or {}
    nalogbi = fns.get("nalogbi") or {}
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

    # --- 2. Налоговая задолженность (DaData + ФНС — объединяем) ---
    debt = fields.get("debt")
    fns_tax_debt = check.get("tax_debt")  # bool из ФНС
    if debt is not None and debt > 0:
        if debt <= 100_000:
            factors.append({"name": "Налоговая задолженность", "score": 10, "comment": f"Долг: {_fmt_money(debt)}"})
        elif debt <= 1_000_000:
            factors.append({"name": "Налоговая задолженность", "score": 20, "comment": f"Долг: {_fmt_money(debt)}"})
        else:
            factors.append({"name": "Налоговая задолженность", "score": 30, "comment": f"Крупный долг: {_fmt_money(debt)}"})
    elif fns_tax_debt:
        # ФНС подтверждает задолженность, но сумма неизвестна
        factors.append({"name": "Налоговая задолженность", "score": 20, "comment": "Есть (ФНС)"})
    elif debt is not None and debt == 0:
        factors.append({"name": "Налоговая задолженность", "score": 0, "comment": "Нет"})
    # Если данных нет — НЕ показываем «Нет данных» (не штрафуем за отсутствие)

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
    # Если капитал неизвестен — не штрафуем

    # --- 5. Возраст компании (вес: до 10) ---
    age = fields.get("company_age_years")
    if age is not None:
        if age >= 3:
            pass  # Норма — не показываем
        elif age >= 1:
            factors.append({"name": "Возраст компании", "score": 5, "comment": f"{age} лет (молодая)"})
        else:
            factors.append({"name": "Возраст компании", "score": 10, "comment": f"{age} лет (очень молодая)"})
    # Если нет данных — не показываем

    # --- 6. Штат сотрудников (вес: до 10) ---
    employees = zchb.get("employee_count") or fields.get("employee_count")
    if employees is not None:
        if employees > 5:
            pass  # Норма — не показываем
        elif employees >= 1:
            factors.append({"name": "Штат сотрудников", "score": 5, "comment": f"{employees} чел. (мало)"})
        else:
            factors.append({"name": "Штат сотрудников", "score": 10, "comment": "0 сотрудников"})
    # Если нет данных — не показываем, не штрафуем

    # --- 7. Выручка (вес: до 10) ---
    income = zchb.get("revenue") or fields.get("income")
    if income is not None:
        if income > 1_000_000:
            pass  # Норма — не показываем
        elif income > 100_000:
            factors.append({"name": "Выручка", "score": 3, "comment": f"Невысокая: {_fmt_money(income)}"})
        elif income > 0:
            factors.append({"name": "Выручка", "score": 7, "comment": f"Очень низкая: {_fmt_money(income)}"})
        else:
            factors.append({"name": "Выручка", "score": 10, "comment": "Нулевая"})
    # Если нет данных — не показываем

    # --- 8. Адрес (вес: до 10) ---
    address = fields.get("address") or fields.get("city")
    if not address:
        factors.append({"name": "Юридический адрес", "score": 10, "comment": "Не указан"})

    # --- 9. Суды (ЗЧБ API, вес: до 25) ---
    courts_total = zchb.get("courts_total")
    courts_defendant = zchb.get("courts_defendant", 0)
    if courts_total is not None:
        if courts_total == 0:
            factors.append({"name": "Судебные дела", "score": 0, "comment": "Нет"})
        elif courts_defendant > 50:
            factors.append({"name": "Судебные дела", "score": 25, "comment": f"Ответчик в {courts_defendant} делах!"})
        elif courts_defendant > 10:
            factors.append({"name": "Судебные дела", "score": 15, "comment": f"Ответчик в {courts_defendant} делах"})
        elif courts_total > 0:
            factors.append({"name": "Судебные дела", "score": 5, "comment": f"{courts_total} дел (незначительно)"})

    # --- 10. ФССП (ЗЧБ API, вес: до 20) ---
    fssp = zchb.get("fssp_count")
    if fssp is not None:
        if fssp == 0:
            factors.append({"name": "ФССП", "score": 0, "comment": "Нет производств"})
        elif fssp > 10:
            factors.append({"name": "ФССП", "score": 20, "comment": f"{fssp} исп. производств!"})
        elif fssp > 0:
            factors.append({"name": "ФССП", "score": 10, "comment": f"{fssp} исп. производств"})

    # --- 11. Массовый адрес / директор (ФНС, вес: до 20 каждый) ---
    if check.get("mass_address"):
        factors.append({"name": "Массовый адрес", "score": 15, "comment": check.get("mass_address_detail", "Да")})
    if check.get("mass_director"):
        factors.append({"name": "Массовый руководитель", "score": 15, "comment": check.get("mass_director_detail", "Да")})
    if check.get("unreliable_address"):
        factors.append({"name": "Недостоверный адрес", "score": 20, "comment": "Отметка ФНС"})
    if check.get("unreliable_director"):
        factors.append({"name": "Недостоверный руководитель", "score": 25, "comment": "Отметка ФНС"})
    if check.get("disqualified"):
        factors.append({"name": "Дисквалификация", "score": 40, "comment": "Руководитель дисквалифицирован!"})
    # tax_debt уже обработан в секции 2 (объединён с DaData debt)
    if check.get("no_reports"):
        factors.append({"name": "Не сдаёт отчётность", "score": 25, "comment": "Не сдаёт отчётность в ФНС"})
    if check.get("liquidation_decision"):
        factors.append({"name": "Решение о ликвидации", "score": 40, "comment": "Есть решение!"})

    # --- 12. Блокировка счетов ФНС (вес: до 30) ---
    if nalogbi.get("has_blocked_accounts"):
        cnt = nalogbi.get("blocked_accounts_count", 0)
        factors.append({"name": "Блокировка счетов", "score": 30, "comment": f"{cnt} решений ФНС"})

    # --- 13. Террорист / экстремист (вес: 100) ---
    if zchb.get("terrorist"):
        factors.append({"name": "ТЕРРОРИСТ/ЭКСТРЕМИСТ", "score": 100, "comment": "В реестре Росфинмониторинга!"})

    # --- 14. Недобросовестный поставщик (вес: 30) ---
    if zchb.get("bad_supplier"):
        factors.append({"name": "Недобросовестный поставщик", "score": 30, "comment": "В реестре ЕИС"})

    # --- 15. Санкции (вес: 20) ---
    if sanctions.get("found"):
        factors.append({"name": "Санкции", "score": 20, "comment": "Найден в санкционных списках"})

    # --- 16. Отказы банков ЦБ (вес: 25) ---
    if cbrf.get("found"):
        cnt = cbrf.get("count", 0)
        factors.append({"name": "Отказы банков (115-ФЗ)", "score": 25, "comment": f"{cnt} отказов"})

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
