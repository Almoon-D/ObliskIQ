"""Pacing: the configured windows are never exceeded, even by many concurrent callers.

IBKR's caps (docs/knowledge/ibkr.md, "Pacing") are enforced before a request leaves, so these
tests run the limiter against virtual time and check every recorded sending instant against
every window it belongs to, and the number of requests in flight against the concurrency cap.
"""

import asyncio
import random
from collections import defaultdict
from collections.abc import Sequence

import pytest

from obliskiq.config.schema_ibkr import IbkrConfig, RateWindow
from obliskiq.ibkr.errors import CircuitOpenError, RetryAdvice
from obliskiq.ibkr.ratelimit import (
    DEFAULT_GROUP,
    BreakerState,
    RateLimiter,
    SlidingWindow,
    SystemClock,
)

PATHS = (
    "/iserver/marketdata/history?conid=1",
    "iserver/marketdata/snapshot",
    "/portfolio/accounts",
    "/portfolio/subaccounts",
    "/iserver/scanner/run",
    "/tickle",
    "/iserver/secdef/search",
)


def assert_window_respected(instants: Sequence[float], window: RateWindow, label: str) -> None:
    """No interval of ``per_seconds`` holds more than ``requests`` instants."""
    ordered = sorted(instants)
    for i in range(len(ordered) - window.requests):
        gap = ordered[i + window.requests] - ordered[i]
        assert gap >= window.per_seconds - 1e-9, (
            f"{label}: {window.requests + 1} requests inside {gap:.3f} s "
            f"(limit {window.requests} per {window.per_seconds} s)"
        )


def assert_paced(limiter: RateLimiter, ibkr: IbkrConfig, sent: list[tuple[str, float]]) -> None:
    for window in ibkr.ratelimit.global_windows:
        assert_window_respected([at for _, at in sent], window, "global")
    by_group: dict[str, list[float]] = defaultdict(list)
    for path, at in sent:
        by_group[limiter.group_for(path)].append(at)
    for group in ibkr.ratelimit.groups:
        for window in group.windows:
            assert_window_respected(by_group[group.name], window, group.name)


async def run_burst(
    limiter: RateLimiter, clock, paths: Sequence[str], duration: float, seed: int | None = None
) -> tuple[list[tuple[str, float]], int]:
    """Send every path concurrently; return the sending instants and the peak in flight.

    With a ``seed``, each request takes a random time up to ``duration``, so the clock lands at
    arbitrary points inside the windows rather than only on their edges.
    """
    rng = random.Random(seed)
    sent: list[tuple[str, float]] = []
    in_flight = 0
    peak = 0

    async def one(path: str) -> None:
        nonlocal in_flight, peak
        async with limiter.slot(path):
            sent.append((path, clock.monotonic()))
            in_flight += 1
            peak = max(peak, in_flight)
            await clock.sleep(duration if seed is None else rng.uniform(0.0, duration))
            in_flight -= 1

    await asyncio.gather(*(one(path) for path in paths))
    return sent, peak


@pytest.fixture
def ibkr(shipped_settings) -> IbkrConfig:
    config: IbkrConfig = shipped_settings.ibkr
    return config


async def test_two_hundred_concurrent_requests_never_exceed_any_window(ibkr, fake_clock):
    limiter = RateLimiter(ibkr, fake_clock)
    rng = random.Random(7)
    paths = [rng.choice(PATHS) for _ in range(200)]
    sent, peak = await run_burst(limiter, fake_clock, paths, duration=0.3, seed=11)
    assert len(sent) == 200
    assert peak <= ibkr.ratelimit.max_concurrency
    assert_paced(limiter, ibkr, sent)


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_fast_endpoints_together_are_held_to_the_global_cap(ibkr, fake_clock, seed):
    """Short requests to groups with roomy windows: here the global window is what binds."""
    limiter = RateLimiter(ibkr, fake_clock)
    rng = random.Random(seed)
    fast = (PATHS[0], PATHS[1], PATHS[-1])
    paths = [rng.choice(fast) for _ in range(200)]
    sent, peak = await run_burst(limiter, fake_clock, paths, duration=0.01, seed=seed)
    assert len(sent) == 200
    assert peak <= ibkr.ratelimit.max_concurrency
    assert_paced(limiter, ibkr, sent)


async def test_history_is_held_to_its_per_minute_cap_as_well_as_per_second(ibkr, fake_clock):
    limiter = RateLimiter(ibkr, fake_clock)
    sent, _ = await run_burst(limiter, fake_clock, [PATHS[0]] * 100, duration=0.0)
    (history,) = [group for group in ibkr.ratelimit.groups if group.name == "history"]
    per_minute = max(history.windows, key=lambda window: window.per_seconds)
    instants = sorted(at for _, at in sent)
    assert instants[per_minute.requests] - instants[0] >= per_minute.per_seconds
    assert limiter.sent("history") == 100
    assert_paced(limiter, ibkr, sent)


