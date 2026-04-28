"""Тесты telemetry — Sentry-инициализация и PII-скраббер.

Главная цель — гарантировать, что чувствительные поля (токены, ИНН,
email, имя, телефон) не уходят в Sentry в открытом виде. Это обязательное
требование 152-ФЗ.
"""
from typing import Any, Dict

import pytest

import telemetry
from telemetry import (
    SCRUB_FIELDS,
    _before_breadcrumb,
    _scrub_event,
    init_sentry,
)


class TestScrubFieldsSet:
    """Проверяем сам список полей — добавление/удаление должно быть
    осознанным изменением."""

    def test_contains_credentials(self):
        assert "password" in SCRUB_FIELDS
        assert "token" in SCRUB_FIELDS
        assert "api_key" in SCRUB_FIELDS
        assert "authorization" in SCRUB_FIELDS

    def test_contains_pii(self):
        # ИНН/ОГРН — важный PII в нашем боте: знание ИНН + ошибка =
        # утечка предмета проверки клиента
        assert "inn" in SCRUB_FIELDS
        assert "ogrn" in SCRUB_FIELDS
        assert "name" in SCRUB_FIELDS
        assert "email" in SCRUB_FIELDS
        assert "phone" in SCRUB_FIELDS
        assert "region" in SCRUB_FIELDS

    def test_contains_session_data(self):
        assert "cookie" in SCRUB_FIELDS
        assert "set-cookie" in SCRUB_FIELDS

    def test_all_fields_lowercase(self):
        # Сравнение в коде идёт через .lower(), значит SCRUB_FIELDS
        # должен быть в lower-case
        for field in SCRUB_FIELDS:
            assert field == field.lower()


class TestScrubEvent:
    def test_request_headers_redacted(self):
        event = {"request": {"headers": {
            "Authorization": "Bearer secret-jwt",
            "X-Api-Key": "no-match",  # ключ-не-секрет, в SCRUB_FIELDS только api_key
            "Cookie": "sid=abc",
            "Content-Type": "application/json",
        }}}
        result = _scrub_event(event)
        headers = result["request"]["headers"]
        assert headers["Authorization"] == "[REDACTED]"
        assert headers["Cookie"] == "[REDACTED]"
        # Не из списка — не трогаем
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Api-Key"] == "no-match"  # не точное совпадение с api_key

    def test_request_cookies_redacted(self):
        event = {"request": {"cookies": {
            "Cookie": "sid=secret",
            "session_id": "keep",
        }}}
        result = _scrub_event(event)
        cookies = result["request"]["cookies"]
        assert cookies["Cookie"] == "[REDACTED]"
        assert cookies["session_id"] == "keep"

    def test_request_data_redacted_when_dict(self):
        event = {"request": {"data": {
            "inn": "7707083893",
            "name": "ООО Ромашка",
            "amount": 1290,
        }}}
        result = _scrub_event(event)
        data = result["request"]["data"]
        assert data["inn"] == "[REDACTED]"
        assert data["name"] == "[REDACTED]"
        assert data["amount"] == 1290

    def test_request_data_string_left_untouched(self):
        # Если data это строка/массив, scrub_mapping не срабатывает,
        # но и не падает
        event = {"request": {"data": "raw=body"}}
        result = _scrub_event(event)
        assert result["request"]["data"] == "raw=body"

    def test_user_redacted(self):
        event = {"user": {
            "email": "client@example.com",
            "phone": "+79001234567",
            "id": "tg-42",  # id остаётся для дебага
        }}
        result = _scrub_event(event)
        user = result["user"]
        assert user["email"] == "[REDACTED]"
        assert user["phone"] == "[REDACTED]"
        assert user["id"] == "tg-42"

    def test_extra_redacted(self):
        event = {"extra": {
            "token": "secret",
            "ogrn": "1027700132195",
            "operation": "fetch_company",
        }}
        result = _scrub_event(event)
        extra = result["extra"]
        assert extra["token"] == "[REDACTED]"
        assert extra["ogrn"] == "[REDACTED]"
        assert extra["operation"] == "fetch_company"

    def test_case_insensitive_key_matching(self):
        event = {"extra": {
            "TOKEN": "x",
            "Inn": "y",
            "EMAIL": "z",
        }}
        result = _scrub_event(event)
        # Все три должны быть отскраблены, несмотря на регистр
        assert result["extra"]["TOKEN"] == "[REDACTED]"
        assert result["extra"]["Inn"] == "[REDACTED]"
        assert result["extra"]["EMAIL"] == "[REDACTED]"

    def test_empty_event(self):
        result = _scrub_event({})
        assert result == {}

    def test_event_without_request_or_user(self):
        # Просто message-event — должен пройти без модификаций
        event = {"message": "something happened", "level": "error"}
        result = _scrub_event(event)
        assert result["message"] == "something happened"

    def test_request_not_dict_does_not_crash(self):
        event = {"request": "string-value"}
        result = _scrub_event(event)
        assert result["request"] == "string-value"

    def test_user_not_dict_does_not_crash(self):
        event = {"user": "anonymous"}
        result = _scrub_event(event)
        assert result["user"] == "anonymous"

    def test_returns_same_event_object(self):
        # _scrub_event мутирует in-place и возвращает тот же объект
        event = {"extra": {"token": "x"}}
        result = _scrub_event(event)
        assert result is event

    def test_hints_argument_accepted_and_ignored(self):
        # Sentry может передать hints — функция принимает и игнорирует
        event = {"extra": {"token": "x"}}
        result = _scrub_event(event, hints={"original_data": "..."})
        assert result["extra"]["token"] == "[REDACTED]"


