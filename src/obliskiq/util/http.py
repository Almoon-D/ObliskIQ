"""HTTP retry loop.

Ported from ObliskIQ-lite-MCP (which took it from IBKR-Portmanteau's ``_get_with_retry``).

* The retry policy is a value built from configuration by the caller, never literals.
* Backoff is capped at ``max_delay_seconds``, so raising the retry count cannot grow the
  wait geometrically.
* A transport failure raises :class:`UpstreamError`. Returning ``None`` for it and a
  ``Response`` for an HTTP error would give every caller two failure shapes to remember.
* Any verb, so signed POSTs share one policy.
* ``Retry-After`` is honoured, in both of its RFC 9110 forms.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..errors import UpstreamError
from ..logging_setup import get_logger

log = get_logger("http")


@dataclass(frozen=True)
class RetryPolicy:
    """Retry parameters. Built from configuration; never constructed with literals in code."""

    retries: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_seconds: float
    timeout_seconds: float
    retry_status_codes: frozenset[int]
    #: Ceiling for a wait the SERVER asked for, separate from and higher than
    #: ``max_delay_seconds``, which bounds our own backoff.
    retry_after_max_seconds: float

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped. ``attempt`` is zero-based."""
        raw = self.base_delay_seconds * 2.0**attempt
        capped = min(raw, self.max_delay_seconds)
        return capped + random.uniform(0.0, self.jitter_seconds)


def _retry_after_seconds(response: httpx.Response, policy: RetryPolicy) -> float | None:
    """The wait the server asked for, in seconds, or ``None``.

    RFC 9110 allows delta-seconds and an HTTP-date, and upstreams use both. The wait is
    bounded by ``retry_after_max_seconds`` rather than ``max_delay_seconds``: the latter
    bounds backoff we chose, and using it here would discard most of an explicit
    instruction — which on a 429 is how a rate limit becomes a longer ban.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except TypeError, ValueError:
            # Neither a delta nor a date: fall back to our own backoff rather than guess.
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = (moment - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return 0.0
    return min(seconds, policy.retry_after_max_seconds)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: RetryPolicy,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
    json_body: Any = None,
    retry_status_codes: Sequence[int] | None = None,
    is_permanent: Callable[[httpx.Response], bool] | None = None,
) -> httpx.Response:
    """Issue a request, retrying transient failures with capped exponential backoff.

    Returns the final response, including a non-2xx one that is not retryable — the caller
    decides what a 404 means. Raises :class:`UpstreamError` when every attempt failed at
    the transport layer, so a dead host is never mistaken for empty data.

    ``is_permanent`` inspects a response whose STATUS says retry and whose BODY says do
    not. IBKR answers "Chart data unavailable" with a bare ``HTTP 500``, which is in the
    retryable set; a caller that recognises its upstream's permanent bodies passes a
    predicate and gets one call. Status alone cannot decide it: the same 500 is a genuine
    fault on other endpoints.
    """
    retryable = (
        frozenset(retry_status_codes)
        if retry_status_codes is not None
        else policy.retry_status_codes
    )
    last_exc: Exception | None = None

    for attempt in range(policy.retries + 1):
        # Reset per attempt: a Retry-After belongs to the response that carried it.
        retry_after: float | None = None
        try:
            response = await client.request(
                method,
                url,
                params=params,
                headers=dict(headers) if headers else None,
                content=content,
                json=json_body,
                timeout=policy.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= policy.retries:
                raise UpstreamError(
                    f"{method} {url} failed at the transport layer after "
                    f"{policy.retries + 1} attempts: {exc!r}"
                ) from exc
            log.warning(
                "http transport failure, retrying",
                extra={"method": method, "url": url, "attempt": attempt + 1, "error": repr(exc)},
            )
        else:
            if response.status_code not in retryable or attempt >= policy.retries:
                return response
            if is_permanent is not None and is_permanent(response):
                log.info(
                    "http retryable status carrying a permanent body; not retrying",
                    extra={"method": method, "url": url, "status": response.status_code},
                )
                return response
            retry_after = _retry_after_seconds(response, policy)
            log.warning(
                "http retryable status, retrying",
                extra={
                    "method": method,
                    "url": url,
                    "attempt": attempt + 1,
                    "status": response.status_code,
                },
            )

        wait = retry_after if retry_after is not None else policy.delay_for(attempt)
        await asyncio.sleep(wait)

    # Unreachable: the final attempt returns or raises above.
    raise UpstreamError(f"{method} {url} exhausted retries: {last_exc!r}")  # pragma: no cover


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    policy: RetryPolicy,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """GET with retry. Thin alias over :func:`request_with_retry`."""
    return await request_with_retry(
        client, "GET", url, policy=policy, params=params, headers=headers
    )
