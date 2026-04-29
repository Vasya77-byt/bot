"""Тесты main — Pyrogram-обработчики бота.

Pyrogram реально не устанавливается в этой среде из-за pyaes, поэтому
подменяем pyrogram через sys.modules ДО импорта main. Глобальные сторы
направляем в tmpdir через env-переменные.

Тестируем:
- pure-хелперы: текст приглашений, нормализация Reply-кнопок, парсинг
  /kp-аргументов, генерация имён файлов и шаблона КП, текст и клавиатура
  тарифов;
- async-обработчики команд (/offer, /documents, /my_subscription,
  /cancel_subscription, /enable_subscription, /kp);
- диспетчер callback'ов (тарифы, mode_*, mass_check для не-business,
  WIP-кнопки ca_*, ca_refresh);
- handle_text_message: Reply-кнопки → действие, mass_check, нормальный
  ИНН-флоу, fallback при пустом тексте;
- бизнес-логику _check_limit_and_count и _send_kp_file.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from typing import Any, List, Optional


# ──────────────────────────────────────────────────────────────────────
# 1) Перед импортом main: env → tmpdir, чтобы UserStore/PaymentsStore/etc
#    не пихали файлы в cwd; pyrogram → фейк.
# ──────────────────────────────────────────────────────────────────────

_TEST_DIR = tempfile.mkdtemp(prefix="bot_main_test_")
os.environ.setdefault("USERS_FILE", os.path.join(_TEST_DIR, "users.json"))
os.environ.setdefault("PAYMENTS_FILE", os.path.join(_TEST_DIR, "payments.json"))
os.environ.setdefault("METADATA_DIR", os.path.join(_TEST_DIR, "metadata"))
os.environ.setdefault("CACHE_DIR", os.path.join(_TEST_DIR, "cache"))
os.environ.setdefault("STORAGE_DIR", os.path.join(_TEST_DIR, "storage"))


def _install_fake_pyrogram() -> None:
    if "pyrogram" in sys.modules and not getattr(sys.modules["pyrogram"], "_is_fake", False):
        return  # настоящий pyrogram уже импортирован

    # ── pyrogram ──
    pyrogram = types.ModuleType("pyrogram")
    pyrogram._is_fake = True

    class Client:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.dispatcher = types.SimpleNamespace(groups={})

        async def start(self): ...
        async def stop(self): ...
        async def send_message(self, *a, **kw): ...

        def run(self, coro):
            import asyncio
            asyncio.get_event_loop().run_until_complete(coro)

    class _Filter:
        def __call__(self, *a, **kw):
            return self

        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    text_filter = _Filter()

    def _command(names):
        return _Filter()

    filters = types.SimpleNamespace(text=text_filter, command=_command)
    pyrogram.Client = Client
    pyrogram.filters = filters

    # ── pyrogram.handlers ──
    handlers = types.ModuleType("pyrogram.handlers")

    class MessageHandler:
        def __init__(self, callback, filters=None):
            self.callback = callback
            self.filters = filters

    class CallbackQueryHandler:
        def __init__(self, callback, filters=None):
            self.callback = callback

    handlers.MessageHandler = MessageHandler
    handlers.CallbackQueryHandler = CallbackQueryHandler

    # ── pyrogram.types ──
    types_mod = types.ModuleType("pyrogram.types")

    class CallbackQuery: ...

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None, url=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class KeyboardButton:
        def __init__(self, text):
            self.text = text

    class ReplyKeyboardMarkup:
        def __init__(self, keyboard, resize_keyboard=False, **kw):
            self.keyboard = keyboard
            self.resize_keyboard = resize_keyboard

    types_mod.CallbackQuery = CallbackQuery
    types_mod.InlineKeyboardButton = InlineKeyboardButton
    types_mod.InlineKeyboardMarkup = InlineKeyboardMarkup
    types_mod.KeyboardButton = KeyboardButton
    types_mod.ReplyKeyboardMarkup = ReplyKeyboardMarkup

    sys.modules["pyrogram"] = pyrogram
    sys.modules["pyrogram.handlers"] = handlers
    sys.modules["pyrogram.types"] = types_mod


_install_fake_pyrogram()

# Теперь импорт main безопасен
import pytest  # noqa: E402

import main  # noqa: E402
from monitoring_store import MonitoringStore  # noqa: E402
from parsers import ParseResult  # noqa: E402
from payments_store import PaymentsStore  # noqa: E402
from schemas import CompanyData  # noqa: E402
from security_check import SecurityResult  # noqa: E402
from user_store import TARIFF_PRICES, UserStore  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# 2) Тестовые фейки сообщения/коллбэка с записью отправленных reply_*.
# ──────────────────────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, text: str = "", user_id: int = 1):
        self.text = text
        self.from_user = types.SimpleNamespace(id=user_id)
        self.replies: List[dict] = []
        self.photos: List[dict] = []
        self.documents: List[dict] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append({"text": text, **kwargs})

    async def reply_photo(self, photo, caption: str = "", **kwargs: Any) -> None:
        self.photos.append({"photo": photo, "caption": caption, **kwargs})

    async def reply_document(self, document, file_name: str = "",
                              caption: str = "", **kwargs: Any) -> None:
        self.documents.append({
            "document": document, "file_name": file_name,
            "caption": caption, **kwargs,
        })


class FakeCallbackQuery:
    def __init__(self, data: str, user_id: int = 1):
        self.data = data
        self.from_user = types.SimpleNamespace(id=user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


# ──────────────────────────────────────────────────────────────────────
# 3) Изоляция глобального состояния main между тестами.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_main(tmp_path, monkeypatch):
    """Перед каждым тестом подменяем глобальные сторы в main на свежие."""
    fresh_users = UserStore(filepath=str(tmp_path / "users.json"))
    fresh_payments = PaymentsStore(filepath=str(tmp_path / "payments.json"))
    fresh_monitoring = MonitoringStore(filepath=str(tmp_path / "monitoring.json"))
    monkeypatch.setattr(main, "user_store", fresh_users)
    monkeypatch.setattr(main, "payments_store", fresh_payments)
    monkeypatch.setattr(main, "monitoring_store", fresh_monitoring)
    monkeypatch.setattr(main, "_user_state", {})
    # subscription_service по умолчанию None
    monkeypatch.setattr(main, "subscription_service", None)
    # По умолчанию считаем, что онбординг пройден — иначе любой тест,
    # проверяющий обработку текста / callback'а, упрётся в гейт.
    # Тесты на сам онбординг пусть явно восстанавливают оригинальную логику.
    async def _no_onboarding(*args, **kwargs):
        return None
    monkeypatch.setattr(main, "_onboarding_step", lambda profile: None)
    monkeypatch.setattr(main, "_onboarding_step_async", _no_onboarding)
    yield


# ──────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────


class TestInnPromptText:
    def test_compare_has_special_message(self):
        text = main._inn_prompt_text("mode_compare")
        assert "первой компании" in text

    @pytest.mark.parametrize("action,word", [
        ("mode_internal_analysis", "внутреннего анализа"),
        ("mode_client_proposal", "коммерческого предложения"),
        ("kp_pdf", "генерации КП (PDF)"),
        ("kp_png", "генерации КП (PNG)"),
        ("mode_mass_check", "массовой проверки"),
    ])
    def test_known_actions_have_label(self, action, word):
        assert word in main._inn_prompt_text(action)

    def test_unknown_action_uses_default_label(self):
        text = main._inn_prompt_text("totally_new_action")
        assert "обработки" in text


class TestMatchReplyButton:
    @pytest.mark.parametrize("text,action", [
        ("📋 Проверка компании", "mode_internal_analysis"),
        ("⚖️ Сравнить", "mode_compare"),
        ("👤 Профиль", "show_profile"),
        ("💎 Тарифы", "show_tariffs"),
    ])
    def test_emoji_buttons_match(self, text, action):
        assert main._match_reply_button(text) == action

    def test_case_insensitive(self):
        assert main._match_reply_button("ПРОФИЛЬ") == "show_profile"

    def test_unknown_text_returns_none(self):
        assert main._match_reply_button("случайный текст без кнопок") is None

    def test_emoji_outside_bmp_stripped(self):
        # ord > 0xFFFF — эмодзи из supplementary plane
        # 💎 (U+1F48E) удаляется, остаётся "Тарифы"
        assert main._match_reply_button("💎 Тарифы") == "show_tariffs"


class TestExtractFormat:
    def test_default_pdf(self):
        assert main._extract_format(["/kp"]) == "pdf"

    def test_explicit_pdf(self):
        assert main._extract_format(["/kp", "pdf"]) == "pdf"

    def test_explicit_png(self):
        assert main._extract_format(["/kp", "png"]) == "png"

    def test_uppercase_normalized(self):
        assert main._extract_format(["/kp", "PNG"]) == "png"


class TestExtractInnArg:
    def test_no_args(self):
        assert main._extract_inn_arg(["/kp"]) is None

    def test_only_format(self):
        assert main._extract_inn_arg(["/kp", "pdf"]) is None

    def test_with_inn(self):
        assert main._extract_inn_arg(["/kp", "pdf", "7707083893"]) == "7707083893"


class TestKpFilename:
    def _parsed(self, inn=None):
        return ParseResult(raw_text="", inn=inn, mode=None,
                           is_request=False, is_proposal=False, company_data=None)

    def test_uses_company_inn_when_present(self):
        c = CompanyData(inn="7707083893")
        assert main._kp_filename(c, self._parsed(), "pdf") == "kp_7707083893.pdf"

    def test_falls_back_to_parsed_inn(self):
        assert main._kp_filename(None, self._parsed(inn="123"), "pdf") == "kp_123.pdf"

    def test_unknown_when_neither_inn_present(self):
        assert main._kp_filename(None, self._parsed(), "png") == "kp_unknown.png"

    def test_company_inn_wins_over_parsed(self):
        c = CompanyData(inn="from_company")
        p = self._parsed(inn="from_parsed")
        assert main._kp_filename(c, p, "pdf") == "kp_from_company.pdf"


class TestKpTemplate:
    def test_title_and_body_returned(self):
        title, body = main._kp_template()
        assert title == "Коммерческое предложение"
        assert "Индивидуальная настройка" in body
        assert "комплаенсу" in body


class TestTariffsText:
    def test_all_tariffs_listed(self):
        text = main._tariffs_text()
        assert "Free" in text
        assert "Start" in text
        assert "Pro" in text
        assert "Business" in text

    @pytest.mark.parametrize("tariff,price", list(TARIFF_PRICES.items()))
    def test_each_paid_price_present(self, tariff, price):
        text = main._tariffs_text()
        # Цены могут быть с пробелом-разделителем тысяч
        with_space = f"{price:,}".replace(",", " ")
        assert (str(price) in text) or (with_space in text)


class TestKeyboards:
    def test_main_menu_has_three_buttons(self):
        kb = main._main_menu()
        rows = kb.inline_keyboard
        assert len(rows) == 3
        # Каждая строка — список кнопок
        assert all(len(row) >= 1 for row in rows)

    def test_reply_keyboard_resize_on(self):
        kb = main._reply_keyboard()
        assert kb.resize_keyboard is True

    def test_company_actions_keyboard_passes_inn_in_callback(self):
        kb = main._company_actions_keyboard("7707083893")
        # Все callback_data вида ca_*:7707083893 должны содержать ИНН
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.callback_data and btn.callback_data.startswith("ca_"):
                    assert "7707083893" in btn.callback_data or btn.callback_data == "ca_proposal:7707083893"

    def test_tariffs_keyboard_callback_format(self):
        kb = main._tariffs_keyboard()
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "tariff_start" in callbacks
        assert "tariff_pro" in callbacks
        assert "tariff_business" in callbacks


# ──────────────────────────────────────────────────────────────────────
# Простые command-обработчики
# ──────────────────────────────────────────────────────────────────────


class TestSimpleHandlers:
    @pytest.mark.asyncio
    async def test_handle_offer_sends_offer_text(self):
        msg = FakeMessage()
        await main.handle_offer(client=None, message=msg)
        assert len(msg.replies) == 1
        from offer import OFFER_TEXT
        assert msg.replies[0]["text"] == OFFER_TEXT

    @pytest.mark.asyncio
    async def test_handle_documents_shows_keyboard(self):
        msg = FakeMessage()
        await main.handle_documents(client=None, message=msg)
        text = msg.replies[0]["text"]
        assert "Правовые документы" in text
        # Inline-кнопки ведут на Telegraph-публикации
        kb = msg.replies[0]["reply_markup"]
        urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
        assert len(urls) == 3
        assert all("telegra.ph" in u for u in urls)

    @pytest.mark.asyncio
    async def test_my_subscription_free_user(self):
        msg = FakeMessage(user_id=42)
        await main.handle_my_subscription(client=None, message=msg)
        assert "Free" in msg.replies[0]["text"]
        assert "3 проверки" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_my_subscription_paid_active(self):
        msg = FakeMessage(user_id=42)
        main.user_store.activate_subscription(42, "pro", days=30, card_token="tok")
        await main.handle_my_subscription(client=None, message=msg)
        text = msg.replies[0]["text"]
        assert "PRO" in text
        assert "активна" in text
        assert "включено" in text  # auto_renew

    @pytest.mark.asyncio
    async def test_cancel_subscription_disables_auto_renew(self):
        main.user_store.activate_subscription(42, "pro", days=30, card_token="tok")
        msg = FakeMessage(user_id=42)
        await main.handle_cancel_subscription(client=None, message=msg)
        assert main.user_store.get(42).auto_renew is False
        assert "отключено" in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_enable_subscription_blocked_for_free(self):
        msg = FakeMessage(user_id=42)
        await main.handle_enable_subscription(client=None, message=msg)
        # auto_renew у free остаётся как был (по умолчанию True), и сообщение про "оформите"
        assert "Тарифы" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_enable_subscription_for_active_paid_user(self):
        # Активная подписка + auto_renew выключен
        main.user_store.activate_subscription(42, "pro", days=30, card_token="tok")
        main.user_store.disable_auto_renew(42)
        msg = FakeMessage(user_id=42)
        await main.handle_enable_subscription(client=None, message=msg)
        assert main.user_store.get(42).auto_renew is True
        assert "включено" in msg.replies[0]["text"].lower()


class TestNewCommandHandlers:
    """Команды из DVAsR: /disclaimer, /tarifs, /cancel."""

    @pytest.mark.asyncio
    async def test_disclaimer_sends_disclaimer_text(self):
        msg = FakeMessage()
        await main.handle_disclaimer(client=None, message=msg)
        text = msg.replies[0]["text"]
        # Ключевые юридические маркеры
        assert "ДИСКЛЕЙМЕР" in text
        assert "152-ФЗ" in text
        assert "открытых источников" in text.lower()

    @pytest.mark.asyncio
    async def test_tarifs_shows_tariffs_text_and_keyboard(self):
        msg = FakeMessage()
        await main.handle_tarifs(client=None, message=msg)
        text = msg.replies[0]["text"]
        assert "Тарифные планы" in text
        # Имеется клавиатура выбора тарифов
        kb = msg.replies[0]["reply_markup"]
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "tariff_pro" in callbacks
        assert "tariff_business" in callbacks

    @pytest.mark.asyncio
    async def test_cancel_clears_active_state(self):
        main._user_state[1] = "mode_internal_analysis"
        msg = FakeMessage(user_id=1)
        await main.handle_cancel(client=None, message=msg)
        assert 1 not in main._user_state
        assert "отменено" in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_cancel_without_active_state_shows_friendly_message(self):
        # State пуст
        assert 1 not in main._user_state
        msg = FakeMessage(user_id=1)
        await main.handle_cancel(client=None, message=msg)
        text = msg.replies[0]["text"]
        # Обоим веткам ясно: нет действия — ничего не отменено
        assert "Нет активного" in text or "нет активного" in text.lower()

    @pytest.mark.asyncio
    async def test_cancel_clears_pending_dict_state(self):
        # compare_step2 — dict-стейт; cancel должен и его обнулять
        main._user_state[1] = {"action": "compare_step2", "inn1": "111"}
        msg = FakeMessage(user_id=1)
        await main.handle_cancel(client=None, message=msg)
        assert 1 not in main._user_state


class TestDocumentsKeyboard:
    def test_three_telegraph_buttons(self):
        kb = main._documents_keyboard()
        rows = kb.inline_keyboard
        assert len(rows) == 3
        urls = [row[0].url for row in rows]
        assert all("telegra.ph" in u for u in urls)

    def test_buttons_cover_all_required_documents(self):
        kb = main._documents_keyboard()
        labels = [row[0].text for row in kb.inline_keyboard]
        joined = " | ".join(labels)
        assert "оферта" in joined.lower()
        assert "соглашение" in joined.lower()
        assert "персональн" in joined.lower()


# ──────────────────────────────────────────────────────────────────────
# handle_callback
# ──────────────────────────────────────────────────────────────────────


class TestHandleCallback:
    @pytest.mark.asyncio
    async def test_mode_internal_analysis_sets_state_and_prompts(self):
        cb = FakeCallbackQuery("mode_internal_analysis", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        assert cb.answered
        assert main._user_state[1] == "mode_internal_analysis"
        assert "ИНН" in cb.message.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_mode_compare_uses_special_prompt(self):
        cb = FakeCallbackQuery("mode_compare", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        assert "первой компании" in cb.message.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_unknown_tariff_callback(self):
        cb = FakeCallbackQuery("tariff_enterprise", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        assert "не найден" in cb.message.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_tariff_callback_without_subscription_service(self):
        # subscription_service=None из autouse fixture
        p = main.user_store.get(1)
        p.email = "buyer@example.com"
        main.user_store.save_profile(p)
        cb = FakeCallbackQuery("tariff_pro", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        assert "не настроен" in cb.message.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_mass_check_blocks_non_business_users(self):
        cb = FakeCallbackQuery("mode_mass_check", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        text = cb.message.replies[0]["text"]
        assert "Business" in text
        assert "только на тарифе" in text
        # Состояние не должно установиться
        assert 1 not in main._user_state

    @pytest.mark.asyncio
    async def test_mass_check_allows_business_user(self):
        main.user_store.set_tariff(7, "business")
        cb = FakeCallbackQuery("mode_mass_check", user_id=7)
        await main.handle_callback(client=None, callback_query=cb)
        assert main._user_state[7] == "mode_mass_check"
        assert "ИНН" in cb.message.replies[0]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data,label_word", [
        ("ca_fns:1234567890", "ФНС"),
        ("ca_egryl:1234567890", "ЕГРЮЛ"),
        ("ca_history:1234567890", "История"),
        ("ca_links:1234567890", "Связи"),
        ("ca_invoice:1234567890", "Запрос счёта"),
        ("ca_proposal:1234567890", "Предложение"),
    ])
    async def test_wip_action_buttons_show_in_progress(self, data, label_word):
        cb = FakeCallbackQuery(data, user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        text = cb.message.replies[0]["text"]
        assert "разработке" in text
        assert label_word in text

    @pytest.mark.asyncio
    async def test_ca_courts_without_zchb_key_shows_friendly_message(
        self, monkeypatch,
    ):
        # Ключ не задан → бот сообщает что источник не настроен
        monkeypatch.setattr(main.zchb, "api_key", "")
        cb = FakeCallbackQuery("ca_courts:7707083893", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        text = cb.message.replies[0]["text"]
        assert "не настроен" in text or "ZCHB_API_KEY" in text

    @pytest.mark.asyncio
    async def test_ca_courts_with_key_calls_zchb(self, monkeypatch):
        from zchb_client import ArbitrationSummary

        async def fake_get(inn):
            return ArbitrationSummary(total_exact=0)

        monkeypatch.setattr(main.zchb, "api_key", "test")
        monkeypatch.setattr(main.zchb, "get_arbitration", fake_get)
        cb = FakeCallbackQuery("ca_courts:7707083893", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        # Сообщения: "Запрашиваю ..." + результат
        joined = " ".join(r["text"] for r in cb.message.replies)
        assert "Запрашиваю" in joined
        assert "не найдено" in joined


class TestCaAiGigaChat:
    """ca_ai больше не WIP — ходит в GigaChat. Нет кредов → friendly fallback."""

    @pytest.mark.asyncio
    async def test_ca_ai_calls_gigachat_with_company_data(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(
                inn=inn, name="ПАО Сбербанк", okved_main="64.19",
                age_years=30, status="Действующая",
            )

        captured = {}

        async def fake_analyze(**kwargs):
            captured.update(kwargs)
            return "🤖 Анализ компании готов."

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.gigachat, "analyze_company", fake_analyze)

        cb = FakeCallbackQuery("ca_ai:7707083893", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)

        assert captured["inn"] == "7707083893"
        assert captured["name"] == "ПАО Сбербанк"
        assert captured["okved"] == "64.19"
        # Ответы: «Запрашиваю...» + сам анализ
        joined = " ".join(r["text"] for r in cb.message.replies)
        assert "Анализ компании готов" in joined

    @pytest.mark.asyncio
    async def test_ca_ai_friendly_message_when_credentials_missing(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        async def empty_analyze(**kwargs):
            return None  # GigaChat вернул None из-за отсутствия кредов

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.gigachat, "analyze_company", empty_analyze)

        cb = FakeCallbackQuery("ca_ai:1234567890", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        joined = " ".join(r["text"] for r in cb.message.replies)
        assert "GIGACHAT_CREDENTIALS" in joined or "не удалось" in joined.lower()

    @pytest.mark.asyncio
    async def test_ca_ai_no_company_passes_inn_as_name(self, monkeypatch):
        async def fake_fetch(inn):
            return None  # компания не найдена

        captured = {}

        async def fake_analyze(**kwargs):
            captured.update(kwargs)
            return "result"

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.gigachat, "analyze_company", fake_analyze)

        cb = FakeCallbackQuery("ca_ai:7777777777", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        # name fallback на ИНН
        assert captured["name"] == "7777777777"


# ──────────────────────────────────────────────────────────────────────
# handle_text_message
# ──────────────────────────────────────────────────────────────────────


class TestHandleTextMessage:
    @pytest.mark.asyncio
    async def test_tariffs_button_shows_tariffs(self):
        msg = FakeMessage(text="💎 Тарифы", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "Тарифные планы" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_profile_button_shows_profile(self):
        msg = FakeMessage(text="👤 Профиль", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "Ваш профиль" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_short_text_falls_back_to_help(self, monkeypatch):
        # Текст короче 3 символов / без букв → не идёт в поиск,
        # а попадает в подсказку
        async def fake_suggest(query, count=5):
            raise AssertionError("suggest must not be called for short text")

        monkeypatch.setattr(main.company_service, "suggest", fake_suggest)
        msg = FakeMessage(text="!!", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "/menu" in msg.replies[-1]["text"]

    @pytest.mark.asyncio
    async def test_unknown_text_triggers_search_with_no_results(self, monkeypatch):
        # «привет» проходит как кандидат на название → поиск
        async def empty_suggest(query, count=5):
            return []

        monkeypatch.setattr(main.company_service, "suggest", empty_suggest)
        msg = FakeMessage(text="привет", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        text = msg.replies[-1]["text"]
        assert "ничего не найдено" in text.lower()
        assert "ИНН" in text

    @pytest.mark.asyncio
    async def test_reply_button_without_inn_asks_for_inn(self):
        msg = FakeMessage(text="📋 Проверка компании", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert main._user_state[1] == "mode_internal_analysis"
        assert "ИНН" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_pending_action_with_invalid_inn_resets(self):
        main._user_state[1] = "mode_internal_analysis"
        msg = FakeMessage(text="не ИНН", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Состояние сброшено
        assert 1 not in main._user_state
        assert "сброшено" in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_compare_step1_asks_for_second_inn(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Первая")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main._user_state[1] = "mode_compare"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Состояние перешло в compare_step2
        assert isinstance(main._user_state[1], dict)
        assert main._user_state[1]["action"] == "compare_step2"
        assert main._user_state[1]["inn1"] == "7707083893"
        assert "второй компании" in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_compare_step2_renders_comparison(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name=f"ООО {inn}", region="Москва")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main._user_state[1] = {"action": "compare_step2", "inn1": "1111111111"}
        msg = FakeMessage(text="2222222222", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Первое сообщение — "загружаю", второе — сравнение
        all_text = " ".join(r["text"] for r in msg.replies)
        assert "1111111111" in all_text
        assert "2222222222" in all_text
        assert "Сравнение компаний" in all_text

    @pytest.mark.asyncio
    async def test_compare_step2_invalid_inn_resets_state(self):
        main._user_state[1] = {"action": "compare_step2", "inn1": "1111111111"}
        msg = FakeMessage(text="без ИНН", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "сброшено" in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_mass_check_no_inns_keeps_state(self):
        main.user_store.set_tariff(1, "business")
        main._user_state[1] = "mode_mass_check"
        msg = FakeMessage(text="абракадабра без цифр", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Состояние возвращено для повторного ввода
        assert main._user_state[1] == "mode_mass_check"
        assert "Не найдено" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_mass_check_processes_multiple_inns(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name=f"ООО {inn}")

        async def fake_security(**kw):
            return SecurityResult(risk_level="low")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", fake_security)
        main.user_store.set_tariff(1, "business")
        main._user_state[1] = "mode_mass_check"

        msg = FakeMessage(text="1111111111, 2222222222\n3333333333", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Сообщения: "Начинаю проверку 3 ИНН" + 3 ответа по компаниям
        assert any("3 ИНН" in r["text"] for r in msg.replies)
        # Каждый ИНН — в каком-то из ответов
        joined = " ".join(r["text"] for r in msg.replies)
        for inn in ("1111111111", "2222222222", "3333333333"):
            assert inn in joined

    @pytest.mark.asyncio
    async def test_kp_pdf_trigger(self, monkeypatch):
        captured = {}

        async def fake_send(message, parsed, company, fmt):
            captured["fmt"] = fmt
            captured["company"] = company

        monkeypatch.setattr(main, "_send_kp_auto", fake_send)

        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)

        msg = FakeMessage(text="7707083893 кп pdf", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert captured["fmt"] == "pdf"

    @pytest.mark.asyncio
    async def test_kp_png_trigger(self, monkeypatch):
        captured = {}

        async def fake_send(message, parsed, company, fmt):
            captured["fmt"] = fmt

        monkeypatch.setattr(main, "_send_kp_auto", fake_send)
        msg = FakeMessage(text="кп png", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert captured["fmt"] == "png"

    @pytest.mark.asyncio
    async def test_inn_text_renders_full_response(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Тест", source="dadata")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        text = msg.replies[0]["text"]
        # Mixed-режим — анализ + мини-КП
        assert "Анализ компании" in text
        assert "Мини-КП" in text


# ──────────────────────────────────────────────────────────────────────
# handle_kp_command
# ──────────────────────────────────────────────────────────────────────


class TestHandleKpCommand:
    @pytest.mark.asyncio
    async def test_no_inn_prompts_for_pdf(self):
        msg = FakeMessage(text="/kp pdf", user_id=1)
        await main.handle_kp_command(client=None, message=msg)
        assert main._user_state[1] == "kp_pdf"
        assert "ИНН" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_no_inn_prompts_for_png(self):
        msg = FakeMessage(text="/kp png", user_id=1)
        await main.handle_kp_command(client=None, message=msg)
        assert main._user_state[1] == "kp_png"

    @pytest.mark.asyncio
    async def test_default_format_pdf(self):
        msg = FakeMessage(text="/kp", user_id=1)
        await main.handle_kp_command(client=None, message=msg)
        assert main._user_state[1] == "kp_pdf"

    @pytest.mark.asyncio
    async def test_with_inn_generates_kp(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Тест")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        msg = FakeMessage(text="/kp pdf 7707083893", user_id=1)
        await main.handle_kp_command(client=None, message=msg)
        # Документ отправлен
        assert len(msg.documents) == 1
        assert msg.documents[0]["file_name"] == "kp_7707083893.pdf"


# ──────────────────────────────────────────────────────────────────────
# _check_limit_and_count
# ──────────────────────────────────────────────────────────────────────


class TestCheckLimitAndCount:
    @pytest.mark.asyncio
    async def test_under_limit_increments(self):
        msg = FakeMessage(user_id=1)
        result = await main._check_limit_and_count(msg, 1)
        assert result is True
        assert main.user_store.get(1).checks_today == 1
        # Не должно быть warning'ов
        assert msg.replies == []

    @pytest.mark.asyncio
    async def test_over_limit_blocks(self):
        # free лимит = 3; делаем 3 проверки
        for _ in range(3):
            await main._check_limit_and_count(FakeMessage(user_id=1), 1)
        msg = FakeMessage(user_id=1)
        result = await main._check_limit_and_count(msg, 1)
        assert result is False
        assert "Лимит" in msg.replies[0]["text"]
        # Счётчик не увеличивается выше 3
        assert main.user_store.get(1).checks_today == 3

    @pytest.mark.asyncio
    async def test_business_unlimited(self):
        main.user_store.activate_subscription(1, "business", days=30, card_token="t")
        # Можем сделать много проверок без блокировки
        for _ in range(20):
            ok = await main._check_limit_and_count(FakeMessage(user_id=1), 1)
            assert ok is True


# ──────────────────────────────────────────────────────────────────────
# _send_kp_file: проверяем оба формата
# ──────────────────────────────────────────────────────────────────────


class TestSendKpFile:
    def _parsed(self, inn=None):
        return ParseResult(raw_text="", inn=inn, mode=None,
                           is_request=False, is_proposal=False, company_data=None)

    @pytest.mark.asyncio
    async def test_pdf_sent_as_document(self):
        msg = FakeMessage(user_id=1)
        company = CompanyData(inn="7707083893", name="ООО Тест")
        await main._send_kp_file(msg, self._parsed(), company, "Title", "Body", "pdf")
        assert len(msg.documents) == 1
        assert msg.documents[0]["file_name"] == "kp_7707083893.pdf"
        # Содержимое — реальный PDF (магия %PDF-)
        doc = msg.documents[0]["document"]
        doc.seek(0)
        assert doc.read(5) == b"%PDF-"

    @pytest.mark.asyncio
    async def test_png_sent_as_photo(self):
        msg = FakeMessage(user_id=1)
        company = CompanyData(inn="7707083893")
        await main._send_kp_file(msg, self._parsed(), company, "T", "B", "png")
        assert len(msg.photos) == 1
        photo = msg.photos[0]["photo"]
        photo.seek(0)
        assert photo.read(8) == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.asyncio
    async def test_unknown_format_treated_as_pdf(self):
        # Любой не-png формат идёт через PDF-ветку
        msg = FakeMessage(user_id=1)
        company = CompanyData(inn="123")
        await main._send_kp_file(msg, self._parsed(), company, "T", "B", "docx")
        assert len(msg.documents) == 1
        assert msg.documents[0]["file_name"] == "kp_123.docx"


# ──────────────────────────────────────────────────────────────────────
# ca_refresh callback flow
# ──────────────────────────────────────────────────────────────────────


class TestCaRefresh:
    @pytest.mark.asyncio
    async def test_refresh_triggers_fetch_and_security(self, monkeypatch):
        fetch_calls = []
        security_calls = []

        async def fake_fetch(inn):
            fetch_calls.append(inn)
            return CompanyData(inn=inn, name="ООО Свежая", okved_main="62.01")

        async def fake_security(**kw):
            security_calls.append(kw)
            return SecurityResult(risk_level="low")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", fake_security)

        cb = FakeCallbackQuery("ca_refresh:7707083893", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)

        assert fetch_calls == ["7707083893"]
        assert security_calls and security_calls[0]["inn"] == "7707083893"
        # Ответы: "Обновляю..." + результат
        assert any("Обновляю" in r["text"] for r in cb.message.replies)

    @pytest.mark.asyncio
    async def test_refresh_swallows_security_error(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        async def boom_security(**kw):
            raise RuntimeError("FSSP down")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", boom_security)

        cb = FakeCallbackQuery("ca_refresh:1234567890", user_id=1)
        # Не должно бросить
        await main.handle_callback(client=None, callback_query=cb)
        # Всё равно отправлено сообщение с обновлением
        assert len(cb.message.replies) >= 2


# ──────────────────────────────────────────────────────────────────────
# _handle_buy_tariff success path
# ──────────────────────────────────────────────────────────────────────


class FakeSubscriptionService:
    def __init__(self, link: str = "https://pay.tochka/op-1",
                 op_id: str = "op-1", exception: Optional[Exception] = None):
        self.link = link
        self.op_id = op_id
        self.exception = exception
        self.calls: List[dict] = []

    async def create_initial_payment(self, user_id, tariff):
        self.calls.append({"user_id": user_id, "tariff": tariff})
        if self.exception:
            raise self.exception
        return self.link, self.op_id


class TestHandleBuyTariff:
    @pytest.mark.asyncio
    async def test_buy_pro_creates_payment_link(self, monkeypatch):
        sub = FakeSubscriptionService()
        monkeypatch.setattr(main, "subscription_service", sub)
        p = main.user_store.get(42)
        p.email = "buyer@example.com"
        main.user_store.save_profile(p)
        cb = FakeCallbackQuery("tariff_pro", user_id=42)
        await main.handle_callback(client=None, callback_query=cb)

        assert sub.calls == [{"user_id": 42, "tariff": "pro"}]
        # Сообщения: "Создаю ссылку..." + сообщение с inline-кнопкой
        assert any("ссылку" in r["text"] for r in cb.message.replies)
        # Последний ответ содержит сумму pro и оферту
        last = cb.message.replies[-1]
        assert "1290" in last["text"] or "1 290" in last["text"]
        assert "/offer" in last["text"]
        # Кнопка "Оплатить" с URL ссылки
        kb = last["reply_markup"]
        url_buttons = [b for row in kb.inline_keyboard for b in row]
        assert any(b.url == "https://pay.tochka/op-1" for b in url_buttons)

    @pytest.mark.asyncio
    async def test_buy_payment_failure_friendly_message(self, monkeypatch):
        sub = FakeSubscriptionService(exception=RuntimeError("Tochka 500"))
        monkeypatch.setattr(main, "subscription_service", sub)
        p = main.user_store.get(1)
        p.email = "buyer@example.com"
        main.user_store.save_profile(p)
        cb = FakeCallbackQuery("tariff_pro", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        # Не падает, шлёт friendly-сообщение
        assert any("Не удалось" in r["text"] for r in cb.message.replies)


# ──────────────────────────────────────────────────────────────────────
# _dispatch_action через handle_text_message с pending state
# ──────────────────────────────────────────────────────────────────────


class TestDispatchActionPending:
    @pytest.mark.asyncio
    async def test_internal_analysis_pending_runs_security_check(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Х", okved_main="62.01")

        async def fake_security(**kw):
            return SecurityResult(
                has_enforcement=True, enforcement_count=2,
                risk_level="medium",
            )

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", fake_security)

        main._user_state[1] = "mode_internal_analysis"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)

        text = msg.replies[0]["text"]
        # Стоп-листы — фирменный заголовок internal_analysis
        assert "Стоп-листы" in text
        # ФССП с 2 производствами
        assert "2 производств" in text
        # Кнопки действий под карточкой
        kb = msg.replies[0]["reply_markup"]
        callbacks = [b.callback_data for row in kb.inline_keyboard
                     for b in row if b.callback_data]
        assert any("ca_courts" in cb for cb in callbacks)

    @pytest.mark.asyncio
    async def test_internal_analysis_pending_security_failure_does_not_crash(
        self, monkeypatch
    ):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        async def boom_security(**kw):
            raise RuntimeError("FSSP failed")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", boom_security)

        main._user_state[1] = "mode_internal_analysis"
        msg = FakeMessage(text="7707083893", user_id=1)
        # Не падает
        await main.handle_text_message(client=None, message=msg)
        assert len(msg.replies) == 1

    @pytest.mark.asyncio
    async def test_internal_analysis_blocked_when_limit_exhausted(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn)

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)

        # Исчерпываем лимит
        for _ in range(3):
            await main._check_limit_and_count(FakeMessage(user_id=1), 1)

        main._user_state[1] = "mode_internal_analysis"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Ответ — про лимит
        assert "Лимит" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_client_proposal_pending(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main._user_state[1] = "mode_client_proposal"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "Коммерческое предложение" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_request_pending(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Тест")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main._user_state[1] = "mode_request"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "ЗАЯВКА НА ОБСЛУЖИВАНИЕ" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_proposal_pending(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Тест")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main._user_state[1] = "mode_proposal"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "ПРЕДЛОЖЕНИЕ ДЛЯ КОМПАНИИ" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_kp_pdf_pending_sends_document(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main._user_state[1] = "kp_pdf"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert len(msg.documents) == 1
        assert msg.documents[0]["file_name"] == "kp_7707083893.pdf"

    @pytest.mark.asyncio
    async def test_kp_png_pending_sends_photo(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main._user_state[1] = "kp_png"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert len(msg.photos) == 1


# ──────────────────────────────────────────────────────────────────────
# _resolve_company helper
# ──────────────────────────────────────────────────────────────────────


class TestResolveCompany:
    @pytest.mark.asyncio
    async def test_returns_company_from_text_json(self):
        import json
        text = json.dumps({"inn": "123", "name": "ООО Из JSON"})
        result = await main._resolve_company(text, inn_arg=None)
        assert result is not None
        assert result.name == "ООО Из JSON"

    @pytest.mark.asyncio
    async def test_uses_inn_arg_when_no_json(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name=f"Resolved {inn}")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        result = await main._resolve_company("просто текст", inn_arg="7707083893")
        assert result.name == "Resolved 7707083893"

    @pytest.mark.asyncio
    async def test_falls_back_to_parsed_inn_in_text(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn)

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        result = await main._resolve_company("проверь 7707083893", inn_arg=None)
        assert result.inn == "7707083893"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_inn(self):
        result = await main._resolve_company("просто текст", inn_arg=None)
        assert result is None


# ──────────────────────────────────────────────────────────────────────
# Monitoring handlers: /monitor /unmonitor /monitoring
# ──────────────────────────────────────────────────────────────────────


class TestHandleMonitorAdd:
    @pytest.mark.asyncio
    async def test_no_inn_arg_prompts_for_inn(self):
        msg = FakeMessage(text="/monitor", user_id=1)
        await main.handle_monitor_add(client=None, message=msg)
        assert main._user_state[1] == "monitor_add"
        assert "ИНН" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_free_user_blocked(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        msg = FakeMessage(text="/monitor 7707083893", user_id=1)
        await main.handle_monitor_add(client=None, message=msg)
        assert "Free" in msg.replies[0]["text"]
        # Подписка не создана
        assert main.monitoring_store.count_for_user(1) == 0

    @pytest.mark.asyncio
    async def test_start_user_can_subscribe(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Тест", status="Действующая")

        async def fake_security(**kw):
            return SecurityResult(risk_level="low")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", fake_security)
        main.user_store.activate_subscription(1, "start", days=30, card_token="t")

        msg = FakeMessage(text="/monitor 7707083893", user_id=1)
        await main.handle_monitor_add(client=None, message=msg)
        assert "Подписка создана" in msg.replies[0]["text"]
        assert main.monitoring_store.count_for_user(1) == 1
        sub = main.monitoring_store.get(1, "7707083893")
        assert sub.snapshot.get("status") == "Действующая"

    @pytest.mark.asyncio
    async def test_limit_exhausted_blocks_new_subscription(self, monkeypatch):
        # На start лимит = 5; занимаем все
        for i in range(5):
            main.monitoring_store.add(1, f"ИНН-{i}", f"Компания {i}")
        main.user_store.activate_subscription(1, "start", days=30, card_token="t")

        async def fake_fetch(inn):
            raise AssertionError("must not fetch when limit exhausted")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        msg = FakeMessage(text="/monitor 7707083893", user_id=1)
        await main.handle_monitor_add(client=None, message=msg)
        assert "Лимит" in msg.replies[0]["text"]
        assert main.monitoring_store.count_for_user(1) == 5

    @pytest.mark.asyncio
    async def test_existing_subscription_updates_in_place(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ООО Свежая", status="Действующая")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check",
                            lambda **kw: __import__("asyncio").sleep(0)
                            if False else None)
        # Простая заглушка под async
        async def fake_security(**kw):
            return SecurityResult(risk_level="low")

        monkeypatch.setattr(main.security_service, "check", fake_security)
        main.user_store.activate_subscription(1, "start", days=30, card_token="t")

        # Существующая подписка
        main.monitoring_store.add(1, "7707083893", "Старое имя")

        msg = FakeMessage(text="/monitor 7707083893", user_id=1)
        await main.handle_monitor_add(client=None, message=msg)
        # Не было превышения лимита, существующая подписка
        assert main.monitoring_store.count_for_user(1) == 1
        # Уведомление о ОБНОВЛЕНИИ, а не СОЗДАНИИ
        text = msg.replies[0]["text"]
        assert "обновлена" in text.lower()

    @pytest.mark.asyncio
    async def test_company_not_found_no_subscription_created(self, monkeypatch):
        async def fake_fetch(inn):
            return None

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        main.user_store.activate_subscription(1, "start", days=30, card_token="t")

        msg = FakeMessage(text="/monitor 7707083893", user_id=1)
        await main.handle_monitor_add(client=None, message=msg)
        assert "не удалось" in msg.replies[0]["text"].lower()
        assert main.monitoring_store.count_for_user(1) == 0

    @pytest.mark.asyncio
    async def test_pending_monitor_add_via_text_message(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X", status="Действующая")

        async def fake_security(**kw):
            return SecurityResult(risk_level="low")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", fake_security)
        main.user_store.activate_subscription(1, "pro", days=30, card_token="t")

        # Сценарий: /monitor без ИНН → state=monitor_add → текстовый ИНН
        main._user_state[1] = "monitor_add"
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert main.monitoring_store.count_for_user(1) == 1


class TestHandleMonitorRemove:
    @pytest.mark.asyncio
    async def test_no_inn_arg_shows_usage(self):
        msg = FakeMessage(text="/unmonitor", user_id=1)
        await main.handle_monitor_remove(client=None, message=msg)
        assert "/unmonitor" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_removes_existing(self):
        main.monitoring_store.add(1, "7707083893", "X")
        msg = FakeMessage(text="/unmonitor 7707083893", user_id=1)
        await main.handle_monitor_remove(client=None, message=msg)
        assert "удалена" in msg.replies[0]["text"].lower()
        assert main.monitoring_store.count_for_user(1) == 0

    @pytest.mark.asyncio
    async def test_remove_missing_friendly_message(self):
        msg = FakeMessage(text="/unmonitor 7707083893", user_id=1)
        await main.handle_monitor_remove(client=None, message=msg)
        assert "нет" in msg.replies[0]["text"].lower()


class TestHandleMonitoringList:
    @pytest.mark.asyncio
    async def test_empty_list(self):
        msg = FakeMessage(text="/monitoring", user_id=1)
        await main.handle_monitoring_list(client=None, message=msg)
        text = msg.replies[0]["text"]
        assert "нет" in text.lower()
        # Free → лимит 0
        assert "0" in text

    @pytest.mark.asyncio
    async def test_list_shows_subscriptions(self):
        main.user_store.activate_subscription(1, "pro", days=30, card_token="t")
        main.monitoring_store.add(1, "111", "ООО Альфа")
        main.monitoring_store.add(1, "222", "ООО Бета")

        msg = FakeMessage(text="/monitoring", user_id=1)
        await main.handle_monitoring_list(client=None, message=msg)
        text = msg.replies[0]["text"]
        assert "ООО Альфа" in text
        assert "ООО Бета" in text
        assert "111" in text
        assert "222" in text

    @pytest.mark.asyncio
    async def test_business_shows_infinity_limit(self):
        main.user_store.activate_subscription(1, "business", days=30, card_token="t")
        main.monitoring_store.add(1, "111", "X")
        msg = FakeMessage(text="/monitoring", user_id=1)
        await main.handle_monitoring_list(client=None, message=msg)
        assert "∞" in msg.replies[0]["text"]


# ──────────────────────────────────────────────────────────────────────
# Партнёрская программа: /start ref_<code> + /referral
# ──────────────────────────────────────────────────────────────────────


class TestHandleStartReferral:
    @pytest.mark.asyncio
    async def test_plain_start_no_referral_message(self):
        msg = FakeMessage(text="/start", user_id=1)
        await main.handle_start(client=None, message=msg)
        # Два ответа: welcome + меню
        assert len(msg.replies) == 2
        assert "реферальной" not in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_help_command_uses_same_handler(self):
        msg = FakeMessage(text="/help", user_id=1)
        await main.handle_start(client=None, message=msg)
        assert any("умею" in r["text"].lower() for r in msg.replies)

    @pytest.mark.asyncio
    async def test_valid_referral_code_attaches_user(self):
        # Сначала создаём референта и берём его код
        referrer = main.user_store.get(100)
        ref_code = referrer.referral_code

        msg = FakeMessage(text=f"/start {ref_code}", user_id=42)
        await main.handle_start(client=None, message=msg)

        # Приглашённый привязан
        assert main.user_store.get(42).referrer_id == 100
        # Welcome содержит сообщение о реферале
        assert "реферальной" in msg.replies[0]["text"].lower()
        assert "15" in msg.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_self_referral_silently_ignored(self):
        # Юзер взял свой собственный код
        profile = main.user_store.get(1)
        msg = FakeMessage(text=f"/start {profile.referral_code}", user_id=1)
        await main.handle_start(client=None, message=msg)
        # Не привязали к самому себе
        assert main.user_store.get(1).referrer_id is None
        # И сообщения «реферал» нет
        assert "реферальной" not in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_invalid_referral_code_silent(self):
        msg = FakeMessage(text="/start ref_unknown", user_id=42)
        await main.handle_start(client=None, message=msg)
        assert main.user_store.get(42).referrer_id is None
        assert "реферальной" not in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_non_ref_argument_ignored(self):
        # /start <что-то-не-ref> — не должно ломать
        msg = FakeMessage(text="/start payment_success", user_id=42)
        await main.handle_start(client=None, message=msg)
        assert main.user_store.get(42).referrer_id is None


class TestHandleReferral:
    @pytest.mark.asyncio
    async def test_shows_code_and_link(self):
        # Эмулируем pyrogram client с .me.username
        import types
        client = types.SimpleNamespace(me=types.SimpleNamespace(username="my_bot"))
        msg = FakeMessage(text="/referral", user_id=1)
        await main.handle_referral(client=client, message=msg)
        text = msg.replies[0]["text"]
        code = main.user_store.get(1).referral_code
        assert code in text
        assert f"https://t.me/my_bot?start={code}" in text

    @pytest.mark.asyncio
    async def test_no_username_fallback(self):
        msg = FakeMessage(text="/referral", user_id=1)
        # client без атрибута me — fallback на /start <код>
        await main.handle_referral(client=None, message=msg)
        text = msg.replies[0]["text"]
        code = main.user_store.get(1).referral_code
        assert code in text

    @pytest.mark.asyncio
    async def test_shows_stats(self):
        # Создаём референта с историей
        referrer = main.user_store.get(1)
        # Привязываем 3 приглашённых, у двух из них фейк-флаг bonus_granted
        main.user_store.set_referrer_by_code(2, referrer.referral_code)
        main.user_store.set_referrer_by_code(3, referrer.referral_code)
        main.user_store.set_referrer_by_code(4, referrer.referral_code)
        main.user_store.award_referral_bonus(2)
        main.user_store.award_referral_bonus(3)

        msg = FakeMessage(text="/referral", user_id=1)
        await main.handle_referral(client=None, message=msg)
        text = msg.replies[0]["text"]
        # 3 приглашено, 2 оплатили, 30 дней суммарно
        assert "Приглашено всего: 3" in text
        assert "Из них оплатили: 2" in text
        assert "30" in text


class TestReferralLinkHelper:
    def test_with_username(self):
        link = main._referral_link("my_bot", "ref_abc12345")
        assert link == "https://t.me/my_bot?start=ref_abc12345"

    def test_without_username_fallback(self):
        link = main._referral_link("", "ref_abc12345")
        assert link == "/start ref_abc12345"


# ──────────────────────────────────────────────────────────────────────
# Поиск по названию: _looks_like_company_query, search results keyboard,
# handle_text_message → suggest, handle_callback search_select:
# ──────────────────────────────────────────────────────────────────────


class TestLooksLikeCompanyQuery:
    @pytest.mark.parametrize("text", [
        "Сбер", "ООО Альфа", "Lukoil", "Зелёный банк",
    ])
    def test_valid_queries(self, text):
        assert main._looks_like_company_query(text) is True

    @pytest.mark.parametrize("text", ["", " ", "12", "ab", "  ab "])
    def test_too_short(self, text):
        assert main._looks_like_company_query(text) is False

    def test_only_digits_or_punct_rejected(self):
        assert main._looks_like_company_query("12345") is False
        assert main._looks_like_company_query("!!!@@@") is False

    def test_command_rejected(self):
        assert main._looks_like_company_query("/help") is False

    def test_mixed_alpha_digit_accepted(self):
        assert main._looks_like_company_query("ABC123") is True


class TestSearchResultsKeyboard:
    def test_button_per_company(self):
        c1 = CompanyData(inn="111", name="A")
        c2 = CompanyData(inn="222", name="B")
        kb = main._search_results_keyboard([c1, c2])
        assert len(kb.inline_keyboard) == 2

    def test_skips_companies_without_inn(self):
        c1 = CompanyData(inn="111", name="A")
        c2 = CompanyData(inn=None, name="B")
        kb = main._search_results_keyboard([c1, c2])
        assert len(kb.inline_keyboard) == 1

    def test_callback_data_format(self):
        c = CompanyData(inn="7707083893", name="X")
        kb = main._search_results_keyboard([c])
        btn = kb.inline_keyboard[0][0]
        assert btn.callback_data == "search_select:7707083893"

    def test_label_includes_inn(self):
        c = CompanyData(inn="7707083893", name="ООО Тест")
        kb = main._search_results_keyboard([c])
        assert "7707083893" in kb.inline_keyboard[0][0].text

    def test_long_name_truncated_to_64_chars(self):
        c = CompanyData(inn="111", name="A" * 200)
        kb = main._search_results_keyboard([c])
        # Telegram-лимит ~64 символа
        assert len(kb.inline_keyboard[0][0].text) <= 64

    def test_empty_list(self):
        kb = main._search_results_keyboard([])
        assert kb.inline_keyboard == []


class TestSearchTrigger:
    @pytest.mark.asyncio
    async def test_search_triggered_for_alpha_text(self, monkeypatch):
        results = [
            CompanyData(inn="111", name="ПАО Сбербанк"),
            CompanyData(inn="222", name="ООО Сбер Авто"),
        ]
        captured = {}

        async def fake_suggest(query, count=5):
            captured["query"] = query
            captured["count"] = count
            return results

        monkeypatch.setattr(main.company_service, "suggest", fake_suggest)
        msg = FakeMessage(text="Сбер", user_id=1)
        await main.handle_text_message(client=None, message=msg)

        assert captured["query"] == "Сбер"
        assert captured["count"] == 5
        # Ответ — заголовок и клавиатура
        assert "найдено" in msg.replies[0]["text"].lower()
        kb = msg.replies[0]["reply_markup"]
        assert len(kb.inline_keyboard) == 2

    @pytest.mark.asyncio
    async def test_search_with_no_results_shows_friendly_message(self, monkeypatch):
        async def empty_suggest(query, count=5):
            return []

        monkeypatch.setattr(main.company_service, "suggest", empty_suggest)
        msg = FakeMessage(text="ОченьРедкоеНазваниеXYZ", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        assert "ничего не найдено" in msg.replies[0]["text"].lower()

    @pytest.mark.asyncio
    async def test_search_skipped_for_inn_input(self, monkeypatch):
        # ИНН должен идти в обычный INN-флоу, не в поиск
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        async def must_not_be_called(query, count=5):
            raise AssertionError("suggest must not be called for INN input")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.company_service, "suggest", must_not_be_called)
        msg = FakeMessage(text="7707083893", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Просто проверим что не было исключения и ответ есть
        assert msg.replies

    @pytest.mark.asyncio
    async def test_search_skipped_when_pending_action(self, monkeypatch):
        # При активном pending state поиск не должен запускаться
        async def must_not_be_called(query, count=5):
            raise AssertionError("suggest must not be called with pending state")

        monkeypatch.setattr(main.company_service, "suggest", must_not_be_called)
        main._user_state[1] = "mode_internal_analysis"
        msg = FakeMessage(text="название без инн", user_id=1)
        await main.handle_text_message(client=None, message=msg)
        # Должно быть сообщение про сброс состояния
        assert "сброшено" in msg.replies[0]["text"].lower()


class TestSearchSelectCallback:
    @pytest.mark.asyncio
    async def test_select_runs_full_analysis(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="ПАО Сбербанк", status="Действующая")

        async def fake_security(**kw):
            return SecurityResult(risk_level="low")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", fake_security)

        cb = FakeCallbackQuery("search_select:7707083893", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        # Ответ — полный анализ + кнопки действий
        assert any("Стоп-листы" in r["text"] for r in cb.message.replies)
        # Лимит инкрементнулся (free=3, после 1 проверки → checks_today=1)
        assert main.user_store.get(1).checks_today == 1

    @pytest.mark.asyncio
    async def test_select_blocked_when_limit_exhausted(self, monkeypatch):
        # Исчерпываем лимит
        for _ in range(3):
            await main._check_limit_and_count(FakeMessage(user_id=1), 1)

        async def fake_fetch(inn):
            raise AssertionError("must not fetch when limit exhausted")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        cb = FakeCallbackQuery("search_select:7707083893", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        assert "Лимит" in cb.message.replies[0]["text"]

    @pytest.mark.asyncio
    async def test_select_with_empty_inn_does_nothing(self, monkeypatch):
        async def fake_fetch(inn):
            raise AssertionError("must not fetch for empty INN")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        cb = FakeCallbackQuery("search_select:", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        # Колбэк ответил, но без сообщений
        assert cb.answered
        assert cb.message.replies == []

    @pytest.mark.asyncio
    async def test_select_security_failure_does_not_crash(self, monkeypatch):
        async def fake_fetch(inn):
            return CompanyData(inn=inn, name="X")

        async def boom_security(**kw):
            raise RuntimeError("FSSP down")

        monkeypatch.setattr(main.company_service, "fetch", fake_fetch)
        monkeypatch.setattr(main.security_service, "check", boom_security)
        cb = FakeCallbackQuery("search_select:7707083893", user_id=1)
        await main.handle_callback(client=None, callback_query=cb)
        # Ответ всё равно пришёл
        assert cb.message.replies
