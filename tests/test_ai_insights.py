"""Тесты AI-инсайтов «3 риска которые вы пропустили»."""
from __future__ import annotations

import pytest

from ai_insights import _parse_insights, generate_insights
from reputation_score import calculate_reputation
from schemas import CompanyData
from security_check import SecurityResult


class FakeGigaChat:
    """Подменяет GigaChatClient для тестов.

    Имеет .credentials (управление через credentials="" → не настроен)
    и метод _chat(prompt) — синхронный, как у настоящего клиента.
    """

    def __init__(self, *, credentials: str = "x", response=None, exc=None):
        self.credentials = credentials
        self.response = response
        self.exc = exc
        self.prompts = []

    def _chat(self, prompt: str):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return self.response


def _company(**kw) -> CompanyData:
    defaults = dict(
        inn="7707083893",
        name="ООО Тест",
        status="Действующая",
        age_years=10,
        capital=1_000_000.0,
        profit_last_year=5_000_000.0,
        revenue_last_year=50_000_000.0,
    )
    defaults.update(kw)
    return CompanyData(**defaults)


# ──────────────────────────────────────────────────────────────────────
# Парсер
# ──────────────────────────────────────────────────────────────────────


class TestParser:
    def test_parses_numbered_list_with_emojis(self):
        raw = (
            "1. 🚨 Существенный убыток >20% выручки.\n"
            "2. ⚠️ 50 исполнительных производств на 100 млн ₽.\n"
            "3. 💡 Возраст компании менее года."
        )
        result = _parse_insights(raw)
        assert len(result) == 3
        assert result[0].startswith("🚨")
        assert "убыток" in result[0].lower()
        assert result[1].startswith("⚠️")
        assert result[2].startswith("💡")

    def test_parses_without_numbering(self):
        raw = "🚨 риск 1\n⚠️ риск 2\n💡 риск 3"
        result = _parse_insights(raw)
        assert len(result) == 3

    def test_caps_at_3_items(self):
        """Модель может вернуть больше — берём первые 3."""
        raw = "\n".join(f"{i}. ⚠️ риск {i}" for i in range(1, 7))
        result = _parse_insights(raw)
        assert len(result) == 3

    def test_skips_lines_without_emoji(self):
        """Бойлерплейт без эмодзи модели — игнорируем."""
        raw = (
            "Вот мой ответ:\n"
            "\n"
            "1. 🚨 настоящий риск\n"
            "Спасибо за вопрос!"
        )
        result = _parse_insights(raw)
        assert result == ["🚨 настоящий риск"]

    def test_empty_input_returns_empty(self):
        assert _parse_insights("") == []
        assert _parse_insights(None) == []  # type: ignore[arg-type]

    def test_truncates_very_long_text(self):
        raw = "1. ⚠️ " + ("очень длинный текст " * 30)
        result = _parse_insights(raw)
        assert len(result[0]) <= 200


# ──────────────────────────────────────────────────────────────────────
# generate_insights
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_none_when_company_is_none():
    gigachat = FakeGigaChat()
    reputation = calculate_reputation(None, None)
    result = await generate_insights(None, None, reputation, gigachat)
    assert result is None
    assert gigachat.prompts == []


@pytest.mark.asyncio
async def test_returns_none_when_gigachat_has_no_credentials():
    gigachat = FakeGigaChat(credentials="")
    company = _company(status="Признана банкротом")
    reputation = calculate_reputation(company, None)
    result = await generate_insights(company, None, reputation, gigachat)
    assert result is None
    assert gigachat.prompts == []


@pytest.mark.asyncio
async def test_returns_none_when_no_risk_factors():
    """Чистая компания → не звоним в GigaChat впустую."""
    gigachat = FakeGigaChat(response="1. ⚠️ что-то")
    company = _company()  # дефолты = чисто
    reputation = calculate_reputation(company, None)
    assert reputation.score == 100
    result = await generate_insights(company, None, reputation, gigachat)
    assert result is None
    # КЛЮЧЕВОЕ: GigaChat не был вызван
    assert gigachat.prompts == []


@pytest.mark.asyncio
async def test_returns_block_with_real_response():
    gigachat = FakeGigaChat(response=(
        "1. 🚨 Существенный убыток превышает 20% выручки.\n"
        "2. ⚠️ ФССП: 50 производств на 100 млн рублей.\n"
        "3. 💡 Минимальный уставный капитал — типичный признак однодневки."
    ))
    company = _company(
        status="Действующая",
        capital=10_000.0,
        profit_last_year=-3_000_000.0,
        revenue_last_year=10_000_000.0,
    )
    security = SecurityResult(
        enforcement_count=50, enforcement_total_sum=100_000_000.0,
    )
    reputation = calculate_reputation(company, security)
    result = await generate_insights(company, security, reputation, gigachat)

    assert result is not None
    assert "🤖 На что обратить внимание" in result
    assert "🚨" in result
    assert "⚠️" in result
    assert "💡" in result
    assert len(gigachat.prompts) == 1


@pytest.mark.asyncio
async def test_prompt_contains_company_facts():
    """Промпт должен включать ключевые данные о компании и риск-факторы."""
    gigachat = FakeGigaChat(response="1. ⚠️ риск")
    company = _company(
        name="ООО Сомнительная",
        capital=10_000.0,
        profit_last_year=-3_000_000.0,
        revenue_last_year=10_000_000.0,
    )
    security = SecurityResult(enforcement_count=10)
    reputation = calculate_reputation(company, security)
    await generate_insights(company, security, reputation, gigachat)

    prompt = gigachat.prompts[0]
    assert "ООО Сомнительная" in prompt
    assert "7707083893" in prompt
    assert "Минимальный уставный капитал" in prompt  # фактор риска
    # Промпт должен явно требовать использовать только предоставленные факты
    assert "Не выдумывай" in prompt or "не выдумывай" in prompt.lower()


@pytest.mark.asyncio
async def test_returns_none_on_gigachat_exception():
    gigachat = FakeGigaChat(exc=RuntimeError("network down"))
    company = _company(status="Признана банкротом")
    reputation = calculate_reputation(company, None)
    result = await generate_insights(company, None, reputation, gigachat)
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_empty_gigachat_response():
    gigachat = FakeGigaChat(response=None)
    company = _company(status="Признана банкротом")
    reputation = calculate_reputation(company, None)
    result = await generate_insights(company, None, reputation, gigachat)
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_unparseable_response():
    """GigaChat вернул мусор без эмодзи-маркеров → не показываем блок."""
    gigachat = FakeGigaChat(response="Извините, не могу помочь с этим вопросом.")
    company = _company(status="Признана банкротом")
    reputation = calculate_reputation(company, None)
    result = await generate_insights(company, None, reputation, gigachat)
    assert result is None


@pytest.mark.asyncio
async def test_returns_partial_block_with_fewer_than_3_items():
    """Если модель вернула 1-2 пункта — показываем их (не врём про '3 риска')."""
    gigachat = FakeGigaChat(response="1. 🚨 только один риск")
    company = _company(status="Признана банкротом")
    reputation = calculate_reputation(company, None)
    result = await generate_insights(company, None, reputation, gigachat)
    assert result is not None
    assert "🚨" in result
    assert result.count("•") == 1
