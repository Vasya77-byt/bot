import asyncio
import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from company_service import CompanyService
from gigachat_client import GigaChatClient
from compliance import assess_risk
from exports import build_kp_pdf, build_kp_png
from logging_config import setup_logging
from offer import OFFER_TEXT
from parsers import ParseResult, parse_message
from payments_store import PaymentsStore
from renderers import render_comparison, render_profile, render_response
from renewal_scheduler import run_renewal_loop
from expiry_reminder import run_expiry_reminder_loop
from schemas import CompanyData
from security_check import SecurityService
from subscription import SubscriptionService
from tochka_client import TochkaClient
from yookassa_client import YooKassaClient, YooKassaError
from user_store import TARIFF_PRICES, UserStore
from settings import Settings
from admin_stats import build_admin_report, parse_admin_user_ids
from zchb_client import ZchbClient
from storage import save_file_bytes
from metadata_store import MetadataStore
from monitoring import make_snapshot
from monitoring_scheduler import run_monitoring_loop
from monitoring_store import MonitoringStore
from payment_poller import run_payment_poller
from report_tokens import ReportTokenStore
from telemetry import init_sentry
from user_store import REFERRAL_BONUS_DAYS, TARIFF_MONITORING_LIMITS
from webhook_server import build_app as build_webhook_app, start_webhook_server


setup_logging()
logger = logging.getLogger("financial-architect")
init_sentry()
metadata_store = MetadataStore()
company_service = CompanyService()
security_service = SecurityService()
gigachat = GigaChatClient()
zchb = ZchbClient()
user_store = UserStore()
payments_store = PaymentsStore()
monitoring_store = MonitoringStore()
report_tokens = ReportTokenStore()

# Сервис подписок инициализируется в main() когда есть Settings
subscription_service: Optional[SubscriptionService] = None

# Обязательная подписка на канал. Заполняется в main() из Settings.
# Пусто = проверка канала отключена.
required_channel: str = ""

# Admin user IDs для команды /admin. Заполняется в main() из Settings.
admin_user_ids: set[int] = set()

# База для ссылок Telegram WebApp. Пусто = кнопка не показывается.
report_base_url: str = ""

# Хранение состояния пользователей (ожидание ИНН)
# Значение: строка (action) или dict с данными многошагового флоу
_user_state: dict[int, Any] = {}


def build_app(settings: Settings) -> Client:
    return Client(
        "financial_architect_bot",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
    )


def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Проверить компанию", callback_data="mode_internal_analysis")],
            [InlineKeyboardButton("📋 Массовая проверка", callback_data="mode_mass_check")],
            [InlineKeyboardButton("🆘 Поддержка", url="https://t.me/YRS75")],
        ]
    )


def _reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📋 Проверка компании"), KeyboardButton("⚖️ Сравнить")],
            [KeyboardButton("👤 Профиль"), KeyboardButton("💎 Тарифы")],
        ],
        resize_keyboard=True,
    )


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Telegram возвращает один из этих статусов когда юзер НЕ подписан.
# Pyrogram отдаёт enum ChatMemberStatus — сравниваем по имени для гибкости.
_NOT_SUBSCRIBED_STATUSES = {"LEFT", "BANNED", "KICKED", "RESTRICTED"}


async def _is_subscribed_to_channel(client, user_id: int) -> bool:
    """True если пользователь подписан на required_channel.
    True если проверка отключена (required_channel пустой)."""
    if not required_channel:
        return True
    try:
        member = await client.get_chat_member(required_channel, user_id)
    except Exception as exc:
        # UserNotParticipant или ChatAdminRequired — считаем не подписан.
        # Логируем чтобы оператор видел проблему (например, бот не админ).
        logger.warning(
            "Channel check failed for user=%s channel=%s: %s",
            user_id, required_channel, exc,
        )
        return False
    status = getattr(member, "status", None)
    name = getattr(status, "name", None) or str(status)
    return name.upper() not in _NOT_SUBSCRIBED_STATUSES


async def _onboarding_step_async(client, profile) -> Optional[str]:
    """Возвращает название следующего шага онбординга или None.
    Порядок: оферта → канал → телефон."""
    if not profile.accepted_offer_at:
        return "offer"
    if required_channel and not await _is_subscribed_to_channel(client, profile.user_id):
        return "channel"
    if not profile.phone:
        return "phone"
    return None


def _onboarding_step(profile) -> Optional[str]:
    """Синхронная упрощённая проверка (без канала). Используется только
    для предварительной проверки и в тестах. Полная проверка — _async."""
    if not profile.accepted_offer_at:
        return "offer"
    if not profile.phone:
        return "phone"
    return None


def _offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Публичная оферта", url="https://telegra.ph/Publichnaya-oferta---Finansovyj-arhitektor-04-27")],
        [InlineKeyboardButton("📋 Пользовательское соглашение", url="https://telegra.ph/Polzovatelskoe-soglashenie-04-27-19")],
        [InlineKeyboardButton("🔒 Политика обработки ПДн", url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-04-27")],
        [InlineKeyboardButton("✅ Принимаю и продолжаю", callback_data="accept_offer")],
    ])


def _channel_keyboard() -> InlineKeyboardMarkup:
    channel = required_channel or ""
    url_part = channel.lstrip("@")
    rows = []
    if url_part and not url_part.startswith("-"):
        rows.append([InlineKeyboardButton(
            "📢 Перейти в канал", url=f"https://t.me/{url_part}"
        )])
    rows.append([InlineKeyboardButton(
        "✅ Я подписался — проверить", callback_data="check_channel"
    )])
    return InlineKeyboardMarkup(rows)


def _phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _send_onboarding_step(message, step: str) -> None:
    """Показывает соответствующий шаг онбординга пользователю."""
    if step == "offer":
        await message.reply_text(
            "👋 Добро пожаловать!\n\n"
            "Перед началом работы ознакомьтесь с правовыми документами "
            "и подтвердите согласие. Это обязательно для использования "
            "сервиса согласно 152-ФЗ «О персональных данных».",
            reply_markup=_offer_keyboard(),
        )
    elif step == "channel":
        channel = required_channel or ""
        await message.reply_text(
            f"📢 Подпишитесь на наш канал {channel}\n\n"
            "Это обязательное условие для использования бота. "
            "В канале — полезные материалы по проверке контрагентов.\n\n"
            "После подписки нажмите «✅ Я подписался — проверить».",
            reply_markup=_channel_keyboard(),
        )
    elif step == "phone":
        await message.reply_text(
            "📱 Поделитесь номером телефона\n\n"
            "Номер нужен для:\n"
            "• Привязки подписки к вашему аккаунту\n"
            "• Связи с поддержкой при необходимости\n\n"
            "Нажмите кнопку ниже — Telegram передаст номер автоматически.",
            reply_markup=_phone_keyboard(),
        )


async def _ensure_onboarded(client, message) -> bool:
    """Гейт: возвращает True если онбординг пройден.
    Иначе показывает следующий шаг и возвращает False."""
    user_id = message.from_user.id
    profile = user_store.get(user_id)
    step = await _onboarding_step_async(client, profile)
    if step is None:
        return True
    await _send_onboarding_step(message, step)
    return False


def _profile_keyboard(profile=None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📋 Мои компании", callback_data="my_companies")],
        [InlineKeyboardButton("🤝 Реферальная программа", callback_data="referral_show")],
    ]
    # Кнопка управления автопродлением — только для платников с активной
    # подпиской. Подписка не отменяется здесь и сейчас, отключается только
    # авто-списание.
    if (profile is not None and profile.tariff != "free"
            and profile.is_subscription_active()):
        if profile.auto_renew:
            rows.append([InlineKeyboardButton(
                "🛑 Отменить автопродление", callback_data="sub_cancel"
            )])
        else:
            rows.append([InlineKeyboardButton(
                "✅ Включить автопродление", callback_data="sub_enable"
            )])
    return InlineKeyboardMarkup(rows)


def _my_companies_text(user_id: int) -> str:
    profile = user_store.get(user_id)
    subs = monitoring_store.list_for_user(user_id)
    limit = _monitoring_limit(profile)
    limit_str = "∞" if limit is None else str(limit)
    header = f"📋 Мои отслеживаемые компании ({len(subs)}/{limit_str})"
    if not subs:
        return (
            f"{header}\n\n"
            "Список пуст.\n\n"
            "Чтобы добавить компанию — отправьте её ИНН в чат, "
            "получите отчёт и нажмите «👁 Отслеживать» под ним.\n\n"
            "Я буду каждый день проверять компанию и пришлю уведомление, "
            "если изменится статус (например, банкротство), руководитель, "
            "адрес, ОКВЭД, ФССП или уровень риска."
        )
    lines = [header, ""]
    for i, sub in enumerate(subs, 1):
        title = sub.name.strip() or f"ИНН {sub.inn}"
        last = sub.last_checked[:10] if sub.last_checked else "—"
        lines.append(f"{i}. {title} (ИНН {sub.inn}) — проверено {last}")
    lines.append("")
    lines.append("Нажмите ❌ под компанией, чтобы снять с отслеживания.")
    return "\n".join(lines)


def _my_companies_keyboard(user_id: int) -> InlineKeyboardMarkup:
    subs = monitoring_store.list_for_user(user_id)
    rows = []
    for sub in subs:
        title = (sub.name.strip() or sub.inn)[:24]
        rows.append([
            InlineKeyboardButton(
                f"📊 {title}", callback_data=f"mc_open:{sub.inn}"
            ),
            InlineKeyboardButton(
                "❌", callback_data=f"mc_remove:{sub.inn}"
            ),
        ])
    rows.append([InlineKeyboardButton("🔄 Обновить список", callback_data="my_companies")])
    return InlineKeyboardMarkup(rows)


def _fmt_money_short(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} млрд ₽"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽"
    if value >= 1_000:
        return f"{value / 1_000:.0f} тыс ₽"
    return f"{value} ₽"


def _format_arbitration(summary, inn: str) -> str:
    """Текстовый рендер сводки арбитражных дел из ЗЧБ."""
    if summary is None:
        return (
            f"⚖️ Арбитражные дела (ИНН {inn})\n\n"
            "Не удалось получить данные. Попробуйте позже."
        )
    if not summary.has_cases:
        return (
            f"⚖️ Арбитражные дела (ИНН {inn})\n\n"
            "✅ Судебных дел не найдено."
        )

    lines = [f"⚖️ Арбитражные дела (ИНН {inn})", ""]
    lines.append(f"Точных дел по ИНН: {summary.total_exact}")
    if summary.total_fuzzy:
        lines.append(f"Похожих по названию: {summary.total_fuzzy} (могут быть чужие)")
    lines.append("")

    if summary.total_exact:
        lines.append(f"• Как истец: {summary.as_plaintiff_count} "
                     f"({_fmt_money_short(summary.plaintiff_claim_sum)})")
        lines.append(f"• Как ответчик: {summary.as_defendant_count} "
                     f"({_fmt_money_short(summary.defendant_claim_sum)})")
        lines.append(f"• Общая сумма исков: {_fmt_money_short(summary.total_claim_sum)}")
        lines.append("")

    # Топ-5 по сумме иска (только точные)
    exact = [c for c in summary.cases if c.accuracy == "exact"]
    top = sorted(exact, key=lambda c: c.sum_rub, reverse=True)[:5]
    if top:
        lines.append("Топ дел по сумме:")
        for c in top:
            role = c.role or "—"
            party = c.counterparty_name or "—"
            lines.append(
                f"• {c.case_number} ({c.started_at}) — {role}, "
                f"{_fmt_money_short(c.sum_rub)}"
            )
            if party != "—":
                lines.append(f"   ↳ {party}")
        lines.append("")
        lines.append("Подробнее по делу — на kad.arbitr.ru")
    return "\n".join(lines)


