"""Per-target retry, on top of the per-request retry in :mod:`obliskiq.util.http`.

The two layers do different jobs. ``request_with_retry`` handles one HTTP call
failing transiently. This handles one *target* — an underlying, a futures root, a
macro series — failing after its requests have already exhausted their own
retries. One dead underlying must degrade a run, never kill it: an unattended
collector that aborts on the first bad symbol loses a day of irreplaceable data
for every symbol after it in the list.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from ..config import Config
from ..errors import PermanentUpstreamError
from ..logging_setup import get_logger

log = get_logger("collect.retry")

T = TypeVar("T")


@dataclass(frozen=True)
class TargetRetryPolicy:
    """Retry parameters for one target. Built from config, never from literals."""

    retries: int
    base_delay_seconds: float
    max_delay_seconds: float
    spacing_seconds: float

    @classmethod
    def from_config(cls, config: Config) -> TargetRetryPolicy:
        cfg = config.section("collectors")
        return cls(
            retries=cfg.require_int("target_retries"),
            base_delay_seconds=cfg.require_float("target_backoff_seconds"),
            max_delay_seconds=cfg.require_float("target_backoff_max_seconds"),
            spacing_seconds=cfg.require_float("request_spacing_seconds"),
        )

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff, capped. ``attempt`` is zero-based."""
        raw = self.base_delay_seconds * (2**attempt)
        return min(raw, self.max_delay_seconds)


async def with_target_retry(
    policy: TargetRetryPolicy,
    target: str,
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Run ``operation``, retrying the whole target with capped backoff.

    Re-raises the last exception when every attempt failed, so the caller records
    a failed target rather than a silent empty one.
    """
    last: Exception | None = None
    for attempt in range(policy.retries + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except PermanentUpstreamError as exc:
            # A contract that does not exist, a fixture never recorded, a
            # credential the broker rejected. The second attempt costs the same
            # wait and returns the same answer. Raised straight through, so the
            # caller records exactly the failure it would have recorded three
            # attempts later — only sooner.
            log.warning(
                "target failed permanently; not retrying",
                extra={"target": target, "error": repr(exc)},
            )
            raise
        except Exception as exc:
            last = exc
            if attempt >= policy.retries:
                break
            wait = policy.delay_for(attempt)
            log.warning(
                "target failed, retrying",
                extra={
                    "target": target,
                    "attempt": attempt + 1,
                    "of": policy.retries + 1,
                    "wait_seconds": wait,
                    "error": repr(exc),
                },
            )
            await asyncio.sleep(wait)
    if last is None:  # pragma: no cover - the loop cannot exit without one
        raise RuntimeError(f"target {target!r} exhausted its retries without an error")
    raise last


async def pace(policy: TargetRetryPolicy) -> None:
    """Sleep between calls in a loop.

    A collector walking a few hundred strikes is the sort of caller IBKR's pacing
    limits exist for; jitter stops several roots hitting the same instant after a
    shared failure.
    """
    if policy.spacing_seconds <= 0:
        return
    await asyncio.sleep(policy.spacing_seconds * (1 + random.random()))
