"""Админ-отчёт: финансы, клиенты, подписки, мониторинг, источники.

Запускается командой /admin (доступ только для пользователей из
ADMIN_USER_IDS). Все цифры считаются прямо при вызове из существующих
store'ов — без кеша, реалтайм.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from monitoring_store import MonitoringStore
from payments_store import PaymentsStore
from user_store import TARIFF_PRICES, UserStore
from zchb_client import ZchbClient

logger = logging.getLogger("financial-architect")

# Тарифы по убыванию старшинства — для подсчёта распределения
_TARIFF_ORDER = ("free", "start", "pro", "business")


def _start_of_month_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _fmt_money(amount: float) -> str:
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f} млн ₽"
    if amount >= 1_000:
        # «125 000 ₽»
        return f"{int(amount):,}".replace(",", " ") + " ₽"
    return f"{int(amount)} ₽"


def _file_size_kb(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return "—"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size // 1024} KB"


async def build_admin_report(
    users: UserStore,
    payments: PaymentsStore,
    monitoring: MonitoringStore,
    zchb: Optional[ZchbClient] = None,
) -> str:
    """Собирает админ-отчёт в одну строку для отправки в Telegram."""
    month_start = _start_of_month_utc()
    month_start_iso = month_start.isoformat()
    now = datetime.now(timezone.utc)

    # ── Сбор по пользователям ──
    profiles = list(users.iter_profiles())
    total_users = len(profiles)
    paid_active = 0
    auto_renew_count = 0
    cancelled_count = 0  # активная подписка, но auto_renew=False
    by_tariff: dict[str, int] = {t: 0 for t in _TARIFF_ORDER}
    new_registrations = 0
    referral_invited = 0
    referral_paid = 0
    expired_this_month = 0

    for p in profiles:
        # Новые регистрации (по дате принятия оферты)
        if p.accepted_offer_at and p.accepted_offer_at >= month_start_iso:
            new_registrations += 1

        # Реферальная статистика
        referral_invited += p.referrals_count
        referral_paid += p.referrals_paid_count

        # Активная подписка
        is_active = p.is_subscription_active()
        effective = p.effective_tariff()
        by_tariff[effective] = by_tariff.get(effective, 0) + 1

        if is_active and p.tariff != "free":
            paid_active += 1
            if p.auto_renew:
                auto_renew_count += 1
            else:
                cancelled_count += 1

        # Истёкшие за месяц без продления (тариф был платный, но
        # сейчас effective == free и tariff_expires_at в этом месяце)
        if (p.tariff != "free" and not is_active and p.tariff_expires_at):
            try:
                expires = datetime.fromisoformat(p.tariff_expires_at)
                if expires >= month_start and expires <= now:
                    expired_this_month += 1
            except ValueError:
                pass

    # ── Сбор по платежам ──
    paid_records = [r for r in payments.iter_all() if r.status == "paid"]
    month_paid = [
        r for r in paid_records
        if r.paid_at and r.paid_at >= month_start_iso
    ]
    month_amount = sum(r.amount for r in month_paid)
    month_count = len(month_paid)
    avg_check = (month_amount / month_count) if month_count else 0.0

    # Самый популярный тариф месяца
    tariff_counts_month: dict[str, int] = {}
    for r in month_paid:
        tariff_counts_month[r.tariff] = tariff_counts_month.get(r.tariff, 0) + 1
    if tariff_counts_month:
        top_tariff_name, top_tariff_count = max(
            tariff_counts_month.items(), key=lambda kv: kv[1],
        )
    else:
        top_tariff_name, top_tariff_count = "—", 0

    # MRR — сумма цен текущих активных платных подписок
    mrr = 0.0
    for p in profiles:
        if p.is_subscription_active() and p.tariff != "free":
            mrr += float(TARIFF_PRICES.get(p.tariff, 0))

    # Конверсия Free → Paid
    conversion = (paid_active / total_users * 100) if total_users else 0.0

    # ── Сбор по мониторингу ──
    all_monitoring = list(monitoring.iter_all())
    total_monitored = len(all_monitoring)
    unique_monitor_users = len({sub.user_id for sub in all_monitoring})

    # ── ZCHB stats ──
    zchb_block = ""
    if zchb is not None and zchb.enabled:
        try:
            stats = await zchb.get_stats()
        except Exception as exc:
            logger.warning("Admin: ZCHB stats failed: %s", exc)
            stats = None
        if stats:
            rem = stats.get("rem_request", "—")
            sumreq = stats.get("sum_request", "—")
            end = stats.get("end_date", "—")
            zchb_block = (
                f"• ZCHB: осталось {rem}, использовано {sumreq} "
                f"(тариф до {end})"
            )
        else:
            zchb_block = "• ZCHB: статистика недоступна"
    else:
        zchb_block = "• ZCHB: ключ не настроен"

    # ── Глобальные квоты API (Step 4: api_quota) ──
    # Локальный счётчик расхода бота — независим от ZCHB-stats апстрима
    # (показывает в т.ч. DaData/ФНС/SBIS/GigaChat, у которых нет своего
    # /stats endpoint).
    quota_lines: list[str] = []
    try:
        from api_quota import get_quota
        snap = get_quota().snapshot()
        for api_name, info in snap.items():
            limit = info["limit"]
            used = info["used"]
            if limit is None:
                quota_lines.append(f"• {api_name}: {used} (безлимит)")
                continue
            marker = ""
            if info["exhausted"]:
                marker = " ⛔️"
            elif info["near"]:
                marker = " ⚠️"
            quota_lines.append(
                f"• {api_name}: {used}/{limit} ({info['percent']:.0f}%){marker}",
            )
    except Exception as exc:
        logger.warning("Admin: api_quota snapshot failed: %s", exc)
        quota_lines = ["• Квоты API: недоступно"]
    quota_block = "\n".join(quota_lines)

    # ── Размер базы ──
    storage_dir = os.getenv("STORAGE_DIR", "storage")
    paths = {
        "users":      os.path.join(storage_dir, "users.json"),
        "payments":   os.getenv("PAYMENTS_FILE", "payments.json"),
        "monitoring": os.path.join(storage_dir, "monitoring.json"),
    }
    # Альтернативные пути если файлы лежат не в storage_dir
    for k in list(paths):
        if not os.path.exists(paths[k]):
            alt = paths[k].split("/")[-1]
            if os.path.exists(alt):
                paths[k] = alt

    db_block = " | ".join(
        f"{k}: {_file_size_kb(p)}" for k, p in paths.items()
    )

    # ── Сборка отчёта ──
    lines = [
        "🛠 Админ-отчёт MondayCompany",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "💰 ФИНАНСЫ (за месяц)",
        f"• Оплат: {month_count} на {_fmt_money(month_amount)}",
        f"• Самый популярный: {top_tariff_name} ({top_tariff_count} шт.)",
        f"• Средний чек: {_fmt_money(avg_check)}",
        f"• MRR (сейчас): {_fmt_money(mrr)}",
        "",
        "👥 КЛИЕНТЫ",
        f"• Всего: {total_users}",
        f"• Платных активных: {paid_active}",
        f"• Конверсия Free→Paid: {conversion:.1f}%",
        f"• Новых регистраций за месяц: {new_registrations}",
        "",
        "🔄 ПОДПИСКИ",
        f"• На автоплатеже: {auto_renew_count}",
        f"• Отменили автопродление: {cancelled_count}",
        f"• Истекло без продления за месяц: {expired_this_month}",
        "• Распределение: " + " / ".join(
            f"{t.title()} {by_tariff.get(t, 0)}" for t in _TARIFF_ORDER
        ),
        "",
        "👁 МОНИТОРИНГ",
        f"• Отслеживаемых ИНН: {total_monitored}",
        f"• Уникальных клиентов в мониторинге: {unique_monitor_users}",
        "",
        "🔌 ИСТОЧНИКИ ДАННЫХ",
        zchb_block,
        "",
        "📊 КВОТЫ API СЕГОДНЯ",
        quota_block,
        "",
        "🤝 РЕФЕРАЛЫ",
        f"• Приглашено: {referral_invited}",
        f"• Из них оплатили: {referral_paid} "
        f"({(referral_paid / referral_invited * 100):.0f}%)"
        if referral_invited else "• Из них оплатили: 0 (—)",
        "",
        "📊 БАЗА",
        f"• {db_block}",
    ]
    return "\n".join(lines)


def parse_admin_user_ids(value: str) -> set[int]:
    """Разбирает env-строку '12345,67890' в set[int]. Невалидные токены
    игнорируются."""
    out: set[int] = set()
    if not value:
        return out
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(int(token))
        except ValueError:
            logger.warning("ADMIN_USER_IDS: invalid token %r", token)
    return out
