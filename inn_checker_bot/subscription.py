"""
Управление подписками — проверка лимитов, определение доступа.
"""

import asyncio
import logging
from functools import partial
from typing import Any

from config import PLAN_LIMITS
import database as db

logger = logging.getLogger(__name__)


async def _run(func, *args, **kwargs):
    """Запуск синхронной функции в executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def _check_access_sync(user_id: int) -> dict[str, Any]:
    """
    Проверяет доступ пользователя перед проверкой (синхронная).

    Возвращает dict:
      allowed: True/False — можно ли делать проверку
      plan: текущий план
      full_report: True/False — показывать полный отчёт
      checks_today: сколько проверок сегодня
      checks_limit: дневной лимит
      checks_remaining: осталось проверок
      promo_checks: промо-проверок осталось
      is_promo: эта проверка по промокоду
      message: текст ошибки (если allowed=False)
    """
    plan = db.get_user_plan(user_id)
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    checks_today = db.get_checks_today(user_id)
    promo_remaining = db.get_promo_checks_remaining(user_id)
    daily_limit = limits["daily_checks"]

    result = {
        "plan": plan,
        "full_report": limits["full_report"],
        "ai_analysis": limits["ai_analysis"],
        "egrul": limits["egrul"],
        "checks_today": checks_today,
        "checks_limit": daily_limit,
        "promo_checks": promo_remaining,
        "is_promo": False,
    }

    # Админ — всегда можно
    if plan == "admin":
        result["allowed"] = True
        result["checks_remaining"] = 9999
        return result

    # Платный план — проверяем дневной лимит
    if plan in ("pro", "business"):
        if checks_today >= daily_limit:
            result["allowed"] = False
            result["checks_remaining"] = 0
            result["message"] = (
                f"❌ Дневной лимит исчерпан ({daily_limit}/{daily_limit})\n"
                "Проверки обновятся завтра."
            )
            return result
        result["allowed"] = True
        result["checks_remaining"] = daily_limit - checks_today
        return result

    # Free план — проверяем промокод → потом дневной лимит
    if promo_remaining > 0:
        # Есть промо-проверки — используем их (полный отчёт!)
        result["allowed"] = True
        result["is_promo"] = True
        result["full_report"] = True
        result["ai_analysis"] = True
        result["egrul"] = True
        result["checks_remaining"] = promo_remaining
        return result

    # Обычный free лимит
    if checks_today >= daily_limit:
        result["allowed"] = False
        result["checks_remaining"] = 0
        result["message"] = (
            f"❌ Лимит бесплатных проверок исчерпан ({daily_limit}/{daily_limit})\n\n"
            "Хотите больше проверок?\n"
            "Введите /tariffs — посмотреть тарифы\n"
            "Введите /promo — активировать промокод\n\n"
            "Проверки обновятся завтра."
        )
        return result

    result["allowed"] = True
    result["checks_remaining"] = daily_limit - checks_today
    return result


async def check_access(user_id: int) -> dict[str, Any]:
    """Async-обёртка для проверки доступа."""
    return await _run(_check_access_sync, user_id)


def _after_check_sync(user_id: int, access: dict[str, Any]) -> str:
    """
    Вызывается ПОСЛЕ проверки. Списывает лимиты (синхронная).
    Возвращает текст для отображения под отчётом.
    """
    # Списываем промо-проверку если по промокоду
    if access.get("is_promo"):
        db.use_promo_check(user_id)
        promo_left = db.get_promo_checks_remaining(user_id)
        db.increment_check(user_id)
        return f"🎁 Промо-проверка • Осталось: {promo_left}"

    # Обычное списание
    user = db.increment_check(user_id)
    plan = access["plan"]
    checks = user["checks_today"]
    limit = access["checks_limit"]
    remaining = max(0, limit - checks)

    if plan == "admin":
        return "⭐ Админ • Безлимитный доступ"

    plan_icons = {"free": "🆓", "pro": "💎", "business": "🏆"}
    plan_names = {"free": "Free", "pro": "Pro", "business": "Business"}
    icon = plan_icons.get(plan, "📋")
    name = plan_names.get(plan, plan)

    parts = [f"{icon} {name} • Проверок сегодня: {checks}/{limit}"]

    if plan == "free" and remaining <= 2:
        parts.append(f"⚡ Осталось {remaining}. /tariffs — больше проверок")

    # Показываем дату истечения для платных
    if plan in ("pro", "business"):
        user_data = db.get_user(user_id)
        if user_data and user_data.get("plan_expires"):
            try:
                from datetime import datetime
                exp = datetime.fromisoformat(user_data["plan_expires"])
                parts.append(f"📅 Подписка до {exp.strftime('%d.%m.%Y')}")
            except (ValueError, TypeError):
                pass

    return " • ".join(parts) if len(parts) == 1 else "\n".join(parts)


async def after_check(user_id: int, access: dict[str, Any]) -> str:
    """Async-обёртка для списания лимитов."""
    return await _run(_after_check_sync, user_id, access)


def _get_profile_text_sync(user_id: int) -> str:
    """Текст профиля пользователя (синхронная)."""
    user = db.ensure_user(user_id)
    plan = db.get_user_plan(user_id)
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    checks_today = db.get_checks_today(user_id)
    promo_left = db.get_promo_checks_remaining(user_id)

    plan_icons = {
        "free": "🆓", "pro": "💎", "business": "🏆", "admin": "⭐"
    }
    plan_names = {
        "free": "Free", "pro": "Pro", "business": "Business", "admin": "Admin"
    }

    icon = plan_icons.get(plan, "📋")
    name = plan_names.get(plan, plan)
    daily = limits["daily_checks"]
    remaining = max(0, daily - checks_today)

    lines = [
        f"👤 <b>Ваш профиль</b>",
        "",
        f"Тариф: {icon} <b>{name}</b>",
        f"Проверок сегодня: {checks_today}/{daily}",
        f"Осталось: {remaining}",
        f"Всего проверок: {user['checks_total']}",
    ]

    if promo_left > 0:
        lines.append(f"🎁 Промо-проверок: {promo_left}")

    if plan in ("pro", "business") and user.get("plan_expires"):
        try:
            from datetime import datetime
            exp = datetime.fromisoformat(user["plan_expires"])
            lines.append(f"📅 Подписка до: <b>{exp.strftime('%d.%m.%Y')}</b>")
        except (ValueError, TypeError):
            pass

    # Возможности текущего плана
    lines.append("")
    lines.append("─── <b>Возможности</b> ───")
    if limits["full_report"]:
        lines.append("✅ Полный отчёт")
    else:
        lines.append("📋 Краткий отчёт")
    if limits["ai_analysis"]:
        lines.append("✅ ИИ-анализ")
    else:
        lines.append("❌ ИИ-анализ")
    if limits["egrul"]:
        lines.append("✅ Выписка ЕГРЮЛ")
    else:
        lines.append("❌ Выписка ЕГРЮЛ")

    return "\n".join(lines)


async def get_profile_text(user_id: int) -> str:
    """Async-обёртка для текста профиля."""
    return await _run(_get_profile_text_sync, user_id)


def get_tariffs_text() -> str:
    """Текст с тарифами (без DB — можно синхронно)."""
    from config import TARIFF_INFO

    lines = [
        "💎 <b>Тарифные планы</b>",
        "",
        "─── 🆓 <b>Free</b> ───",
        "Бесплатно навсегда",
        "• 3 проверки в день",
        "• Краткий отчёт + светофор",
        "• Стоп-листы и суды (сводка)",
        "",
    ]

    for plan_id, info in TARIFF_INFO.items():
        lines.append(f"─── {info['name']} ───")
        lines.append(f"💰 {info['price']}")
        lines.append(f"📊 {info['checks']}")
        features = info['features'].split(' • ')
        for feat in features:
            lines.append(f"  ✅ {feat}")
        lines.append("")

    lines.append("─── 💳 <b>Оплата</b> ───")
    lines.append("Для подключения тарифа напишите в поддержку:")
    lines.append("/support — связаться с нами")
    lines.append("")
    lines.append("<i>Цены на 15% ниже аналогов (Контур, Руспрофайл)</i>")
    lines.append("")
    lines.append("Есть промокод? Введите /promo")

    return "\n".join(lines)
