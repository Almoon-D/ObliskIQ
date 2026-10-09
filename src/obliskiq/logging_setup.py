"""Structured logging.

The bot runs unattended, so the log is the only account of what happened. JSON by default,
one object per line, on stderr; ``text`` for interactive use. Timestamps are UTC in both
formats. Ported from ObliskIQ-lite-MCP.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from .errors import ConfigError

type LogFormat = Literal["json", "text"]

#: LogRecord attributes that are not caller-supplied context.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}

#: Second-resolution UTC stamp for the text format.
TEXT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any ``extra=`` fields merged in at the top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _coerce(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _coerce(value: Any) -> Any:
    """JSON-native values pass through; anything else is logged as its ``str``.

    ``str`` of a pydantic ``SecretStr`` is its mask, so a secret passed as context is
    logged masked.
    """
    if isinstance(value, str | int | float | bool | type(None) | list | dict):
        return value
    return str(value)


def _text_formatter() -> logging.Formatter:
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt=TEXT_TIME_FORMAT
    )
    formatter.converter = time.gmtime
    return formatter


def configure_logging(
    level: str,
    fmt: LogFormat,
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Install the root handler. Idempotent — safe to call from every entrypoint."""
    if fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    elif fmt == "text":
        formatter = _text_formatter()
    else:
        raise ConfigError(f"unknown log format {fmt!r}; expected 'json' or 'text'")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx logs every request at INFO, which drowns a collector run.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger("obliskiq")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("obliskiq") else f"obliskiq.{name}")
