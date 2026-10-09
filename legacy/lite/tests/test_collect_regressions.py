"""Regressions for defects found in a correctness pass over this session's code.

Each test here failed before the fix beside it. They are kept together because
what they share is the failure MODE: every one of them was silent — a wrong date,
a dropped row, a run reporting success over nothing. None would have shown up as
an error in a log nobody reads.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
from datetime import date

import httpx
import pytest

from conftest import without_backoff
from obliskiq.collect.chain_collector import ChainCollector
from obliskiq.collect.curve_collector import CurveCollector
from obliskiq.collect.runlog import CollectorRun, RunStatus
from obliskiq.collect.source import FixtureMarketData
from obliskiq.config import Config
from obliskiq.errors import NoMatchingTargets, StoreError
from obliskiq.macro.fetchers import EcbFetcher
from obliskiq.macro.series import POLICY_UNKNOWN, SeriesSpec, SourcePolicy

SESSION = date(2026, 1, 15)


def collect_config(config, **chain_overrides) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    data["collectors"]["chain"].update(chain_overrides)
    return Config(data)


def client_for(payload) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )


# ── 1. SDMX: the time dimension is not reliably last ──


#: An AllDimensions layout — which is what the OECD fetcher used to request —
#: with TIME FIRST of three dimensions. Reading the last field of the key would
#: take the measure index as if it were a date.
#:
#: The OECD fetcher is gone and these three tests are not. They exercise the
#: SDMX decoder the ECB still runs on, and the layouts are properties of SDMX
#: rather than of the publisher that happened to expose them: the next source on
#: this format will serve the same shapes.
SDMX_TIME_FIRST = {
    "dataSets": [{"observations": {"0:0:0": [3.15], "1:0:0": [3.22], "2:0:0": [3.30]}}],
    "structure": {
        "dimensions": {
            "observation": [
                {
                    "id": "TIME_PERIOD",
                    "values": [{"id": "2025-10"}, {"id": "2025-11"}, {"id": "2025-12"}],
                },
                {"id": "REF_AREA", "values": [{"id": "ESP"}]},
                {"id": "MEASURE", "values": [{"id": "LI"}]},
            ]
        }
    },
}


async def test_the_time_dimension_is_read_from_its_actual_position(config):
    """Assuming time comes last attaches every value to the wrong date — and the
    dates it produces are real dates, so nothing downstream can tell."""
    fetcher = EcbFetcher(without_backoff(config), client_for(SDMX_TIME_FIRST))
    spec = SeriesSpec(id="X", source="ecb", frequency="monthly", group="euro", key="A/B")
    rows = await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION)

    assert [(r.reference_date, r.value) for r in rows] == [
        ("2025-10-01", 3.15),
        ("2025-11-01", 3.22),
        ("2025-12-01", 3.30),
    ]


async def test_an_observation_key_shorter_than_the_time_position_is_dropped(config):
    """Dropped, not positioned by guesswork: an observation on the wrong date is
    worse than a missing one."""
    payload = copy.deepcopy(SDMX_TIME_FIRST)
    payload["structure"]["dimensions"]["observation"].insert(
        0, {"id": "FREQ", "values": [{"id": "M"}]}
    )
    payload["dataSets"][0]["observations"] = {"0": [1.0]}  # too short to reach TIME
    fetcher = EcbFetcher(without_backoff(config), client_for(payload))
    spec = SeriesSpec(id="X", source="ecb", frequency="monthly", group="euro", key="A/B")
    assert (
        await fetcher.fetch(spec, SourcePolicy("ecb", True, POLICY_UNKNOWN, 0), start=SESSION) == []
    )


async def test_several_series_for_one_key_warns_instead_of_merging(config, caplog):
    """Merging them would present one series' numbers under another's id."""
    payload = {
        "dataSets": [
            {
                "series": {
                    "0:0": {"observations": {"0": [1.0], "1": [2.0]}},
                    "0:1": {"observations": {"0": [90.0], "1": [91.0]}},
                }
            }
        ],
        "structure": {
            "dimensions": {
                "observation": [
                    {"id": "TIME_PERIOD", "values": [{"id": "2025-11"}, {"id": "2025-12"}]}
                ]
            }
        },
    }
    fetcher = EcbFetcher(without_backoff(config), client_for(payload))
    spec = SeriesSpec(id="X", source="ecb", frequency="monthly", group="euro", key="A/B")

    with caplog.at_level("WARNING"):
        policy = SourcePolicy("ecb", True, POLICY_UNKNOWN, 0)
        rows = await fetcher.fetch(spec, policy, start=SESSION)

    assert [r.value for r in rows] == [1.0, 2.0], "only the first series is used"
    assert any(
        "under-specified" in record.message or "several series" in record.message
        for record in caplog.records
    )


# ── 2. chain: a strike that round-trips differently must not vanish ──


class EchoesADifferentStrike(FixtureMarketData):
    """Returns contracts whose echoed strike differs in the last float bit.

    A real endpoint can round-trip 600.0 as 600.00000000000006. Keying on the echo
    made the row fail the wanted-set test and disappear with no error at all.
    """

    async def option_contracts(self, conid, *, month, exchange, strike, right):
        rows = await super().option_contracts(
            conid, month=month, exchange=exchange, strike=strike, right=right
        )
        return [{**row, "strike": row["strike"] + 1e-9} for row in rows]


async def test_a_strike_echoed_with_float_noise_is_still_recorded(
    seeded_universe, config, fixtures_dir
):
    seeded = EchoesADifferentStrike(fixtures_dir)
    with CollectorRun(seeded_universe, "chain") as run:
        await ChainCollector(seeded_universe, collect_config(config), seeded).run(
            run, only=["FCX"], today=SESSION
        )

    stored = seeded_universe.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0]
    assert stored > 0, "the rows were silently dropped by the strike-key mismatch"
    assert run.targets_failed == 0


# ── 3. the expiry cap is per underlying, not per month ──


class TwoExpiriesInTheSecondMonth(FixtureMarketData):
    """APR26 lists one expiry, JUN26 lists two.

    The old code capped each month at the full budget and only checked the total
    afterwards, so a month that came in under budget let the next one overshoot.
    """

    async def option_contracts(self, conid, *, month, exchange, strike, right):
        rows = await super().option_contracts(
            conid, month=month, exchange=exchange, strike=strike, right=right
        )
        if month == "JUN26":
            rows = rows + [
                {**row, "maturityDate": "20260626", "conid": row["conid"] + 500_000} for row in rows
            ]
        return rows


async def test_the_expiry_budget_is_spent_across_months_not_per_month(
    seeded_universe, config, fixtures_dir
):
    md = TwoExpiriesInTheSecondMonth(fixtures_dir)
    cfg = collect_config(config, max_expiries_per_underlying=2)
    with CollectorRun(seeded_universe, "chain") as run:
        await ChainCollector(seeded_universe, cfg, md).run(run, only=["FCX"], today=SESSION)

    expiries = {
        row[0] for row in seeded_universe.execute("SELECT DISTINCT expiry FROM chain_snapshots")
    }
    assert len(expiries) == 2, f"budget of 2 overspent to {len(expiries)}: {sorted(expiries)}"


# ── 4. curve: no roots is a failure, not a clean run ──


async def test_a_curve_run_with_no_tracked_roots_refuses_rather_than_reporting_success(
    seeded_universe, config, market_data
):
    """The exact 'success over nothing' failure the rest of the package guards
    against. Curve shape cannot be recovered later, so a quiet zero is the most
    expensive outcome available."""
    with seeded_universe.transaction() as connection:
        connection.execute("UPDATE futures_roots SET tracked = 0")

    with (
        pytest.raises(StoreError, match="obliskiq-collect universe"),
        CollectorRun(seeded_universe, "curve") as run,
    ):
        await CurveCollector(seeded_universe, collect_config(config), market_data).run(
            run, today=SESSION
        )


async def test_an_only_filter_matching_no_tracked_root_says_so(
    seeded_universe, config, market_data
):
    with (
        pytest.raises(NoMatchingTargets, match="none of the requested roots"),
        CollectorRun(seeded_universe, "curve") as run,
    ):
        await CurveCollector(seeded_universe, collect_config(config), market_data).run(
            run, only=["NOSUCHROOT"], today=SESSION
        )


async def test_the_two_empty_cases_are_different_kinds_of_empty(
    seeded_universe, config, market_data
):
    """An unfiltered empty table stays a hard failure everywhere; a filter naming
    none of the targets is a narrower thing. Only the second is recoverable inside
    the daily sequence, so they must not collapse into one exception type — but
    NoMatchingTargets stays a StoreError so any existing handler still catches it.
    """
    assert issubclass(NoMatchingTargets, StoreError)

    with seeded_universe.transaction() as connection:
        connection.execute("UPDATE futures_roots SET tracked = 0")

    with (
        pytest.raises(StoreError) as caught,
        CollectorRun(seeded_universe, "curve") as run,
    ):
        await CurveCollector(seeded_universe, collect_config(config), market_data).run(
            run, today=SESSION
        )
    assert not isinstance(caught.value, NoMatchingTargets)


async def test_a_skipped_step_is_not_logged_as_a_failed_one(seeded_universe, config, market_data):
    """The run log is what an operator reads to answer "did the curve collector
    fail last night". Curve shape cannot be recovered later, so that alert has to
    stay trustworthy: a manual run scoped with --only must not leave an 'error'
    row behind. It must not leave an 'ok' one either — nothing was collected.
    """
    with pytest.raises(NoMatchingTargets), CollectorRun(seeded_universe, "curve") as run:
        await CurveCollector(seeded_universe, collect_config(config), market_data).run(
            run, only=["NOSUCHROOT"], today=SESSION
        )

    row = seeded_universe.execute(
        "SELECT status, rows_written FROM collector_runs "
        "WHERE collector = 'curve' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == RunStatus.SKIPPED
    assert row["rows_written"] == 0


async def test_every_collector_refuses_a_filter_that_names_none_of_its_targets(
    seeded_universe, config, market_data
):
    """The guard was on the curve collector alone. chain and macro both reported
    'ok' over zero rows for a filter that matched nothing — the same
    success-over-nothing failure, in the two collectors whose data is the
    irreplaceable part. Consistency here is the point: all three now raise, and
    the daily sequence steps over all three the same way.
    """
    from obliskiq.collect.chain_collector import chain_targets

    with pytest.raises(NoMatchingTargets, match="none of the requested symbols"):
        chain_targets(seeded_universe, only=["NOSUCHSYMBOL"])

    # An unfiltered call is untouched: an empty universe is a different statement
    # from a filter naming nothing, and only the filtered case is recoverable.
    assert chain_targets(seeded_universe) != []


# ── 5. backfill: the walk starts from the session, not the wall clock ──


class NoRecentBarsOnly:
    """Returns nothing for the recent fetch, so the backward walk falls back to
    its starting cursor — which must be the session date it was told about."""

    source = "cp_api"

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def bars(self, conid, *, bar, period, exchange=None, start_time=None):
        self.requests.append({"period": period, "start_time": start_time})
        return []

    async def resolve_conid(self, symbol, *, sec_type="STK"):
        return None

    async def quotes(self, conids):
        return {}

    async def portfolio_positions(self):
        return []


async def test_the_backward_walk_starts_from_the_session_not_today(seeded_universe, config):
    """A run told which day it is collecting must not silently walk back from a
    different one — otherwise a backfill replayed for a past date quietly asks for
    the wrong window."""
    from obliskiq.collect.backfill import Backfill
    from obliskiq.collect.vol import FREQ_DAILY

    with seeded_universe.transaction() as connection:
        connection.execute("UPDATE universe SET collect_bars = 0")
        connection.execute("UPDATE universe SET collect_bars = 1 WHERE symbol = 'SPY'")

    past = date(2025, 6, 30)
    md = NoRecentBarsOnly()
    cfg = collect_config(config)
    with CollectorRun(seeded_universe, "backfill") as run:
        await Backfill(seeded_universe, cfg, md).run(run, frequencies=[FREQ_DAILY], today=past)

    walks = [r for r in md.requests if r["start_time"]]
    assert walks, "the walk should have started"
    assert walks[0]["start_time"].startswith(past.strftime("%Y%m%d"))
    assert past != date.today(), "the fixture session must differ from today for this to bite"


# ── 6. no production assert survives python -O ──


def test_no_load_bearing_assert_remains_in_the_package(repo_root):
    """`python -O` strips asserts. An unattended collector run that way would lose
    a control-flow guard and fail in a way nobody could read."""
    import ast

    offenders: list[str] = []
    for path in sorted((repo_root / "src" / "obliskiq").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}")
    assert not offenders, f"asserts in production code: {offenders}"


# ── 7. the realised-vol estimator ──


def test_realised_vol_uses_the_sample_standard_deviation():
    """ddof=1. With a 30-bar window the population estimator understates vol by
    about 1.7%, which is small, systematic, and lands straight in the strike band."""
    import math
    import statistics

    from obliskiq.collect.vol import realised_vol

    closes = [100.0 * math.exp(0.01 * ((-1) ** i)) for i in range(31)]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    got = realised_vol(closes, tenor_days=30, trading_days_per_year=252.0)
    assert got == pytest.approx(statistics.stdev(returns) * math.sqrt(252.0))
    assert got != pytest.approx(statistics.pstdev(returns) * math.sqrt(252.0))
