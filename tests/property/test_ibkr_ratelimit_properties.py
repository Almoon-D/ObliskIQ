"""Properties of the pacing windows: whatever the request times, no window is ever exceeded."""

import asyncio
from collections.abc import Sequence

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.conftest import FakeClock
from tests.unit.test_ibkr_ratelimit import (
    PATHS,
    assert_paced,
    assert_window_respected,
    run_burst,
)

from obliskiq.config.schema_ibkr import RateWindow
from obliskiq.ibkr.ratelimit import RateLimiter, SlidingWindow

windows = st.builds(
    RateWindow,
    requests=st.integers(min_value=1, max_value=50),
    per_seconds=st.floats(min_value=0.01, max_value=1_000.0, allow_nan=False),
)
# Arrival gaps that include exact multiples of the period and values that round badly.
gaps = st.lists(
    st.one_of(
        st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
        st.sampled_from([0.0, 0.1, 1.0 / 3.0, 60.0]),
    ),
    min_size=1,
    max_size=200,
)


@given(window=windows, arrivals=gaps, start=st.floats(min_value=0.0, max_value=1e7))
def test_a_window_admits_no_more_than_its_cap_in_any_period(
    window: RateWindow, arrivals: Sequence[float], start: float
):
    """Each arrival waits as the limiter would (wait, sleep, ask again) before recording."""
    sliding = SlidingWindow(window)
    now = start
    admitted: list[float] = []
    for gap in arrivals:
        now += gap
        for _ in range(1_000):
            wait = sliding.wait(now)
            if wait <= 0:
                break
            now += wait
        else:
            raise AssertionError("an arrival never fitted")
        sliding.record(now)
        admitted.append(now)
    assert_window_respected(admitted, window, "window")


@settings(max_examples=25, deadline=None)
@given(
    choices=st.lists(st.sampled_from(PATHS), min_size=1, max_size=120),
    seed=st.integers(min_value=0, max_value=2**16),
)
def test_concurrent_mixes_of_endpoints_never_exceed_any_window(shipped_settings, choices, seed):
    clock = FakeClock()
    limiter = RateLimiter(shipped_settings.ibkr, clock)
    sent, peak = asyncio.run(run_burst(limiter, clock, choices, duration=0.05, seed=seed))
    assert len(sent) == len(choices)
    assert peak <= shipped_settings.ibkr.ratelimit.max_concurrency
    assert_paced(limiter, shipped_settings.ibkr, sent)
