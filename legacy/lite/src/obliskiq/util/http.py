"""HTTP retry helper.

Ported from IBKR-Portmanteau's ``_get_with_retry``. Changed while porting:

* The retry policy comes from config (``http.*``) instead of being written into the
  default arguments.
* Backoff is capped at ``http.max_delay_seconds``. The original doubled without a
  ceiling, so a raised retry count grew the wait geometrically.
* A transport failure raises ``UpstreamError`` rather than returning ``None``. The
  original returned ``None`` on transport failure and a ``Response`` on an HTTP
  error, so every caller had to remember two failure shapes and most checked one.
* Works for any verb, not just GET, so the OAuth POSTs share one policy.
* Honours ``Retry-After`` when the server sends it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..config import Config
from ..errors import UpstreamError
from ..logging_setup import get_logger

log = get_logger("http")


@dataclass(frozen=True)
class RetryPolicy:
    """Retry parameters. Built from config; never constructed with literals in code."""

    retries: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_seconds: float
    timeout_seconds: float
    retry_status_codes: frozenset[int]
    #: Ceiling for a wait the SERVER asked for, separate from and higher than
    #: ``max_delay_seconds``, which bounds our own backoff.
    retry_after_max_seconds: float

    @classmethod
    def from_config(cls, config: Config, section: str = "http") -> RetryPolicy:
        cfg = config.section(section)
        return cls(
            retries=cfg.require_int("retries"),
            base_delay_seconds=cfg.require_float("base_delay_seconds"),
            max_delay_seconds=cfg.require_float("max_delay_seconds"),
            jitter_seconds=cfg.require_float("jitter_seconds"),
            timeout_seconds=cfg.require_float("timeout_seconds"),
            retry_after_max_seconds=cfg.require_float("retry_after_max_seconds"),
            retry_status_codes=frozenset(
                int(code) for code in cfg.require_list("retry_status_codes")
            ),
        )

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped. ``attempt`` is zero-based."""
        raw = self.base_delay_seconds * (2**attempt)
        capped = min(raw, self.max_delay_seconds)
        return capped + random.uniform(0.0, self.jitter_seconds)


def _retry_after_seconds(response: httpx.Response, policy: RetryPolicy) -> float | None:
    """The wait the server asked for, in seconds, or ``None``.

    RFC 9110 allows both forms and upstreams use both: delta-seconds, and an
    HTTP-date. Only the first was read, so a dated header fell through to our own
    exponential backoff — which on a 429 means retrying well before the server
    said to.

    Bounded by ``retry_after_max_seconds`` rather than ``max_delay_seconds``:
    the latter bounds backoff WE chose, and using it here silently discarded most
    of an explicit instruction.
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
        except (TypeError, ValueError):
            # Neither a delta nor a date. Fall through to our own backoff rather
            # than guessing at what the upstream meant.
            return None
        if moment is None:
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

    Returns the final response, including a non-2xx one that is not retryable — the
    caller decides what a 404 means. Raises ``UpstreamError`` when every attempt
    failed at the transport layer, so a dead host is never mistaken for empty data.

    ``is_permanent`` inspects a response whose STATUS says retry and whose BODY
    says do not. Upstreams do this: IBKR answers "Chart data unavailable" and
    "Contract details are not available" with a bare ``HTTP 500``, which is in
    the retryable set, so every permanently absent target spent three attempts
    here and then two more in the per-target loop — nine calls for an answer that
    could not change. A caller that can recognise its own upstream's permanent
    bodies passes a predicate and gets one call. Status alone cannot decide it:
    the same 500 is a genuine fault on every other endpoint.
    """
    retryable = (
        frozenset(retry_status_codes)
        if retry_status_codes is not None
        else policy.retry_status_codes
    )
    last_response: httpx.Response | None = None
    last_exc: Exception | None = None

    for attempt in range(policy.retries + 1):
        # Reset per attempt: a Retry-After belongs to the response that carried
        # it, and must not time the wait after a later transport failure.
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
            last_response = response
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

    if last_response is not None:  # pragma: no cover - loop always returns or raises
        return last_response
    raise UpstreamError(f"{method} {url} exhausted retries: {last_exc!r}")


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
