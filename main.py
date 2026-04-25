import asyncio
import logging
import re
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from company_service import CompanyService
from compliance import assess_risk
from logging_config import setup_logging
from offer import OFFER_TEXT
from parsers import ParseResult, parse_message
from payments_store import PaymentsStore
from renderers import (
    render_checks_history,
    render_comparison,
    render_egryl,
    render_fns_card,
    render_profile,
    render_response,
)
from renewal_scheduler import run_renewal_loop
from schemas import CompanyData, empty_company
from security_check import SecurityService
from subscription import SubscriptionService
from tochka_client import TochkaClient
from user_store import TARIFF_PRICES, UserStore
from settings import Settings
from telemetry import init_sentry
from webhook_server import build_app as build_webhook_app, start_webhook_server
import sd_notify


setup_logging()
logger = logging.getLogger("financial-architect")
init_sentry()
company_service = CompanyService()
security_service = SecurityService()
user_store = UserStore()
payments_store = PaymentsStore()

# Сервис подписок и настройки инициализируются в main()
subscription_service: Optional[SubscriptionService] = None
_settings: Optional[Settings] = None

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
            [
                InlineKeyboardButton(
                    "📊 Внутренний анализ",
                    callback_data="mode_internal_analysis",
                ),
            ],
            [
                InlineKeyboardButton("📞 Поддержка", callback_data="support"),
                InlineKeyboardButton("💳 Тарифы", callback_data="show_tariffs"),
            ],
        ]
    )


