"""Structured logging. Ported from ObliskIQ-lite-MCP's tests/test_util.py.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import logging
import re

import pytest
from pydantic import SecretStr

from obliskiq.errors import ConfigError
from obliskiq.logging_setup import configure_logging, get_logger


def test_logs_are_one_json_object_per_line():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    get_logger("test").info("collected", extra={"rows": 42, "target": "CL"})

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "collected"
    assert payload["rows"] == 42
    assert payload["target"] == "CL"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "obliskiq.test"
    assert payload["ts"].endswith("+00:00")


def test_a_non_json_context_value_is_logged_as_text():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    get_logger("test").info("context", extra={"when": object.__new__(object)})
    assert json.loads(stream.getvalue())["when"].startswith("<object object")


def test_a_secret_passed_as_context_is_logged_masked():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    get_logger("test").info("credential", extra={"token": SecretStr("s3cr3t-value")})
    assert "s3cr3t-value" not in stream.getvalue()
    assert json.loads(stream.getvalue())["token"] == "**********"


def test_exceptions_are_captured_in_the_record():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    try:
        raise ValueError("collector failed")
    except ValueError:
        get_logger("test").exception("run aborted")
    payload = json.loads(stream.getvalue().strip())
    assert "ValueError: collector failed" in payload["exception"]


def test_a_stack_is_captured_when_asked_for():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    get_logger("test").info("where", stack_info=True)
    assert "stack" in json.loads(stream.getvalue())


def test_configure_logging_is_idempotent():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    configure_logging("INFO", "json", stream=stream)
    get_logger("test").info("once")
    assert len(stream.getvalue().strip().splitlines()) == 1


def test_the_level_is_applied():
    stream = io.StringIO()
    configure_logging("warning", "json", stream=stream)
    get_logger("test").info("hidden")
    get_logger("test").warning("shown")
    assert [json.loads(line)["message"] for line in stream.getvalue().splitlines()] == ["shown"]


def test_httpx_chatter_is_suppressed():
    """httpx logs every request at INFO, which drowns a collector run."""
    configure_logging("INFO", "json", stream=io.StringIO())
    assert logging.getLogger("httpx").level == logging.WARNING


def test_text_format_is_available_for_interactive_use_and_stamped_in_utc():
    stream = io.StringIO()
    configure_logging("INFO", "text", stream=stream)
    get_logger("obliskiq.cli").info("hello")
    line = stream.getvalue().strip()
    assert not line.startswith("{")
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[INFO\] obliskiq\.cli: hello$", line)


def test_an_unknown_format_fails_loudly():
    with pytest.raises(ConfigError, match="unknown log format"):
        configure_logging("INFO", "xml", stream=io.StringIO())  # type: ignore[arg-type]
