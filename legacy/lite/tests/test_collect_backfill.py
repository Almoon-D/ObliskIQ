"""Backfill: watermarks, gaps, and the probed daily cap.

Bars ARE retrievable retrospectively, so a lagging backfill is recoverable. What
is not recoverable is a watermark that lies — it either re-fetches history
forever, or claims a span nobody holds and hides a hole from every percentile
built on it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from obliskiq.collect.backfill import Backfill, bar_targets, read_watermark
from obliskiq.collect.runlog import CollectorRun, RunStatus
from obliskiq.collect.source import Bar
from obliskiq.collect.state import KEY_DAILY_BAR_CAP, get_state, set_state
from obliskiq.collect.vol import FREQ_DAILY, FREQ_MONTHLY, FREQ_WEEKLY
from obliskiq.config import Config
from obliskiq.errors import PermanentUpstreamError

SESSION = date(2026, 1, 15)


def config_for(config, **backfill_overrides) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    data["collectors"]["backfill"].update(backfill_overrides)
    return Config(data)


#: Bars one mock call returns. A real endpoint caps its response, which is exactly
#: what the backfill probes for rather than assuming.
MOCK_BARS_PER_CALL = 250


class RecordingMarketData:
    """A MarketData that serves a bounded window and records every request.

    Behaviour under test is "which requests were issued", which a fixture that
    always returns the same thing cannot show. ``bars_per_call`` simulates the
    per-call cap: without one a single request would cover any window and the
    chunking would never run.
    """

    source = "cp_api"

    def __init__(
        self,
        *,
        earliest: date,
        latest: date,
        gap: tuple[date, date] | None = None,
        bars_per_call: int = MOCK_BARS_PER_CALL,
    ):
        self.requests: list[dict] = []
        self._earliest, self._latest, self._gap = earliest, latest, gap
        self._bars_per_call = bars_per_call

    async def bars(self, conid, *, bar, period, exchange=None, start_time=None):
        self.requests.append(
            {"conid": conid, "bar": bar, "period": period, "start_time": start_time}
        )
        span = min(int("".join(ch for ch in period if ch.isdigit()) or 0), self._bars_per_call)
        end = self._latest
        if start_time:
            end = min(end, date(int(start_time[:4]), int(start_time[4:6]), int(start_time[6:8])))
        out, day = [], end
        while len(out) < span and day >= self._earliest:
            in_gap = self._gap is not None and self._gap[0] <= day <= self._gap[1]
            if day.weekday() < 5 and not in_gap:
                out.append(
                    Bar(
                        ts=f"{day.isoformat()}T00:00:00Z",
                        open=1.0,
                        high=1.0,
                        low=1.0,
                        close=100.0,
                        volume=1.0,
                    )
                )
            day -= timedelta(days=1)
        return sorted(out, key=lambda b: b.ts)

    async def resolve_conid(self, symbol, *, sec_type="STK"):
        return None

    async def quotes(self, conids):
        return {}

    async def portfolio_positions(self):
        return []


async def run_backfill(store, cfg, market_data, *, only=None, frequencies=None, session=SESSION):
    with CollectorRun(store, "backfill") as run:
        await Backfill(store, cfg, market_data).run(
            run, only=only, frequencies=frequencies, today=session
        )
    return run


@pytest.fixture()
def one_target(seeded_universe):
    """A single collectable contract, so request counts are unambiguous."""
    with seeded_universe.transaction() as connection:
        connection.execute("UPDATE universe SET collect_bars = 0")
        connection.execute("UPDATE universe SET collect_bars = 1 WHERE symbol = 'SPY'")
    return seeded_universe


# ── watermarks ──


async def test_bars_are_written_and_the_watermark_recorded(one_target, config):
    md = RecordingMarketData(earliest=SESSION - timedelta(days=400), latest=SESSION)
    await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_WEEKLY])

    mark = read_watermark(one_target, 756733, FREQ_WEEKLY)
    assert not mark.is_empty
    assert mark.latest <= SESSION
    assert (
        one_target.execute("SELECT COUNT(*) FROM bars WHERE freq = ?", (FREQ_WEEKLY,)).fetchone()[0]
        > 0
    )


async def test_the_watermark_is_derived_from_the_table_not_from_the_request(one_target, config):
    """A watermark computed from what was ASKED for would claim a span nobody
    holds and paper over a gap in the middle of the series."""
    md = RecordingMarketData(earliest=SESSION - timedelta(days=100), latest=SESSION)
    await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_WEEKLY])

    mark = read_watermark(one_target, 756733, FREQ_WEEKLY)
    stored = one_target.execute(
        "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM bars WHERE contract_id = 756733 AND freq = ?",
        (FREQ_WEEKLY,),
    ).fetchone()
    assert mark.earliest.isoformat() == stored["lo"][:10]
    assert mark.latest.isoformat() == stored["hi"][:10]


async def test_an_incremental_run_asks_for_a_short_window_not_the_whole_history(one_target, config):
    """'NEVER re-fetch history already held.' A second run must not re-request the
    window it already has."""
    cfg = config_for(config)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=800), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_WEEKLY])
    first_span = _span(md.requests[0]["period"])

    md.requests.clear()
    await run_backfill(
        one_target, cfg, md, frequencies=[FREQ_WEEKLY], session=SESSION + timedelta(days=3)
    )
    assert md.requests
    assert _span(md.requests[0]["period"]) < first_span


async def test_a_series_already_covering_the_window_issues_no_historical_request(
    one_target, config
):
    cfg = config_for(config)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=900), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])

    assert any(request["start_time"] for request in md.requests), "the first run must walk back"

    md.requests.clear()
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])
    # The window is held now: only the short recent-end refresh, no backward walk.
    assert md.requests
    assert all(request["start_time"] is None for request in md.requests)


async def test_the_overlap_corrects_a_partial_final_bar_without_refetching_history(
    one_target, config
):
    overlap = config.require_int("collectors.backfill.watermark_overlap_days")
    cfg = config_for(config)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=800), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_WEEKLY])

    md.requests.clear()
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_WEEKLY], session=SESSION)
    # A same-day re-run still asks for at least the overlap, and nothing like the
    # original window.
    assert _span(md.requests[0]["period"]) >= 1
    assert overlap > 0


# ── gaps ──


async def test_a_gap_in_the_series_does_not_abort_the_run_or_reset_the_watermark(
    one_target, config
):
    """The machine or the API will miss days. The watermark records what is HELD,
    not an assumption that the series is unbroken."""
    gap = (SESSION - timedelta(days=40), SESSION - timedelta(days=30))
    md = RecordingMarketData(earliest=SESSION - timedelta(days=200), latest=SESSION, gap=gap)
    run = await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_DAILY])

    assert run.status() == RunStatus.OK
    mark = read_watermark(one_target, 756733, FREQ_DAILY)
    assert mark.earliest < gap[0] and mark.latest > gap[1]
    missing = one_target.execute(
        "SELECT COUNT(*) FROM bars WHERE contract_id = 756733 AND freq = ? AND ts >= ? AND ts <= ?",
        (FREQ_DAILY, f"{gap[0].isoformat()}T00:00:00Z", f"{gap[1].isoformat()}T23:59:59Z"),
    ).fetchone()[0]
    assert missing == 0


# ── the probed daily cap ──


async def test_the_daily_cap_is_probed_rather_than_assumed(one_target, config):
    """ibkr.cp_api.history.max_bars_per_request is documented and UNVERIFIED, so
    the real figure is measured."""
    cfg = config_for(config)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=900), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])

    probed = get_state(one_target, KEY_DAILY_BAR_CAP)
    assert probed is not None
    assert probed["bars"] > 0
    assert probed["chunk_days"] > 0
    assert probed["probed_with"] == "SPY"


async def test_a_truncated_response_narrows_the_chunk(one_target, config):
    """If the endpoint returns fewer bars than the span implies, the chunk shrinks
    to what actually comes back."""
    cfg = config_for(config, probe_request_days=400, daily_chunk_days=365)
    tight = RecordingMarketData(
        earliest=SESSION - timedelta(days=3000), latest=SESSION, bars_per_call=30
    )
    await run_backfill(one_target, cfg, tight, frequencies=[FREQ_DAILY])

    probed = get_state(one_target, KEY_DAILY_BAR_CAP)
    assert probed["bars"] == 30
    assert probed["chunk_days"] < cfg.require_int("collectors.backfill.daily_chunk_days")


async def test_the_probe_is_remembered_between_runs(one_target, config):
    """Measuring it on every run would waste a request a day forever."""
    cfg = config_for(config)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=900), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])
    first = get_state(one_target, KEY_DAILY_BAR_CAP)

    set_state(one_target, KEY_DAILY_BAR_CAP, {**first, "chunk_days": 7})
    md.requests.clear()
    with one_target.transaction() as connection:
        connection.execute("DELETE FROM bars")
        connection.execute("DELETE FROM bar_watermarks")
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])
    assert get_state(one_target, KEY_DAILY_BAR_CAP)["chunk_days"] == 7


async def test_probing_can_be_switched_off(one_target, config):
    cfg = config_for(config, probe_daily_cap=False)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=900), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])
    assert get_state(one_target, KEY_DAILY_BAR_CAP) is None


async def test_the_backward_walk_stops_when_it_stops_making_progress(one_target, config):
    """Progress is judged by whether the earliest bar actually moved, not by trusting
    a request parameter — which is what makes the loop correct whichever way the
    endpoint reads startTime."""
    cfg = config_for(config, max_chunks_per_contract=20)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=120), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])

    # Far fewer than the chunk ceiling: the walk gave up once history ran out.
    walks = [request for request in md.requests if request["start_time"]]
    assert len(walks) < cfg.require_int("collectors.backfill.max_chunks_per_contract")


async def test_the_chunk_ceiling_bounds_a_runaway_walk(one_target, config):
    cfg = config_for(config, max_chunks_per_contract=2, probe_daily_cap=False, daily_chunk_days=5)
    md = RecordingMarketData(earliest=date(1990, 1, 1), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])
    walks = [request for request in md.requests if request["start_time"]]
    assert len(walks) <= 2


# ── frequency selection ──


async def test_monthly_history_is_for_the_commodity_complex_only(seeded_universe, config):
    """Twenty years of monthly bars places a commodity in its cycle. It buys nothing
    on a single equity."""
    md = RecordingMarketData(earliest=SESSION - timedelta(days=8000), latest=SESSION)
    await run_backfill(seeded_universe, config_for(config), md, frequencies=[FREQ_MONTHLY])

    collected = {
        row[0]
        for row in seeded_universe.execute(
            "SELECT DISTINCT u.sector FROM bars b JOIN universe u ON u.contract_id = b.contract_id "
            "WHERE b.freq = ?",
            (FREQ_MONTHLY,),
        )
    }
    buckets = set(config.require_list("commodities.buckets"))
    assert collected and collected <= buckets


async def test_monthly_can_be_widened_to_everything(seeded_universe, config):
    md = RecordingMarketData(earliest=SESSION - timedelta(days=8000), latest=SESSION)
    await run_backfill(
        seeded_universe,
        config_for(config, monthly_roots_only=False),
        md,
        frequencies=[FREQ_MONTHLY],
    )
    sectors = {
        row[0]
        for row in seeded_universe.execute(
            "SELECT DISTINCT u.sector FROM bars b JOIN universe u ON u.contract_id = b.contract_id "
            "WHERE b.freq = ?",
            (FREQ_MONTHLY,),
        )
    }
    assert not sectors <= set(config.require_list("commodities.buckets"))


async def test_weekly_comes_back_in_a_single_call(one_target, config):
    """step=ONE_WEEK, step_count=260 returns 5+ years in one call. Confirmed
    behaviour, and the reason weekly is not chunked."""
    md = RecordingMarketData(earliest=SESSION - timedelta(days=2000), latest=SESSION)
    await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_WEEKLY])
    assert len(md.requests) == 1
    assert md.requests[0]["period"].endswith("y")


async def test_the_weekly_window_comes_from_config(one_target, config):
    md = RecordingMarketData(earliest=SESSION - timedelta(days=4000), latest=SESSION)
    await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_WEEKLY])
    assert md.requests[0]["period"] == f"{config.require_int('data.weekly_window_years')}y"


# ── idempotency, provenance, partial runs ──


async def test_re_running_writes_no_duplicate_bars(one_target, config):
    cfg = config_for(config)
    md = RecordingMarketData(earliest=SESSION - timedelta(days=300), latest=SESSION)
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])
    before = one_target.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    await run_backfill(one_target, cfg, md, frequencies=[FREQ_DAILY])
    assert one_target.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == before


async def test_the_collection_route_is_recorded(one_target, config):
    md = RecordingMarketData(earliest=SESSION - timedelta(days=100), latest=SESSION)
    await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_WEEKLY])
    assert {row[0] for row in one_target.execute("SELECT DISTINCT source FROM bars")} == {"cp_api"}


async def test_one_failing_contract_does_not_kill_the_run(seeded_universe, config):
    class HalfBroken(RecordingMarketData):
        async def bars(self, conid, *, bar, period, exchange=None, start_time=None):
            if conid == 756733:
                raise RuntimeError("no market data permissions for this contract")
            return await super().bars(
                conid, bar=bar, period=period, exchange=exchange, start_time=start_time
            )

    md = HalfBroken(earliest=SESSION - timedelta(days=100), latest=SESSION)
    run = await run_backfill(seeded_universe, config_for(config), md, frequencies=[FREQ_WEEKLY])

    assert run.status() == RunStatus.PARTIAL
    assert run.targets_failed >= 1 and run.targets_ok >= 1
    assert seeded_universe.execute("SELECT COUNT(*) FROM bars").fetchone()[0] > 0


class ShortHistory(RecordingMarketData):
    """A route that REFUSES a window longer than the instrument has.

    This is what `iserver/marketdata/history` really does — measured 2026-09-15
    on BOVA11, which measures brazil/broad: HTTP 500 "Chart data unavailable" for
    730d and 365d, 181 bars for 182d, identically with the exchange on the
    request and with it omitted. It does not return a short series; it refuses.
    """

    def __init__(self, *, serves_up_to_days: int, **kwargs):
        super().__init__(**kwargs)
        self._serves_up_to = serves_up_to_days
        self.refused: list[str] = []

    async def bars(self, conid, *, bar, period, exchange=None, start_time=None):
        if _span(period) > self._serves_up_to:
            self.refused.append(period)
            raise PermanentUpstreamError(
                "GET iserver/marketdata/history returned HTTP 500: "
                '{"error":"Chart data unavailable"}'
            )
        return await super().bars(
            conid, bar=bar, period=period, exchange=exchange, start_time=start_time
        )


async def test_a_refused_window_is_asked_again_for_half_of_it(one_target, config):
    """An instrument with less history than the window used to collect NOTHING.

    `_collect` opens by asking for the whole window in one call, so a refusal
    failed the target outright and the nightly run reported `partial` for it
    forever. Halving finds what the route will serve without any per-instrument
    declaration.
    """
    md = ShortHistory(serves_up_to_days=200, earliest=SESSION - timedelta(days=400), latest=SESSION)
    run = await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_DAILY])

    assert md.refused, "the fixture never refused anything, so nothing was under test"
    assert run.targets_failed == 0, "a short-history instrument still failed its target"
    assert one_target.execute("SELECT COUNT(*) FROM bars").fetchone()[0] > 0


async def test_the_step_down_stops_at_the_configured_floor_and_still_fails(one_target, config):
    """Widening what can be collected must not hide a target that cannot be.

    A route refusing even the floor is a real failure and has to surface as one,
    rather than being walked down to a single session until something answers.
    """
    floor = config.require_int("collectors.backfill.span_stepdown_min_days")
    md = ShortHistory(serves_up_to_days=1, earliest=SESSION - timedelta(days=400), latest=SESSION)
    run = await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_DAILY])

    assert run.targets_failed >= 1
    assert md.refused, "nothing was refused"
    assert all(_span(period) >= floor for period in md.refused), (
        f"the walk went below the {floor}-day floor: {md.refused}"
    )


async def test_a_refusal_during_the_backward_walk_stops_it_rather_than_failing(one_target, config):
    """Reaching past the start of an instrument's history is not a failure.

    The backward walk is speculative by construction — it asks for history that
    may simply not exist — and it already treats an EMPTY chunk as the signal to
    stop. The route answers "before this instrument existed" with HTTP 500
    instead, and letting that propagate threw away the recent bars the same run
    had already collected. Measured 2026-09-15 on BOVA11: it serves its recent
    window and refuses every span beyond it, and the target collected nothing.
    """

    class RefusesHistoryBeforeAPoint(RecordingMarketData):
        async def bars(self, conid, *, bar, period, exchange=None, start_time=None):
            if start_time is not None:
                raise PermanentUpstreamError(
                    "GET iserver/marketdata/history returned HTTP 500: "
                    '{"error":"Chart data unavailable"}'
                )
            return await super().bars(
                conid, bar=bar, period=period, exchange=exchange, start_time=start_time
            )

    md = RefusesHistoryBeforeAPoint(
        earliest=SESSION - timedelta(days=400), latest=SESSION, bars_per_call=60
    )
    run = await run_backfill(one_target, config_for(config), md, frequencies=[FREQ_DAILY])

    assert run.targets_failed == 0, (
        "a refusal while reaching for history that is not there failed the target"
    )
    assert one_target.execute("SELECT COUNT(*) FROM bars").fetchone()[0] > 0, (
        "the bars the recent window DID return were thrown away"
    )


def test_only_flagged_contracts_are_walked(seeded_universe):
    with seeded_universe.transaction() as connection:
        connection.execute("UPDATE universe SET collect_bars = 0 WHERE symbol = 'SPY'")
    assert "SPY" not in {t.symbol for t in bar_targets(seeded_universe)}


def _span(period: str) -> int:
    return int("".join(ch for ch in period if ch.isdigit()))


# ── a route that serves the whole window in one call ──


async def test_a_route_serving_the_whole_window_needs_no_chunking(one_target, config):
    """The Custom Connector returns the full daily window in a single response.

    Verified live 2026-09-03: one `period=TWO_YEARS`, `step=ONE_DAY` request came
    back with the entire two years, which is `data.daily_window_years`. The
    chunking loop and the probed per-call cap exist for a route that caps its
    response; against one that does not, they must quietly not engage rather than
    issuing a walk that re-fetches the same bars.
    """
    cfg = config_for(config, probe_daily_cap=False)
    years = cfg.require_int("data.daily_window_years")
    earliest = SESSION - timedelta(days=round(years * 365.25) + 30)
    # bars_per_call high enough that the window is never what bounds a response.
    market_data = RecordingMarketData(earliest=earliest, latest=SESSION, bars_per_call=10_000)
    await run_backfill(one_target, cfg, market_data, frequencies=[FREQ_DAILY])

    daily = [r for r in market_data.requests if r["bar"] == "1d"]
    # One request fetches the window; the backward walk asks once more, sees the
    # earliest bar has not moved, and stops. It never walks chunk by chunk.
    assert len(daily) <= 2, daily
    assert daily[0]["start_time"] is None

    with one_target.transaction() as connection:
        oldest, newest, count = connection.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM bars WHERE freq = '1d'"
        ).fetchone()
    # The whole window landed from those requests, not a chunk of it. The mock
    # serves weekdays only, so the oldest bar is the first trading day on or after
    # the window's start rather than the start itself.
    assert date.fromisoformat(oldest[:10]) - earliest < timedelta(days=4)
    assert newest[:10] == SESSION.isoformat()
    assert count > years * 200
