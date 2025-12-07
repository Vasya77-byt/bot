import json
import logging
import os
from typing import Any, Dict


LOG_FORMAT_JSON = "json"
LOG_FORMAT_PLAIN = "plain"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info
        # Include extra fields if present
        for key, value in record.__dict__.items():
            if key not in payload and key not in {"name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs", "relativeCreated", "thread", "threadName", "processName", "process"}:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv("LOG_FORMAT", LOG_FORMAT_JSON).lower()

    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler()
    if fmt == LOG_FORMAT_PLAIN:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    else:
        handler.setFormatter(JsonFormatter())

    root.addHandler(handler)
    root.setLevel(level)