async def test_the_scanner_parameters_are_asked_at_most_once_per_window(ibkr, fake_clock):
    limiter = RateLimiter(ibkr, fake_clock)
    sent, _ = await run_burst(limiter, fake_clock, ["/iserver/scanner/params"] * 3, duration=0.0)
    (group,) = [group for group in ibkr.ratelimit.groups if group.name == "scanner_params"]
    instants = sorted(at for _, at in sent)
    assert instants[1] - instants[0] >= group.windows[0].per_seconds
    assert instants[2] - instants[1] >= group.windows[0].per_seconds


async def test_a_retry_after_pauses_the_whole_group(ibkr, fake_clock):
    limiter = RateLimiter(ibkr, fake_clock)
    start = fake_clock.monotonic()
    limiter.pause("snapshot", 30.0)
    limiter.pause("snapshot", 5.0)  # a shorter pause never shortens a longer one
    async with limiter.slot("/iserver/marketdata/snapshot"):
        assert fake_clock.monotonic() >= start + 30.0
    async with limiter.slot("/iserver/accounts"):
        pass  # another group is not held
    assert limiter.sent(DEFAULT_GROUP) == 1


@pytest.mark.parametrize(
    ("path", "group"),
    [
        ("/iserver/marketdata/history?conid=1&period=1d", "history"),
        ("iserver/marketdata/history/", "history"),
        ("/iserver/marketdata/historyx", DEFAULT_GROUP),
        ("/portfolio/accounts", "portfolio_accounts"),
        ("/portfolio/subaccounts", "portfolio_accounts"),
        ("/portfolio/DU_PAPER/positions/0", DEFAULT_GROUP),
        ("/iserver/scanner/params", "scanner_params"),
        ("/iserver/scanner/run", "scanner_run"),
        ("/tickle", "tickle"),
        ("", DEFAULT_GROUP),
    ],
)
def test_paths_map_to_their_group(ibkr, path, group):
    assert RateLimiter(ibkr).group_for(path) == group


def test_the_longest_configured_path_wins_over_its_prefix(ibkr):
    data = ibkr.ratelimit.model_dump()
    data["groups"].append(
        {
            "name": "marketdata",
            "paths": ["iserver/marketdata"],
            "windows": [data["global_windows"][0]],
        }
    )
    config = ibkr.model_copy(update={"ratelimit": type(ibkr.ratelimit).model_validate(data)})
    limiter = RateLimiter(config)
    assert limiter.group_for("/iserver/marketdata/history") == "history"
    assert limiter.group_for("/iserver/marketdata/unsubscribeall") == "marketdata"


def test_a_sliding_window_frees_a_slot_exactly_one_period_later():
    window = SlidingWindow(RateWindow(requests=2, per_seconds=1.0))
    assert window.wait(0.0) == 0.0
    window.record(0.0)
    window.record(0.5)
    assert window.wait(0.6) == pytest.approx(0.4)
    assert window.wait(1.0) == 0.0


async def test_the_system_clock_moves_forward():
    clock = SystemClock()
    before = clock.monotonic()
    await clock.sleep(0.0)
    assert clock.monotonic() >= before


# ── circuit breaker ──


def _fail(limiter: RateLimiter, group: str, times: int) -> None:
    for _ in range(times):
        limiter.failure(group)


async def test_a_group_opens_after_the_threshold_and_fails_fast(ibkr, fake_clock):
    limiter = RateLimiter(ibkr, fake_clock)
    threshold = ibkr.ratelimit.breaker_failure_threshold
    _fail(limiter, "snapshot", threshold - 1)
    assert limiter.breaker_state("snapshot") is BreakerState.CLOSED
    limiter.failure("snapshot")
    assert limiter.breaker_state("snapshot") is BreakerState.OPEN
    with pytest.raises(CircuitOpenError) as caught:
        async with limiter.slot("/iserver/marketdata/snapshot"):
            pass
    assert caught.value.retry is RetryAdvice.BACKOFF
    assert caught.value.retry_after == pytest.approx(ibkr.ratelimit.breaker_open_seconds)
    assert fake_clock.slept == [], "an open circuit fails without waiting"
    async with limiter.slot("/iserver/marketdata/history"):
        pass  # other groups are unaffected