def _egrul_actions_keyboard(inn: str) -> InlineKeyboardMarkup:
    """Кнопки внутри ЕГРЮЛ-блока."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📜 Полная история записей (+10 запросов)",
            callback_data=f"ca_egryl_history:{inn}",
        )],
        [InlineKeyboardButton(
            "📥 Выписка PDF с ЭЦП ФНС",
            callback_data=f"ca_egryl_pdf:{inn}",
        )],
    ])


def _format_egrul_summary(card, inn: str) -> str:
    """Сводка ЕГРЮЛ: основные сведения о компании.
    История изменений вынесена в отдельную кнопку «📜 История»."""
    if card is None:
        return (
            f"🏛 ЕГРЮЛ (ИНН {inn})\n\n"
            "Не удалось получить данные. Попробуйте позже."
        )

    lines = [f"🏛 ЕГРЮЛ — {card.name_short or card.name_full or ('ИНН ' + inn)}", ""]
    if card.name_full and card.name_full != card.name_short:
        lines.append(f"Полное наименование: {card.name_full}")
    if card.inn:
        lines.append(f"ИНН: {card.inn}")
    if card.ogrn:
        lines.append(f"ОГРН: {card.ogrn}")
    if card.status:
        lines.append(f"Статус: {card.status}")
    if card.tax_regime:
        lines.append(f"Налоговый режим: {card.tax_regime}")
    if card.msp_category:
        lines.append(f"Категория МСП: {card.msp_category}")
    if card.licenses_count:
        lines.append(f"Лицензий: {card.licenses_count}")
    lines.append("")
    lines.append(
        "📜 Полная история изменений (директор/адрес/реорганизация) — "
        "нажмите кнопку «📜 История» под отчётом."
    )
    lines.append("")
    lines.append(
        "📥 Бесплатная выписка с ЭЦП ФНС — egrul.nalog.ru (кнопка ниже)."
    )
    return "\n".join(lines)


def _format_egrul_history(records, ogrn: str) -> str:
    """Полная история записей ЕГРЮЛ."""
    if records is None:
        return (
            f"📜 История ЕГРЮЛ (ОГРН {ogrn})\n\n"
            "Не удалось получить данные. Попробуйте позже."
        )
    if not records:
        return (
            f"📜 История ЕГРЮЛ (ОГРН {ogrn})\n\n"
            "Записей в ЕГРЮЛ не найдено."
        )

    lines = [f"📜 История ЕГРЮЛ (ОГРН {ogrn})", "", f"Всего записей: {len(records)}", ""]
    sorted_records = sorted(records, key=lambda r: r.date or "", reverse=True)
    for i, r in enumerate(sorted_records[:30], 1):
        date = r.date[:10] if r.date else "—"
        lines.append(f"{i}. {date} — {r.type_name or r.type_code}")
        if r.grn:
            lines.append(f"   ГРН: {r.grn}")
        if r.authority_name:
            short = r.authority_name[:80] + ("…" if len(r.authority_name) > 80 else "")
            lines.append(f"   {short}")
        lines.append("")
    if len(sorted_records) > 30:
        lines.append(f"… показано 30 из {len(sorted_records)}.")
    return "\n".join(lines)


def _format_history(card, events, inn: str, company=None) -> str:
    """Текущее состояние + лента свежих изменений по разделам:
    директор / учредители / адрес / ОКВЭД / наименование / капитал.

    Структура раздела:
    - 🔹 ТЕКУЩЕЕ значение из card/company (всегда видно)
    - 🔸 Историческое из events (если есть)
    - Если ни того ни другого — ✅ Не менялся

    ВАЖНО: метод diffs ЗЧБ возвращает только свежие изменения
    (~6-12 месяцев), не полную биографию с момента регистрации.
    """
    name = ""
    if card is not None:
        name = card.name_short or card.name_full or ""
    elif company is not None and company.name:
        name = company.name

    title_lines = ["📜 История изменений"]
    if name:
        title_lines.append(name)
    title_lines.append(f"ИНН {inn}")

    events = events or []

    # Группируем события по типу
    by_field: dict[str, list] = {
        "name": [], "director": [], "founders": [], "address": [],
        "okved": [], "okved_main": [], "okved_extra": [],
        "capital": [], "reorganization": [], "other": [],
    }
    for ev in events:
        if ev.field_type in by_field:
            by_field[ev.field_type].append(ev)
        else:
            by_field["other"].append(ev)

    # Объединяем okved_main + okved_extra + okved в один раздел
    okved_all = by_field["okved_main"] + by_field["okved"] + by_field["okved_extra"]
    okved_all.sort(key=lambda e: e.timestamp, reverse=True)

    sections: list[str] = []

    # Хелпер форматирования даты события — берём date_iso если есть, иначе ts
    from datetime import datetime as _dt

    def _fmt_date(ev) -> str:
        if ev.date_iso:
            return ev.date_iso[:10].replace("-", ".")[8:10] + "." + ev.date_iso[5:7] + "." + ev.date_iso[:4] if False else ev.date_iso[:10]
        if ev.timestamp:
            return _dt.utcfromtimestamp(ev.timestamp).strftime("%Y-%m-%d")
        return "—"

    # ── Директор ──
    section = ["━━━ 👤 РУКОВОДИТЕЛЬ ━━━"]
    director_events = by_field["director"]
    director_events.sort(key=lambda e: e.timestamp, reverse=True)

    # Текущий директор из card
    current_director_added = False
    if card and card.director_name:
        line = f"🔹 {card.director_name}"
        if card.director_inn:
            line += f" (ИНН {card.director_inn})"
        section.append(line)
        if card.director_position:
            section.append(f"   {card.director_position}")
        if card.director_started_at:
            section.append(f"   Действует с {card.director_started_at[:10]}")
        current_director_added = True

    # Исторические события — показываем только если ФИО не совпадает с текущим
    seen_keys = set()
    if card and card.director_name:
        seen_keys.add((card.director_name.upper(), ""))
    historical_dirs = []
    for ev in director_events:
        key = (ev.person_name.upper(), ev.extra)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        historical_dirs.append(ev)

    for ev in historical_dirs[:5]:
        line = f"🔸 {ev.person_name}"
        if ev.person_inn:
            line += f" (ИНН {ev.person_inn})"
        section.append(line)
        if ev.extra:
            section.append(f"   {ev.extra}")
        section.append(f"   Действовал с {_fmt_date(ev)}")

    if not current_director_added and not historical_dirs:
        section.append("Данных о руководителе нет в открытых источниках")
    sections.append("\n".join(section))

    # ── Учредители ──
    section = ["━━━ 👥 УЧРЕДИТЕЛИ ━━━"]
    founder_events = by_field["founders"]
    founder_events.sort(key=lambda e: e.timestamp, reverse=True)

    # Текущие учредители из card
    current_founders = (card.founders if card else []) or []
    if current_founders:
        for f in current_founders[:5]:
            line = f"🔹 {f.name or 'Неизвестно'}"
            if f.inn:
                line += f" (ИНН {f.inn})"
            if f.share_pct > 0:
                line += f" — доля {f.share_pct:g}%"
            elif f.share_abs > 0:
                line += f" — {_fmt_money_short(f.share_abs)}"
            section.append(line)
            if f.started_at:
                section.append(f"   Действует с {f.started_at}")
        if len(current_founders) > 5:
            section.append(f"… и ещё {len(current_founders) - 5} учредителей")

    # Исторические события — учредители которых уже нет в текущем составе
    current_inns = {f.inn for f in current_founders if f.inn}
    historical_founders = [
        ev for ev in founder_events
        if ev.person_inn and ev.person_inn not in current_inns
    ]
    for ev in historical_founders[:3]:
        line = f"🔸 {ev.summary}"
        if ev.person_inn:
            line += f" (ИНН {ev.person_inn})"
        section.append(line)
        section.append(f"   Действовал с {_fmt_date(ev)}")

    if not current_founders and not historical_founders:
        section.append("Данных об учредителях нет в открытых источниках")
    sections.append("\n".join(section))

    # ── Адрес ──
    section = ["━━━ 📍 АДРЕС ━━━"]
    address_events = by_field["address"]
    address_events.sort(key=lambda e: e.timestamp, reverse=True)

    # Текущий адрес из company
    current_address = ""
    if company and company.address:
        current_address = company.address
    if current_address:
        section.append(f"🔹 {current_address[:150]}")
        if company.reg_date:
            section.append(f"   В ЕГРЮЛ с {company.reg_date}")

    # Исторические — только если адрес отличается от текущего
    norm_current = current_address.lower().replace(" ", "").replace(",", "")[:50]
    historical_addrs = []
    seen_norms = {norm_current} if norm_current else set()
    for ev in address_events:
        norm = ev.summary.lower().replace(" ", "").replace(",", "")[:50]
        if norm in seen_norms:
            continue
        seen_norms.add(norm)
        historical_addrs.append(ev)

    for ev in historical_addrs[:3]:
        section.append(f"🔸 {ev.summary[:120]}")
        section.append(f"   Действовал с {_fmt_date(ev)}")

    if not current_address and not historical_addrs:
        section.append("✅ Не менялся")
    sections.append("\n".join(section))

    # ── Основной ОКВЭД ──
    section = ["━━━ 🏷 ОКВЭД ━━━"]
    # Текущий ОКВЭД из company
    if company and company.okved_main:
        line = f"🔹 {company.okved_main}"
        if company.okved_name:
            line += f" — {company.okved_name}"
        section.append(line[:200])

    # Исторические из events — только если код отличается
    current_code = (company.okved_main if company else "") or ""
    seen_codes = {current_code} if current_code else set()
    historical_okveds = []
    for ev in okved_all:
        # Извлекаем код из summary "X.YZ — название"
        code_part = ev.summary.split("—")[0].strip().split()[0] if ev.summary else ""
        if code_part in seen_codes:
            continue
        seen_codes.add(code_part)
        historical_okveds.append(ev)

    for ev in historical_okveds[:5]:
        section.append(f"🔸 {ev.summary[:120]}")
        section.append(f"   Действовал с {_fmt_date(ev)}")

    if not (company and company.okved_main) and not historical_okveds:
        section.append("✅ Не менялся")
    sections.append("\n".join(section))

    # ── Наименование ──
    section = ["━━━ 📛 НАИМЕНОВАНИЕ ━━━"]
    name_events = by_field["name"]
    name_events.sort(key=lambda e: e.timestamp, reverse=True)
    current_name = ""
    if card:
        current_name = card.name_full or card.name_short or ""
    if current_name:
        section.append(f"🔹 {current_name[:120]}")
    seen_names = {current_name.upper().strip()} if current_name else set()
    historical_names = []
    for ev in name_events:
        n = ev.summary.upper().strip()
        if n in seen_names:
            continue
        seen_names.add(n)
        historical_names.append(ev)
    for ev in historical_names[:3]:
        section.append(f"🔸 {ev.summary[:100]}")
        section.append(f"   Действовало с {_fmt_date(ev)}")
    if not current_name and not historical_names:
        section.append("✅ Не менялось")
    sections.append("\n".join(section))

    # ── Уставный капитал ──
    section = ["━━━ 💰 УСТАВНЫЙ КАПИТАЛ ━━━"]
    cap_events = by_field["capital"]
    cap_events.sort(key=lambda e: e.timestamp, reverse=True)
    current_capital = 0
    if card and card.capital:
        current_capital = card.capital
    elif company and company.capital:
        current_capital = company.capital
    if current_capital:
        section.append(f"🔹 {_fmt_money_short(current_capital)}")
    for ev in cap_events[:3]:
        section.append(f"🔸 {ev.summary}")
        section.append(f"   Действовал с {_fmt_date(ev)}")
    if not current_capital and not cap_events:
        section.append("✅ Не менялся")
    else:
        for i, ev in enumerate(cap_events[:5]):
            marker = "🔹" if i == 0 else "🔸"
            verb = "Действует с" if i == 0 else "Действовал с"
            section.append(f"{marker} {ev.summary}")
            section.append(f"   {verb} {_fmt_date(ev)}")
    sections.append("\n".join(section))

    # ── Реорганизация (если есть) ──
    reorg_events = by_field["reorganization"]
    if reorg_events:
        reorg_events.sort(key=lambda e: e.timestamp, reverse=True)
        section = ["━━━ 🔄 РЕОРГАНИЗАЦИЯ ━━━"]
        for ev in reorg_events[:3]:
            section.append(f"⚠️ {ev.summary} ({_fmt_date(ev)})")
        sections.append("\n".join(section))

    text = "\n".join(title_lines) + "\n\n" + "\n\n".join(sections)

    # Бюджет под Telegram (4096). Если перебрали — отрезаем последние секции
    if len(text) > 3900:
        text = text[:3900] + "\n\n…отчёт обрезан до лимита Telegram."
    return text


def _format_inspections(inspections, inn: str) -> str:
    """Рендер истории проверок из ЕРП."""
    if inspections is None:
        return (
            f"📜 Проверки (ИНН {inn})\n\n"
            "Не удалось получить данные. Попробуйте позже."
        )
    if not inspections:
        return (
            f"📜 Проверки (ИНН {inn})\n\n"
            "✅ В Едином Реестре Проверок записей не найдено."
        )

    lines = [f"📜 Проверки из ЕРП (ИНН {inn})", ""]
    lines.append(f"Всего записей: {len(inspections)}")
    lines.append("")

    # Сортируем по дате начала, последние сверху
    sorted_inspect = sorted(
        inspections,
        key=lambda r: r.start_date or "",
        reverse=True,
    )
    for i, r in enumerate(sorted_inspect[:10], 1):
        period = r.start_date[:10] if r.start_date else "—"
        if r.end_date and r.end_date[:10] != period:
            period = f"{period} → {r.end_date[:10]}"
        type_part = " ".join(filter(None, [r.inspection_type, r.carryout_form]))
        lines.append(f"{i}. {period} — {type_part or 'Проверка'}")
        if r.authority:
            short = r.authority[:80] + ("…" if len(r.authority) > 80 else "")
            lines.append(f"   Орган: {short}")
        if r.status:
            status_emoji = "✅" if "заверш" in r.status.lower() else "⏳"
            lines.append(f"   Статус: {status_emoji} {r.status}")
        if r.has_violations:
            lines.append("   ⚠️ Выявлены нарушения")
        if r.risk_category:
            lines.append(f"   Категория риска: {r.risk_category}")
        lines.append("")
    if len(sorted_inspect) > 10:
        lines.append(f"… показано 10 из {len(sorted_inspect)} проверок.")
    return "\n".join(lines)


async def _format_links(card, inn: str) -> str:
    """Связи компании: куда тянутся нити через директора и учредителей.

    Структура:
    - 👤 РУКОВОДИТЕЛЬ — текущий директор + где он ещё работает
    - 👥 УЧРЕДИТЕЛИ-ФИЗЛИЦА — каждый + его компании
    - 🏢 УЧРЕДИТЕЛИ-ЮРЛИЦА — список (без углубления)
    - 📋 ИТОГ — общая статистика связанных компаний

    Жгём максимум 6 fl-card запросов на отчёт (1 директор + до 5 учредителей).
    Кеш 24ч.
    """
    if card is None:
        return (
            f"🔗 Связи (ИНН {inn})\n\n"
            "Не удалось получить данные. Попробуйте позже."
        )

    name = card.name_short or card.name_full or f"ИНН {inn}"
    title_lines = ["🔗 Связи компании", name, f"ИНН {inn}"]
    sections: list[str] = []

    # Накопители для итоговой статистики
    all_related_inns: set[str] = set()
    warnings: list[str] = []
    # Чтобы не показывать саму проверяемую компанию в списке "связанных"
    self_inn = (card.inn or inn or "").strip()

    # ━━━ РУКОВОДИТЕЛЬ ━━━
    section = ["━━━ 👤 РУКОВОДИТЕЛЬ ━━━"]
    if not card.director_name:
        section.append("Текущий руководитель не указан в карточке.")
    else:
        line = f"👤 {card.director_name}"
        if card.director_inn:
            line += f" (ИНН {card.director_inn})"
        section.append(line)
        if card.director_position:
            section.append(f"   {card.director_position}")
        if card.director_started_at:
            section.append(f"   Действует с {card.director_started_at[:10]}")

        if card.director_inn:
            fl = await zchb.get_fl_card(card.director_inn)
            if fl:
                _append_fl_summary(section, fl, all_related_inns, warnings,
                                   self_inn=self_inn, indent=True)
            else:
                section.append("   Связанных компаний не найдено.")
        elif card.director_namesake_count >= 50:
            section.append(
                f"   ℹ️ ИНН руководителя не указан в карточке. "
                f"Однофамильцев-руководителей: {card.director_namesake_count}"
            )
    sections.append("\n".join(section))

    # ━━━ УЧРЕДИТЕЛИ-ФИЗЛИЦА ━━━
    fl_founders = [f for f in (card.founders or []) if f.type == "fl"]
    if fl_founders:
        section = ["━━━ 👥 УЧРЕДИТЕЛИ-ФИЗЛИЦА ━━━"]
        seen_inns: set[str] = set()
        # Не дёргаем fl-card для директора повторно (если он же и учредитель)
        director_inn = card.director_inn or ""
        if director_inn:
            seen_inns.add(director_inn)

        # Учредители-ФЛ, совпадающие с директором (по ИНН)
        same_as_director = [
            f for f in fl_founders if f.inn and f.inn == director_inn
        ]
        # Учредители у которых нет ИНН (по ним fl-card сделать нельзя)
        no_inn_founders = [f for f in fl_founders if not f.inn]

        # Уникальные с ИНН и не совпадающие с директором — для запросов (макс 5)
        to_query: list = []
        for f in fl_founders:
            if f.inn and f.inn not in seen_inns:
                seen_inns.add(f.inn)
                to_query.append(f)
            if len(to_query) >= 5:
                break

        if not to_query and not no_inn_founders and not same_as_director:
            section.append(
                "Учредителей-физлиц не указано (см. блок Юрлица ниже)."
            )
        else:
            for f in to_query:
                line = f"👤 {f.name or 'Без имени'}"
                if f.inn:
                    line += f" (ИНН {f.inn})"
                if f.share_pct > 0:
                    line += f" — доля {f.share_pct:g}%"
                section.append(line)
                fl = await zchb.get_fl_card(f.inn)
                if fl:
                    _append_fl_summary(section, fl, all_related_inns,
                                       warnings, self_inn=self_inn, indent=True)
                else:
                    section.append("   Связанных компаний не найдено.")

            for f in no_inn_founders[:5]:
                line = f"👤 {f.name or 'Без имени'}"
                if f.share_pct > 0:
                    line += f" — доля {f.share_pct:g}%"
                section.append(line)
                section.append(
                    "   ℹ️ ИНН не указан в реестре — углублённую проверку "
                    "связей сделать нельзя."
                )

            # Учредители = директор: явная пометка вместо "не показаны"
            for f in same_as_director:
                line = f"👤 {f.name or 'Без имени'}"
                if f.inn:
                    line += f" (ИНН {f.inn})"
                if f.share_pct > 0:
                    line += f" — доля {f.share_pct:g}%"
                section.append(line)
                section.append(
                    "   ↑ Это руководитель компании (связи показаны выше)"
                )

            shown = (len(to_query) + min(len(no_inn_founders), 5)
                     + len(same_as_director))
            if len(fl_founders) > shown:
                section.append(
                    f"   … и ещё {len(fl_founders) - shown} "
                    "учредителей-физлиц (показаны не все для экономии запросов)"
                )
        sections.append("\n".join(section))

    # ━━━ УЧРЕДИТЕЛИ-ЮРЛИЦА ━━━
    ul_founders = [f for f in (card.founders or []) if f.type == "ul"]
    if ul_founders:
        section = ["━━━ 🏢 УЧРЕДИТЕЛИ-ЮРЛИЦА ━━━"]
        for f in ul_founders[:7]:
            line = f"🏢 {f.name or 'Без названия'}"
            if f.inn and f.inn != self_inn:
                line += f" (ИНН {f.inn})"
                all_related_inns.add(f.inn)
            if f.share_pct > 0:
                line += f" — доля {f.share_pct:g}%"
            section.append(line)
        if len(ul_founders) > 7:
            section.append(f"… и ещё {len(ul_founders) - 7} юрлиц-учредителей")
        sections.append("\n".join(section))

    # ━━━ ИТОГ ━━━
    summary = ["━━━ 📋 ИТОГ ━━━"]
    if all_related_inns:
        summary.append(
            f"Связанных компаний обнаружено: {len(all_related_inns)}"
        )
    else:
        summary.append(
            "✅ Связей с другими компаниями не найдено — компания "
            "выглядит обособленной."
        )
    if warnings:
        # Дедупликация
        seen = set()
        for w in warnings:
            if w not in seen:
                seen.add(w)
                summary.append(w)
    sections.append("\n".join(summary))

    text = "\n".join(title_lines) + "\n\n" + "\n\n".join(sections)
    if len(text) > 3900:
        text = text[:3900] + "\n\n…отчёт обрезан до лимита Telegram."
    return text


def _append_fl_summary(section: list, fl, related_inns: set,
                       warnings: list, self_inn: str = "",
                       indent: bool = False) -> None:
    """Добавляет в section строки с компаниями где физлицо
    руководит/учредитель/ИП. Аккумулирует ИНН в related_inns
    и ⚠️ предупреждения о массовости в warnings.

    Сама проверяемая компания (self_inn) исключается из списков —
    нет смысла показывать её как «связанную».
    """
    pad = "   " if indent else ""

    if fl.is_mass_leader:
        section.append(f"{pad}⚠️ Признан МАССОВЫМ руководителем")
        warnings.append(
            f"⚠️ {fl.full_name or fl.inn_fl}: массовый руководитель"
        )
    if fl.is_mass_founder:
        section.append(f"{pad}⚠️ Признан МАССОВЫМ учредителем")
        warnings.append(
            f"⚠️ {fl.full_name or fl.inn_fl}: массовый учредитель"
        )

    # Фильтруем self_inn — сама проверяемая компания не должна быть
    # в списках "связанных"
    leads = [c for c in (fl.leads or []) if (c.inn or "") != self_inn]
    founds = [c for c in (fl.founds or []) if (c.inn or "") != self_inn]
    sole = fl.sole_props or []

    has_external = bool(leads or founds or sole)
    if not has_external:
        if not fl.is_mass_leader and not fl.is_mass_founder:
            section.append(
                f"{pad}✅ В других компаниях не фигурирует"
            )
        return

    if leads:
        section.append(f"{pad}Руководит ещё в {len(leads)} комп.:")
        for c in leads[:4]:
            marker = "✅" if c.is_active else "⛔"
            cname = (c.name_short or c.name_full or f"ИНН {c.inn}")[:45]
            section.append(f"{pad}• {marker} {cname}")
            if c.inn:
                related_inns.add(c.inn)
        if len(leads) > 4:
            section.append(f"{pad}  … и ещё {len(leads) - 4}")

    if founds:
        section.append(f"{pad}Учредитель ещё в {len(founds)} комп.:")
        for c in founds[:4]:
            marker = "✅" if c.is_active else "⛔"
            cname = (c.name_short or c.name_full or f"ИНН {c.inn}")[:45]
            section.append(f"{pad}• {marker} {cname}")
            if c.inn:
                related_inns.add(c.inn)
        if len(founds) > 4:
            section.append(f"{pad}  … и ещё {len(founds) - 4}")

    if sole:
        section.append(f"{pad}ИП на этом ИНН: {len(sole)}")


def _format_finance(card, security, company, inn: str) -> str:
    """Финансовый отчёт по компании. Структурирован по разделам с
    подсветкой 🔴/🟡/✅, чтобы новичок сразу видел на что обратить внимание.

    Источники: ZCHB card (выручка/налоги/задолженности/контракты),
    SecurityResult (ФССП, индекс ЗЧБ), CompanyData (имя, fallback-финансы
    из DaData/SBIS если в ZCHB пусто — типично для банков).
    """
    name = ""
    if company is not None and company.name:
        name = company.name
    elif card is not None and (card.name_short or card.name_full):
        name = card.name_short or card.name_full

    # Финансовая компания (банк/страховщик/НПФ) — особенный случай:
    # их отчётность хранится по форме ЦБ, а не ФНС
    is_financial = False
    if company and company.okved_main:
        prefix = company.okved_main.split(".")[0]
        is_financial = prefix in ("64", "65", "66")

    title_lines = ["📊 Финансы"]
    if name:
        title_lines.append(name)
    title_lines.append(f"ИНН {inn}")

    if card is None and security is None and company is None:
        return "\n".join(title_lines + ["", "Не удалось получить данные. Попробуйте позже."])

    sections: list[str] = []
    findings: list[str] = []

    # ━━━ ВЫРУЧКА И ПРИБЫЛЬ ━━━
    block = ["━━━ 💹 ВЫРУЧКА И ПРИБЫЛЬ ━━━"]
    history = (card.finance_history if card else None) or []
    if history:
        for f in history[:5]:
            rev = int(f.revenue) if f.revenue else 0
            prof = int(f.profit) if f.profit else 0
            inc = int(f.income) if f.income else 0
            exp = int(f.expense) if f.expense else 0
            if rev or prof:
                margin = (prof / rev * 100) if rev > 0 else 0
                margin_str = f" (рент. {margin:.0f}%)" if rev > 0 else ""
                block.append(
                    f"{f.year}: {_fmt_money_short(rev)} выручки, "
                    f"{_fmt_money_short(prof)} прибыли{margin_str}"
                )
            elif inc or exp:
                block.append(
                    f"{f.year} (УСН): доход {_fmt_money_short(inc)}, "
                    f"расход {_fmt_money_short(exp)}"
                )

        # Тренд: сравнение последних 2 годов с непустой выручкой
        revenues = [(f.year, f.revenue or f.income or 0) for f in history
                    if (f.revenue or f.income or 0) > 0]
        if len(revenues) >= 2:
            current = revenues[0][1]
            prev = revenues[1][1]
            if prev > 0:
                delta_pct = (current - prev) / prev * 100
                if delta_pct >= 5:
                    block.append(f"↗️ Выручка растёт: +{delta_pct:.0f}% к прошлому году")
                elif delta_pct <= -5:
                    block.append(f"↘️ Выручка падает: {delta_pct:.0f}% к прошлому году")
                    findings.append(f"⚠️ Падение выручки на {abs(delta_pct):.0f}% год к году")
                else:
                    block.append(f"→ Выручка стабильна ({delta_pct:+.0f}%)")

        last_profit = next(
            (f.profit for f in history if f.profit is not None and f.profit != 0),
            None,
        )
        if last_profit is not None and last_profit < 0:
            findings.append(f"⚠️ Компания убыточна: {_fmt_money_short(int(last_profit))}")
    elif (not is_financial) and company and (
        company.revenue_last_year or company.profit_last_year
    ):
        # Fallback на CompanyData (DaData/SBIS) — только для НЕ-финансовых
        # компаний. Для банков/страховщиков DaData часто отдаёт данные
        # одного юрлица из группы, не консолидированные → не показываем.
        rev = int(company.revenue_last_year or 0)
        prof = int(company.profit_last_year or 0)
        margin_str = f" (рент. {prof/rev*100:.0f}%)" if rev > 0 else ""
        block.append(
            f"Выручка: {_fmt_money_short(rev)}, "
            f"прибыль: {_fmt_money_short(prof)}{margin_str}"
        )
        block.append("Источник: реестры ФНС/DaData (упрощённо)")
    else:
        if is_financial:
            block.append(
                "ℹ️ Это финансовая организация (ОКВЭД 64-66). "
                "Полная отчётность ведётся по форме ЦБ (101/102) "
                "и публикуется на cbr.ru."
            )
        else:
            block.append("Финансовая отчётность в открытых реестрах ФНС не найдена.")
    sections.append("\n".join(block))

    # ━━━ НАЛОГИ ━━━
    tax_lines = ["━━━ 🏛 НАЛОГИ ━━━"]
    if card and card.tax_regime:
        tax_lines.append(f"Режим: {card.tax_regime}")
    elif is_financial:
        tax_lines.append("Режим: ОСНО (банки/страховщики)")
    else:
        tax_lines.append("Режим: не указан в открытых реестрах")
    if card and card.msp_category:
        tax_lines.append(f"Категория МСП: {card.msp_category}")
    else:
        tax_lines.append("Категория МСП: не входит (крупное предприятие или иное)")
    if card and card.tax_violations_sum > 0:
        tax_lines.append(
            f"💸 Налоговые штрафы за период: "
            f"{_fmt_money_short(int(card.tax_violations_sum))}"
        )
        if card.tax_violations_history:
            for year, summ in card.tax_violations_history[:3]:
                tax_lines.append(f"   {year}: {_fmt_money_short(int(summ))}")
    else:
        tax_lines.append("✅ Налоговых штрафов в открытых данных нет")
    sections.append("\n".join(tax_lines))

    # ━━━ ⚠️ ЗАДОЛЖЕННОСТИ ━━━
    debts: list[str] = []
    fssp_count = security.enforcement_count if security else 0
    fssp_sum = security.enforcement_total_sum if security else 0
    if fssp_count > 0:
        marker = "🔴" if fssp_sum > 1_000_000 or fssp_count > 10 else "🟡"
        debts.append(
            f"{marker} ФССП: {fssp_count} производств "
            f"на {_fmt_money_short(int(fssp_sum))}"
        )
        if fssp_count > 10:
            findings.append(f"🔴 Много исполнительных производств: {fssp_count}")
    else:
        debts.append("✅ ФССП: исполнительных производств нет")

    if card is not None:
        if card.tax_debt_sum > 0:
            marker = "🔴" if card.tax_debt_sum > 1_000_000 else "🟡"
            debts.append(
                f"{marker} Налоговая задолженность: "
                f"{_fmt_money_short(int(card.tax_debt_sum))}"
            )
            for item in card.tax_debt_items[:5]:
                name_short = item.tax_name[:55].lower()
                debts.append(f"   • {name_short}: {_fmt_money_short(int(item.total))}")
            if card.tax_debt_sum > 1_000_000:
                findings.append(
                    f"🔴 Крупная налоговая задолженность: "
                    f"{_fmt_money_short(int(card.tax_debt_sum))}"
                )
        else:
            debts.append("✅ Налоговая задолженность: чисто")

        if card.in_debt_registry:
            debts.append("🔴 В реестре ФНС: взыскиваемая судебными приставами задолженность")
            findings.append("🔴 Включена в реестр ФНС по взыскиваемой задолженности")
        else:
            debts.append("✅ Реестр взыскиваемой задолженности ФНС: чисто")

    sections.append("━━━ ⚠️ ЗАДОЛЖЕННОСТИ ━━━\n" + "\n".join(debts))

    # ━━━ ПЕРСОНАЛ ━━━
    employees = 0
    payroll = 0
    avg = 0
    if card:
        employees = card.employees_count or 0
        payroll = card.payroll_fund or 0
        avg = card.avg_salary or 0
    # Fallback на CompanyData если в card пусто.
    # Для финансовых организаций (банки) НЕ используем — там DaData отдаёт
    # данные филиала, а не группы (например, у Сбера 25 «сотрудников» в DaData).
    if not employees and company and company.employees_count and not is_financial:
        employees = company.employees_count

    if employees or payroll or avg:
        block = ["━━━ 👥 ПЕРСОНАЛ ━━━"]
        if employees:
            block.append(f"Сотрудников: {employees}")
        if payroll:
            block.append(f"Фонд оплаты труда: {_fmt_money_short(int(payroll))}")
        if avg:
            block.append(f"Средняя ЗП: {_fmt_money_short(int(avg))}")
        # Выручка на сотрудника
        last_revenue = 0
        if card and card.finance_history:
            last_revenue = next(
                (f.revenue for f in card.finance_history if f.revenue and f.revenue > 0),
                0,
            )
        if not last_revenue and company and company.revenue_last_year:
            last_revenue = company.revenue_last_year
        if last_revenue and employees:
            per_emp = last_revenue / employees
            block.append(f"Выручка на сотрудника: {_fmt_money_short(int(per_emp))}/год")
        sections.append("\n".join(block))

    # ━━━ ГОСКОНТРАКТЫ ━━━
    if card and (card.contracts_supplier_count or card.contracts_customer_count):
        block = ["━━━ 📦 ГОСКОНТРАКТЫ ━━━"]
        if card.contracts_supplier_count:
            block.append(
                f"Поставщик: {card.contracts_supplier_count} контрактов "
                f"на {_fmt_money_short(int(card.contracts_supplier_sum))}"
            )
        if card.contracts_customer_count:
            block.append(
                f"Заказчик: {card.contracts_customer_count} "
                f"на {_fmt_money_short(int(card.contracts_customer_sum))}"
            )
        if card.contracts_supplier_count > 50:
            block.append("✅ Активный поставщик государства")
        if card.is_unreliable_supplier:
            block.append("🔴 В реестре недобросовестных поставщиков (ФАС)")
            findings.append("🔴 В реестре недобросовестных поставщиков ФАС")
        sections.append("\n".join(block))

    # ━━━ КАПИТАЛ И ЛИЦЕНЗИИ ━━━
    capital_block = []
    cap_value = 0
    if card and card.capital:
        cap_value = card.capital
    elif company and company.capital:
        cap_value = company.capital
    if cap_value:
        capital_block.append(f"Уставный капитал: {_fmt_money_short(int(cap_value))}")
        if cap_value <= 10_000:
            capital_block.append(
                "🟡 Минимальный размер — стандарт для ООО, "
                "ограничивает ответственность"
            )
    if card and card.licenses_count:
        capital_block.append(f"Лицензий: {card.licenses_count}")
    if capital_block:
        sections.append("━━━ 💰 КАПИТАЛ И ЛИЦЕНЗИИ ━━━\n" + "\n".join(capital_block))

    # ━━━ ИНДЕКС ЗЧБ ━━━
    if security and (security.zchb_risk_level or security.zchb_details):
        block = ["━━━ 🎯 ИНДЕКС ЗЧБ ━━━"]
        if security.zchb_risk_level:
            block.append(f"Индекс компании: {security.zchb_risk_level}")
        if security.zchb_details:
            block.append(security.zchb_details)
        sections.append("\n".join(block))

    # ━━━ НА ЧТО ОБРАТИТЬ ВНИМАНИЕ ━━━
    # Дополнительные детекторы рисков из агрегированных данных
    risk_index = (security.zchb_risk_level or "").lower() if security else ""
    risk_taxes = ""
    if security and security.zchb_details:
        # zchb_details = "Налоговые риски: <уровень>"
        d = security.zchb_details.lower()
        if "высок" in d:
            risk_taxes = "высокий"
        elif "средн" in d:
            risk_taxes = "средний"

    if "низк" in risk_index:
        findings.append("🔴 Низкий индекс надёжности ЗЧБ — повышенный риск работы")
    elif "средн" in risk_index:
        findings.append("🟡 Средний индекс ЗЧБ — требует дополнительной проверки")
    if risk_taxes == "высокий":
        findings.append("🔴 Высокие налоговые риски (по оценке ЗЧБ)")
    elif risk_taxes == "средний":
        findings.append("🟡 Средние налоговые риски (по оценке ЗЧБ)")

    # Низкая рентабельность (только для НЕ-финансовых, у банков своя метрика)
    if not is_financial and history:
        last_with_data = next(
            (f for f in history
             if (f.revenue and f.revenue > 0) and f.profit is not None),
            None,
        )
        if last_with_data:
            margin = last_with_data.profit / last_with_data.revenue * 100
            if 0 < margin < 5:
                findings.append(
                    f"🟡 Низкая рентабельность: {margin:.0f}% — на грани окупаемости"
                )
            elif margin < 0:
                # уже было findings выше про убыточность
                pass

    # Малый штат + большая выручка (типичная схема технической компании)
    if not is_financial and employees and employees <= 5 and history:
        last_revenue = next(
            (f.revenue or f.income or 0 for f in history
             if (f.revenue or f.income or 0) > 0),
            0,
        )
        if last_revenue > 20_000_000:  # > 20 млн с малым штатом
            findings.append(
                f"🟡 Высокая выручка ({_fmt_money_short(int(last_revenue))}) "
                f"при штате {employees} — типично для торговых/технических компаний"
            )

    # Положительные сигналы — но не показываем противоречивые
    has_negative_signals = bool(findings) or "низк" in risk_index
    positives = []
    if card is not None:
        if (not card.in_debt_registry and not card.in_no_reporting_registry
                and not has_negative_signals):
            positives.append("✅ В реестрах ФНС нет негативных записей")
        if card.contracts_supplier_count > 0 and not card.is_unreliable_supplier:
            positives.append("✅ Работает с госконтрактами без претензий")
        if card.licenses_count > 0:
            positives.append(f"✅ Имеет лицензии ({card.licenses_count})")
    if "высок" in risk_index:
        positives.append("✅ Высокий индекс надёжности (ЗЧБ)")
    if fssp_count == 0:
        positives.append("✅ Нет исполнительных производств ФССП")
    if card and card.tax_debt_sum == 0:
        positives.append("✅ Нет задолженности перед бюджетом")
    if is_financial and not history:
        positives.append(
            "ℹ️ Это банк/страховщик — расширенная отчётность на cbr.ru"
        )

    summary_block = ["━━━ 📋 НА ЧТО ОБРАТИТЬ ВНИМАНИЕ ━━━"]
    if findings:
        summary_block.extend(findings)
    if positives:
        summary_block.extend(positives[:5])
    if not findings and not positives:
        summary_block.append(
            "Нет ярких сигналов — обычная картина. "
            "Проверьте основные параметры в разделах выше."
        )
    sections.append("\n".join(summary_block))

    return "\n".join(title_lines) + "\n\n" + "\n\n".join(sections)


def _inn_prompt_text(action: str) -> str:
    """Промпт «отправь ИНН или название».
    Один формат для всех режимов — заголовок зависит от действия."""
    titles = {
        "mode_internal_analysis": "🔍 Внутренний анализ компании",
        "mode_client_proposal":   "💼 Коммерческое предложение",
        "mode_compare":           "⚖️ Сравнение компаний",
        "mode_request":           "📨 Заявка",
        "mode_proposal":          "📝 Предложение",
        "kp_pdf":                 "📄 Генерация КП (PDF)",
        "kp_png":                 "🖼 Генерация КП (PNG)",
        "mode_mass_check":        "📋 Массовая проверка",
    }
    title = titles.get(action, "🔍 Проверка компании")
    if action == "mode_compare":
        return (
            f"{title}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Отправьте ИНН или НАЗВАНИЕ первой компании.\n\n"
            "Например:\n"
            "• 7707083893\n"
            "• Сбербанк"
        )
    return (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Отправьте ИНН или НАЗВАНИЕ компании.\n\n"
        "Например:\n"
        "• 7707083893\n"
        "• Сбербанк"
    )


async def handle_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Обработка нажатий на кнопки меню."""
    data = callback_query.data
    user_id = callback_query.from_user.id
    logger.info("Callback from user %s: %s", user_id, data)

    # Принятие оферты — доступно без онбординга
    if data == "accept_offer":
        await callback_query.answer()
        profile = user_store.get(user_id)
        if not profile.accepted_offer_at:
            profile.accepted_offer_at = datetime.now(timezone.utc).isoformat()
            user_store.save_profile(profile)
        # Показываем следующий шаг (канал или телефон) — он зависит от настроек
        next_step = await _onboarding_step_async(client, profile)
        if next_step is None:
            await callback_query.message.reply_text(
                "✅ Регистрация завершена. Можно начинать работу.",
                reply_markup=_reply_keyboard(),
            )
            await callback_query.message.reply_text(
                "Выберите действие:", reply_markup=_main_menu(),
            )
        else:
            await callback_query.message.reply_text("✅ Оферта принята.")
            await _send_onboarding_step(callback_query.message, next_step)
        return

    # Повторная проверка подписки на канал — доступна без полного онбординга
    if data == "check_channel":
        profile = user_store.get(user_id)
        if not profile.accepted_offer_at:
            await callback_query.answer("Сначала примите оферту", show_alert=True)
            await _send_onboarding_step(callback_query.message, "offer")
            return
        subscribed = await _is_subscribed_to_channel(client, user_id)
        if not subscribed:
            await callback_query.answer(
                "Похоже, вы ещё не подписаны. Подпишитесь и нажмите снова.",
                show_alert=True,
            )
            return
        await callback_query.answer("Подписка подтверждена!", show_alert=False)
        next_step = await _onboarding_step_async(client, profile)
        if next_step is None:
            await callback_query.message.reply_text(
                "✅ Регистрация завершена. Можно начинать работу.",
                reply_markup=_reply_keyboard(),
            )
            await callback_query.message.reply_text(
                "Выберите действие:", reply_markup=_main_menu(),
            )
        else:
            await _send_onboarding_step(callback_query.message, next_step)
        return

    # Гейт онбординга для всех остальных callback'ов
    profile = user_store.get(user_id)
    step = await _onboarding_step_async(client, profile)
    if step is not None:
        await callback_query.answer("Сначала пройдите регистрацию", show_alert=True)
        await _send_onboarding_step(callback_query.message, step)
        return

    # Кнопки действий под карточкой компании
    if data.startswith("ca_"):
        await callback_query.answer()
        action_part = data.split(":")[0]  # ca_courts, ca_ai, etc.
        inn_part = data.split(":")[1] if ":" in data else ""

        wip_actions = {}

        if action_part == "ca_courts" and inn_part:
            await callback_query.answer()
            if not zchb.enabled:
                await callback_query.message.reply_text(
                    "⚠️ Источник арбитражных дел не настроен (ZCHB_API_KEY)."
                )
                return
            await callback_query.message.reply_text("⚖️ Запрашиваю арбитражные дела...")
            summary = await zchb.get_arbitration(inn_part)
            await callback_query.message.reply_text(
                _format_arbitration(summary, inn_part),
                disable_web_page_preview=True,
            )
            return

        if action_part == "ca_finance" and inn_part:
            await callback_query.answer()
            if not zchb.enabled:
                await callback_query.message.reply_text(
                    "⚠️ Источник финансовых данных не настроен (ZCHB_API_KEY)."
                )
                return
            await callback_query.message.reply_text("📊 Собираю финансовый отчёт...")
            company = await company_service.fetch(inn_part)
            try:
                card = await zchb.get_card(inn_part)
            except Exception as exc:
                logger.exception("ca_finance: get_card failed: %s", exc)
                card = None
            sec = None
            try:
                sec = await security_service.check(
                    inn=inn_part,
                    name=company.name if company else None,
                    okved=company.okved_main if company else None,
                    ogrn=company.ogrn if company else None,
                )
            except Exception as exc:
                logger.exception("ca_finance: security check failed: %s", exc)
            try:
                text = _format_finance(card, sec, company, inn_part)
            except Exception as exc:
                logger.exception("ca_finance: format failed: %s", exc)
                await callback_query.message.reply_text(
                    "⚠️ Не удалось собрать финансовый отчёт. Попробуйте позже."
                )
                return
            await callback_query.message.reply_text(
                text, disable_web_page_preview=True,
            )
            return

        if action_part == "ca_links" and inn_part:
            await callback_query.answer()
            if not zchb.enabled:
                await callback_query.message.reply_text(
                    "⚠️ Источник связей не настроен (ZCHB_API_KEY)."
                )
                return
            await callback_query.message.reply_text(
                "🔗 Анализирую связи (директор и учредители)..."
            )
            card = await zchb.get_card(inn_part)
            text = await _format_links(card, inn_part)
            await callback_query.message.reply_text(
                text, disable_web_page_preview=True,
            )
            return

        if action_part == "ca_history" and inn_part:
            await callback_query.answer()
            if not zchb.enabled:
                await callback_query.message.reply_text(
                    "⚠️ Источник истории не настроен (ZCHB_API_KEY)."
                )
                return
            await callback_query.message.reply_text(
                "📜 Собираю историю изменений компании..."
            )
            company = await company_service.fetch(inn_part)
            try:
                card = await zchb.get_card(inn_part)
            except Exception as exc:
                logger.exception("ca_history: get_card failed: %s", exc)
                card = None
            ogrn = ""
            if card and card.ogrn:
                ogrn = card.ogrn
            elif company and company.ogrn:
                ogrn = company.ogrn
            events = None
            if ogrn:
                try:
                    events = await zchb.get_diffs(ogrn)
                except Exception as exc:
                    logger.exception("ca_history: get_diffs failed: %s", exc)
                    events = None
            try:
                text = _format_history(card, events, inn_part, company)
            except Exception as exc:
                logger.exception("ca_history: format failed: %s", exc)
                await callback_query.message.reply_text(
                    "⚠️ Не удалось собрать историю. Попробуйте позже.\n"
                    "Если ошибка повторяется — сообщите в поддержку: @YRS75"
                )
                return
            await callback_query.message.reply_text(
                text, disable_web_page_preview=True,
            )
            return

        if action_part == "ca_egryl" and inn_part:
            await callback_query.answer()
            if not zchb.enabled:
                await callback_query.message.reply_text(
                    "⚠️ Источник ЕГРЮЛ не настроен (ZCHB_API_KEY)."
                )
                return
            card = await zchb.get_card(inn_part)
            await callback_query.message.reply_text(
                _format_egrul_summary(card, inn_part),
                reply_markup=_egrul_actions_keyboard(inn_part),
                disable_web_page_preview=True,
            )
            return

        if action_part == "ca_egryl_history" and inn_part:
            await callback_query.answer(
                "Запрос полной истории — это +10 запросов ЗЧБ",
                show_alert=False,
            )
            await callback_query.message.reply_text(
                "📜 Загружаю полную историю записей ЕГРЮЛ "
                "(метод fns-card, ~10 секунд)..."
            )
            # fns-card требует ОГРН — берём из кешированной card
            card = await zchb.get_card(inn_part)
            ogrn = (card.ogrn if card else "") or ""
            if not ogrn:
                await callback_query.message.reply_text(
                    "⚠️ Не удалось определить ОГРН для запроса."
                )
                return
            records = await zchb.get_fns_card_egrul(ogrn)
            await callback_query.message.reply_text(
                _format_egrul_history(records, ogrn),
                disable_web_page_preview=True,
            )
            return

        if action_part == "ca_egryl_pdf" and inn_part:
            await callback_query.answer(
                "Выписка ЕГРЮЛ с ЭЦП ФНС доступна бесплатно "
                "на egrul.nalog.ru",
                show_alert=True,
            )
            await callback_query.message.reply_text(
                "📥 Полная выписка ЕГРЮЛ с электронной подписью ФНС\n\n"
                f"https://egrul.nalog.ru/index.html\n\n"
                f"Введите ИНН {inn_part} — получите PDF за 1 минуту, бесплатно."
            )
            return

        if action_part == "ca_pdf" and inn_part:
            await callback_query.answer()
            await callback_query.message.reply_text("📄 Готовлю PDF-отчёт...")
            company = await company_service.fetch(inn_part)
            sec = None
            try:
                sec = await security_service.check(
                    inn=inn_part,
                    name=company.name if company else None,
                    okved=company.okved_main if company else None,
                    ogrn=company.ogrn if company else None,
                )
            except Exception as exc:
                logger.error("PDF report security check failed for %s: %s",
                             inn_part, exc)
            parsed = ParseResult(
                raw_text=inn_part, inn=inn_part, mode="internal_analysis",
                is_request=False, is_proposal=False, company_data=company,
            )
            body = render_response(
                parsed=parsed, company=company, risk=set(), security=sec,
            )
            company_name = (company.name if company else inn_part) or inn_part
            title = f"Отчёт о проверке: {company_name}"
            try:
                content = build_kp_pdf(title, body, company)
            except Exception as exc:
                logger.exception("PDF build failed for %s: %s", inn_part, exc)
                error_text = "⚠️ Не удалось собрать PDF.\n"
                if "шрифт не найден" in str(exc).lower() or "font" in str(exc).lower():
                    error_text += (
                        "На сервере не установлен шрифт с поддержкой кириллицы. "
                        "Сообщите в поддержку: @YRS75"
                    )
                else:
                    error_text += "Сохраните текст отчёта из чата или попробуйте ещё раз."
                await callback_query.message.reply_text(error_text)
                return
            filename = f"report_{inn_part}.pdf"
            doc = BytesIO(content)
            doc.name = filename
            try:
                await callback_query.message.reply_document(
                    document=doc, file_name=filename,
                    caption=f"📄 Отчёт по ИНН {inn_part}",
                )
            except Exception as exc:
                logger.exception("PDF send failed for %s: %s", inn_part, exc)
                await callback_query.message.reply_text(
                    "⚠️ PDF собрался, но Telegram отказался принимать файл. "
                    "Сообщите в поддержку: @YRS75"
                )
            return

        if action_part == "ca_ai" and inn_part:
            await callback_query.answer()
            await callback_query.message.reply_text("🤖 Запрашиваю ИИ-анализ у GigaChat...")
            company = await company_service.fetch(inn_part)
            result = await gigachat.analyze_company(
                name=company.name if company else inn_part,
                inn=inn_part,
                okved=company.okved_main if company else None,
                okved_name=company.okved_name if company else None,
                age_years=company.age_years if company else None,
                revenue=company.revenue_last_year if company else None,
                profit=company.profit_last_year if company else None,
                employees=company.employees_count if company else None,
                region=company.region if company else None,
                status=company.status if company else None,
            )
            if result:
                company_name = company.name if company else inn_part
                await callback_query.message.reply_text(
                    f"🤖 ИИ-анализ\n"
                    f"Компания: {company_name}\n\n"
                    f"{result}"
                )
            else:
                await callback_query.message.reply_text(
                    "❌ Не удалось получить ИИ-анализ. Проверьте GIGACHAT_CREDENTIALS в .env"
                )
            return

        if action_part == "ca_monitor" and inn_part:
            await callback_query.message.reply_text("⏳ Добавляю в отслеживаемые...")
            await _do_monitor_add(callback_query.message, user_id, inn_part)
        elif action_part == "ca_unmonitor" and inn_part:
            removed = monitoring_store.remove(user_id, inn_part)
            if removed:
                await callback_query.message.reply_text(
                    f"🛑 Компания снята с отслеживания (ИНН {inn_part})."
                )
            else:
                await callback_query.message.reply_text(
                    f"⚠️ Компания не была в списке отслеживания (ИНН {inn_part})."
                )
        elif action_part in wip_actions:
            label = wip_actions[action_part]
            await callback_query.message.reply_text(
                f"⏳ {label} — раздел в разработке.\n"
                f"Будет доступен после подключения ЗЧБ и Контур.Фокус."
            )
        return

    # Выбор компании из результатов поиска по названию
    if data.startswith("search_select:"):
        await callback_query.answer()
        inn = data.split(":", 1)[1]
        if not inn:
            return
        # Засчитываем как обычную проверку — лимиты должны работать
        allowed = await _check_limit_and_count(callback_query.message, user_id)
        if not allowed:
            return
        company = await company_service.fetch(inn)
        sec_result = None
        try:
            sec_result = await security_service.check(
                inn=inn,
                name=company.name if company else None,
                okved=company.okved_main if company else None,

                ogrn=company.ogrn if company else None,
            )
        except Exception as exc:
            logger.error("Security check failed for INN %s: %s", inn, exc)
        parsed_inner = ParseResult(
            raw_text=inn, inn=inn, mode="internal_analysis",
            is_request=False, is_proposal=False, company_data=company,
        )
        reply = render_response(
            parsed=parsed_inner, company=company, risk=set(), security=sec_result,
        )
        await callback_query.message.reply_text(
            reply,
            disable_web_page_preview=True,
            reply_markup=_company_actions_keyboard(inn, user_id),
        )
        return

    # Реферальная программа — показывается из карточки профиля
    if data == "referral_show":
        await callback_query.answer()
        profile = user_store.get(user_id)
        text = _format_referral_message(profile, _bot_username(client))
        await callback_query.message.reply_text(text, disable_web_page_preview=True)
        return

    # Список «Мои компании» из карточки профиля
    if data == "my_companies":
        await callback_query.answer()
        await callback_query.message.reply_text(
            _my_companies_text(user_id),
            reply_markup=_my_companies_keyboard(user_id),
        )
        return

    # Управление автопродлением подписки (из карточки профиля)
    if data == "sub_cancel":
        await callback_query.answer()
        profile = user_store.get(user_id)
        if profile.tariff == "free" or not profile.is_subscription_active():
            await callback_query.message.reply_text(
                "У вас нет активной платной подписки."
            )
            return
        # Если подключены платежи — отменяем подписку и в Точке.
        # В любом случае выключаем auto_renew локально.
        if subscription_service is not None:
            try:
                await subscription_service.cancel_user_subscription(user_id)
            except Exception as exc:
                logger.warning("sub_cancel via Tochka failed: %s", exc)
                user_store.disable_auto_renew(user_id)
        else:
            user_store.disable_auto_renew(user_id)
        profile = user_store.get(user_id)
        expires = profile.tariff_expires_at[:10] if profile.tariff_expires_at else "—"
        await callback_query.message.reply_text(
            "🔕 Автопродление отключено.\n\n"
            f"Подписка останется активной до {expires}, "
            "после этого тариф переключится на Free.\n\n"
            "Можно включить обратно в любой момент — кнопка появится "
            "в карточке профиля."
        )
        return

    if data == "sub_enable":
        await callback_query.answer()
        profile = user_store.get(user_id)
        if profile.tariff == "free" or not profile.is_subscription_active():
            await callback_query.message.reply_text(
                "Сначала оформите подписку через «💎 Тарифы»."
            )
            return
        user_store.enable_auto_renew(user_id)
        await callback_query.message.reply_text(
            "🔔 Автопродление включено.\n\n"
            "Тариф будет автоматически продлеваться по окончании срока."
        )
        return

    # Открыть свежий отчёт по компании из списка «Мои компании»
    if data.startswith("mc_open:"):
        await callback_query.answer()
        inn = data.split(":", 1)[1]
        if not inn:
            return
        allowed = await _check_limit_and_count(callback_query.message, user_id)
        if not allowed:
            return
        company = await company_service.fetch(inn)
        sec_result = None
        try:
            sec_result = await security_service.check(
                inn=inn,
                name=company.name if company else None,
                okved=company.okved_main if company else None,

                ogrn=company.ogrn if company else None,
            )
        except Exception as exc:
            logger.error("Security check failed for INN %s: %s", inn, exc)
        parsed_inner = ParseResult(
            raw_text=inn, inn=inn, mode="internal_analysis",
            is_request=False, is_proposal=False, company_data=company,
        )
        reply = render_response(
            parsed=parsed_inner, company=company, risk=set(), security=sec_result,
        )
        await callback_query.message.reply_text(
            reply,
            disable_web_page_preview=True,
            reply_markup=_company_actions_keyboard(inn, user_id),
        )
        return

    # Удаление компании из списка отслеживания
    if data.startswith("mc_remove:"):
        inn = data.split(":", 1)[1]
        removed = monitoring_store.remove(user_id, inn)
        await callback_query.answer(
            "Снята с отслеживания" if removed else "Не была в списке",
            show_alert=False,
        )
        await callback_query.message.reply_text(
            _my_companies_text(user_id),
            reply_markup=_my_companies_keyboard(user_id),
        )
        return

    # Кнопки выбора тарифа — показываем меню методов оплаты
    if data.startswith("tariff_"):
        await callback_query.answer()
        tariff = data.replace("tariff_", "")
        if tariff not in TARIFF_PRICES:
            await callback_query.message.reply_text("Тариф не найден.")
            return
        # Для фискального чека нужен email. Запрашиваем его один раз.
        if not profile.email:
            _user_state[user_id] = {"action": "await_email", "tariff": tariff}
            await callback_query.message.reply_text(
                "📧 Перед оплатой введите email — на него придёт фискальный "
                "чек (требование 54-ФЗ).\n\n"
                "Введите адрес одной строкой:"
            )
            return
        await _show_payment_methods(callback_query.message, tariff)
        return

    # Кнопки выбора метода оплаты: pay_<method>_<tariff>
    if data.startswith("pay_"):
        parts = data.split("_", 2)
        if len(parts) != 3 or parts[1] not in PAYMENT_METHODS:
            await callback_query.answer("Неизвестный метод оплаты", show_alert=True)
            return
        _, method, tariff = parts
        if tariff not in TARIFF_PRICES:
            await callback_query.answer("Тариф не найден", show_alert=True)
            return
        await callback_query.answer()
        await _handle_buy_tariff(
            callback_query.message, user_id, tariff, method=method,
        )
        return

    if data == "mode_mass_check":
        await callback_query.answer()
        profile = user_store.get(user_id)
        if profile.tariff != "business":
            await callback_query.message.reply_text(
                "📋 Массовая проверка доступна только на тарифе Business.\n\n"
                "Перейдите на Business для получения доступа — нажмите «💎 Тарифы»."
            )
            return
        _user_state[user_id] = "mode_mass_check"
        await callback_query.message.reply_text(
            "Отправьте несколько ИНН — по одному в строке или через запятую:"
        )
        return

    _user_state[user_id] = data
    await callback_query.answer()
    await callback_query.message.reply_text(_inn_prompt_text(data))


