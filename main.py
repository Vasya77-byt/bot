import logging
from io import BytesIO
from typing import Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from company_service import CompanyService
from compliance import assess_risk
from exports import build_kp_pdf, build_kp_png
from logging_config import setup_logging
from parsers import ParseResult, parse_message
from renderers import render_response
from schemas import CompanyData
from security_check import SecurityService
from settings import Settings
from storage import save_file_bytes
from metadata_store import MetadataStore
from telemetry import init_sentry


setup_logging()
logger = logging.getLogger("financial-architect")
init_sentry()
metadata_store = MetadataStore()
company_service = CompanyService()
security_service = SecurityService()

# Хранение состояния пользователей (ожидание ИНН)
_user_state: dict[int, str] = {}


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
                InlineKeyboardButton(
                    "📝 Коммерческое предложение",
                    callback_data="mode_client_proposal",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Дай заявку",
                    callback_data="mode_request",
                ),
                InlineKeyboardButton(
                    "💼 Дай предложение",
                    callback_data="mode_proposal",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📄 Сгенерировать КП (PDF)",
                    callback_data="kp_pdf",
                ),
                InlineKeyboardButton(
                    "🖼 Сгенерировать КП (PNG)",
                    callback_data="kp_png",
                ),
            ],
        ]
    )


def _inn_prompt_text(action: str) -> str:
    labels = {
        "mode_internal_analysis": "внутреннего анализа",
        "mode_client_proposal": "коммерческого предложения",
        "mode_request": "заявки",
        "mode_proposal": "предложения",
        "kp_pdf": "генерации КП (PDF)",
        "kp_png": "генерации КП (PNG)",
    }
    label = labels.get(action, "обработки")
    return f"Для {label} отправьте ИНН компании (10 или 12 цифр):"


async def handle_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Обработка нажатий на кнопки меню."""
    data = callback_query.data
    user_id = callback_query.from_user.id
    logger.info("Callback from user %s: %s", user_id, data)

    # Кнопки выбора тарифа
    if data.startswith("tariff_"):
        await callback_query.answer()
        tariff_messages = {
            "tariff_free": (
                "🆓 Тариф Free — Бесплатно\n\n"
                "Вы выбрали бесплатный тариф.\n"
                "Он подключён автоматически — можете пользоваться прямо сейчас!\n\n"
                "• 3 проверки в день\n"
                "• Краткий отчёт + светофор\n"
                "• Стоп-листы и суды (сводка)"
            ),
            "tariff_start": (
                "⭐️ Тариф Start — 490 ₽/мес\n\n"
                "Для подключения свяжитесь с нами:\n"
                "@support_username\n\n"
                "• 50 проверок/день\n"
                "• Полный отчёт, ЕГРЮЛ, Суды/ФССП, Стоп-листы"
            ),
            "tariff_pro": (
                "💎 Тариф Pro — 1 290 ₽/мес\n\n"
                "Для подключения свяжитесь с нами:\n"
                "@support_username\n\n"
                "• 300 проверок/день\n"
                "• Всё из Start + ИИ-анализ, Связи, История, Мониторинг"
            ),
            "tariff_business": (
                "🏆 Тариф Business — 2 490 ₽/мес\n\n"
                "Для подключения свяжитесь с нами:\n"
                "@support_username\n\n"
                "• Безлимитные проверки\n"
                "• Всё из Pro + API доступ, Массовые проверки, PDF/1С экспорт"
            ),
        }
        text = tariff_messages.get(data, "Тариф не найден.")
        await callback_query.message.reply_text(text)
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
        # Остальные действия — запрашиваем ИНН
        _user_state.pop(user_id, None)
        _user_state[user_id] = reply_action
        await message.reply_text(_inn_prompt_text(reply_action))
        return

    # Если пользователь в состоянии ожидания ИНН
    pending_action = _user_state.pop(user_id, None)
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

    # Если ни ИНН, ни команды — подсказка
    await message.reply_text(
        "👋 Отправьте ИНН компании или нажмите /menu для выбора действия."
    )


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
        "Для подключения тарифа нажмите соответствующую кнопку\n"
        "\n"
        "Цены на 15% ниже аналогов (Контур, Руспрофайл)"
    )


def _tariffs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆓 Free", callback_data="tariff_free"),
            InlineKeyboardButton("⭐️ Start — 490 ₽/мес", callback_data="tariff_start"),
        ],
        [
            InlineKeyboardButton("💎 Pro — 1 290 ₽/мес", callback_data="tariff_pro"),
            InlineKeyboardButton("🏆 Business — 2 490 ₽/мес", callback_data="tariff_business"),
        ],
    ])


def _match_reply_button(text: str) -> Optional[str]:
    """Сопоставляет текст Reply-кнопок с действиями."""
    mapping = {
        "проверка компании": "mode_internal_analysis",
        "сравнить": "mode_client_proposal",
        "профиль": "mode_request",
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
        await message.reply_text(reply, disable_web_page_preview=True)

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


def main() -> None:
    settings = Settings.from_env()
    app = build_app(settings)

    async def start_handler(client: Client, message) -> None:
        await message.reply_text(
            "👋 Финансовый архитектор онлайн!\n\n"
            "Я помогу с анализом компаний и подготовкой КП.\n\n"
            "Что умею:\n"
            "• Отправьте ИНН — получите анализ компании\n"
            "• Нажмите кнопку ниже для нужного действия\n"
            "• /kp pdf <ИНН> — сгенерировать КП в PDF\n"
            "• /kp png <ИНН> — сгенерировать КП в PNG\n"
            "• /menu — показать меню",
            reply_markup=_main_menu(),
        )

    async def menu_handler(client: Client, message) -> None:
        await message.reply_text(
            "Выберите действие:",
            reply_markup=_main_menu(),
        )

    app.add_handler(MessageHandler(start_handler, filters.command(["start", "help"])))
    app.add_handler(MessageHandler(menu_handler, filters.command(["menu"])))
    app.add_handler(MessageHandler(handle_kp_command, filters.command(["kp"])))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(
            handle_text_message,
            filters.text & ~filters.command(["start", "help", "menu", "kp"]),
        )
    )

    logger.info("Bot starting...")
    app.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