async def test_success_resets_the_failure_count(ibkr, fake_clock):
    limiter = RateLimiter(ibkr, fake_clock)
    threshold = ibkr.ratelimit.breaker_failure_threshold
    _fail(limiter, "history", threshold - 1)
    limiter.success("history")
    _fail(limiter, "history", threshold - 1)
    assert limiter.breaker_state("history") is BreakerState.CLOSED


async def _opened(ibkr: IbkrConfig, clock) -> RateLimiter:
    limiter = RateLimiter(ibkr, clock)
    _fail(limiter, "snapshot", ibkr.ratelimit.breaker_failure_threshold)
    clock.now += ibkr.ratelimit.breaker_open_seconds
    return limiter


async def test_after_the_pause_one_probe_is_let_through_and_success_closes(ibkr, fake_clock):
    limiter = await _opened(ibkr, fake_clock)
    async with limiter.slot("/iserver/marketdata/snapshot"):
        assert limiter.breaker_state("snapshot") is BreakerState.HALF_OPEN
        with pytest.raises(CircuitOpenError, match="probe"):
            async with limiter.slot("/iserver/marketdata/snapshot"):
                pass
        limiter.success("snapshot")
    assert limiter.breaker_state("snapshot") is BreakerState.CLOSED


async def test_a_failed_probe_reopens_the_circuit_at_once(ibkr, fake_clock):
    limiter = await _opened(ibkr, fake_clock)
    async with limiter.slot("/iserver/marketdata/snapshot"):
        limiter.failure("snapshot")
    assert limiter.breaker_state("snapshot") is BreakerState.OPEN
    with pytest.raises(CircuitOpenError, match="paused"):
        async with limiter.slot("/iserver/marketdata/snapshot"):
            pass


async def test_a_probe_without_an_outcome_lets_the_next_request_probe(ibkr, fake_clock):
    limiter = await _opened(ibkr, fake_clock)
    async with limiter.slot("/iserver/marketdata/snapshot"):
        pass  # rejected as malformed: neither success nor failure is reported
    async with limiter.slot("/iserver/marketdata/snapshot"):
        assert limiter.breaker_state("snapshot") is BreakerState.HALF_OPEN


async def test_a_probe_waiting_for_its_window_is_not_counted_as_in_flight(ibkr, fake_clock):
    limiter = await _opened(ibkr, fake_clock)
    limiter.pause("snapshot", 2.0)
    sent, _ = await run_burst(limiter, fake_clock, ["/iserver/marketdata/snapshot"], duration=0.0)
    assert len(sent) == 1
    assert limiter.breaker_state("snapshot") is BreakerState.HALF_OPEN


def test_float_rounding_never_reports_a_full_window_as_free():
    """Found by the burst test: ``old + 60 - now`` rounded to zero while ``old <= now - 60`` did
    not hold, so the window admitted without expiring and let a whole burst through at once."""
    window = SlidingWindow(RateWindow(requests=1, per_seconds=60.0))
    old, now = 1001.0163615708808, 1061.0163615708807
    window.record(old)
    if window.wait(now) <= 0:
        window.record(now)
    assert window.wait(now) > 0


async def test_a_tickle_never_waits_behind_another_groups_window(ibkr, fake_clock):
    """Review round 1: requests queued for a slow window (scanner parameters, one per 16 min)
    held every concurrency permit, and the keepalive tickle went out 960 s late."""
    limiter = RateLimiter(ibkr, fake_clock)
    start = fake_clock.monotonic()
    waiters = ibkr.ratelimit.max_concurrency + 1
    sent: dict[str, list[float]] = defaultdict(list)

    async def one(path: str) -> None:
        async with limiter.slot(path):
            sent[path].append(fake_clock.monotonic())

    scanner = [asyncio.create_task(one("/iserver/scanner/params")) for _ in range(waiters)]
    await asyncio.sleep(0)
    await one("/tickle")
    assert sent["/tickle"] == [start]
    for task in scanner:
        task.cancel()
    await asyncio.gather(*scanner, return_exceptions=True)


async def test_a_full_house_waits_for_a_permit_then_sends(ibkr, fake_clock):
    limiter = RateLimiter(ibkr, fake_clock)
    cap = ibkr.ratelimit.max_concurrency
    release = asyncio.Event()
    in_flight = 0
    peak = 0

    async def hold() -> None:
        nonlocal in_flight, peak
        async with limiter.slot("/iserver/secdef/search"):
            in_flight += 1
            peak = max(peak, in_flight)
            await release.wait()
            in_flight -= 1

    holders = [asyncio.create_task(hold()) for _ in range(cap + 2)]
    for _ in range(50):
        await asyncio.sleep(0)
    assert peak == cap, "no more than the cap in flight while the others wait"
    release.set()
    await asyncio.gather(*holders)
    assert limiter.sent(DEFAULT_GROUP) == cap + 2
