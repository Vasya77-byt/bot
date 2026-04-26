import asyncio
import logging
import os
import signal
import socket
from io import BytesIO
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from admin_report import build_report, is_admin
from ai_analyst import analyse_company
from bulk_check import (
    BULK_LIMITS,
    check_companies as bulk_check_companies,
    format_bulk_results,
    parse_inns,
)
from company_service import CompanyService
from watchlist_store import WatchlistStore
from watch_scheduler import run_watch_loop, make_snapshot
from compliance import assess_risk
from exports import build_kp_pdf, build_kp_png
from logging_config import setup_logging
from offer import OFFER_TEXT
from parsers import ParseResult, parse_message
from payments_store import PaymentsStore
from referral_store import (
    REFERRAL_BONUS_DAYS,
    REFERRAL_DISCOUNT_PCT,
    ReferralStore,
    make_referral_code,
    parse_referral_code,
)
from renderers import render_comparison, render_profile, render_response
from renewal_scheduler import run_renewal_loop
from schemas import CompanyData
from security_check import SecurityService
from subscription import SubscriptionService
from tochka_client import TochkaClient
from user_store import TARIFF_LIMITS, TARIFF_PRICES, UserStore
from settings import Settings
from storage import save_file_bytes
from metadata_store import MetadataStore
from telemetry import init_sentry
from webhook_server import build_app as build_webhook_app, start_webhook_server


setup_logging()
logger = logging.getLogger("financial-architect")


def _sd_notify(state: str) -> None:
    """Отправляет уведомление systemd через NOTIFY_SOCKET."""
    sock_path = os.getenv("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(sock_path)
            s.sendall(state.encode())
    except OSError:
        pass
init_sentry()
metadata_store = MetadataStore()
company_service = CompanyService()
security_service = SecurityService()
user_store = UserStore()
payments_store = PaymentsStore()
watchlist_store = WatchlistStore()
referral_store = ReferralStore()

# Сервис подписок инициализируется в main() когда есть Settings
subscription_service: Optional[SubscriptionService] = None

# API-ключ для ИИ-анализа, устанавливается в main()
_gigachat_credentials: str = ""

# Username бота — заполняется при старте
_bot_username: str = ""

# Список администраторов — заполняется при старте
_admin_ids: list[int] = []

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
                    "📊 Проверить компанию",
                    callback_data="mode_internal_analysis",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Массовая проверка",
                    callback_data="mode_bulk_check",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🆘 Поддержка",
                    url="https://t.me/YRS75",
                ),
            ],
        ]
    )