async def handle_contact(client: Client, message) -> None:
    """Обрабатывает поделённый контакт — шаг 'phone' онбординга."""
    user_id = message.from_user.id
    contact = message.contact
    if contact is None:
        return
    # Принимаем только свой собственный контакт. Если клиент попытается
    # отправить чужой — игнорируем и просим свой.
    if contact.user_id and contact.user_id != user_id:
        await message.reply_text(
            "Поделитесь, пожалуйста, своим номером, а не чужим контактом.",
            reply_markup=_phone_keyboard(),
        )
        return

    phone = (contact.phone_number or "").strip()
    if not phone:
        await message.reply_text(
            "Не удалось получить номер. Попробуйте ещё раз.",
            reply_markup=_phone_keyboard(),
        )
        return
    if not phone.startswith("+"):
        phone = "+" + phone

    profile = user_store.get(user_id)
    profile.phone = phone
    user_store.save_profile(profile)

    next_step = await _onboarding_step_async(client, profile)
    if next_step is None:
        await message.reply_text(
            f"✅ Номер сохранён: {phone}\n\n"
            "Регистрация завершена. Можно начинать работу.",
            reply_markup=_reply_keyboard(),
        )
        await message.reply_text("Выберите действие:", reply_markup=_main_menu())
    else:
        await message.reply_text(f"✅ Номер сохранён: {phone}")
        await _send_onboarding_step(message, next_step)


