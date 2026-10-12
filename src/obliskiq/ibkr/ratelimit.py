"""Pacing: no request leaves faster than IBKR allows, and a failing endpoint group stops asking.

IBKR paces the Web API per endpoint and globally, and a violation puts the caller's IP in a
15-minute penalty box (docs/knowledge/ibkr.md, "Pacing"). So the limits are enforced before a
request is sent, never discovered from a 429:

* every request takes a slot from the global windows and from its endpoint group's windows
  (history has two: per second and per minute); a slot is reserved atomically, so concurrent
  callers can never jointly exceed a window;
* at most ``max_concurrency`` requests are in flight; the slot is reserved only once a request
  holds a concurrency permit, so a reserved request is sent at once and the recorded instant is
  the sending instant;
* a ``Retry-After`` pauses the whole group, not just the request that carried it;
* a group that keeps failing opens its circuit: requests to it fail fast for a pause, then one
  probe is let through, and its outcome closes or re-opens the circuit.

Every attempt passes here, retries included. Time comes from an injectable :class:`Clock`.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..config.schema_ibkr import IbkrConfig, RateWindow
from ..logging_setup import get_logger
from .errors import CircuitOpenError

log = get_logger("ibkr.ratelimit")

#: The group of an endpoint no configured group names: it is paced by the global windows only.
DEFAULT_GROUP = "default"


class Clock(Protocol):
    """Monotonic time and sleeping, injectable so pacing can be tested exactly."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The process's monotonic clock and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SlidingWindow:
    """At most ``requests`` instants inside any interval of ``per_seconds``."""

    def __init__(self, window: RateWindow) -> None:
        self._requests = window.requests
        self._per_seconds = window.per_seconds
        self._instants: deque[float] = deque()

    def wait(self, now: float) -> float:
        """Seconds until one more instant fits; zero when it fits now.

        Expiry and the wait are the same expression (``instant + per_seconds - now``), so float
        rounding can never report a full window as free without also expiring its oldest instant.
        """
        while self._instants and self._instants[0] + self._per_seconds - now <= 0:
            self._instants.popleft()
        if len(self._instants) < self._requests:
            return 0.0
        return self._instants[0] + self._per_seconds - now

    def record(self, now: float) -> None:
        self._instants.append(now)


def _windows(windows: Sequence[RateWindow]) -> tuple[SlidingWindow, ...]:
    return tuple(SlidingWindow(window) for window in windows)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; lets one probe through after ``pause``."""

    threshold: int
    pause: float
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    probing: bool = False

    def admit(self, group: str, now: float) -> None:
        """Raise :class:`CircuitOpenError` unless a request to the group may be sent now."""
        if self.state is BreakerState.CLOSED:
            return
        if self.state is BreakerState.OPEN:
            remaining = self.opened_at + self.pause - now
            if remaining > 0:
                raise CircuitOpenError(
                    f"requests to the {group} endpoints are paused after repeated failures; "
                    f"{remaining:.0f} s remain",
                    retry_after=remaining,
                )
            self.state = BreakerState.HALF_OPEN
            self.probing = False
        if self.probing:
            raise CircuitOpenError(
                f"a probe request to the {group} endpoints is already in flight",
                retry_after=self.pause,
            )
        self.probing = True

    def success(self) -> None:
        self.state = BreakerState.CLOSED
        self.failures = 0
        self.probing = False

    def failure(self, group: str, now: float) -> None:
        self.failures += 1
        if self.state is BreakerState.HALF_OPEN or self.failures >= self.threshold:
            if self.state is not BreakerState.OPEN:
                log.warning(
                    "endpoint group circuit opened",
                    extra={"group": group, "failures": self.failures, "pause_seconds": self.pause},
                )
            self.state = BreakerState.OPEN
            self.opened_at = now
            self.probing = False


@dataclass
class _Group:
    name: str
    windows: tuple[SlidingWindow, ...]
    breaker: CircuitBreaker
    paused_until: float = 0.0
    sent: int = field(default=0)


class RateLimiter:
    """Admits requests only as fast as the configured windows allow."""

    def __init__(self, ibkr: IbkrConfig, clock: Clock | None = None) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._global = _windows(ibkr.ratelimit.global_windows)
        self._permits = asyncio.Semaphore(ibkr.ratelimit.max_concurrency)
        self._lock = asyncio.Lock()
        self._routes: list[tuple[str, str]] = []
        self._groups: dict[str, _Group] = {}
        for group in [*ibkr.ratelimit.groups, None]:
            name = DEFAULT_GROUP if group is None else group.name
            self._groups[name] = _Group(
                name=name,
                windows=() if group is None else _windows(group.windows),
                breaker=CircuitBreaker(
                    threshold=ibkr.ratelimit.breaker_failure_threshold,
                    pause=ibkr.ratelimit.breaker_open_seconds,
                ),
            )
            if group is not None:
                self._routes += [(path.strip("/"), group.name) for path in group.paths]
        # Longest path first, so a more specific path wins over its prefix.
        self._routes.sort(key=lambda route: len(route[0]), reverse=True)

    def group_for(self, path: str) -> str:
        """The group an endpoint path belongs to (``path`` without the API prefix or a query)."""
        clean = path.split("?", 1)[0].strip("/")
        for prefix, name in self._routes:
            if clean == prefix or clean.startswith(f"{prefix}/"):
                return name
        return DEFAULT_GROUP

    def sent(self, group: str) -> int:
        """How many requests the group has sent (for reporting and tests)."""
        return self._groups[group].sent

    @asynccontextmanager
    async def slot(self, path: str) -> AsyncIterator[str]:
        """Hold a sending slot for one request to ``path``; yields the group name.

        Raises :class:`CircuitOpenError` without waiting when the group's circuit is open.
        """
        group = self._groups[self.group_for(path)]
        async with self._permits:
            while True:
                async with self._lock:
                    now = self._clock.monotonic()
                    group.breaker.admit(group.name, now)
                    wait = max(
                        [window.wait(now) for window in (*self._global, *group.windows)]
                        + [group.paused_until - now]
                    )
                    if wait <= 0:
                        for window in (*self._global, *group.windows):
                            window.record(now)
                        group.sent += 1
                        break
                    # Not sending yet: a probe admitted above is released for the next attempt.
                    group.breaker.probing = False
                await self._clock.sleep(wait)
            try:
                yield group.name
            finally:
                # The probe's outcome is reported by success() or failure(); an outcome that is
                # neither (a request IBKR rejected as malformed) lets the next request probe.
                group.breaker.probing = False

    def pause(self, group: str, seconds: float) -> None:
        """Hold every request to ``group`` for ``seconds`` (a ``Retry-After``)."""
        state = self._groups[group]
        state.paused_until = max(state.paused_until, self._clock.monotonic() + seconds)

    def success(self, group: str) -> None:
        self._groups[group].breaker.success()

    def failure(self, group: str) -> None:
        self._groups[group].breaker.failure(group, self._clock.monotonic())

    def breaker_state(self, group: str) -> BreakerState:
        return self._groups[group].breaker.state