def _inn_prompt_text(action: str) -> str:
    labels = {
        "mode_internal_analysis": "внутреннего анализа",
        "mode_compare": "сравнения",
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
            "ca_ai": "🤖 ИИ-анализ",
            "ca_links": "🔗 Связи компании",
        }

        async def _fetch_for_button(inn: str):
            c = await company_service.fetch(inn)
            return c or empty_company(inn)

        if action_part == "ca_refresh" and inn_part:
            _user_state[user_id] = "mode_internal_analysis"
            await callback_query.message.reply_text("🔄 Обновляю данные...")
            company = await company_service.fetch(inn_part)
            from parsers import ParseResult as PR
            parsed_refresh = PR(raw_text=inn_part, inn=inn_part, mode="internal_analysis",
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
            from renderers import render_response as rr
            reply = rr(parsed=parsed_refresh, company=company, risk=set(), security=sec_result)
            await callback_query.message.reply_text(
                reply,
                disable_web_page_preview=True,
                reply_markup=_company_actions_keyboard(inn_part),
            )

        elif action_part == "ca_egryl" and inn_part:
            company = await _fetch_for_button(inn_part)
            await callback_query.message.reply_text(
                render_egryl(company), disable_web_page_preview=True
            )

        elif action_part == "ca_fns" and inn_part:
            company = await _fetch_for_button(inn_part)
            await callback_query.message.reply_text(
                render_fns_card(company), disable_web_page_preview=True
            )

        elif action_part == "ca_history":
            profile = user_store.get(user_id)
            await callback_query.message.reply_text(
                render_checks_history(profile.checks_history)
            )

        elif action_part in wip_actions:
            label = wip_actions[action_part]
            await callback_query.message.reply_text(
                f"⏳ {label} — раздел в разработке.\n"
                f"Будет доступен в следующих обновлениях."
            )
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

    # Кнопка поддержки
    if data == "support":
        await callback_query.answer()
        username = _settings.support_username if _settings else ""
        text = (
            f"📞 Поддержка\n\nНапишите нам: @{username}\n\nОтветим в течение рабочего дня."
            if username else
            "📞 Поддержка\n\nВоспользуйтесь командой /offer — там указан email для связи."
        )
        await callback_query.message.reply_text(text)
        return

    # Кнопка тарифов из меню
    if data == "show_tariffs":
        await callback_query.answer()
        await callback_query.message.reply_text(
            _tariffs_text(), reply_markup=_tariffs_keyboard()
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
            await message.reply_text(render_profile(profile))
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
                _fetch_company(inn1, user_id),
                _fetch_company(inn2, user_id),
            )
            reply = render_comparison(company1, inn1, company2, inn2)
            await message.reply_text(reply, disable_web_page_preview=True)
        else:
            await message.reply_text(
                "⚠️ Не распознала ИНН. Состояние сброшено.\n\n"
                "Нажмите /menu для выбора действия."
            )
        return

    if pending_action and parsed.inn:
        company = await _fetch_company(parsed.inn, user_id)
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
        company = await _fetch_company(parsed.inn, user_id)

    # Если есть ИНН — анализируем
    if parsed.inn or parsed.company_data:
        risk = assess_risk(text)
        reply = render_response(parsed=parsed, company=company, risk=risk)
        await message.reply_text(reply, disable_web_page_preview=True)
        return

    # Если ни ИНН, ни команды — подсказка
    await message.reply_text(
        "👋 Отправьте ИНН компании или нажмите /menu для выбора действия."
    )


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

    profile = user_store.get(user_id)
    if not profile.email:
        await message.reply_text(
            "📧 Перед оплатой укажите email для чека (54-ФЗ):\n\n"
            "/set_email ваш@почта.ру\n\n"
            "После этого снова выберите тариф."
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

    elif action == "mode_compare":
        # Шаг 1 сравнения: получили ИНН первой компании, просим вторую
        _user_state[message.from_user.id] = {"action": "compare_step2", "inn1": parsed.inn}
        await message.reply_text(
            f"✅ Первая компания: {company.name if company else parsed.inn}\n\n"
            "Теперь отправьте ИНН второй компании для сравнения:"
        )


async def _fetch_company(inn: str, user_id: Optional[int] = None) -> Optional[CompanyData]:
    company = await company_service.fetch(inn)
    if company and user_id:
        user_store.add_to_history(user_id, inn, company.name or inn)
    return company


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
    profile = user_store.disable_auto_renew(user_id)
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


_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


async def handle_set_email(client: Client, message) -> None:
    """Сохраняет email пользователя для чеков 54-ФЗ."""
    user_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        profile = user_store.get(user_id)
        current = profile.email or "не задан"
        await message.reply_text(
            f"📧 Ваш email: {current}\n\n"
            "Чтобы изменить, отправьте:\n"
            "/set_email ваш@почта.ру\n\n"
            "Email нужен для отправки чека по 54-ФЗ после оплаты."
        )
        return

    email = parts[1].strip()
    if not _EMAIL_RE.match(email):
        await message.reply_text(
            "⚠️ Некорректный email. Формат: /set_email user@example.com"
        )
        return

    user_store.set_email(user_id, email)
    await message.reply_text(f"✅ Email сохранён: {email}")


async def handle_offer(client: Client, message) -> None:
    await message.reply_text(OFFER_TEXT)


async def handle_support(client: Client, message) -> None:
    username = _settings.support_username if _settings else ""
    if username:
        text = (
            f"📞 Поддержка\n\n"
            f"Напишите нам: @{username}\n\n"
            f"Мы ответим в течение рабочего дня."
        )
    else:
        text = (
            "📞 Поддержка\n\n"
            "Для обращений по работе бота и вопросам подписки "
            "воспользуйтесь командой /offer — там указан email для связи."
        )
    await message.reply_text(text)


def main() -> None:
    global subscription_service, _settings

    settings = Settings.from_env()
    _settings = settings

    # Инициализация платёжного сервиса
    webhook_runner = None
    if settings.payments_enabled:
        tochka = TochkaClient(
            jwt_token=settings.tochka_jwt,
            customer_code=settings.tochka_customer_code,
            merchant_id=settings.tochka_merchant_id,
            base_url=settings.tochka_base_url,
            webhook_secret=settings.tochka_webhook_secret,
        )
        subscription_service = SubscriptionService(
            tochka=tochka,
            users=user_store,
            payments=payments_store,
            redirect_url=settings.payment_redirect_url,
            fail_redirect_url=settings.payment_fail_redirect_url,
        )
    else:
        logger.warning("Payments disabled: set TOCHKA_JWT and TOCHKA_CUSTOMER_CODE to enable")

    async def start_handler(client: Client, message) -> None:
        await message.reply_text(
            "👋 Финансовый архитектор онлайн!\n\n"
            "Я помогу с анализом компаний.\n\n"
            "Что умею:\n"
            "• Отправьте ИНН — получите анализ компании\n"
            "• Нажмите кнопку ниже для нужного действия\n"
            "• /menu — показать меню\n"
            "• /my_subscription — статус подписки\n"
            "• /set_email — email для чека 54-ФЗ\n"
            "• /offer — публичная оферта\n"
            "• /support — связаться с поддержкой",
            reply_markup=_main_menu(),
        )

    async def menu_handler(client: Client, message) -> None:
        await message.reply_text(
            "Выберите действие:",
            reply_markup=_main_menu(),
        )

    async def _heartbeat_loop() -> None:
        # systemd WatchdogSec — интервал должен быть в 2+ раза меньше WatchdogSec
        while True:
            sd_notify.watchdog()
            await asyncio.sleep(30)

    async def run_all() -> None:
        nonlocal webhook_runner
        # Client создаётся ВНУТРИ event loop, иначе dispatcher tasks
        # окажутся на другом loop и не будут вызываться (Pyrogram баг с asyncio.run)
        app = build_app(settings)

        app.add_handler(MessageHandler(start_handler, filters.command(["start", "help"])))
        app.add_handler(MessageHandler(menu_handler, filters.command(["menu"])))
        app.add_handler(MessageHandler(handle_my_subscription, filters.command(["my_subscription"])))
        app.add_handler(MessageHandler(handle_cancel_subscription, filters.command(["cancel_subscription"])))
        app.add_handler(MessageHandler(handle_enable_subscription, filters.command(["enable_subscription"])))
        app.add_handler(MessageHandler(handle_set_email, filters.command(["set_email"])))
        app.add_handler(MessageHandler(handle_offer, filters.command(["offer"])))
        app.add_handler(MessageHandler(handle_support, filters.command(["support"])))
        app.add_handler(CallbackQueryHandler(handle_callback))
        app.add_handler(
            MessageHandler(
                handle_text_message,
                filters.text & ~filters.command([
                    "start", "help", "menu",
                    "my_subscription", "cancel_subscription", "enable_subscription",
                    "set_email", "offer", "support",
                ]),
            )
        )

        await app.start()
        logger.info("Bot started (client)")
        sd_notify.ready()
        sd_notify.status("Bot is running")

        async def notify(user_id: int, text: str) -> None:
            try:
                await app.send_message(user_id, text)
            except Exception as exc:
                logger.error("Failed to notify %s: %s", user_id, exc)

        tasks: list[asyncio.Task] = [asyncio.create_task(_heartbeat_loop())]
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

        logger.info("Bot is running. Press Ctrl+C to stop.")
        try:
            # Держим event loop живым — бот обслуживает хендлеры через Pyrogram dispatcher
            stop_event = asyncio.Event()
            await stop_event.wait()
        finally:
            sd_notify.stopping()
            for t in tasks:
                t.cancel()
            if webhook_runner is not None:
                await webhook_runner.cleanup()
            await app.stop()

    logger.info("Bot starting...")
    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
