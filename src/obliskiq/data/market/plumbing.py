"""What every collector shares: per-target outcomes and their verdict, per-target retry and the
pacing between calls.

Ported from lite's ``collect/runlog.py`` (``TargetOutcome``, ``CollectorRun.status``) and
``collect/retry.py``. **One dead target degrades a run, it never kills it**: an unattended
collector that aborts on the first bad symbol loses a day of irreplaceable chain data for every
symbol after it. A target is retried whole with capped exponential backoff after its requests
have spent the session's own retries; a failure that a second attempt cannot change (a contract
that does not exist, an ambiguous resolution, a request IBKR will never accept) is not retried.
A collection's verdict is the run log's rule: every target failed is ``error``, some ``partial``,
a fallback ``degraded``, no target ``skipped`` with the reason, never ``ok`` over nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...config.schema_collectors import RetryConfig
from ...errors import ObliskError
from ...ibkr.errors import AmbiguousContractError, IbkrError, RetryAdvice
from ...logging_setup import get_logger
from ...orchestration.runlog import DEGRADED, ERROR, OK, PARTIAL, SKIPPED, StepOutcome

log = get_logger("data.market")

#: Where every collected row comes from: the Client Portal Web API (docs/knowledge/ibkr.md, CP).
MARKET_SOURCE = "cp"

#: How many failed targets a step's record names.
SHOWN_FAILURES = 20

type Sleep = Callable[[float], Awaitable[None]]


class PermanentCollectionError(ObliskError):
    """A target that a second attempt cannot fix today (no spot to centre a band on, no option
    month in the window); recorded as failed without spending the backoff."""


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    """One target's result inside a collection."""

    target: str
    ok: bool
    rows: int = 0
    degraded: bool = False
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Collection:
    """One collector's walk over its targets, and the step verdict it earns."""

    step: str
    outcomes: list[TargetOutcome] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    #: Why there were no targets, when there were none.
    empty_reason: str = ""

    def succeeded(
        self, target: str, rows: int, *, degraded: bool = False, reason: str = "", **detail: Any
    ) -> None:
        self.outcomes.append(TargetOutcome(target, True, rows, degraded, reason, detail))
        log.info(
            "target collected",
            extra={"step": self.step, "target": target, "rows": rows, "degraded": degraded},
        )

    def failed(self, target: str, reason: str, **detail: Any) -> None:
        self.outcomes.append(TargetOutcome(target, False, reason=reason, detail=detail))
        log.warning("target failed", extra={"step": self.step, "target": target, "reason": reason})

    @property
    def rows(self) -> int:
        return sum(outcome.rows for outcome in self.outcomes)

    @property
    def status(self) -> str:
        if not self.outcomes:
            return SKIPPED
        failed = sum(1 for outcome in self.outcomes if not outcome.ok)
        if failed:
            return ERROR if failed == len(self.outcomes) else PARTIAL
        return DEGRADED if any(outcome.degraded for outcome in self.outcomes) else OK

    def judge(self) -> StepOutcome:
        """The step's record: its verdict, counts, the failures and the degraded targets."""
        failures = [o for o in self.outcomes if not o.ok]
        degraded = [o for o in self.outcomes if o.ok and o.degraded]
        reason = (
            self.empty_reason
            if not self.outcomes
            else "; ".join(
                f"{o.target}: {o.reason}" for o in (failures or degraded)[:SHOWN_FAILURES]
            )
        )
        detail: dict[str, Any] = {
            "rows": self.rows,
            "targets": len(self.outcomes),
            "failed": [o.target for o in failures],
            "degraded": [o.target for o in degraded],
            **self.notes,
        }
        return StepOutcome(self.step, self.status, reason, detail)


def is_permanent(error: BaseException) -> bool:
    """Whether retrying the target could change the answer today."""
    if isinstance(error, PermanentCollectionError | AmbiguousContractError):
        return True
    return isinstance(error, IbkrError) and error.retry is RetryAdvice.NEVER


class Pacer:
    """Retries one target whole and paces the calls of a loop (``collectors.retry``)."""

    def __init__(self, config: RetryConfig, sleep: Sleep) -> None:
        self._config = config
        self._sleep = sleep

    def delay_for(self, attempt: int) -> float:
        """Capped exponential backoff; ``attempt`` is zero-based."""
        backoff = float(self._config.backoff_seconds * 2**attempt)
        return min(backoff, self._config.backoff_max_seconds)

    async def retry[T](self, target: str, operation: Callable[[], Awaitable[T]]) -> T:
        """Run ``operation``; on a failure that may pass, back off and run it again. The last
        failure is raised, so the caller records a failed target, never a silent empty one."""
        attempt = 0
        while True:
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_permanent(exc) or attempt >= self._config.target_retries:
                    raise
                wait = self.delay_for(attempt)
                log.warning(
                    "target failed, retrying",
                    extra={"target": target, "attempt": attempt + 1, "wait_seconds": wait},
                )
                await self._sleep(wait)
                attempt += 1

    async def pace(self) -> None:
        """The pause between two calls of a loop, jittered so several loops recovering from one
        shared failure do not hit the same instant."""
        spacing = self._config.request_spacing_seconds
        if spacing > 0:
            await self._sleep(spacing * (1 + random.random()))


def failure_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"
