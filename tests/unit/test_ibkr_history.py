"""The history reader: probe-chunked, never trusting the count asked for.

Ported in intent from ObliskIQ-lite-MCP's ``tests/test_collect_backfill.py`` (the chunking cases;
the store watermark cases arrive with the collectors in Session 8).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from obliskiq.ibkr.errors import NoDataPermanentError
from obliskiq.ibkr.history import HistoryReader, start_time

BASE = "https://gw.test/v1/api"
HISTORY = f"{BASE}/iserver/marketdata/history"
CONID = 756733
LATEST = date(2026, 10, 9)
RECENT = start_time(LATEST)


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC).timestamp() * 1000)


class HistoryRoute:
    """A history endpoint serving weekdays from ``first`` to the requested end, at most ``cap``
    bars per answer, refusing spans longer than ``refuse_above`` days with IBKR's no-data 500."""

    def __init__(
        self,
        *,
        first: date,
        cap: int,
        refuse_above: int | None = None,
        refuse_before: date | None = None,
    ) -> None:
        self.first = first
        self.cap = cap
        self.refuse_above = refuse_above
        self.refuse_before = refuse_before
        self.calls: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.calls.append(params)
        span = int(params["period"].removesuffix("d"))
        stamp = params.get("startTime")
        end = LATEST if stamp is None else datetime.strptime(stamp[:8], "%Y%m%d").date()
        refused = (self.refuse_above is not None and span > self.refuse_above) or (
            self.refuse_before is not None and end < self.refuse_before
        )
        if refused:
            return httpx.Response(500, json={"error": "Chart data unavailable"})
        start = max(self.first, end - timedelta(days=span))
        days = [
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
        ][-self.cap :]
        rows: list[dict[str, Any]] = [
            {"t": _ms(day), "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100.0} for day in days
        ]
        return httpx.Response(200, json={"data": rows, "mdAvailability": "S"})


@pytest.fixture
def reader(make_session, shipped_settings):
    return HistoryReader(
        make_session(),
        shipped_settings.ibkr,
        calendar_days_per_year=shipped_settings.numerics.bsm.day_count_basis,
    )


def _route(router: respx.MockRouter, fake: HistoryRoute) -> None:
    router.get(HISTORY).mock(side_effect=fake)


@respx.mock
async def test_one_request_returns_what_was_served_not_what_was_asked(reader):
    fake = HistoryRoute(first=date(2020, 1, 1), cap=10)
    _route(respx.mock, fake)
    history = await reader.bars(CONID, period="30d", bar="1d")
    assert len(history.bars) == 10
    assert [b.start for b in history.bars] == sorted(b.start for b in history.bars)


@respx.mock
async def test_an_omitted_exchange_is_absent_from_the_wire(reader):
    fake = HistoryRoute(first=date(2020, 1, 1), cap=5)
    _route(respx.mock, fake)
    await reader.bars(CONID, period="30d", bar="1d", exchange=None)
    await reader.bars(CONID, period="30d", bar="1d", exchange="ARCA")
    assert "exchange" not in fake.calls[0]
    assert fake.calls[1]["exchange"] == "ARCA"
    assert fake.calls[0]["outsideRth"] == "false"


def test_the_start_time_is_the_end_of_that_day():
    assert start_time(date(2026, 9, 15)) == "20260915-23:59:59"


@respx.mock
async def test_the_daily_cap_is_probed_rather_than_assumed(reader, shipped_settings):
    """lite measured 257 bars for 400 days; a truncated probe narrows the chunk."""
    fake = HistoryRoute(first=date(2010, 1, 1), cap=100)
    _route(respx.mock, fake)
    chunk = await reader.chunk_days(CONID, exchange=None)
    history = shipped_settings.ibkr.history
    assert fake.calls[0]["period"] == f"{history.probe_request_days}d"
    expected = round(
        100 * shipped_settings.numerics.bsm.day_count_basis / history.sessions_per_year
    )
    assert chunk == expected < history.chunk_days


@respx.mock
async def test_a_full_probe_is_capped_at_the_configured_chunk(reader, shipped_settings):
    fake = HistoryRoute(first=date(2010, 1, 1), cap=1000)
    _route(respx.mock, fake)
    assert await reader.chunk_days(CONID, exchange=None) == shipped_settings.ibkr.history.chunk_days


@respx.mock
async def test_the_probe_is_remembered_by_the_reader(reader):
    fake = HistoryRoute(first=date(2010, 1, 1), cap=100)
    _route(respx.mock, fake)
    first = await reader.chunk_days(CONID, exchange=None)
    assert await reader.chunk_days(CONID, exchange=None) == first
    assert len(fake.calls) == 1


@respx.mock
async def test_a_refused_probe_falls_back_to_the_configured_chunk(reader, shipped_settings):
    fake = HistoryRoute(first=date(2010, 1, 1), cap=100, refuse_above=200)
    _route(respx.mock, fake)
    assert await reader.chunk_days(CONID, exchange=None) == shipped_settings.ibkr.history.chunk_days


@respx.mock
async def test_an_empty_probe_falls_back_to_the_configured_chunk(reader, shipped_settings):
    respx.mock.get(HISTORY).mock(return_value=httpx.Response(200, json={"data": []}))
    assert await reader.chunk_days(CONID, exchange=None) == shipped_settings.ibkr.history.chunk_days


@respx.mock
async def test_a_route_serving_the_whole_window_needs_no_chunking(reader):
    fake = HistoryRoute(first=date(2010, 1, 1), cap=10_000)
    _route(respx.mock, fake)
    earliest = LATEST - timedelta(days=60)
    history = await reader.daily_between(CONID, earliest=earliest, latest=LATEST)
    walk = [call for call in fake.calls if call.get("startTime") not in (None, RECENT)]
    assert history.bars[0].start.date() <= earliest + timedelta(days=3)
    # The recent window covered it: it ends at `latest`, and the walk asked for at most one more.
    assert fake.calls[0]["startTime"] == RECENT
    assert len(walk) <= 1


@respx.mock
async def test_the_backward_walk_reaches_the_window_in_chunks(reader):
    fake = HistoryRoute(first=date(2010, 1, 1), cap=120)
    _route(respx.mock, fake)
    earliest = LATEST - timedelta(days=700)
    history = await reader.daily_between(CONID, earliest=earliest, latest=LATEST)
    assert history.bars[0].start.date() <= earliest
    assert any("startTime" in call for call in fake.calls)
    days = [bar.start for bar in history.bars]
    assert days == sorted(set(days)), "bars are deduplicated and sorted"


@respx.mock
async def test_the_backward_walk_stops_when_it_stops_making_progress(reader):
    """An instrument whose history begins inside the window: the walk ends, nothing fails."""
    first = LATEST - timedelta(days=200)
    fake = HistoryRoute(first=first, cap=60)
    _route(respx.mock, fake)
    history = await reader.daily_between(
        CONID, earliest=LATEST - timedelta(days=2000), latest=LATEST
    )
    assert history.bars[0].start.date() >= first
    walk = [call for call in fake.calls if "startTime" in call]
    assert len(walk) < 12


@respx.mock
async def test_the_chunk_ceiling_bounds_a_runaway_walk(reader, shipped_settings):
    fake = HistoryRoute(first=date(1990, 1, 1), cap=5)
    _route(respx.mock, fake)
    await reader.daily_between(CONID, earliest=date(1995, 1, 1), latest=LATEST)
    walk = [call for call in fake.calls if call.get("startTime") not in (None, RECENT)]
    assert len(walk) == shipped_settings.ibkr.history.max_chunks


@respx.mock
async def test_a_refused_window_is_asked_again_for_half_of_it(reader):
    """BOVA11 refused 730 and 365 days and served 181 bars for 182 (lite, 2026-09-15)."""
    fake = HistoryRoute(first=date(2010, 1, 1), cap=10_000, refuse_above=200)
    _route(respx.mock, fake)
    history = await reader.daily_between(
        CONID, earliest=LATEST - timedelta(days=730), latest=LATEST
    )
    periods = [call["period"] for call in fake.calls if call.get("startTime") == RECENT]
    assert periods[:3] == ["730d", "365d", "182d"]
    assert history.bars


@respx.mock
async def test_the_step_down_stops_at_the_floor_and_still_fails(reader, shipped_settings):
    floor = shipped_settings.ibkr.history.span_stepdown_min_days
    fake = HistoryRoute(first=date(2010, 1, 1), cap=10_000, refuse_above=floor - 1)
    _route(respx.mock, fake)
    with pytest.raises(NoDataPermanentError):
        await reader.daily_between(CONID, earliest=LATEST - timedelta(days=400), latest=LATEST)
    spans = [int(call["period"].removesuffix("d")) for call in fake.calls]
    assert min(spans) >= floor


@respx.mock
async def test_a_refusal_during_the_backward_walk_stops_it_rather_than_failing(reader):
    refuse_before = LATEST - timedelta(days=100)
    fake = HistoryRoute(first=date(2010, 1, 1), cap=50, refuse_before=refuse_before)
    _route(respx.mock, fake)
    history = await reader.daily_between(
        CONID, earliest=LATEST - timedelta(days=1000), latest=LATEST
    )
    assert history.bars, "the recent bars already collected are kept"


@respx.mock
async def test_a_walk_that_returns_the_same_bars_stops(reader):
    """Whichever way IBKR reads startTime, a chunk that reaches no further back ends the walk."""
    rows = [{"t": _ms(LATEST - timedelta(days=offset)), "c": 1.0} for offset in range(5)]
    route = respx.get(HISTORY).mock(return_value=httpx.Response(200, json={"data": rows}))
    history = await reader.daily_between(
        CONID, earliest=LATEST - timedelta(days=400), latest=LATEST
    )
    assert len(history.bars) == 5
    assert route.call_count == 3, "recent window, probe, one walk step"


@respx.mock
async def test_a_window_ending_in_the_past_is_anchored_at_its_end(reader):
    """Review round 1: the first request had no startTime, so it was anchored at today and
    served bars after ``latest``."""
    fake = HistoryRoute(first=date(2010, 1, 1), cap=10_000)
    _route(respx.mock, fake)
    latest = LATEST - timedelta(days=90)
    earliest = latest - timedelta(days=60)
    history = await reader.daily_between(CONID, earliest=earliest, latest=latest)
    assert fake.calls[0]["startTime"] == start_time(latest)
    assert history.bars
    assert all(earliest <= bar.start.date() <= latest for bar in history.bars)


@respx.mock
async def test_bars_served_outside_the_window_are_left_out(reader):
    """A route that serves more than was asked for adds nothing outside ``[earliest, latest]``."""
    served = [LATEST - timedelta(days=offset) for offset in (0, 5, 10, 400)]
    respx.get(f"{BASE}/iserver/marketdata/history").mock(
        return_value=httpx.Response(
            200, json={"data": [{"t": _ms(day), "c": 1.0} for day in served]}
        )
    )
    latest = LATEST - timedelta(days=5)
    history = await reader.daily_between(CONID, earliest=LATEST - timedelta(days=30), latest=latest)
    assert [bar.start.date() for bar in history.bars] == [
        LATEST - timedelta(days=10),
        latest,
    ]
