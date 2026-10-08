"""Ported utilities: numeric coercion, HTTP retry, structured logging.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import UTC

import httpx
import pytest

from obliskiq.errors import UpstreamError
from obliskiq.logging_setup import configure_logging, get_logger
from obliskiq.util.http import RetryPolicy, _retry_after_seconds, request_with_retry
from obliskiq.util.numeric import safe_float, safe_int

# ── safe_float ──


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 0.0),
        ("", 0.0),
        (".", 0.0),  # FRED's missing-observation placeholder
        ("12.5", 12.5),
        (" 12.5 ", 12.5),
        ("1,234.56", 1234.56),  # thousands separators
        ("100", 100.0),  # IBKR returns strikes as strings
        (7, 7.0),
        (7.5, 7.5),
        (True, 1.0),
        ("C228.50", 228.50),  # IBKR prefixes a previous close with C
        ("garbage", 0.0),
    ],
)
def test_safe_float_handles_the_shapes_the_apis_actually_return(raw, expected):
    assert safe_float(raw) == expected


def test_safe_float_can_distinguish_absent_from_zero():
    """The original always collapsed both to 0.0, which silently turns a missing
    bid into a real one."""
    assert safe_float(None, None) is None
    assert safe_float("", None) is None
    assert safe_float("garbage", None) is None
    assert safe_float("0", None) == 0.0
    assert safe_float(0, None) == 0.0


def test_safe_int_goes_through_float_so_decimal_strings_parse():
    assert safe_int("1,234.0") == 1234
    assert safe_int("12.9") == 12
    assert safe_int(None, None) is None
    assert safe_int("nope") == 0


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "Infinity", "-inf", "1e999", "-1e999"])
def test_safe_float_treats_a_non_finite_parse_as_absent(raw):
    """JSON's own grammar admits these literals, and Python's float() parses all
    of them without complaint — so an upstream serving one reaches this function
    looking like an ordinary string. Every caller already reads `default` as
    "unusable"; a non-finite parse is exactly that, not a real quote."""
    assert safe_float(raw) == 0.0
    assert safe_float(raw, None) is None


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_safe_float_treats_a_non_finite_python_float_as_absent(raw):
    """The int/float fast path bypassed string parsing entirely, so a value
    that arrived already decoded — the common case for a JSON response body —
    was never checked at all."""
    assert safe_float(raw, None) is None


def test_safe_float_still_parses_ordinary_scientific_notation():
    """The guard must not confuse a large-but-real magnitude with an overflow."""
    assert safe_float("1e6") == 1_000_000.0
    assert safe_float(1e6) == 1_000_000.0


def test_safe_int_rejects_non_finite_strings():
    """Already correct before this session's changes — int() raises on inf/nan
    literals — and pinned here so a future change to safe_float's plumbing
    cannot regress it silently."""
    assert safe_int("nan", None) is None
    assert safe_int("inf", None) is None
    assert safe_int("1e999", None) is None


# ── retry policy ──


def test_policy_is_built_from_config(config):
    policy = RetryPolicy.from_config(config)
    assert policy.retries == config.require_int("http.retries")
    assert 429 in policy.retry_status_codes


def test_backoff_is_capped(config):
    """The original doubled without a ceiling, so raising the retry count grew the
    wait geometrically."""
    policy = RetryPolicy.from_config(config)
    for attempt in range(20):
        assert policy.delay_for(attempt) <= policy.max_delay_seconds + policy.jitter_seconds


def test_backoff_grows_before_it_caps(config):
    policy = RetryPolicy.from_config(config)
    assert policy.delay_for(0) < policy.max_delay_seconds


@pytest.fixture()
def fast_policy(config):
    """Same policy, no real sleeping."""
    import dataclasses

    return dataclasses.replace(
        RetryPolicy.from_config(config),
        base_delay_seconds=0.0,
        max_delay_seconds=0.0,
        jitter_seconds=0.0,
    )


async def test_a_retryable_status_is_retried_then_returned(fast_policy):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "https://x.test/y", policy=fast_policy)
    assert response.status_code == 503
    assert calls["n"] == fast_policy.retries + 1


async def test_a_success_after_a_transient_failure_is_returned(fast_policy):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "https://x.test/y", policy=fast_policy)
    assert response.status_code == 200


async def test_a_non_retryable_status_returns_immediately(fast_policy):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "https://x.test/y", policy=fast_policy)
    assert response.status_code == 404
    assert calls["n"] == 1, "a 404 is an answer, not a transient failure"


async def test_a_dead_host_raises_rather_than_returning_none(fast_policy):
    """The original returned None on a transport failure and a Response on an HTTP
    error, so every caller had two failure shapes to remember and most checked one."""

    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="transport layer"):
            await request_with_retry(client, "GET", "https://x.test/y", policy=fast_policy)


async def test_retry_after_is_honoured(config, monkeypatch):
    """Assert the WAIT, not the call count.

    The previous version of this test set ``Retry-After: 0`` and also zeroed
    max_delay_seconds, then asserted only that two calls happened — which held
    just as well with the header handling deleted. Recording the sleeps is what
    distinguishes "waited because the server said so" from "waited zero because
    the policy said so".
    """
    import dataclasses

    policy = dataclasses.replace(
        RetryPolicy.from_config(config),
        base_delay_seconds=0.0,
        jitter_seconds=0.0,
        max_delay_seconds=0.0,
        retries=1,
    )
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "7"}, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await request_with_retry(client, "GET", "https://x.test/y", policy=policy)

    # Our own backoff is zeroed, so a 7 can only have come from the header.
    assert slept == [7.0]


async def test_a_dated_retry_after_is_read_as_well_as_a_delta(config, monkeypatch):
    """RFC 9110 allows an HTTP-date, and upstreams send one.

    It used to raise ValueError inside the parser and fall through to our own
    backoff, so a 429 was retried well before the server said to.
    """
    import dataclasses
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    policy = dataclasses.replace(
        RetryPolicy.from_config(config),
        base_delay_seconds=0.0,
        jitter_seconds=0.0,
        max_delay_seconds=0.0,
        retries=1,
    )
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    when = datetime.now(UTC) + timedelta(seconds=30)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": format_datetime(when)}, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await request_with_retry(client, "GET", "https://x.test/y", policy=policy)

    assert len(slept) == 1
    assert slept[0] == pytest.approx(30.0, abs=2.0)


async def test_a_servers_wait_is_capped_by_its_own_ceiling_not_by_our_backoff(config):
    """A server asking for ten minutes is bounded, but not down to our backoff.

    max_delay_seconds bounds the backoff WE choose. Using it here turned an
    explicit sixty-second instruction into eight seconds, which is how a rate
    limit becomes a longer one.
    """
    policy = RetryPolicy.from_config(config)
    response = httpx.Response(429, headers={"Retry-After": "600"})
    seconds = _retry_after_seconds(response, policy)

    assert seconds == pytest.approx(policy.retry_after_max_seconds)
    assert seconds > policy.max_delay_seconds


async def test_an_unparseable_retry_after_falls_back_to_our_own_backoff(config):
    policy = RetryPolicy.from_config(config)
    response = httpx.Response(429, headers={"Retry-After": "soon-ish"})
    assert _retry_after_seconds(response, policy) is None


async def test_a_retry_after_already_in_the_past_waits_no_time(config):
    policy = RetryPolicy.from_config(config)
    response = httpx.Response(429, headers={"Retry-After": "-5"})
    assert _retry_after_seconds(response, policy) == 0.0


async def test_retry_works_for_any_verb(fast_policy):
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await request_with_retry(client, "POST", "https://x.test/y", policy=fast_policy)
    assert seen == ["POST"]


# ── structured logging ──


def test_logs_are_one_json_object_per_line():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    get_logger("test").info("collected", extra={"rows": 42, "target": "CL"})

    line = stream.getvalue().strip()
    payload = json.loads(line)
    assert payload["message"] == "collected"
    assert payload["rows"] == 42 and payload["target"] == "CL"
    assert payload["level"] == "INFO"
    assert payload["ts"].endswith("+00:00")


def test_exceptions_are_captured_in_the_record():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    try:
        raise ValueError("collector failed")
    except ValueError:
        get_logger("test").exception("run aborted")
    payload = json.loads(stream.getvalue().strip())
    assert "ValueError: collector failed" in payload["exception"]


def test_configure_logging_is_idempotent():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    configure_logging("INFO", "json", stream=stream)
    get_logger("test").info("once")
    assert len(stream.getvalue().strip().splitlines()) == 1


def test_httpx_chatter_is_suppressed():
    """httpx logs every request at INFO, which drowns a collector run."""
    configure_logging("INFO", "json", stream=io.StringIO())
    assert logging.getLogger("httpx").level == logging.WARNING


def test_text_format_is_available_for_interactive_use():
    stream = io.StringIO()
    configure_logging("INFO", "text", stream=stream)
    get_logger("test").info("hello")
    assert "hello" in stream.getvalue()
    assert not stream.getvalue().strip().startswith("{")
