"""Тесты GigaChatClient — авторизация, чат, формирование промпта.

HTTP мокаются через monkeypatch на gigachat_client.requests.post —
никаких реальных запросов к API Сбера.
"""
from typing import Any, Dict, Optional

import pytest

import gigachat_client
from gigachat_client import GigaChatClient


class FakeResponse:
    def __init__(
        self, status_code: int = 200,
        payload: Optional[Dict[str, Any]] = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> Dict[str, Any]:
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}: {self.text}")


class TestGetToken:
    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
        client = GigaChatClient()
        assert client._get_token() is None

    def test_success_returns_access_token(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "Y3JlZHM=")
        client = GigaChatClient()
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return FakeResponse(200, {"access_token": "tok-123"})

        monkeypatch.setattr(gigachat_client.requests, "post", fake_post)
        assert client._get_token() == "tok-123"
        assert "ngw.devices.sberbank" in captured["url"]
        assert captured["headers"]["Authorization"] == "Basic Y3JlZHM="

    def test_http_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()
        monkeypatch.setattr(
            gigachat_client.requests, "post",
            lambda *a, **kw: FakeResponse(401, {}, text="unauthorized"),
        )
        assert client._get_token() is None

    def test_network_exception_returns_none(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()

        def boom(*a, **kw):
            raise ConnectionError("dns failure")

        monkeypatch.setattr(gigachat_client.requests, "post", boom)
        assert client._get_token() is None


class TestChat:
    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
        client = GigaChatClient()
        assert client._chat("test prompt") is None

    def test_success_returns_content(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()

        responses = iter([
            FakeResponse(200, {"access_token": "tok"}),  # auth
            FakeResponse(200, {
                "choices": [{"message": {"content": "Анализ компании"}}],
            }),
        ])

        def fake_post(*a, **kw):
            return next(responses)

        monkeypatch.setattr(gigachat_client.requests, "post", fake_post)
        result = client._chat("prompt")
        assert result == "Анализ компании"

    def test_chat_http_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()

        responses = iter([
            FakeResponse(200, {"access_token": "tok"}),  # auth ok
            FakeResponse(500, {}, text="server error"),   # chat fails
        ])
        monkeypatch.setattr(
            gigachat_client.requests, "post",
            lambda *a, **kw: next(responses),
        )
        assert client._chat("prompt") is None


class TestAnalyzeCompany:
    @pytest.mark.asyncio
    async def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
        client = GigaChatClient()
        result = await client.analyze_company(name="X", inn="123")
        assert result is None

    @pytest.mark.asyncio
    async def test_success_passes_company_data_in_prompt(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()

        captured_prompts = []

        def fake_chat(prompt: str):
            captured_prompts.append(prompt)
            return "ИИ-анализ готов"

        monkeypatch.setattr(client, "_chat", fake_chat)

        result = await client.analyze_company(
            name="ПАО Сбербанк", inn="7707083893",
            okved="64.19", okved_name="Денежное посредничество",
            age_years=30, revenue=100_000_000_000.0, profit=10_000_000_000.0,
            employees=250000, region="Москва", status="Действующая",
        )
        assert result == "ИИ-анализ готов"
        prompt = captured_prompts[0]
        assert "Сбербанк" in prompt
        assert "7707083893" in prompt
        assert "Действующая" in prompt
        assert "64.19" in prompt
        # Денежные суммы форматируются
        assert "100.0 млрд" in prompt
        assert "10.0 млрд" in prompt
        # Шаблон вывода
        assert "ИТОГОВАЯ ОЦЕНКА" in prompt
        assert "ФИНАНСОВОЕ СОСТОЯНИЕ" in prompt
        assert "ВЫЯВЛЕННЫЕ РИСКИ" in prompt

    @pytest.mark.asyncio
    async def test_prompt_handles_missing_fields(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()

        captured = []

        def fake_chat(prompt: str):
            captured.append(prompt)
            return "OK"

        monkeypatch.setattr(client, "_chat", fake_chat)
        await client.analyze_company(name="X", inn="1")
        prompt = captured[0]
        # Пустые поля → «нет данных», не None
        assert "нет данных" in prompt
        assert "None" not in prompt

    @pytest.mark.asyncio
    async def test_margin_calculated_when_revenue_and_profit_present(
        self, monkeypatch
    ):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()

        captured = []

        def fake_chat(prompt: str):
            captured.append(prompt)
            return "OK"

        monkeypatch.setattr(client, "_chat", fake_chat)
        await client.analyze_company(
            name="X", inn="1", revenue=10_000_000, profit=2_000_000,
        )
        # 2M / 10M = 20%
        assert "20.0%" in captured[0]

    @pytest.mark.asyncio
    async def test_chat_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "x")
        client = GigaChatClient()
        monkeypatch.setattr(client, "_chat", lambda p: None)
        result = await client.analyze_company(name="X", inn="1")
        assert result is None