async def handle_text_message(client: Client, message) -> None:
    """Обработка текстовых сообщений."""
    text: str = message.text or ""
    user_id = message.from_user.id

    # Гейт онбординга. Email-гейт срабатывает уже после полного онбординга
    # и обрабатывается ниже отдельной веткой.
    profile = user_store.get(user_id)
    step = await _onboarding_step_async(client, profile)
    if step is not None:
        await _send_onboarding_step(message, step)
        return

    # Шаг ввода email перед первой оплатой
    pending_email_tariff = _user_state.get(user_id)
    if isinstance(pending_email_tariff, dict) and pending_email_tariff.get("action") == "await_email":
        clean = text.strip()
        if not EMAIL_RE.match(clean):
            await message.reply_text(
                "⚠️ Это не похоже на email. Введите корректный адрес "
                "(например, name@example.com):"
            )
            return
        profile.email = clean
        user_store.save_profile(profile)
        tariff = pending_email_tariff["tariff"]
        _user_state.pop(user_id, None)
        await message.reply_text(f"✅ Email сохранён: {clean}")
        await _show_payment_methods(message, tariff)
        return

    parsed: ParseResult = parse_message(text)
    logger.info("Parsed message from user %s: %s", user_id, parsed)

    # Обработка Reply-кнопок (нижнее меню)
    reply_action = _match_reply_button(text)
    if reply_action:
        # Тарифы — показываем сразу, ИНН не нужен
        if reply_action == "show_tariffs":
            await message.reply_text(_tariffs_text(), reply_markup=_tariffs_keyboard())
            return
        # Профиль — показываем сразу, ИНН не нужен
        if reply_action == "show_profile":
            profile = user_store.get(user_id)
            mon_count = monitoring_store.count_for_user(user_id)
            mon_limit = _monitoring_limit(profile)
            await message.reply_text(
                render_profile(profile, mon_count, mon_limit),
                reply_markup=_profile_keyboard(profile),
            )
            return
        # Остальные действия — запрашиваем ИНН
        _user_state.pop(user_id, None)
        _user_state[user_id] = reply_action
        await message.reply_text(_inn_prompt_text(reply_action))
        return

    # Если пользователь в состоянии ожидания ИНН
    pending_action = _user_state.pop(user_id, None)

    # Многошаговое сравнение — шаг 2: ждём ИНН второй компании
    if isinstance(pending_action, dict) and pending_action.get("action") == "compare_step2":
        if parsed.inn:
            inn1 = pending_action["inn1"]
            inn2 = parsed.inn
            await message.reply_text("🔍 Загружаю данные обеих компаний...")
            company1, company2 = await asyncio.gather(
                _fetch_company(inn1),
                _fetch_company(inn2),
            )
            reply = render_comparison(company1, inn1, company2, inn2)
            await message.reply_text(reply, disable_web_page_preview=True)
        else:
            await message.reply_text(
                "⚠️ Не распознала ИНН. Состояние сброшено.\n\n"
                "Нажмите /menu для выбора действия."
            )
        return

    if pending_action == "monitor_add":
        if parsed.inn:
            await _do_monitor_add(message, user_id, parsed.inn)
        else:
            await message.reply_text(
                "⚠️ Не распознала ИНН. Подписка не создана. /monitor <ИНН>"
            )
        return

    if pending_action == "mode_mass_check":
        inns = re.findall(r'\b\d{10}(?:\d{2})?\b', text)
        if not inns:
            await message.reply_text(
                "⚠️ Не найдено ни одного ИНН.\n\n"
                "Отправьте ИНН через запятую или по одному в строке:"
            )
            _user_state[user_id] = "mode_mass_check"
            return
        await message.reply_text(f"🔍 Начинаю проверку {len(inns)} ИНН...")
        for inn in inns:
            allowed = await _check_limit_and_count(message, user_id)
            if not allowed:
                break
            company = await _fetch_company(inn)
            sec_result = None
            try:
                sec_result = await security_service.check(
                    inn=inn,
                    name=company.name if company else None,
                    okved=company.okved_main if company else None,

                    ogrn=company.ogrn if company else None,
                )
            except Exception as exc:
                logger.error("Security check failed: %s", exc)
            parsed_inner = ParseResult(
                raw_text=inn, inn=inn, mode="internal_analysis",
                is_request=False, is_proposal=False, company_data=company,
            )
            reply = render_response(parsed=parsed_inner, company=company, risk=set(), security=sec_result)
            await message.reply_text(
                reply,
                disable_web_page_preview=True,
                reply_markup=_company_actions_keyboard(inn, user_id),
            )
        return

    if pending_action and parsed.inn:
        company = await _fetch_company(parsed.inn)
        await _dispatch_action(message, pending_action, parsed, company)
        return
    elif pending_action and not parsed.inn:
        # Не ИНН — пробуем найти по названию через DaData suggest
        if _looks_like_company_query(text):
            suggestions = await company_service.suggest(text.strip(), count=5)
            if suggestions:
                # Возвращаем pending_action на место — пусть пользователь
                # сначала выберет компанию из списка, потом получит отчёт
                # через search_select callback.
                await message.reply_text(
                    f"🔎 По запросу «{text.strip()}» найдено "
                    f"{len(suggestions)}. Выберите компанию для проверки:",
                    reply_markup=_search_results_keyboard(suggestions),
                )
                return
            await message.reply_text(
                f"🔎 По запросу «{text.strip()}» ничего не найдено.\n\n"
                "Попробуйте другое начало названия или отправьте ИНН "
                "напрямую (10 или 12 цифр)."
            )
            return
        # Совсем не похоже на название (например, «привет») — сбрасываем
        await message.reply_text(
            "⚠️ Не распознала ИНН или название. Состояние сброшено.\n\n"
            "Отправьте ИНН (10 или 12 цифр) или название компании, "
            "либо нажмите /menu для выбора действия."
        )
        return

    # Обычная обработка текста с ИНН
    company = parsed.company_data
    if not company and parsed.inn:
        company = await _fetch_company(parsed.inn)

    # Проверка текстовых триггеров КП
    lower_text = text.lower()
    if "кп pdf" in lower_text or "kp pdf" in lower_text:
        await _send_kp_auto(message, parsed, company, fmt="pdf")
        return
    if "кп png" in lower_text or "kp png" in lower_text:
        await _send_kp_auto(message, parsed, company, fmt="png")
        return

    # Если есть ИНН — анализируем
    if parsed.inn or parsed.company_data:
        risk = assess_risk(text)
        reply = render_response(parsed=parsed, company=company, risk=risk)
        await message.reply_text(reply, disable_web_page_preview=True)
        return

    # Если запрос явно похож на вопрос — сразу ИИ-помощник, без DaData.
    # Иначе юзер увидит «по запросу не найдено» вместо ответа на свой вопрос.
    if _looks_like_question(text):
        ai_reply = await gigachat.help_user(text.strip())
        if ai_reply:
            await message.reply_text(_format_help_reply(ai_reply))
            return
        # ИИ недоступен — даём минимальный fallback
        await message.reply_text(
            "👋 Я помогаю с проверкой компаний. Отправьте ИНН "
            "или название, либо нажмите /menu."
        )
        return

    # Поиск по началу названия компании (DaData suggest)
    if _looks_like_company_query(text):
        suggestions = await company_service.suggest(text.strip(), count=5)
        if suggestions:
            await message.reply_text(
                f"🔎 По запросу «{text.strip()}» найдено {len(suggestions)}. "
                "Выберите компанию для проверки:",
                reply_markup=_search_results_keyboard(suggestions),
            )
            return
        # Suggest пуст — это либо очень редкое название, либо вопрос
        # которого мы не распознали выше. ИИ-помощник как fallback.
        ai_reply = await gigachat.help_user(text.strip())
        if ai_reply:
            await message.reply_text(_format_help_reply(ai_reply))
            return
        await message.reply_text(
            f"🔎 По запросу «{text.strip()}» ничего не найдено.\n\n"
            "Попробуйте другое начало названия или отправьте ИНН напрямую."
        )
        return

    # Ничего не подошло — короткий текст или неизвестная slash-команда.
    # ИИ подскажет.
    ai_reply = await gigachat.help_user(text.strip())
    if ai_reply:
        await message.reply_text(_format_help_reply(ai_reply))
        return

    await message.reply_text(
        "👋 Отправьте ИНН или название компании, либо нажмите /menu."
    )


