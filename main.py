import asyncio
import logging
import re
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
from schemas import CompanyData
from security_check import SecurityService
from subscription import SubscriptionService
from tochka_client import TochkaClient
from user_store import TARIFF_PRICES, UserStore
from settings import Settings
from storage import save_file_bytes
from metadata_store import MetadataStore
from monitoring import make_snapshot
from monitoring_scheduler import run_monitoring_loop
from monitoring_store import MonitoringStore
from payment_poller import run_payment_poller
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
user_store = UserStore()
payments_store = PaymentsStore()
monitoring_store = MonitoringStore()

# Сервис подписок инициализируется в main() когда есть Settings
subscription_service: Optional[SubscriptionService] = None

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


def _profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Реферальная программа", callback_data="referral_show")],
    ])


def _inn_prompt_text(action: str) -> str:
    labels = {
        "mode_internal_analysis": "внутреннего анализа",
        "mode_client_proposal": "коммерческого предложения",
        "mode_compare": "сравнения",
        "mode_request": "заявки",
        "mode_proposal": "предложения",
        "kp_pdf": "генерации КП (PDF)",
        "kp_png": "генерации КП (PNG)",
        "mode_mass_check": "массовой проверки",
    }
    if action == "mode_compare":
        return "Отправьте ИНН первой компании (10 или 12 цифр):"
    label = labels.get(action, "обработки")
    return f"Для {label} отправьте ИНН компании (10 или 12 цифр):"


async def handle_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Обработка нажатий на кнопки меню."""
    data = callback_query.data
    user_id = callback_query.from_user.id
    logger.info("Callback from user %s: %s", user_id, data)

    # Кнопки действий под карточкой компании
    if data.startswith("ca_"):
        await callback_query.answer()
        action_part = data.split(":")[0]  # ca_courts, ca_ai, etc.
        inn_part = data.split(":")[1] if ":" in data else ""

        wip_actions = {
            "ca_courts": "⚖️ Суды",
            "ca_fns": "🏦 ФНС",
            "ca_egryl": "🏛 ЕГРЮЛ",
            "ca_history": "📜 История",
            "ca_links": "🔗 Связи",
            "ca_invoice": "🧾 Запрос счёта",
            "ca_proposal": "📝 Предложение",
        }

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
                    f"🤖 ИИ-анализ: {company_name}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{result}"
                )
            else:
                await callback_query.message.reply_text(
                    "❌ Не удалось получить ИИ-анализ. Проверьте GIGACHAT_CREDENTIALS в .env"
                )
            return

        if action_part == "ca_refresh" and inn_part:
            _user_state[user_id] = "mode_internal_analysis"
            await callback_query.message.reply_text("🔄 Обновляю данные...")
            company = await company_service.fetch(inn_part)
            parsed_refresh = ParseResult(raw_text=inn_part, inn=inn_part, mode="internal_analysis",
                                         is_request=False, is_proposal=False, company_data=company)
            sec_result = None
            try:
                sec_result = await security_service.check(
                    inn=inn_part,
                    name=company.name if company else None,
                    okved=company.okved_main if company else None,
                )
            except Exception as exc:
                logger.error("Security check failed: %s", exc)
            reply = render_response(parsed=parsed_refresh, company=company, risk=set(), security=sec_result)
            await callback_query.message.reply_text(
                reply,
                disable_web_page_preview=True,
                reply_markup=_company_actions_keyboard(inn_part),
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
            reply_markup=_company_actions_keyboard(inn),
        )
        return

    # Реферальная программа — показывается из карточки профиля
    if data == "referral_show":
        await callback_query.answer()
        profile = user_store.get(user_id)
        text = _format_referral_message(profile, _bot_username(client))
        await callback_query.message.reply_text(text, disable_web_page_preview=True)
        return

    # Кнопки выбора тарифа — создаём платёж
    if data.startswith("tariff_"):
        await callback_query.answer()
        tariff = data.replace("tariff_", "")
        if tariff not in TARIFF_PRICES:
            await callback_query.message.reply_text("Тариф не найден.")
            return
        await _handle_buy_tariff(callback_query.message, user_id, tariff)
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


async def handle_text_message(client: Client, message) -> None:
    """Обработка текстовых сообщений."""
    text: str = message.text or ""
    user_id = message.from_user.id
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
            await message.reply_text(
                render_profile(profile),
                reply_markup=_profile_keyboard(),
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
                reply_markup=_company_actions_keyboard(inn),
            )
        return

    if pending_action and parsed.inn:
        company = await _fetch_company(parsed.inn)
        await _dispatch_action(message, pending_action, parsed, company)
        return
    elif pending_action and not parsed.inn:
        # Не ИНН — сбрасываем состояние, не застреваем
        await message.reply_text(
            "⚠️ Не распознала ИНН. Состояние сброшено.\n\n"
            "Отправьте ИНН (10 или 12 цифр) или нажмите /menu для выбора действия."
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
        # Поиск выполнен, но пусто — сообщим пользователю
        await message.reply_text(
            f"🔎 По запросу «{text.strip()}» ничего не найдено.\n\n"
            "Попробуйте другое начало названия или отправьте ИНН напрямую."
        )
        return

    # Если ни ИНН, ни команды — подсказка
    await message.reply_text(
        "👋 Отправьте ИНН компании или нажмите /menu для выбора действия."
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


def _company_actions_keyboard(inn: str) -> InlineKeyboardMarkup:
    """Кнопки действий под карточкой компании."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚖️ Суды", callback_data=f"ca_courts:{inn}"),
            InlineKeyboardButton("🏦 ФНС", callback_data=f"ca_fns:{inn}"),
        ],
        [
            InlineKeyboardButton("🤖 ИИ-анализ", callback_data=f"ca_ai:{inn}"),
            InlineKeyboardButton("🏛 ЕГРЮЛ", callback_data=f"ca_egryl:{inn}"),
        ],
        [
            InlineKeyboardButton("📜 История", callback_data=f"ca_history:{inn}"),
            InlineKeyboardButton("🔗 Связи", callback_data=f"ca_links:{inn}"),
        ],
        [
            InlineKeyboardButton("🔄 Обновить", callback_data=f"ca_refresh:{inn}"),
        ],
    ])


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


