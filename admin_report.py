"""Аналитика для администратора: активные клиенты, выручка, тарифы.

Команда `/report` в боте: вызывает `build_report(...)` и отправляет
сообщение администратору. Доступ ограничен через ADMIN_IDS в Settings.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

from payments_store import PaymentsStore
from user_store import TARIFF_LABELS, UserProfile, UserStore


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float) -> str:
    """1234567.89 → '1 234 568'."""
    return f"{value:,.0f}".replace(",", " ")


def build_report(
    users: UserStore,
    payments: PaymentsStore,
    *,
    period_days: int = 30,
) -> str:
    """Формирует текстовый отчёт за последние period_days."""
    now = datetime.now(timezone.utc)
    period_start = now - timedelta(days=period_days)

    profiles: list[UserProfile] = list(users.iter_profiles())

    # Активные платные клиенты — те, у кого подписка ещё не истекла
    active_paid = [p for p in profiles if p.is_subscription_active() and p.tariff != "free"]
    free_users = [p for p in profiles if p.effective_tariff() == "free"]

    # Платежи за период
    paid_records = []
    for raw in payments._data:  # iterate over raw dicts; PaymentsStore не имеет публичного итератора
        if raw.get("status") != "paid":
            continue
        paid_at = _parse_iso(raw.get("paid_at", ""))
        if not paid_at:
            continue
        if paid_at >= period_start:
            paid_records.append(raw)

    total_amount = sum(float(r.get("amount", 0)) for r in paid_records)
    total_revenue_all_time = sum(
        float(r.get("amount", 0))
        for r in payments._data
        if r.get("status") == "paid"
    )

    tariff_counter: Counter[str] = Counter()
    for r in paid_records:
        tariff = r.get("tariff") or "—"
        tariff_counter[tariff] += 1

    # Самый часто оплачиваемый тариф
    if tariff_counter:
        top_tariff, top_count = tariff_counter.most_common(1)[0]
        top_tariff_label = TARIFF_LABELS.get(top_tariff, top_tariff)
        top_line = f"{top_tariff_label} — {top_count} оплат"
    else:
        top_line = "— (нет оплат за период)"

    # Распределение платежей по тарифам
    tariff_breakdown_lines = []
    for tariff, count in tariff_counter.most_common():
        label = TARIFF_LABELS.get(tariff, tariff)
        tariff_breakdown_lines.append(f"  • {label}: {count}")

    # Активные подписки по тарифам
    active_by_tariff: Counter[str] = Counter(p.tariff for p in active_paid)
    active_breakdown_lines = []
    for tariff in ("start", "pro", "business"):
        cnt = active_by_tariff.get(tariff, 0)
        label = TARIFF_LABELS.get(tariff, tariff)
        active_breakdown_lines.append(f"  • {label}: {cnt}")

    period_label = f"{period_start.strftime('%d.%m.%Y')} — {now.strftime('%d.%m.%Y')}"

    lines = [
        "📊 Админ-отчёт",
        f"Период: последние {period_days} дн. ({period_label})",
        "",
        f"👥 Всего пользователей: {len(profiles)}",
        f"⭐️ Активных платных клиентов: {len(active_paid)}",
        *active_breakdown_lines,
        f"🆓 На тарифе Free: {len(free_users)}",
        "",
        f"💰 Оплачено за период: {_fmt_money(total_amount)} ₽",
        f"   Платежей: {len(paid_records)}",
        f"💼 Выручка за всё время: {_fmt_money(total_revenue_all_time)} ₽",
        "",
        f"🏆 Самый частый тариф: {top_line}",
    ]
    if tariff_breakdown_lines:
        lines.append("Разбивка по оплатам:")
        lines.extend(tariff_breakdown_lines)

    return "\n".join(lines)


def is_admin(user_id: int, admin_ids: Iterable[int]) -> bool:
    return user_id in set(admin_ids)
