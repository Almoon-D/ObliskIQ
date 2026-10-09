"""Structured logging.

Collectors run unattended on a VM with no human present, so the log is the only
account of what happened. JSON by default, one object per line, stderr.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

#: LogRecord attributes that are not caller-supplied context.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any extra= fields merged in at the top level."""

    def __init__(self, *, utc: bool = True) -> None:
        super().__init__()
        self._utc = utc

    def format(self, record: logging.LogRecord) -> str:
        tz = UTC if self._utc else None
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=tz).isoformat(),
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
    if isinstance(value, (str, int, float, bool, type(None), list, dict)):
        return value
    return str(value)


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    *,
    utc: bool = True,
    stream: Any = None,
) -> logging.Logger:
    """Install the root handler. Idempotent — safe to call from every entrypoint."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter(utc=utc))
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
            )
        )
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx logs every request at INFO, which drowns a collector run.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger("obliskiq")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"obliskiq.{name}" if not name.startswith("obliskiq") else name)
