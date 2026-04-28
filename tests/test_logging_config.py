"""Тесты logging_config — JsonFormatter и setup_logging."""
import json
import logging

import pytest

from logging_config import JsonFormatter, setup_logging


def _make_record(
    *,
    level: int = logging.INFO,
    msg: str = "hello",
    name: str = "test",
    args=None,
    extra: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name, level=level, pathname="x.py", lineno=1,
        msg=msg, args=args or (), exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


class TestJsonFormatter:
    def test_basic_record(self):
        record = _make_record(msg="hello world", name="my-logger",
                              level=logging.INFO)
        output = JsonFormatter().format(record)
        payload = json.loads(output)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "my-logger"
        assert payload["message"] == "hello world"

    def test_format_args_substituted(self):
        record = _make_record(msg="user %s checked %s",
                              args=("vasya", "INN-123"))
        payload = json.loads(JsonFormatter().format(record))
        # getMessage() выполняет % substitution
        assert payload["message"] == "user vasya checked INN-123"

    def test_warning_level(self):
        record = _make_record(level=logging.WARNING, msg="warn")
        payload = json.loads(JsonFormatter().format(record))
        assert payload["level"] == "WARNING"

    def test_error_level(self):
        record = _make_record(level=logging.ERROR, msg="err")
        payload = json.loads(JsonFormatter().format(record))
        assert payload["level"] == "ERROR"

    def test_exc_info_serialized(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = _make_record(msg="failed", exc_info=exc_info)
        payload = json.loads(JsonFormatter().format(record))
        assert "exc_info" in payload
        assert "ValueError" in payload["exc_info"]
        assert "boom" in payload["exc_info"]

    def test_extra_fields_included(self):
        record = _make_record(msg="evt", extra={
            "user_id": 42,
            "operation": "fetch_company",
        })
        payload = json.loads(JsonFormatter().format(record))
        assert payload["user_id"] == 42
        assert payload["operation"] == "fetch_company"

    def test_standard_log_record_fields_excluded(self):
        record = _make_record(msg="m")
        payload = json.loads(JsonFormatter().format(record))
        # Поля LogRecord не должны протекать в выход
        assert "pathname" not in payload
        assert "lineno" not in payload
        assert "thread" not in payload
        assert "process" not in payload
        assert "filename" not in payload

    def test_unicode_preserved(self):
        # ensure_ascii=False — кириллица должна быть в JSON как есть
        record = _make_record(msg="Сбербанк проверен")
        output = JsonFormatter().format(record)
        assert "Сбербанк" in output
        # И что это валидный JSON
        json.loads(output)

    def test_stack_info_serialized(self):
        record = _make_record(msg="m")
        record.stack_info = "Stack frame:\n  File foo.py"
        payload = json.loads(JsonFormatter().format(record))
        assert payload["stack_info"] == "Stack frame:\n  File foo.py"

    def test_output_is_single_line(self):
        # JSON-вывод должен быть однострочным (для линий-парсеров типа Loki)
        record = _make_record(msg="multi\nline\nmessage")
        output = JsonFormatter().format(record)
        # Сообщение многострочное, но JSON — один объект на одной строке
        assert output.count("\n") == 0


class TestSetupLogging:
    def test_default_format_is_json(self, monkeypatch):
        for var in ("LOG_LEVEL", "LOG_FORMAT"):
            monkeypatch.delenv(var, raising=False)
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, JsonFormatter)

    def test_plain_format_uses_plain_formatter(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "plain")
        setup_logging()
        root = logging.getLogger()
        formatter = root.handlers[0].formatter
        assert not isinstance(formatter, JsonFormatter)
        assert isinstance(formatter, logging.Formatter)

    def test_log_level_respected(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        setup_logging()
        assert logging.getLogger().level == logging.DEBUG

    def test_log_level_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "warning")
        setup_logging()
        assert logging.getLogger().level == logging.WARNING

    def test_default_log_level_info(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        setup_logging()
        assert logging.getLogger().level == logging.INFO

    def test_existing_handlers_cleared(self, monkeypatch):
        # Добавляем фантомный handler
        root = logging.getLogger()
        phantom = logging.NullHandler()
        root.addHandler(phantom)
        # Теперь setup_logging должен его убрать
        setup_logging()
        assert phantom not in root.handlers
        # И должен быть ровно один handler
        assert len(root.handlers) == 1

    def test_log_format_case_insensitive(self, monkeypatch):
        # Проверяем разные регистры значения LOG_FORMAT
        monkeypatch.setenv("LOG_FORMAT", "PLAIN")
        setup_logging()
        formatter = logging.getLogger().handlers[0].formatter
        assert not isinstance(formatter, JsonFormatter)

    def test_unknown_format_defaults_to_json(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "yaml")
        setup_logging()
        formatter = logging.getLogger().handlers[0].formatter
        # Неизвестный формат — fallback на JSON
        assert isinstance(formatter, JsonFormatter)


@pytest.fixture(autouse=True)
def restore_root_logger():
    """После каждого теста возвращаем root logger в исходное состояние,
    чтобы setup_logging в одном тесте не ломал последующие."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