def _inn_prompt_text(action: str) -> str:
    labels = {
        "mode_internal_analysis": "внутреннего анализа",
        "mode_client_proposal": "коммерческого предложения",
        "mode_compare": "сравнения",
        "mode_request": "заявки",
        "mode_proposal": "предложения",
        "kp_pdf": "генерации КП (PDF)",
        "kp_png": "генерации КП (PNG)",
    }
    if action == "mode_compare":
        return "Отправьте ИНН первой компании (10 или 12 цифр):"
    if action == "mode_internal_analysis":
        return "Для внутреннего анализа отправьте ИНН или название компании (10 или 12 цифр):"
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
        elif action_part == "ca_ai" and inn_part:
            if not _gigachat_credentials:
                await callback_query.message.reply_text(
                    "⚙️ ИИ-анализ временно недоступен: не задан GIGACHAT_CREDENTIALS."
                )
            else:
                await callback_query.message.reply_text("🤖 Анализирую компанию, подождите...")
                company = await company_service.fetch(inn_part)
                if not company:
                    await callback_query.message.reply_text("Не удалось получить данные о компании.")
                else:
                    result = await analyse_company(company, _gigachat_credentials)
                    if result:
                        name = company.name or inn_part
                        await callback_query.message.reply_text(
                            f"🤖 ИИ-анализ: {name}\n\n{result}"
                        )
                    else:
                        await callback_query.message.reply_text(
                            "⚠️ Не удалось выполнить ИИ-анализ. Попробуйте позже."
                        )
        elif action_part == "ca_watch" and inn_part:
            company = await company_service.fetch(inn_part)
            name = (company.name if company else None) or inn_part
            added = watchlist_store.add(user_id, inn_part, name)
            if added:
                if company:
                    watchlist_store.update_snapshot(user_id, inn_part, make_snapshot(company))
                await callback_query.message.reply_text(
                    f"✅ Компания {name} успешно добавлена в ваш список для отслеживания изменений."
                )
            else:
                await callback_query.message.reply_text(
                    f"ℹ️ Компания {name} уже есть в вашем списке отслеживания."
                )
        elif action_part == "ca_pdf" and inn_part:
            await callback_query.message.reply_text("📄 Формирую PDF, секунду...")
            company = await company_service.fetch(inn_part)
            from parsers import ParseResult as PR
            parsed_pdf = PR(raw_text=inn_part, inn=inn_part, mode="internal_analysis",
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
            from exports import build_company_report_pdf
            body_text = rr(parsed=parsed_pdf, company=company, risk=set(), security=sec_result)
            try:
                pdf_bytes = build_company_report_pdf(company, body_text)
                buf = BytesIO(pdf_bytes)
                safe_name = (company.name if company else inn_part) or inn_part
                safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in safe_name)[:50]
                buf.name = f"report_{safe_name}_{inn_part}.pdf"
                await callback_query.message.reply_document(
                    document=buf,
                    caption=f"📄 Отчёт по компании {company.name if company else inn_part}",
                )
            except Exception as exc:
                logger.error("PDF generation failed: %s", exc)
                await callback_query.message.reply_text(
                    "⚠️ Не удалось сформировать PDF. Попробуйте позже."
                )
        elif action_part in wip_actions:
            label = wip_actions[action_part]
            await callback_query.message.reply_text(
                f"⏳ {label} — раздел в разработке.\n"
                f"Будет доступен после подключения ЗЧБ и Контур.Фокус."
            )
        return

    # Кнопки генерации КП из карточки компании (содержат ИНН в callback)
    if data.startswith("kp_") and ":" in data:
        await callback_query.answer()
        action, inn_part = data.split(":", 1)
        if inn_part:
            from parsers import ParseResult as PR
            parsed_kp = PR(raw_text=inn_part, inn=inn_part, mode=None,
                           is_request=False, is_proposal=False)
            company = await company_service.fetch(inn_part)
            fmt = "pdf" if action == "kp_pdf" else "png"
            title, body = _kp_template()
            await _send_kp_file(callback_query.message, parsed_kp, company, title, body, fmt)
        return

    # Кнопка "Реферальная программа" из профиля
    if data == "show_referral":
        await callback_query.answer()
        await _show_referral(callback_query.message, user_id)
        return

    # Кнопка "Мой список отслеживания" из профиля
    if data == "show_watchlist":
        await callback_query.answer()
        entries = watchlist_store.get_list(user_id)
        if not entries:
            await callback_query.message.reply_text(
                "📋 Ваш список отслеживания пуст.\n\n"
                "Чтобы добавить компанию — получите отчёт и нажмите кнопку 🔔 Отслеживать."
            )
        else:
            lines = ["📋 Ваши компании для отслеживания:\n"]
            for i, e in enumerate(entries, 1):
                added = e.added_at[:10] if e.added_at else "—"
                checked = e.last_checked[:10] if e.last_checked else "ещё не проверялась"
                lines.append(f"{i}. {e.name}\n   ИНН: {e.inn} | добавлена: {added} | проверена: {checked}")
            lines.append("\nОтправьте ИНН компании чтобы получить свежий отчёт.")
            await callback_query.message.reply_text("\n".join(lines))
        return

    # Выбор компании из результатов поиска по названию
    if data.startswith("name_select:"):
        await callback_query.answer()
        inn_part = data.split(":", 1)[1]
        if not await _check_limit_and_count(callback_query.message, user_id):
            return
        await callback_query.message.reply_text("🔍 Загружаю данные о компании...")
        company = await _fetch_company(inn_part)
        from parsers import ParseResult as PR
        parsed_sel = PR(raw_text=inn_part, inn=inn_part, mode="internal_analysis",
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
        reply = rr(parsed=parsed_sel, company=company, risk=set(), security=sec_result)
        await callback_query.message.reply_text(
            reply,
            disable_web_page_preview=True,
            reply_markup=_company_actions_keyboard(inn_part),
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

    # Массовая проверка — переводим пользователя в режим ожидания файла
    if data == "mode_bulk_check":
        await callback_query.answer()
        await _start_bulk_check(callback_query.message, user_id)
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
            watchlist_count = len(watchlist_store.get_list(user_id))
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📋 Мои компании для отслеживания ({watchlist_count})",
                    callback_data="show_watchlist",
                )],
                [InlineKeyboardButton(
                    "🎁 Реферальная программа",
                    callback_data="show_referral",
                )],
            ])
            await message.reply_text(render_profile(profile), reply_markup=keyboard)
            return
        # Массовая проверка — просим загрузить файл
        if reply_action == "mode_bulk_check":
            await _start_bulk_check(message, user_id)
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

    if pending_action and parsed.inn:
        company = await _fetch_company(parsed.inn)
        await _dispatch_action(message, pending_action, parsed, company)
        return
    elif pending_action and not parsed.inn:
        # Нет ИНН — пробуем поиск по названию
        await _handle_name_search(message, text, user_id)
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

    # Нет ИНН и нет состояния — пробуем поиск по названию
    await _handle_name_search(message, text, user_id)


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
            InlineKeyboardButton("🔔 Отслеживать", callback_data=f"ca_watch:{inn}"),
            InlineKeyboardButton("🔄 Обновить", callback_data=f"ca_refresh:{inn}"),
        ],
        [
            InlineKeyboardButton("📄 Скачать PDF", callback_data=f"ca_pdf:{inn}"),
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
        link, op_id, amount, discount_applied = await subscription_service.create_initial_payment(
            user_id, tariff
        )
    except Exception as exc:
        logger.exception("Payment creation failed: %s", exc)
        await message.reply_text(
            "❌ Не удалось создать платёж. Попробуйте позже или свяжитесь с поддержкой."
        )
        return

    base_price = TARIFF_PRICES[tariff]
    amount_int = int(round(amount))
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {amount_int} ₽", url=link)],
    ])
    if discount_applied:
        invoice_text = (
            f"Счёт на оплату тарифа *{tariff.upper()}*.\n\n"
            f"Цена: ~{base_price} ₽~ → *{amount_int} ₽* "
            f"(скидка {REFERRAL_DISCOUNT_PCT}% по реферальной программе)\n\n"
            "После успешной оплаты тариф активируется автоматически.\n"
            "Карта сохранится для автопродления — отключить: /cancel_subscription\n\n"
            "Нажимая «Оплатить», вы принимаете условия /offer"
        )
    else:
        invoice_text = (
            f"Счёт на оплату тарифа *{tariff.upper()}* — {amount_int} ₽/мес.\n\n"
            "После успешной оплаты тариф активируется автоматически.\n"
            "Карта сохранится для автопродления — отключить: /cancel_subscription\n\n"
            "Нажимая «Оплатить», вы принимаете условия /offer"
        )
    await message.reply_text(invoice_text, reply_markup=keyboard)


