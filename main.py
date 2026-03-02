import logging
import re
from typing import Optional

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from compliance import assess_risk
from exports import build_kp_pdf, build_kp_png
from ledger import Ledger
from logging_config import setup_logging
from parsers import ParseResult, parse_message
from renderers import (
    render_carryover_summary,
    render_contracts,
    render_debts,
    render_ledger_status,
    render_response,
)
from schemas import CompanyData, Contract, Debt
from sbis_client import SbisClient
from settings import Settings
from storage import save_file_bytes
from metadata_store import MetadataStore
from telemetry import init_sentry


setup_logging()
logger = logging.getLogger("financial-architect")
init_sentry()
metadata_store = MetadataStore()
ledger = Ledger()


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
            f"Финансовый архитектор онлайн. Учётный период: {ledger.year} год.\n"
            "Выберите режим или пришлите ИНН/JSON.",
            reply_markup=_main_menu(),
        )

    async def menu_handler(client: Client, message) -> None:
        await message.reply_text(
            "Меню быстрого старта:",
            reply_markup=_main_menu(),
        )

    app.add_handler(MessageHandler(start_handler, filters.command(["start", "help"])))
    app.add_handler(MessageHandler(menu_handler, filters.command(["menu"])))
    app.add_handler(MessageHandler(handle_ledger_command, filters.command(["ledger"])))
    app.add_handler(MessageHandler(handle_contracts_command, filters.command(["contracts"])))
    app.add_handler(MessageHandler(handle_debts_command, filters.command(["debts"])))
    app.add_handler(MessageHandler(handle_add_contract_command, filters.command(["add_contract"])))
    app.add_handler(MessageHandler(handle_add_debt_command, filters.command(["add_debt"])))
    app.add_handler(MessageHandler(handle_carryover_command, filters.command(["carryover"])))
    app.add_handler(
        MessageHandler(handle_text_message, filters.text & ~filters.command(
            ["start", "help", "ledger", "contracts", "debts",
             "add_contract", "add_debt", "carryover"]
        ))
    )
    app.add_handler(MessageHandler(handle_kp_command, filters.command(["kp"])))

    logger.info("Bot starting... Accounting year: %d", ledger.year)
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
            [
                InlineKeyboardButton(
                    "Контракты",
                    switch_inline_query_current_chat="/contracts",
                ),
                InlineKeyboardButton(
                    "Задолженности",
                    switch_inline_query_current_chat="/debts",
                ),
                InlineKeyboardButton(
                    "Сводка учёта",
                    switch_inline_query_current_chat="/ledger",
                ),
            ],
        ]
    )


# ── Команды учёта ──


async def handle_ledger_command(client: Client, message) -> None:
    """
    /ledger — показать сводку учёта за текущий год.
    """
    contracts = ledger.get_active_contracts()
    debts = ledger.get_outstanding_debts()
    reply = render_ledger_status(contracts, debts, ledger.year)
    await message.reply_text(reply)


async def handle_contracts_command(client: Client, message) -> None:
    """
    /contracts — показать все контракты текущего года.
    """
    contracts = ledger.list_contracts()
    reply = render_contracts(contracts)
    await message.reply_text(reply)


async def handle_debts_command(client: Client, message) -> None:
    """
    /debts — показать все задолженности текущего года.
    """
    debts = ledger.list_debts()
    reply = render_debts(debts)
    await message.reply_text(reply)


async def handle_add_contract_command(client: Client, message) -> None:
    """
    /add_contract <ИНН> <контрагент> <сумма> <предмет>
    Пример: /add_contract 7700000000 ООО_Ромашка 500000 Поставка_оборудования
    """
    text = message.text or ""
    args = text.split()
    if len(args) < 4:
        await message.reply_text(
            "Формат: /add_contract <ИНН> <контрагент> <сумма> [предмет]\n"
            "Пример: /add_contract 7700000000 ООО_Ромашка 500000 Поставка_оборудования"
        )
        return

    inn = args[1] if len(args) > 1 else None
    counterparty = (args[2] if len(args) > 2 else "").replace("_", " ")
    try:
        total = float(args[3]) if len(args) > 3 else 0.0
    except ValueError:
        total = 0.0
    subject = " ".join(args[4:]).replace("_", " ") if len(args) > 4 else ""

    contract = Contract(
        inn=inn,
        counterparty=counterparty,
        subject=subject,
        total_amount=total,
        paid_amount=0.0,
        remaining_amount=total,
        status="active",
    )
    saved = ledger.add_contract(contract)
    await message.reply_text(
        f"Контракт добавлен (ID: {saved.contract_id}):\n"
        f"Контрагент: {counterparty}\n"
        f"ИНН: {inn}\nСумма: {total:,.0f}\nПредмет: {subject or '—'}"
    )


async def handle_add_debt_command(client: Client, message) -> None:
    """
    /add_debt <receivable|payable> <ИНН> <контрагент> <сумма> [описание]
    Пример: /add_debt receivable 7700000000 ООО_Ромашка 150000 За_поставку_Q4
    """
    text = message.text or ""
    args = text.split()
    if len(args) < 5:
        await message.reply_text(
            "Формат: /add_debt <receivable|payable> <ИНН> <контрагент> <сумма> [описание]\n"
            "  receivable — нам должны (дебиторская)\n"
            "  payable — мы должны (кредиторская)\n"
            "Пример: /add_debt receivable 7700000000 ООО_Ромашка 150000 За_поставку"
        )
        return

    direction = args[1] if len(args) > 1 else "receivable"
    if direction not in ("receivable", "payable"):
        await message.reply_text("Направление должно быть receivable или payable.")
        return

    inn = args[2] if len(args) > 2 else None
    counterparty = (args[3] if len(args) > 3 else "").replace("_", " ")
    try:
        amount = float(args[4]) if len(args) > 4 else 0.0
    except ValueError:
        amount = 0.0
    description = " ".join(args[5:]).replace("_", " ") if len(args) > 5 else ""

    direction_label = "дебиторская (нам должны)" if direction == "receivable" \
        else "кредиторская (мы должны)"

    debt = Debt(
        inn=inn,
        counterparty=counterparty,
        direction=direction,
        amount=amount,
        status="outstanding",
        description=description,
    )
    saved = ledger.add_debt(debt)
    await message.reply_text(
        f"Задолженность добавлена (ID: {saved.debt_id}):\n"
        f"Тип: {direction_label}\n"
        f"Контрагент: {counterparty}\nИНН: {inn}\n"
        f"Сумма: {amount:,.0f}\nОписание: {description or '—'}"
    )


async def handle_carryover_command(client: Client, message) -> None:
    """
    /carryover — показать сводку переноса данных из предыдущего года.
    """
    summary = ledger.get_carryover_summary(ledger.year - 1, ledger.year)
    if not summary:
        await message.reply_text(
            f"Сводка переноса из {ledger.year - 1} в {ledger.year} не найдена.\n"
            "Запустите скрипт миграции: python migrate_2025_to_2026.py"
        )
        return
    reply = render_carryover_summary(summary)
    await message.reply_text(reply)


# ── KP команды (без изменений) ──


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