def _format_help_reply(ai_text: str) -> str:
    """Форматирует ответ ИИ-помощника единым стилем."""
    return (
        "🤖 Помощник MondayCompany\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ai_text.strip()}\n\n"
        "💡 /menu — главное меню"
    )


def _search_results_keyboard(suggestions) -> InlineKeyboardMarkup:
    """Inline-клавиатура с результатами поиска по названию.
    callback_data = search_select:<инн>; кнопки без ИНН не добавляются."""
    rows = []
    for company in suggestions:
        if not company.inn:
            continue
        name = (company.name or company.inn).strip()
        # Telegram-лимит на текст кнопки — около 64 символов
        max_name = 64 - len(company.inn) - 4  # учитываем " (" и ")"
        if len(name) > max_name and max_name > 0:
            name = name[:max_name].rstrip() + "…"
        label = f"{name} ({company.inn})"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"search_select:{company.inn}"),
        ])
    return InlineKeyboardMarkup(rows)


def _looks_like_company_query(text: str) -> bool:
    """Эвристика: похож ли текст на запрос названия компании.
    Используем перед DaData-запросом, чтобы не платить за «привет»."""
    clean = text.strip()
    if len(clean) < 3:
        return False
    if clean.startswith("/"):
        return False
    # Должна быть хотя бы одна буква (кириллическая или латинская)
    return any(c.isalpha() for c in clean)