class TestBeforeBreadcrumb:
    def test_data_redacted(self):
        crumb = {"data": {"inn": "123", "msg": "ok"}}
        result = _before_breadcrumb(crumb, hint=None)
        assert result["data"]["inn"] == "[REDACTED]"
        assert result["data"]["msg"] == "ok"

    def test_no_data_does_not_crash(self):
        crumb = {"category": "log", "message": "x"}
        result = _before_breadcrumb(crumb, hint=None)
        assert result == crumb

    def test_non_dict_data_does_not_crash(self):
        crumb = {"data": "raw-value"}
        result = _before_breadcrumb(crumb, hint=None)
        assert result["data"] == "raw-value"

    def test_case_insensitive(self):
        crumb = {"data": {"TOKEN": "x"}}
        result = _before_breadcrumb(crumb, hint=None)
        assert result["data"]["TOKEN"] == "[REDACTED]"

    def test_returns_same_crumb_object(self):
        crumb = {"data": {"token": "x"}}
        result = _before_breadcrumb(crumb, hint=None)
        assert result is crumb


class TestInitSentry:
    def test_no_dsn_returns_none(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        assert init_sentry() is None

    def test_with_dsn_returns_dsn(self, monkeypatch):
        captured = {}

        def fake_init(**kwargs):
            captured.update(kwargs)

        monkeypatch.setenv("SENTRY_DSN", "https://abc@sentry.io/123")
        monkeypatch.setattr(telemetry.sentry_sdk, "init", fake_init)
        result = init_sentry()
        assert result == "https://abc@sentry.io/123"
        assert captured["dsn"] == "https://abc@sentry.io/123"

    def test_env_vars_propagated_to_sdk(self, monkeypatch):
        captured = {}

        def fake_init(**kwargs):
            captured.update(kwargs)

        monkeypatch.setenv("SENTRY_DSN", "https://x@s.io/1")
        monkeypatch.setenv("SENTRY_ENV", "production")
        monkeypatch.setenv("SENTRY_RELEASE", "v1.2.3")
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.25")
        monkeypatch.setenv("SENTRY_SEND_DEFAULT_PII", "true")
        monkeypatch.setenv("SENTRY_MAX_BREADCRUMBS", "50")
        monkeypatch.setenv("SENTRY_ATTACH_STACKTRACE", "true")
        monkeypatch.setattr(telemetry.sentry_sdk, "init", fake_init)

        init_sentry()
        assert captured["environment"] == "production"
        assert captured["release"] == "v1.2.3"
        assert captured["traces_sample_rate"] == 0.25
        assert captured["send_default_pii"] is True
        assert captured["max_breadcrumbs"] == 50
        assert captured["attach_stacktrace"] is True

    def test_defaults_applied(self, monkeypatch):
        captured = {}
        monkeypatch.setenv("SENTRY_DSN", "https://x@s.io/1")
        for var in ("SENTRY_ENV", "SENTRY_RELEASE", "SENTRY_TRACES_SAMPLE_RATE",
                    "SENTRY_SEND_DEFAULT_PII", "SENTRY_MAX_BREADCRUMBS",
                    "SENTRY_ATTACH_STACKTRACE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(telemetry.sentry_sdk, "init",
                            lambda **kw: captured.update(kw))
        init_sentry()
        assert captured["environment"] == "dev"
        assert captured["release"] is None
        assert captured["traces_sample_rate"] == 0.0
        assert captured["send_default_pii"] is False
        assert captured["max_breadcrumbs"] == 100
        assert captured["attach_stacktrace"] is False

    def test_scrubber_passed_as_before_send(self, monkeypatch):
        captured = {}
        monkeypatch.setenv("SENTRY_DSN", "https://x@s.io/1")
        monkeypatch.setattr(telemetry.sentry_sdk, "init",
                            lambda **kw: captured.update(kw))
        init_sentry()
        # before_send и before_breadcrumb должны быть нашими скрабберами
        assert captured["before_send"] is _scrub_event
        assert captured["before_breadcrumb"] is _before_breadcrumb

    def test_pii_off_lowercase_variants(self, monkeypatch):
        captured = {}
        monkeypatch.setenv("SENTRY_DSN", "https://x@s.io/1")
        monkeypatch.setenv("SENTRY_SEND_DEFAULT_PII", "False")
        monkeypatch.setattr(telemetry.sentry_sdk, "init",
                            lambda **kw: captured.update(kw))
        init_sentry()
        assert captured["send_default_pii"] is False
