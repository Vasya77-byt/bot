import logging
from typing import Optional

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from compliance import assess_risk
from exports import build_kp_pdf, build_kp_png
from logging_config import setup_logging
from parsers import ParseResult, parse_message
from renderers import render_response
from schemas import CompanyData
from sbis_client import SbisClient
from settings import Settings
from storage import save_file_bytes
from metadata_store import MetadataStore
from telemetry import init_sentry


setup_logging()
logger = logging.getLogger("financial-architect")
init_sentry()
metadata_store = MetadataStore()


async def handle_text_message(client: Client, message) -> None:
    text: str = message.text or ""
    parsed: ParseResult = parse_message(text)
    logger.info("Parsed message: %s", parsed)

    company = parsed.company_data
    if not company and parsed.inn:
        sbis = SbisClient()
        company = await sbis.fetch_company_data(parsed.inn)

    lower_text = text.lower()
    if "кп pdf" in lower_text or "kp pdf" in lower_text:
        await _send_kp_auto(message, parsed, company, fmt="pdf")
        return
    if "кп png" in lower_text or "kp png" in lower_text:
        await _send_kp_auto(message, parsed, company, fmt="png")
        return

    risk = assess_risk(text)
    reply = render_response(parsed=parsed, company=company, risk=risk)
    await message.reply_text(reply, disable_web_page_preview=True)


def build_app(settings: Settings) -> Client:
    return Client(
        "financial_architect_bot",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
    )


def main() -> None:
    settings = Settings.from_env()
    app = build_app(settings)

    async def start_handler(client: Client, message) -> None:
        await message.reply_text(
            "Финансовый архитектор онлайн. Выберите режим или пришлите ИНН/JSON.",
            reply_markup=_main_menu(),
        )

    async def menu_handler(client: Client, message) -> None:
        await message.reply_text(
            "Меню быстрого старта:",
            reply_markup=_main_menu(),
        )

    app.add_handler(MessageHandler(start_handler, filters.command(["start", "help"])))
    app.add_handler(MessageHandler(menu_handler, filters.command(["menu"])))
    app.add_handler(
        MessageHandler(handle_text_message, filters.text & ~filters.command(["start", "help"]))
    )
    app.add_handler(MessageHandler(handle_kp_command, filters.command(["kp"])))

    logger.info("Bot starting...")
    app.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Внутренний анализ",
                    switch_inline_query_current_chat="mode=internal_analysis 7700000000",
                ),
                InlineKeyboardButton(
                    "Коммерческое предложение",
                    switch_inline_query_current_chat="mode=client_proposal 7700000000",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Дай заявку",
                    switch_inline_query_current_chat="дай заявку 7700000000",
                ),
                InlineKeyboardButton(
                    "Дай предложение",
                    switch_inline_query_current_chat="дай предложение 7700000000",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Пример JSON",
                    switch_inline_query_current_chat='{"inn":"7700000000","name":"ООО Ромашка","okved_main":"62.01"}',
                )
            ],
            [
                InlineKeyboardButton(
                    "Сгенерировать КП (PDF)",
                    switch_inline_query_current_chat="/kp pdf 7700000000",
                ),
                InlineKeyboardButton(
                    "Сгенерировать КП (PNG)",
                    switch_inline_query_current_chat="/kp png 7700000000",
                ),
            ],
        ]
    )


async def handle_kp_command(client: Client, message) -> None:
    """
    Команда: /kp <pdf|png> <ИНН?>
    Если ИНН не указан — используем текст или заглушку.
    """
    text = message.text or ""
    args = text.split()
    fmt = _extract_format(args)
    inn = _extract_inn_arg(args)

    parsed = parse_message(text)
    company = await _resolve_company(text, inn)

    title, body = _kp_template()
    await _send_kp_file(message, parsed, company, title, body, fmt)


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

    sbis = SbisClient()
    return await sbis.fetch_company_data(inn)


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
    body = render_response(parsed=parsed, company=company, risk=set())
    await _send_kp_file(message, parsed, company, "Коммерческое предложение", body, fmt)


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
        await message.reply_photo(content, caption="Ваше КП (PNG)")
    else:
        content = build_kp_pdf(title, body, company)
        save_file_bytes(content, filename)
        metadata_store.append(filename, company, "pdf")
        await message.reply_document(document=("kp.pdf", content), caption="Ваше КП (PDF)")