# Слова которые почти наверняка означают вопрос/просьбу о помощи,
# а не название компании. На таком запросе НЕ дёргаем DaData (иначе
# юзер увидит «по запросу X не найдено» — это путает).
_QUESTION_MARKERS = (
    "как ", "что ", "что-", "где ", "когда ", "почему", "зачем",
    "можно ли", "можно", "помоги", "помогите", "подскажи", "подскажите",
    "не понимаю", "не работает", "не могу", "хочу ", "нужно ",
    "что делать", "как сделать", "как получить", "как найти",
    "как добавить", "как отменить", "как проверить",
)


def _looks_like_question(text: str) -> bool:
    """True если текст явно вопрос/просьба, а не название компании."""
    clean = text.strip().lower()
    if not clean:
        return False
    if "?" in clean:
        return True
    return any(clean.startswith(m) or f" {m}" in f" {clean}"
               for m in _QUESTION_MARKERS)


def _user_has_premium(user_id: int) -> bool:
    """Премиум = активная подписка Pro или Business."""
    if not user_id:
        return False
    profile = user_store.get(user_id)
    if not profile.is_subscription_active():
        return False
    return profile.effective_tariff() in ("pro", "business")


def _build_web_report_button(
    inn: str, user_id: int,
) -> Optional[InlineKeyboardButton]:
    """Кнопка «🌐 Веб-отчёт» — только для премиума и при настроенном URL.

    Возвращает None, если функция недоступна (не премиум, нет URL,
    создание токена упало) — кнопку показывать не будем.
    """
    if not report_base_url:
        return None
    if not _user_has_premium(user_id):
        return None
    try:
        token = report_tokens.create(user_id=user_id, inn=inn)
    except Exception as exc:
        logger.warning("Не удалось создать токен веб-отчёта: %s", exc)
        return None
    url = f"{report_base_url}/report/{token}"
    return InlineKeyboardButton("🌐 Веб-отчёт", web_app=WebAppInfo(url=url))


def _company_actions_keyboard(inn: str, user_id: int = 0) -> InlineKeyboardMarkup:
    """Кнопки действий под карточкой компании."""
    is_monitored = bool(user_id) and monitoring_store.get(user_id, inn) is not None
    if is_monitored:
        monitor_btn = InlineKeyboardButton(
            "✅ Отслеживается — снять", callback_data=f"ca_unmonitor:{inn}"
        )
    else:
        monitor_btn = InlineKeyboardButton(
            "👁 Отслеживать", callback_data=f"ca_monitor:{inn}"
        )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("⚖️ Суды", callback_data=f"ca_courts:{inn}"),
            InlineKeyboardButton("📊 Финансы", callback_data=f"ca_finance:{inn}"),
        ],
        [
            InlineKeyboardButton("🤖 ИИ-анализ", callback_data=f"ca_ai:{inn}"),
            InlineKeyboardButton("🏛 ЕГРЮЛ", callback_data=f"ca_egryl:{inn}"),
        ],
        [
            InlineKeyboardButton("📜 История", callback_data=f"ca_history:{inn}"),
            InlineKeyboardButton("🔗 Связи", callback_data=f"ca_links:{inn}"),
        ],
        [monitor_btn],
        [
            InlineKeyboardButton("📄 Скачать PDF", callback_data=f"ca_pdf:{inn}"),
        ],
    ]
    web_btn = _build_web_report_button(inn, user_id)
    if web_btn is not None:
        # Веб-отчёт ставим самой первой кнопкой — это главная фича премиума.
        rows.insert(0, [web_btn])
    return InlineKeyboardMarkup(rows)


def _tariffs_text() -> str:
    return (
        "💎 Тарифные планы\n"
        "\n"
        "─── 🆓 Free ───\n"
        "Бесплатно навсегда\n"
        "• 3 проверки в день\n"
        "• Краткий отчёт + светофор\n"
        "• Стоп-листы и суды (сводка)\n"
        "\n"
        "─── ⭐️ Start ───\n"
        "💰 490 ₽/мес\n"
        "📊 50 проверок/день\n"
        "  ✅ Полный отчёт\n"
        "  ✅ ЕГРЮЛ\n"
        "  ✅ Суды/ФССП\n"
        "  ✅ Стоп-листы\n"
        "\n"
        "─── 💎 Pro ───\n"
        "💰 1 290 ₽/мес\n"
        "📊 300 проверок/день\n"
        "  ✅ Всё из Start\n"
        "  ✅ ИИ-анализ\n"
        "  ✅ Связи\n"
        "  ✅ История\n"
        "  ✅ Мониторинг\n"
        "\n"
        "─── 🏆 Business ───\n"
        "💰 2 490 ₽/мес\n"
        "📊 Безлимитные проверки\n"
        "  ✅ Всё из Pro\n"
        "  ✅ API доступ\n"
        "  ✅ Массовые проверки\n"
        "  ✅ PDF/1С экспорт\n"
        "\n"
        "─── 💳 Оплата ───\n"
        "Для подключения тарифа нажмите соответствующую кнопку\n\n"
        "Цены на 15% ниже аналогов (Контур, Руспрофайл)"
    )


def _tariffs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐️ Start — 490 ₽/мес", callback_data="tariff_start"),
        ],
        [
            InlineKeyboardButton("💎 Pro — 1 290 ₽/мес", callback_data="tariff_pro"),
        ],
        [
            InlineKeyboardButton("🏆 Business — 2 490 ₽/мес", callback_data="tariff_business"),
        ],
    ])


PAYMENT_METHODS = ("card", "sbp", "tpay", "sberpay")


def _payment_methods_keyboard(tariff: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ СБП", callback_data=f"pay_sbp_{tariff}")],
        [InlineKeyboardButton("🟢 SberPay", callback_data=f"pay_sberpay_{tariff}")],
        [InlineKeyboardButton("🟡 T-Pay", callback_data=f"pay_tpay_{tariff}")],
        [InlineKeyboardButton("💳 Номер карты", callback_data=f"pay_card_{tariff}")],
    ])


async def _show_payment_methods(message, tariff: str) -> None:
    price = TARIFF_PRICES[tariff]
    await message.reply_text(
        f"Тариф *{tariff.upper()}* — {price} ₽/мес.\n\n"
        "Выберите способ оплаты:",
        reply_markup=_payment_methods_keyboard(tariff),
    )