def _match_reply_button(text: str) -> Optional[str]:
    """Сопоставляет текст Reply-кнопок с действиями."""
    mapping = {
        "проверка компании": "mode_internal_analysis",
        "массовая проверка": "mode_bulk_check",
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


async def _handle_name_search(message, query: str, user_id: int) -> None:
    """Поиск компании по названию через DaData. Показывает список кнопок для выбора."""
    stripped = query.strip()
    if len(stripped) < 3:
        await message.reply_text(
            "👋 Отправьте ИНН компании (10 или 12 цифр) или название для поиска."
        )
        return

    results = await company_service.dadata.search_by_name(stripped, count=5)
    if not results:
        await message.reply_text(
            f"🔍 По запросу «{stripped}» ничего не найдено.\n\n"
            "Попробуйте уточнить название или введите ИНН напрямую."
        )
        return

    buttons = []
    for r in results:
        city = f", {r['city']}" if r.get("city") else ""
        label = f"{r['name']}{city}"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"name_select:{r['inn']}")])

    await message.reply_text(
        f"🔍 Найдено компаний по запросу «{stripped}»:\nВыберите нужную:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _start_bulk_check(message, user_id: int) -> None:
    """Запускает режим массовой проверки — просит загрузить файл."""
    profile = user_store.get(user_id)
    tariff = profile.effective_tariff()
    limit = BULK_LIMITS.get(tariff, 0)
    if limit == 0:
        await message.reply_text(
            "⛔️ Массовая проверка недоступна на тарифе Free.\n\n"
            "Перейдите на платный тариф — нажмите «Тарифы»."
        )
        return

    _user_state[user_id] = "bulk_check_await_file"
    await message.reply_text(
        f"📋 Массовая проверка компаний\n\n"
        f"Отправьте файл со списком ИНН:\n"
        f"• .txt — по одному ИНН в строке\n"
        f"• .xlsx — ИНН в любой ячейке\n\n"
        f"Лимит на ваш тариф ({tariff.upper()}): до {limit} компаний за раз.\n"
        f"Время обработки: ~3-5 секунд на компанию."
    )


async def handle_document_message(client: Client, message: Message) -> None:
    """Обработка загруженных документов для массовой проверки."""
    user_id = message.from_user.id
    state = _user_state.get(user_id)

    if state != "bulk_check_await_file":
        await message.reply_text(
            "📎 Я получил файл, но сейчас не жду загрузку.\n\n"
            "Чтобы запустить массовую проверку — нажмите «Массовая проверка» в меню."
        )
        return

    _user_state.pop(user_id, None)

    doc = message.document
    if not doc:
        return

    filename = doc.file_name or "file"
    name_lower = filename.lower()
    if not (name_lower.endswith(".txt") or name_lower.endswith(".xlsx") or name_lower.endswith(".xls")):
        await message.reply_text(
            "⚠️ Поддерживаются только файлы .txt и .xlsx. Попробуйте ещё раз."
        )
        return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await message.reply_text("⚠️ Файл слишком большой (>5 МБ).")
        return

    await message.reply_text("📥 Загружаю файл...")
    try:
        buf = await client.download_media(message, in_memory=True)
        content = bytes(buf.getbuffer()) if hasattr(buf, "getbuffer") else buf
    except Exception as exc:
        logger.error("Failed to download bulk file: %s", exc)
        await message.reply_text("⚠️ Не удалось загрузить файл. Попробуйте ещё раз.")
        return

    inns = parse_inns(filename, content)
    if not inns:
        await message.reply_text(
            "⚠️ В файле не найдено ИНН (10 или 12 цифр).\n\n"
            "Проверьте формат: один ИНН в строке (.txt) или в ячейках (.xlsx)."
        )
        return

    profile = user_store.get(user_id)
    tariff = profile.effective_tariff()
    limit = BULK_LIMITS.get(tariff, 0)
    if limit == 0:
        await message.reply_text("⛔️ Массовая проверка недоступна на вашем тарифе.")
        return

    truncated = False
    if len(inns) > limit:
        inns = inns[:limit]
        truncated = True

    # Дневной лимит проверок
    daily_limit = TARIFF_LIMITS.get(tariff)
    if daily_limit is not None:
        remaining = max(0, daily_limit - profile.checks_today)
        if remaining < len(inns):
            await message.reply_text(
                f"⛔️ Сегодня осталось {remaining} проверок из дневного лимита {daily_limit} "
                f"(тариф {tariff.upper()}), а в файле {len(inns)}.\n\n"
                f"Сократите файл или дождитесь завтрашнего обновления лимита."
            )
            return

    header = f"🔄 Запускаю проверку {len(inns)} компаний"
    if truncated:
        header += f" (превышен лимит тарифа — взял первые {limit})"
    status_msg = await message.reply_text(header + "...")

    last_edit = [0]

    async def progress_cb(done: int, total: int) -> None:
        # Обновляем статус каждые 5 компаний или на последней
        if done == total or done - last_edit[0] >= 5:
            last_edit[0] = done
            try:
                await status_msg.edit_text(f"🔄 Обработано {done}/{total} компаний...")
            except Exception:
                pass

    results = await bulk_check_companies(
        inns,
        company_service,
        security_service=security_service,
        progress_cb=progress_cb,
    )

    # Списываем дневные проверки за фактически обработанные
    for _ in results:
        user_store.increment_checks(user_id)

    report = format_bulk_results(results)

    # Если отчёт длинный — отправляем как файл
    if len(report) > 3500:
        buf2 = BytesIO(report.encode("utf-8"))
        buf2.name = f"bulk_results_{user_id}.txt"
        await message.reply_document(
            document=buf2,
            caption=f"📊 Результаты массовой проверки ({len(results)} компаний)",
        )
    else:
        await message.reply_text(report)

    try:
        await status_msg.edit_text(f"✅ Готово: {len(results)} компаний обработано.")
    except Exception:
        pass


def _build_referral_link(user_id: int) -> str:
    code = make_referral_code(user_id)
    if _bot_username:
        return f"https://t.me/{_bot_username}?start={code}"
    return f"start={code}"


async def _show_referral(message, user_id: int) -> None:
    """Показывает реферальную ссылку и статистику."""
    link = _build_referral_link(user_id)
    stats = referral_store.stats(user_id)
    text = (
        "🎁 Реферальная программа\n"
        "\n"
        "Приглашайте коллег и партнёров — получайте бонусы:\n"
        f"• Друг получает скидку {REFERRAL_DISCOUNT_PCT}% на первый платёж\n"
        f"• Вы получаете +{REFERRAL_BONUS_DAYS} дней подписки за каждого оплатившего\n"
        "\n"
        "🔗 Ваша персональная ссылка:\n"
        f"`{link}`\n"
        "\n"
        "📊 Ваша статистика:\n"
        f"• Перешли по ссылке: {stats['total']}\n"
        f"• Оплатили подписку: {stats['converted']}\n"
        f"• Всего начислено бонусных дней: {stats['bonus_days']}\n"
        "\n"
        "Поделитесь ссылкой в чате — Telegram сразу превратит её в кнопку «Открыть бота»."
    )
    await message.reply_text(text, disable_web_page_preview=True)


async def _check_limit_and_count(message, user_id: int) -> bool:
    """Проверяет лимит проверок и увеличивает счётчик.
    Возвращает True если проверка разрешена, False — если лимит исчерпан."""
    profile = user_store.get(user_id)
    if not profile.can_check():
        effective = profile.effective_tariff()
        limit = TARIFF_LIMITS.get(effective, 0)
        await message.reply_text(
            f"⛔️ Лимит проверок исчерпан.\n\n"
            f"Ваш тариф: {effective.upper()} — {limit} проверок в день.\n"
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


async def handle_offer(client: Client, message) -> None:
    await message.reply_text(OFFER_TEXT)


async def handle_admin_report(client: Client, message) -> None:
    """Команда /report — отчёт администратору за последние 30 дней.

    Принимает опциональный аргумент — количество дней: /report 7
    """
    user_id = message.from_user.id
    if not is_admin(user_id, _admin_ids):
        # Тихо игнорируем — не палим существование команды
        return

    args = (message.text or "").split()
    period_days = 30
    if len(args) >= 2 and args[1].isdigit():
        period_days = max(1, min(365, int(args[1])))

    try:
        report = build_report(user_store, payments_store, period_days=period_days)
    except Exception as exc:
        logger.exception("Admin report failed: %s", exc)
        await message.reply_text(f"⚠️ Ошибка формирования отчёта: {exc}")
        return

    await message.reply_text(report)


def main() -> None:
    global subscription_service, _gigachat_credentials, _bot_username, _admin_ids

    settings = Settings.from_env()
    _gigachat_credentials = settings.gigachat_credentials
    _admin_ids = list(settings.admin_ids)
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
            referrals=referral_store,
        )
    else:
        logger.warning("Payments disabled: set TOCHKA_JWT and TOCHKA_CUSTOMER_CODE to enable")

    async def run_all() -> None:
        nonlocal webhook_runner
        global _bot_username
        # Client создаётся ВНУТРИ event loop — иначе dispatcher tasks
        # окажутся на другом loop и не будут вызываться (Pyrogram + asyncio.run)
        app = build_app(settings)

        async def start_handler(client: Client, message) -> None:
            user_id = message.from_user.id
            # Парсим параметр /start ref_XXXXX — это переход по реферальной ссылке
            args = (message.text or "").split(maxsplit=1)
            referral_note = ""
            if len(args) == 2:
                referrer_id = parse_referral_code(args[1].strip())
                if referrer_id and referrer_id != user_id:
                    if not referral_store.has_active_referrer(user_id):
                        # Связываем только новых пользователей, у которых ещё нет реферера
                        # и которые ещё ни разу не платили
                        profile = user_store.get(user_id)
                        if not profile.tariff_expires_at:
                            linked = referral_store.link(user_id, referrer_id)
                            if linked:
                                referral_note = (
                                    f"\n🎁 Вы получили скидку {REFERRAL_DISCOUNT_PCT}% "
                                    f"на первый платёж по реферальной ссылке!\n"
                                )
            await message.reply_text(
                "👋 Финансовый архитектор онлайн!\n\n"
                "Я помогу с анализом компаний и подготовкой КП.\n\n"
                "Что умею:\n"
                "• Отправьте ИНН — получите анализ компании\n"
                "• Нажмите кнопку ниже для нужного действия\n"
                "• /kp pdf <ИНН> — сгенерировать КП в PDF\n"
                "• /kp png <ИНН> — сгенерировать КП в PNG\n"
                "• /menu — показать меню\n"
                "• /my_subscription — статус подписки\n"
                "• /offer — публичная оферта\n"
                f"{referral_note}\n"
                "По всем вопросам: @YRS75",
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
        app.add_handler(MessageHandler(handle_my_subscription, filters.command(["my_subscription"])))
        app.add_handler(MessageHandler(handle_cancel_subscription, filters.command(["cancel_subscription"])))
        app.add_handler(MessageHandler(handle_enable_subscription, filters.command(["enable_subscription"])))
        app.add_handler(MessageHandler(handle_offer, filters.command(["offer"])))
        app.add_handler(MessageHandler(handle_admin_report, filters.command(["report"])))
        app.add_handler(CallbackQueryHandler(handle_callback))
        app.add_handler(MessageHandler(handle_document_message, filters.document))
        app.add_handler(
            MessageHandler(
                handle_text_message,
                filters.text & ~filters.command([
                    "start", "help", "menu", "kp",
                    "my_subscription", "cancel_subscription", "enable_subscription", "offer", "report",
                ]),
            )
        )

        await app.start()
        try:
            me = await app.get_me()
            _bot_username = me.username or ""
            logger.info("Bot started (client) as @%s", _bot_username)
        except Exception as exc:
            logger.warning("Failed to fetch bot username: %s", exc)
            logger.info("Bot started (client)")

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

        tasks.append(asyncio.create_task(
            run_watch_loop(watchlist_store, company_service, notify)
        ))

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        _sd_notify("READY=1\nSTATUS=Bot is running")
        logger.info("Bot is running. Press Ctrl+C to stop.")

        async def watchdog_loop() -> None:
            while not stop_event.is_set():
                _sd_notify("WATCHDOG=1")
                await asyncio.sleep(30)

        tasks.append(asyncio.create_task(watchdog_loop()))
        await stop_event.wait()

        for t in tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        if webhook_runner is not None:
            await webhook_runner.cleanup()
        try:
            await app.stop()
        except RuntimeError:
            pass
        logger.info("Bot stopped.")

    logger.info("Bot starting...")
    asyncio.run(run_all())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
