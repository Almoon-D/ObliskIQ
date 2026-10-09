"""The HTTP retry loop. Ported from ObliskIQ-lite-MCP's tests/test_util.py, mocked with respx.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from obliskiq.errors import UpstreamError
from obliskiq.util.http import (
    RetryPolicy,
    _retry_after_seconds,
    get_with_retry,
    request_with_retry,
)

URL = "https://x.test/y"


@pytest.fixture
def policy() -> RetryPolicy:
    """The policy ObliskIQ-lite-MCP ran with (its config.yaml ``http`` block)."""
    return RetryPolicy(
        retries=3,
        base_delay_seconds=0.4,
        max_delay_seconds=8.0,
        jitter_seconds=0.4,
        timeout_seconds=15.0,
        retry_status_codes=frozenset({429, 500, 502, 503, 504}),
        retry_after_max_seconds=120.0,
    )


@pytest.fixture
def fast_policy(policy: RetryPolicy) -> RetryPolicy:
    """The same policy with no real sleeping."""
    return dataclasses.replace(
        policy, base_delay_seconds=0.0, max_delay_seconds=0.0, jitter_seconds=0.0
    )


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every sleep instead of waiting."""
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    return recorded


def test_backoff_is_capped(policy):
    """Raising the retry count must not grow the wait geometrically."""
    for attempt in range(20):
        assert policy.delay_for(attempt) <= policy.max_delay_seconds + policy.jitter_seconds


def test_backoff_grows_before_it_caps(policy):
    assert policy.delay_for(0) < policy.max_delay_seconds


async def test_a_retryable_status_is_retried_then_returned(fast_policy, respx_mock):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(503, text="unavailable"))
    async with httpx.AsyncClient() as client:
        response = await request_with_retry(client, "GET", URL, policy=fast_policy)
    assert response.status_code == 503
    assert route.call_count == fast_policy.retries + 1


async def test_a_success_after_a_transient_failure_is_returned(fast_policy, respx_mock):
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(429), httpx.Response(200, json={})]
    )
    async with httpx.AsyncClient() as client:
        response = await request_with_retry(client, "GET", URL, policy=fast_policy)
    assert response.status_code == 200


async def test_a_non_retryable_status_returns_immediately(fast_policy, respx_mock):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        response = await request_with_retry(client, "GET", URL, policy=fast_policy)
    assert response.status_code == 404
    assert route.call_count == 1, "a 404 is an answer, not a transient failure"


async def test_a_dead_host_raises_rather_than_returning_none(fast_policy, respx_mock):
    """One failure shape for callers: a transport failure raises."""
    route = respx_mock.get(URL).mock(side_effect=httpx.ConnectError("no route to host"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamError, match="transport layer"):
            await request_with_retry(client, "GET", URL, policy=fast_policy)
    assert route.call_count == fast_policy.retries + 1


async def test_a_transport_failure_then_success_is_returned(fast_policy, respx_mock):
    respx_mock.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)])
    async with httpx.AsyncClient() as client:
        response = await request_with_retry(client, "GET", URL, policy=fast_policy)
    assert response.status_code == 200


async def test_a_permanent_body_is_not_retried(fast_policy, respx_mock):
    """A retryable status whose body says the answer cannot change gets one call."""
    route = respx_mock.get(URL).mock(
        return_value=httpx.Response(500, text="Chart data unavailable")
    )
    async with httpx.AsyncClient() as client:
        response = await request_with_retry(
            client,
            "GET",
            URL,
            policy=fast_policy,
            is_permanent=lambda r: "unavailable" in r.text,
        )
    assert response.status_code == 500
    assert route.call_count == 1


async def test_a_caller_can_narrow_the_retryable_statuses(fast_policy, respx_mock):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        response = await request_with_retry(
            client, "GET", URL, policy=fast_policy, retry_status_codes=[429]
        )
    assert response.status_code == 503
    assert route.call_count == 1


async def test_retry_after_is_honoured(fast_policy, slept, respx_mock):
    """Assert the wait, not the call count: our own backoff is zero, so a 7 can only have
    come from the header."""
    respx_mock.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))
    async with httpx.AsyncClient() as client:
        await request_with_retry(
            client, "GET", URL, policy=dataclasses.replace(fast_policy, retries=1)
        )
    assert slept == [7.0]


async def test_a_dated_retry_after_is_read_as_well_as_a_delta(fast_policy, slept, respx_mock):
    """RFC 9110 allows an HTTP-date, and upstreams send one."""
    when = datetime.now(UTC) + timedelta(seconds=30)
    respx_mock.get(URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": format_datetime(when)})
    )
    async with httpx.AsyncClient() as client:
        await request_with_retry(
            client, "GET", URL, policy=dataclasses.replace(fast_policy, retries=1)
        )
    assert len(slept) == 1
    assert slept[0] == pytest.approx(30.0, abs=2.0)


def test_a_naive_dated_retry_after_is_read_as_utc(policy):
    when = datetime.now(UTC) + timedelta(seconds=30)
    naive = when.strftime("%a, %d %b %Y %H:%M:%S -0000")
    seconds = _retry_after_seconds(httpx.Response(429, headers={"Retry-After": naive}), policy)
    assert seconds == pytest.approx(30.0, abs=2.0)


def test_a_servers_wait_is_capped_by_its_own_ceiling_not_by_our_backoff(policy):
    """max_delay_seconds bounds the backoff WE choose; an explicit instruction is bounded
    by its own, higher ceiling."""
    seconds = _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "600"}), policy)
    assert seconds == pytest.approx(policy.retry_after_max_seconds)
    assert seconds > policy.max_delay_seconds


@pytest.mark.parametrize(("header", "expected"), [("soon-ish", None), ("-5", 0.0), ("", None)])
def test_retry_after_edge_cases(policy, header, expected):
    response = httpx.Response(429, headers={"Retry-After": header} if header else {})
    assert _retry_after_seconds(response, policy) == expected


async def test_retry_works_for_any_verb(fast_policy, respx_mock):
    route = respx_mock.post(URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as client:
        await request_with_retry(client, "POST", URL, policy=fast_policy, json_body={"a": 1})
    assert route.call_count == 1
    assert route.calls.last.request.method == "POST"


async def test_get_with_retry_passes_params_and_headers(fast_policy, respx_mock):
    route = respx_mock.get(URL, params={"q": "1"}).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        response = await get_with_retry(
            client, URL, policy=fast_policy, params={"q": "1"}, headers={"X-Test": "yes"}
        )
    assert response.status_code == 200
    assert route.calls.last.request.headers["X-Test"] == "yes"