async def _handle_buy_tariff(
    message, user_id: int, tariff: str, method: str = "",
) -> None:
    """Создаёт платёжную ссылку выбранным методом и отправляет кнопку оплаты."""
    if subscription_service is None:
        await message.reply_text(
            "⚠️ Приём платежей пока не настроен. Обратитесь к администратору."
        )
        return

    await message.reply_text("💳 Создаю платёжную ссылку...")
    try:
        link, op_id = await subscription_service.create_initial_payment(
            user_id, tariff, method=method,
        )
    except YooKassaError as exc:
        # Метод оплаты не подключён в магазине ЮKassa, либо временная ошибка.
        logger.warning(
            "YooKassa payment creation failed for method=%s: %s", method, exc,
        )
        await message.reply_text(
            "❌ Этот способ оплаты временно недоступен.\n"
            "Попробуйте другой способ из меню.",
            reply_markup=_payment_methods_keyboard(tariff),
        )
        return
    except Exception as exc:
        logger.exception("Payment creation failed: %s", exc)
        await message.reply_text(
            "❌ Не удалось создать платёж. Попробуйте позже или свяжитесь с поддержкой."
        )
        return

    price = TARIFF_PRICES[tariff]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {price} ₽", url=link)],
    ])
    await message.reply_text(
        f"Счёт на оплату тарифа *{tariff.upper()}* — {price} ₽/мес.\n\n"
        "После успешной оплаты тариф активируется автоматически.\n"
        "Способ оплаты сохранится для автопродления — "
        "отключить: /cancel_subscription\n\n"
        "Нажимая «Оплатить», вы принимаете условия /offer",
        reply_markup=keyboard,
    )


def _match_reply_button(text: str) -> Optional[str]:
    """Сопоставляет текст Reply-кнопок с действиями."""
    mapping = {
        "проверка компании": "mode_internal_analysis",
        "сравнить": "mode_compare",
        "профиль": "show_profile",
        "тарифы": "show_tariffs",
    }
    # Убираем эмодзи и лишние пробелы
    clean = text.strip()
    for char in clean:
        if ord(char) > 0xFFFF:
            clean = clean.replace(char, "")
    clean = clean.strip().lower()

    for keyword, action in mapping.items():
        if keyword in clean:
            return action
    return None


async def _dispatch_action(
    message, action: str, parsed: ParseResult, company: Optional[CompanyData]
) -> None:
    """Выполняет действие после получения ИНН."""
    risk = assess_risk(message.text or "")
    user_id = message.from_user.id

    # Проверяем лимит для действий, связанных с проверкой компании
    if action in ("mode_internal_analysis", "mode_compare"):
        allowed = await _check_limit_and_count(message, user_id)
        if not allowed:
            return

    if action == "mode_internal_analysis":
        # Проверка безопасности — встраиваем в анализ
        sec_result = None
        if parsed.inn:
            try:
                sec_result = await security_service.check(
                    inn=parsed.inn,
                    name=company.name if company else None,
                    okved=company.okved_main if company else None,

                    ogrn=company.ogrn if company else None,
                )
            except Exception as exc:
                logger.error("Security check failed for INN %s: %s", parsed.inn, exc)

        parsed_with_mode = ParseResult(
            raw_text=parsed.raw_text,
            inn=parsed.inn,
            mode="internal_analysis",
            is_request=False,
            is_proposal=False,
            company_data=company,
        )
        reply = render_response(parsed=parsed_with_mode, company=company, risk=risk, security=sec_result)
        inn = parsed.inn or ""
        await message.reply_text(
            reply,
            disable_web_page_preview=True,
            reply_markup=_company_actions_keyboard(inn, user_id),
        )

    elif action == "mode_client_proposal":
        parsed_with_mode = ParseResult(
            raw_text=parsed.raw_text,
            inn=parsed.inn,
            mode="client_proposal",
            is_request=False,
            is_proposal=False,
            company_data=company,
        )
        reply = render_response(parsed=parsed_with_mode, company=company, risk=risk)
        await message.reply_text(reply, disable_web_page_preview=True)

    elif action == "mode_compare":
        # Шаг 1 сравнения: получили ИНН первой компании, просим вторую
        _user_state[message.from_user.id] = {"action": "compare_step2", "inn1": parsed.inn}
        await message.reply_text(
            f"✅ Первая компания: {company.name if company else parsed.inn}\n\n"
            "Теперь отправьте ИНН второй компании для сравнения:"
        )

    elif action == "mode_request":
        parsed_with_mode = ParseResult(
            raw_text=parsed.raw_text,
            inn=parsed.inn,
            mode=None,
            is_request=True,
            is_proposal=False,
            company_data=company,
        )
        reply = render_response(parsed=parsed_with_mode, company=company, risk=risk)
        await message.reply_text(reply, disable_web_page_preview=True)

    elif action == "mode_proposal":
        parsed_with_mode = ParseResult(
            raw_text=parsed.raw_text,
            inn=parsed.inn,
            mode=None,
            is_request=False,
            is_proposal=True,
            company_data=company,
        )
        reply = render_response(parsed=parsed_with_mode, company=company, risk=risk)
        await message.reply_text(reply, disable_web_page_preview=True)

    elif action == "kp_pdf":
        title, body = _kp_template()
        await _send_kp_file(message, parsed, company, title, body, "pdf")

    elif action == "kp_png":
        title, body = _kp_template()
        await _send_kp_file(message, parsed, company, title, body, "png")


async def _fetch_company(inn: str) -> Optional[CompanyData]:
    return await company_service.fetch(inn)


async def _check_limit_and_count(message, user_id: int) -> bool:
    """Проверяет лимит проверок и увеличивает счётчик.
    Возвращает True если проверка разрешена, False — если лимит исчерпан."""
    profile = user_store.get(user_id)
    if not profile.can_check():
        from user_store import TARIFF_LIMITS
        limit = TARIFF_LIMITS.get(profile.tariff, 0)
        await message.reply_text(
            f"⛔️ Лимит проверок исчерпан.\n\n"
            f"Ваш тариф: {profile.tariff.upper()} — {limit} проверок в день.\n"
            f"Лимит обновится завтра.\n\n"
            f"Для увеличения лимита перейдите на более высокий тариф — нажмите «Тарифы»."
        )
        return False
    user_store.increment_checks(user_id)
    return True


def _extract_format(args: list[str]) -> str:
    return args[1].lower() if len(args) >= 2 else "pdf"


def _extract_inn_arg(args: list[str]) -> Optional[str]:
    return args[2] if len(args) >= 3 else None


async def _resolve_company(text: str, inn_arg: Optional[str]) -> Optional[CompanyData]:
    parsed: ParseResult = parse_message(text)
    if parsed.company_data:
        return parsed.company_data

    inn = inn_arg or parsed.inn
    if not inn:
        return None

    return await _fetch_company(inn)


def _kp_template() -> tuple[str, str]:
    title = "Коммерческое предложение"
    body = (
        "— Индивидуальная настройка РКО и платежной архитектуры.\n"
        "— Согласование лимитов и назначений, чтобы не ловить стопы.\n"
        "— Сопровождение по комплаенсу и ответы на запросы банка.\n"
        "— Канал связи с менеджером и быстрые консультации по операциям."
    )
    return title, body


def _kp_filename(company: Optional[CompanyData], parsed: ParseResult, ext: str) -> str:
    inn = None
    if company and company.inn:
        inn = company.inn
    elif parsed.inn:
        inn = parsed.inn
    suffix = inn or "unknown"
    return f"kp_{suffix}.{ext}"


async def _send_kp_auto(message, parsed: ParseResult, company: Optional[CompanyData], fmt: str) -> None:
    title, body = _kp_template()
    await _send_kp_file(message, parsed, company, title, body, fmt)


async def _send_kp_file(
    message,
    parsed: ParseResult,
    company: Optional[CompanyData],
    title: str,
    body: str,
    fmt: str,
) -> None:
    filename = _kp_filename(company, parsed, fmt)
    if fmt == "png":
        content = build_kp_png(title, body, company)
        save_file_bytes(content, filename)
        metadata_store.append(filename, company, "png")
        photo = BytesIO(content)
        photo.name = filename
        await message.reply_photo(photo, caption="Ваше КП (PNG)")
    else:
        content = build_kp_pdf(title, body, company)
        save_file_bytes(content, filename)
        metadata_store.append(filename, company, "pdf")
        doc = BytesIO(content)
        doc.name = filename
        await message.reply_document(document=doc, file_name=filename, caption="Ваше КП (PDF)")


async def handle_kp_command(client: Client, message) -> None:
    """
    Команда: /kp <pdf|png> <ИНН?>
    Если ИНН не указан — просим прислать.
    """
    text = message.text or ""
    args = text.split()
    fmt = _extract_format(args)
    inn = _extract_inn_arg(args)

    if not inn:
        parsed = parse_message(text)
        inn = parsed.inn

    if not inn:
        action = "kp_pdf" if fmt == "pdf" else "kp_png"
        _user_state[message.from_user.id] = action
        await message.reply_text("Для генерации КП отправьте ИНН компании (10 или 12 цифр):")
        return

    company = await _fetch_company(inn)
    parsed = parse_message(text)
    title, body = _kp_template()
    await _send_kp_file(message, parsed, company, title, body, fmt)


async def handle_my_subscription(client: Client, message) -> None:
    """Показывает статус подписки."""
    user_id = message.from_user.id
    profile = user_store.get(user_id)
    if profile.tariff == "free":
        await message.reply_text(
            "🆓 У вас бесплатный тариф Free — 3 проверки в день.\n\n"
            "Чтобы оформить подписку, нажмите «Тарифы»."
        )
        return
    expires = profile.tariff_expires_at[:10] if profile.tariff_expires_at else "—"
    auto = "включено" if profile.auto_renew else "выключено"
    active = "активна" if profile.is_subscription_active() else "истекла"
    await message.reply_text(
        f"📄 Ваша подписка\n\n"
        f"Тариф: {profile.tariff.upper()}\n"
        f"Статус: {active}\n"
        f"Действует до: {expires}\n"
        f"Автопродление: {auto}\n\n"
        f"Отключить автопродление: /cancel_subscription\n"
        f"Включить автопродление: /enable_subscription"
    )


async def handle_cancel_subscription(client: Client, message) -> None:
    user_id = message.from_user.id
    # Если подключены платежи — отменяем подписку и на стороне Точки.
    # Без этого Точка может попытаться списать с карты в свой график.
    if subscription_service is not None:
        try:
            await subscription_service.cancel_user_subscription(user_id)
        except Exception as exc:
            logger.warning("cancel_user_subscription failed: %s", exc)
            user_store.disable_auto_renew(user_id)
    else:
        user_store.disable_auto_renew(user_id)
    profile = user_store.get(user_id)
    expires = profile.tariff_expires_at[:10] if profile.tariff_expires_at else "—"
    await message.reply_text(
        "🔕 Автопродление отключено.\n\n"
        f"Подписка останется активной до {expires}, затем переключится на Free.\n"
        f"Включить обратно: /enable_subscription"
    )


async def handle_enable_subscription(client: Client, message) -> None:
    user_id = message.from_user.id
    profile = user_store.get(user_id)
    if profile.tariff == "free" or not profile.is_subscription_active():
        await message.reply_text(
            "Сначала оформите подписку через «Тарифы»."
        )
        return
    user_store.enable_auto_renew(user_id)
    await message.reply_text("🔔 Автопродление включено.")


def _monitoring_limit(profile) -> Optional[int]:
    return TARIFF_MONITORING_LIMITS.get(profile.effective_tariff(), 0)


async def handle_monitor_add(client: Client, message) -> None:
    """Команда: /monitor <ИНН> — подписаться на мониторинг."""
    text = message.text or ""
    args = text.split()
    user_id = message.from_user.id
    inn = args[1] if len(args) >= 2 else None

    if not inn:
        # Запросим ИНН
        _user_state[user_id] = "monitor_add"
        await message.reply_text(
            "👁 Отправьте ИНН компании для подписки на мониторинг (10 или 12 цифр):"
        )
        return

    await _do_monitor_add(message, user_id, inn)


async def _do_monitor_add(message, user_id: int, inn: str) -> None:
    profile = user_store.get(user_id)
    limit = _monitoring_limit(profile)
    current = monitoring_store.count_for_user(user_id)

    if limit == 0:
        await message.reply_text(
            "👁 Мониторинг ИНН недоступен на тарифе Free.\n\n"
            "Перейдите на Start или выше — нажмите «💎 Тарифы»."
        )
        return

    # Проверяем лимит до того, как тратить запрос на DaData/FNS
    existing = monitoring_store.get(user_id, inn)
    if not existing and limit is not None and current >= limit:
        await message.reply_text(
            f"⛔️ Лимит мониторинга исчерпан: {current}/{limit}.\n\n"
            "Удалите одну из подписок (/unmonitor <ИНН>) или перейдите на "
            "более высокий тариф."
        )
        return

    company = await company_service.fetch(inn)
    if not company:
        await message.reply_text(
            f"⚠️ Не удалось получить данные по ИНН {inn}. Подписка не создана."
        )
        return

    security = None
    try:
        security = await security_service.check(
            inn=inn,
            name=company.name,
            okved=company.okved_main,
            ogrn=company.ogrn,
        )
    except Exception as exc:
        logger.error("Monitoring add: security check failed for %s: %s", inn, exc)

    snapshot = make_snapshot(company, security)
    monitoring_store.add(
        user_id=user_id,
        inn=inn,
        name=company.name or "",
        snapshot=snapshot,
    )

    if existing:
        await message.reply_text(
            f"🔄 Подписка обновлена: {company.name or inn} (ИНН {inn}).\n"
            "Уведомлю при изменениях статуса, директора, ФССП и т.п."
        )
    else:
        await message.reply_text(
            f"✅ Подписка создана: {company.name or inn} (ИНН {inn}).\n"
            "Проверка раз в сутки. Уведомлю при изменениях."
        )


async def handle_monitor_remove(client: Client, message) -> None:
    """Команда: /unmonitor <ИНН>"""
    text = message.text or ""
    args = text.split()
    user_id = message.from_user.id

    if len(args) < 2:
        await message.reply_text(
            "Укажите ИНН для отписки: /unmonitor 7707083893"
        )
        return

    inn = args[1]
    removed = monitoring_store.remove(user_id, inn)
    if removed:
        await message.reply_text(f"🔕 Подписка на ИНН {inn} удалена.")
    else:
        await message.reply_text(f"Подписки на ИНН {inn} нет.")


