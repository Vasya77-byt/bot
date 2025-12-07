import logging

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from compliance import assess_risk
from parsers import ParseResult, parse_message
from renderers import render_response
from sbis_client import SbisClient
from settings import Settings


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("financial-architect")


async def handle_text_message(client: Client, message) -> None:
    text: str = message.text or ""
    parsed: ParseResult = parse_message(text)
    logger.info("Parsed message: %s", parsed)

    company = parsed.company_data
    if not company and parsed.inn:
        sbis = SbisClient()
        company = await sbis.fetch_company_data(parsed.inn)

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
        await message.reply_text("Финансовый архитектор онлайн.")

    app.add_handler(MessageHandler(start_handler, filters.command(["start", "help"])))
    app.add_handler(
        MessageHandler(handle_text_message, filters.text & ~filters.command(["start", "help"]))
    )

    logger.info("Bot starting...")
    app.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")