async def _handle_buy_tariff(message, user_id: int, tariff: str) -> None:
    """Создаёт платёжную ссылку в Точке и отправляет пользователю кнопку оплаты."""
    if subscription_service is None:
        await message.reply_text(
            "⚠️ Приём платежей пока не настроен. Обратитесь к администратору."
        )
        return

    await message.reply_text("💳 Создаю платёжную ссылку...")
    try:
        link, op_id = await subscription_service.create_initial_payment(user_id, tariff)
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
        "Карта сохранится для автопродления — отключить: /cancel_subscription\n\n"
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
            reply_markup=_company_actions_keyboard(inn),
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

    welcome = (
        f"{referral_message}"
        "Я помогу с анализом компаний и подготовкой КП.\n\n"
        "Что умею:\n"
        "• Отправьте ИНН — получите анализ компании\n"
        "• Нажмите кнопку ниже для нужного действия\n"
        "• /kp pdf <ИНН> — сгенерировать КП в PDF\n"
        "• /kp png <ИНН> — сгенерировать КП в PNG\n"
        "• /menu — показать меню\n"
        "• /monitoring — мониторинг ИНН\n"
        "• /referral — реферальная программа\n"
        "• /my_subscription — статус подписки\n"
        "• /documents — правовые документы\n\n"
        "По всем вопросам: @YRS75"
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


def main() -> None:
    global subscription_service

    settings = Settings.from_env()
    app = build_app(settings)

    # Инициализация платёжного сервиса
    webhook_runner = None
    if settings.payments_enabled:
        tochka = TochkaClient(
            jwt_token=settings.tochka_jwt,
            customer_code=settings.tochka_customer_code,
            client_id=settings.tochka_client_id,
            merchant_id=settings.tochka_merchant_id,
            base_url=settings.tochka_base_url,
        )
        subscription_service = SubscriptionService(
            tochka=tochka,
            users=user_store,
            payments=payments_store,
            redirect_url=settings.payment_redirect_url,
            fail_redirect_url=settings.payment_fail_redirect_url,
            tax_system_code=settings.tochka_tax_system_code,
        )
    else:
        logger.warning("Payments disabled: set TOCHKA_JWT and TOCHKA_CUSTOMER_CODE to enable")

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
            CallbackQueryHandler(handle_callback),
            MessageHandler(
                handle_text_message,
                filters.text & ~filters.command([
                    "start", "help", "menu", "kp",
                    "my_subscription", "cancel_subscription", "enable_subscription",
                    "monitor", "unmonitor", "monitoring", "referral",
                    "offer", "disclaimer", "tarifs", "cancel", "documents",
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
                subscription=subscription_service,
                notify=notify,
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