async def handle_monitoring_list(client: Client, message) -> None:
    """Команда: /monitoring — список активных подписок."""
    user_id = message.from_user.id
    subs = monitoring_store.list_for_user(user_id)
    profile = user_store.get(user_id)
    limit = _monitoring_limit(profile)
    limit_str = "∞" if limit is None else str(limit)

    if not subs:
        await message.reply_text(
            f"👁 Активных подписок нет (лимит {limit_str}).\n\n"
            "Добавить: /monitor <ИНН>"
        )
        return

    lines = [f"👁 Ваши подписки ({len(subs)}/{limit_str}):", ""]
    for sub in subs:
        title = sub.name or sub.inn
        last = sub.last_checked[:10] if sub.last_checked else "—"
        lines.append(f"• {title} (ИНН {sub.inn}) — проверено {last}")
    lines.append("")
    lines.append("Удалить: /unmonitor <ИНН>")
    await message.reply_text("\n".join(lines))


async def handle_start(client: Client, message) -> None:
    """Команды /start и /help. Поддерживает /start ref_<code> для
    привязки приглашённого к рефереру."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split()

    referral_message = ""
    if len(parts) >= 2 and parts[1].startswith("ref_"):
        # Создаём профиль приглашённого, если его ещё нет, и привязываем
        user_store.get(user_id)
        if user_store.set_referrer_by_code(user_id, parts[1]):
            referral_message = (
                "🎁 Вы пришли по реферальной ссылке. "
                "Когда оформите подписку — пригласившему начислится "
                f"+{REFERRAL_BONUS_DAYS} дней тарифа.\n\n"
            )

    profile = user_store.get(user_id)
    step = await _onboarding_step_async(client, profile)

    if step is not None:
        # Новый или незавершивший онбординг — короткое приветствие + шаг
        if referral_message:
            await message.reply_text(referral_message)
        await _send_onboarding_step(message, step)
        return

    welcome = (
        f"{referral_message}"
        "👋 Добро пожаловать в MondayCompany — сервис проверки "
        "юридических и физических лиц!\n\n"
        "🔍 С помощью бота вы можете:\n"
        "– Проверять контрагентов и частных лиц по множеству источников\n"
        "– Получать скоринг и структурированные отчёты в PDF\n"
        "– Следить за изменениями в компании\n"
        "– Получать помощь от ИИ-агента\n"
        "– Посмотреть связи компании и её историю\n\n"
        "/menu — показать меню\n"
        "/documents — правовые документы\n"
        "/referral — реферальная программа\n\n"
        "🛠 Если у вас возникли вопросы или предложения — "
        "напишите нам: @YRS75"
    )
    await message.reply_text(welcome, reply_markup=_reply_keyboard())
    await message.reply_text(
        "Выберите действие:",
        reply_markup=_main_menu(),
    )


def _referral_link(bot_username: str, code: str) -> str:
    """Формирует deep-link для приглашения."""
    if not bot_username:
        return f"/start {code}"
    return f"https://t.me/{bot_username}?start={code}"


def _format_referral_message(profile, bot_username: str) -> str:
    link = _referral_link(bot_username, profile.referral_code)
    return (
        "🤝 Партнёрская программа\n\n"
        f"Ваша ссылка:\n{link}\n\n"
        f"Код: {profile.referral_code}\n\n"
        "Как это работает:\n"
        f"• За каждого приглашённого, который оформит подписку, вам "
        f"начислится +{REFERRAL_BONUS_DAYS} дней тарифа.\n"
        "• Если у вас Free — получите Start на бонусный период.\n"
        "• Если у вас платный тариф — продлим текущий.\n\n"
        f"📊 Статистика:\n"
        f"Приглашено всего: {profile.referrals_count}\n"
        f"Из них оплатили: {profile.referrals_paid_count}\n"
        f"Получено бонусных дней: {profile.referral_bonus_days_total}"
    )


def _bot_username(client: Client) -> str:
    me = getattr(client, "me", None)
    if me is None:
        return ""
    return getattr(me, "username", "") or ""


async def handle_referral(client: Client, message) -> None:
    """Команда /referral — показывает реф-код, ссылку и статистику."""
    user_id = message.from_user.id
    profile = user_store.get(user_id)
    text = _format_referral_message(profile, _bot_username(client))
    await message.reply_text(text, disable_web_page_preview=True)


async def handle_offer(client: Client, message) -> None:
    await message.reply_text(OFFER_TEXT)


DISCLAIMER_TEXT = """ДИСКЛЕЙМЕР

⚠️ Внимание: Данный сервис предназначен исключительно для справочной и аналитической информации. Все данные, предоставляемые ботом, получены из открытых источников, в том числе государственных реестров, официальных публикаций, открытых баз и общедоступных онлайн-ресурсов.

📝 Сервис не является государственным органом, не гарантирует полноту и актуальность сведений на момент запроса, и не может использоваться как единственное основание для принятия юридически значимых решений.

🔐 Используя данный сервис, вы подтверждаете, что:

• действуете в соответствии с законодательством РФ (включая 152-ФЗ «О персональных данных»);

• не используете полученную информацию для дискриминации, шантажа, вторжения в частную жизнь или противоправных действий;

• понимаете, что ответственность за использование информации лежит на пользователе.

💬 При наличии вопросов, неточностей или претензий — просьба обратиться через обратную связь в боте."""


async def handle_disclaimer(client: Client, message) -> None:
    await message.reply_text(DISCLAIMER_TEXT)


async def handle_tarifs(client: Client, message) -> None:
    await message.reply_text(_tariffs_text(), reply_markup=_tariffs_keyboard())


async def handle_cancel(client: Client, message) -> None:
    user_id = message.from_user.id
    if _user_state.pop(user_id, None) is not None:
        await message.reply_text("❌ Действие отменено.\n\nНажмите /menu для выбора нового действия.")
    else:
        await message.reply_text("Нет активного действия для отмены.\n\nНажмите /menu для выбора действия.")


def _documents_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Публичная оферта", url="https://telegra.ph/Publichnaya-oferta---Finansovyj-arhitektor-04-27")],
        [InlineKeyboardButton("📋 Пользовательское соглашение", url="https://telegra.ph/Polzovatelskoe-soglashenie-04-27-19")],
        [InlineKeyboardButton("🔒 Обработка персональных данных", url="https://telegra.ph/Politika-obrabotki-personalnyh-dannyh-04-27")],
    ])


async def handle_documents(client: Client, message) -> None:
    await message.reply_text(
        "📂 Правовые документы\n\nВыберите документ для просмотра:",
        reply_markup=_documents_keyboard(),
    )


async def handle_admin(client: Client, message) -> None:
    """Команда /admin — реалтайм-отчёт для админов.
    Доступ ограничен set'ом admin_user_ids (заполняется из ENV)."""
    user_id = message.from_user.id
    if not admin_user_ids:
        # Если админы не настроены — команда вообще не работает.
        # Не палим её существование тем кто случайно ввёл.
        return
    if user_id not in admin_user_ids:
        # Тихо игнорируем — обычный юзер не увидит даже намёка на команду
        logger.info("Admin: access denied for user_id=%s", user_id)
        return

    await message.reply_text("⏳ Собираю отчёт...")
    try:
        report = await build_admin_report(
            users=user_store,
            payments=payments_store,
            monitoring=monitoring_store,
            zchb=zchb,
        )
    except Exception as exc:
        logger.exception("Admin report failed: %s", exc)
        await message.reply_text(
            "⚠️ Не удалось собрать отчёт. Подробности в логах:\n"
            f"`{type(exc).__name__}: {exc}`"
        )
        return
    await message.reply_text(report, disable_web_page_preview=True)


def main() -> None:
    global subscription_service, required_channel, admin_user_ids
    global report_base_url

    settings = Settings.from_env()
    required_channel = settings.required_channel
    if required_channel:
        logger.info("Required channel subscription: %s", required_channel)
    admin_user_ids = parse_admin_user_ids(settings.admin_user_ids)
    if admin_user_ids:
        logger.info("Admins configured: %d user(s)", len(admin_user_ids))
    report_base_url = settings.report_base_url
    if report_base_url:
        logger.info("Web report base URL: %s", report_base_url)
    app = build_app(settings)

    # Инициализация платёжного сервиса.
    # Активный провайдер берётся из PAYMENT_PROVIDER (tochka|yookassa).
    # Клиент второго провайдера тоже создаётся, если его credentials заданы —
    # это нужно для обработки webhook'ов старых платежей при миграции.
    webhook_runner = None
    if settings.payments_enabled:
        tochka_client_obj: Optional[TochkaClient] = None
        yookassa_client_obj: Optional[YooKassaClient] = None

        if settings.tochka_enabled:
            tochka_client_obj = TochkaClient(
                jwt_token=settings.tochka_jwt,
                customer_code=settings.tochka_customer_code,
                client_id=settings.tochka_client_id,
                merchant_id=settings.tochka_merchant_id,
                base_url=settings.tochka_base_url,
            )
        if settings.yookassa_enabled:
            yookassa_client_obj = YooKassaClient(
                shop_id=settings.yookassa_shop_id,
                secret_key=settings.yookassa_secret_key,
                base_url=settings.yookassa_base_url,
                webhook_secret=settings.yookassa_webhook_secret,
            )

        subscription_service = SubscriptionService(
            tochka=tochka_client_obj,
            yookassa=yookassa_client_obj,
            provider=settings.payment_provider,
            users=user_store,
            payments=payments_store,
            redirect_url=settings.payment_redirect_url,
            fail_redirect_url=settings.payment_fail_redirect_url,
            tax_system_code=settings.tochka_tax_system_code,
            yookassa_tax_system_code=settings.yookassa_tax_system_code,
            yookassa_vat_code=settings.yookassa_vat_code,
            yookassa_save_payment_method=settings.yookassa_save_payment_method,
        )
        logger.info(
            "Payments enabled: provider=%s tochka=%s yookassa=%s",
            settings.payment_provider,
            settings.tochka_enabled, settings.yookassa_enabled,
        )
    else:
        logger.warning(
            "Payments disabled: set credentials for %s to enable",
            settings.payment_provider,
        )

    async def menu_handler(client: Client, message) -> None:
        await message.reply_text(
            "Выберите действие:",
            reply_markup=_main_menu(),
        )

    def _build_handlers() -> list:
        return [
            MessageHandler(handle_start, filters.command(["start", "help"])),
            MessageHandler(menu_handler, filters.command(["menu"])),
            MessageHandler(handle_kp_command, filters.command(["kp"])),
            MessageHandler(handle_my_subscription, filters.command(["my_subscription"])),
            MessageHandler(handle_cancel_subscription, filters.command(["cancel_subscription"])),
            MessageHandler(handle_enable_subscription, filters.command(["enable_subscription"])),
            MessageHandler(handle_monitor_add, filters.command(["monitor"])),
            MessageHandler(handle_monitor_remove, filters.command(["unmonitor"])),
            MessageHandler(handle_monitoring_list, filters.command(["monitoring"])),
            MessageHandler(handle_referral, filters.command(["referral"])),
            MessageHandler(handle_offer, filters.command(["offer"])),
            MessageHandler(handle_disclaimer, filters.command(["disclaimer"])),
            MessageHandler(handle_tarifs, filters.command(["tarifs"])),
            MessageHandler(handle_cancel, filters.command(["cancel"])),
            MessageHandler(handle_documents, filters.command(["documents"])),
            MessageHandler(handle_admin, filters.command(["admin"])),
            CallbackQueryHandler(handle_callback),
            MessageHandler(handle_contact, filters.contact),
            MessageHandler(
                handle_text_message,
                filters.text & ~filters.command([
                    "start", "help", "menu", "kp",
                    "my_subscription", "cancel_subscription", "enable_subscription",
                    "monitor", "unmonitor", "monitoring", "referral",
                    "offer", "disclaimer", "tarifs", "cancel", "documents",
                    "admin",
                ]),
            ),
        ]

    def register_handlers_sync() -> None:
        # Pyrogram's app.add_handler schedules a task that may run on the wrong
        # event loop or after updates already arrived. Insert handlers directly
        # into the dispatcher's groups to make registration synchronous and reliable.
        groups = app.dispatcher.groups
        if 0 not in groups:
            groups[0] = []
        for handler in _build_handlers():
            groups[0].append(handler)

    async def run_all() -> None:
        nonlocal webhook_runner
        await app.start()
        register_handlers_sync()
        logger.info(
            "Bot started (client). Registered handler groups: %s",
            {g: len(h) for g, h in app.dispatcher.groups.items()},
        )

        async def notify(user_id: int, text: str) -> None:
            try:
                await app.send_message(user_id, text)
            except Exception as exc:
                logger.error("Failed to notify %s: %s", user_id, exc)

        tasks: list[asyncio.Task] = []
        if subscription_service is not None:
            web_app = build_webhook_app(
                tochka=subscription_service.tochka,
                yookassa=subscription_service.yookassa,
                subscription=subscription_service,
                notify=notify,
                report_tokens=report_tokens,
                company_service=company_service,
                security_service=security_service,
                zchb=zchb,
            )
            webhook_runner = await start_webhook_server(
                web_app, host=settings.webhook_host, port=settings.webhook_port
            )
            tasks.append(asyncio.create_task(
                run_renewal_loop(subscription_service, notify=notify)
            ))
            # Поллер pending-платежей — safety-net если webhook потерялся
            tasks.append(asyncio.create_task(
                run_payment_poller(subscription_service, notify=notify)
            ))

        # Мониторинг подписок на ИНН — запускаем независимо от платежей
        tasks.append(asyncio.create_task(
            run_monitoring_loop(
                monitoring=monitoring_store,
                company_service=company_service,
                security_service=security_service,
                notify=notify,
            )
        ))

        # Напоминания об окончании платной подписки —
        # тоже независимо от платежей (нужно даже когда auto_renew=False)
        tasks.append(asyncio.create_task(
            run_expiry_reminder_loop(users=user_store, notify=notify)
        ))

        logger.info("Bot is running. Press Ctrl+C to stop.")
        try:
            # Держим event loop живым — бот обслуживает хендлеры через Pyrogram dispatcher
            stop_event = asyncio.Event()
            await stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            if webhook_runner is not None:
                await webhook_runner.cleanup()
            await app.stop()

    logger.info("Bot starting...")
    try:
        # Use Pyrogram's own runner instead of asyncio.run. Pyrogram internally
        # uses asyncio.get_event_loop() / loop.run_until_complete; using
        # asyncio.run() creates a brand-new loop on every invocation and leaves
        # Pyrogram's pre-created Queue / scheduled tasks bound to a different
        # loop, so updates queued by the session never reach the handler workers.
        app.run(run_all())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
