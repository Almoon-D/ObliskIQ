"""A cell must name the sample actually behind it, not the one it asked for.

Several families apply a floor that admits a much shorter sample than the window
they advertise. ``realised_vol_30d_percentile`` was stamped with a multi-year
span while resting on as few as twenty-one overlapping readings, and
``correlation_250d`` is admitted at half its window and was still labelled
"250d" — while ``rankings.py`` requires the FULL window for the same pair, so
the grid and the correlation matrix could disagree under one name with nothing
recorded to explain why.

``Cell.n_obs`` carries the count, and the window label repeats it in prose.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from obliskiq.compute.indicators import (
    STATUS_OK,
    IndicatorInputs,
    correlation_to_benchmark,
)
from obliskiq.compute.indicators import (
    realised_vol as realised_vol_family,
)

AS_OF = date(2026, 3, 2)
CONTRACT_ID = 1
BENCHMARK_ID = 2


def _universe(store):
    # One grid_measures row per (market, sector): the benchmark measures a
    # different cell, so it gets its own.
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, grid_measures) VALUES (?, ?, 'USD', 'ETF', ?, ?, 1)",
            [
                (CONTRACT_ID, "SPY", "us", "broad"),
                (BENCHMARK_ID, "ACWI", "world", "broad"),
            ],
        )


def _bars(store, contract_id, count, *, seed=0.0):
    """`count` daily closes on a deterministic wobble, oldest first."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) "
            "VALUES (?, ?, '1d', ?, 'cp_api')",
            [
                (
                    contract_id,
                    (AS_OF - timedelta(days=count - n)).isoformat() + "T00:00:00Z",
                    100.0 * math.exp(0.01 * math.sin(n * 0.7 + seed)),
                )
                for n in range(count)
            ],
        )


def _ohlc_bars(store, contract_id, count, *, seed=0.0):
    """The same wobble, with a genuine open and a real intraday range."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, source) "
            "VALUES (?, ?, '1d', ?, ?, ?, ?, 'cp_api')",
            [
                (
                    contract_id,
                    (AS_OF - timedelta(days=count - n)).isoformat() + "T00:00:00Z",
                    100.0 * math.exp(0.01 * math.sin(n * 0.7 + seed - 0.3)),
                    100.0 * math.exp(0.01 * math.sin(n * 0.7 + seed) + 0.004),
                    100.0 * math.exp(0.01 * math.sin(n * 0.7 + seed) - 0.004),
                    100.0 * math.exp(0.01 * math.sin(n * 0.7 + seed)),
                )
                for n in range(count)
            ],
        )


def _inputs(store, config, *, benchmark_contract_id=None):
    return IndicatorInputs.bind(
        store,
        config,
        as_of=AS_OF,
        market="us",
        sector="broad",
        contract_id=CONTRACT_ID,
        symbol="SPY",
        market_spec={"currency": "USD"},
        benchmark_contract_id=benchmark_contract_id,
        benchmark_market="us",
    )


#: Enough daily bars to clear grid.realised_vol_percentile_min_independent at
#: the 30-day reference tenor, and nowhere near the advertised two-year span.
#: Independent readings cost bars at thirty times the rate overlapping ones did,
#: which is the whole point: seventy bars used to buy forty "readings" and now
#: buy one.
BARS_FOR_AN_INDEPENDENT_PERCENTILE = 300


def test_a_realised_vol_percentile_names_the_readings_behind_it(store, config):
    """Enough bars to clear the floor, nowhere near the advertised span."""
    _universe(store)
    _bars(store, CONTRACT_ID, BARS_FOR_AN_INDEPENDENT_PERCENTILE)

    cells = {c.indicator: c for c in realised_vol_family(_inputs(store, config))}
    cell = cells.get("realised_vol_30d_percentile")
    assert cell is not None and cell.status == STATUS_OK

    assert cell.n_obs is not None and cell.n_obs > 0
    assert f"{cell.n_obs} readings" in (cell.window or ""), cell.window
    # The count is far below what the advertised span would hold, and the label
    # now says so rather than implying a full history.
    assert cell.n_obs < 252


def test_a_range_vol_percentile_names_the_readings_behind_it_too(store, config):
    """The new leaves obey the same rule as the one they sit beside.

    A window label is a claim about what the number was computed on, and a
    percentile admitted below its advertised span has to say so whichever
    estimator produced it. A range estimator reads one term per bar where a
    return needs two prices, so on a given history it fits at most one more
    independent window than the close-to-close leaf — never fewer, and never
    more than one more. That is the label doing its job, not a discrepancy.
    """
    _universe(store)
    _ohlc_bars(store, CONTRACT_ID, BARS_FOR_AN_INDEPENDENT_PERCENTILE)

    cells = {c.indicator: c for c in realised_vol_family(_inputs(store, config))}
    close_to_close = cells["realised_vol_30d_percentile"]
    for name in ("parkinson_vol_30d_percentile", "garman_klass_vol_30d_percentile"):
        cell = cells.get(name)
        assert cell is not None and cell.status == STATUS_OK, cell
        assert cell.n_obs is not None and cell.n_obs > 0
        assert f"{cell.n_obs} readings" in (cell.window or ""), cell.window
        assert cell.n_obs < 252
        assert close_to_close.n_obs <= cell.n_obs <= close_to_close.n_obs + 1


def test_a_vol_percentile_counts_readings_that_share_no_bars(store, config):
    """The count is the evidence claim, and it used to overstate it thirtyfold.

    Sampled every session, a 30-day reading and the next share 29 bars, so 300
    bars produced 270 "readings" resting on ten independent windows. What n_obs
    reports now is those windows: the number of times the tenor fits in the
    history, give or take the boundary bar the two shapes differ over.
    """
    _universe(store)
    _ohlc_bars(store, CONTRACT_ID, BARS_FOR_AN_INDEPENDENT_PERCENTILE)
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")

    cells = {c.indicator: c for c in realised_vol_family(_inputs(store, config))}
    independent = BARS_FOR_AN_INDEPENDENT_PERCENTILE // tenor
    for name in (
        "realised_vol_30d_percentile",
        "parkinson_vol_30d_percentile",
        "garman_klass_vol_30d_percentile",
    ):
        cell = cells[name]
        assert cell.status == STATUS_OK, cell
        assert independent - 1 <= cell.n_obs <= independent, (name, cell.n_obs)


def test_a_percentile_below_the_independent_floor_says_which_floor(store, config):
    """Seventy bars is not a two-year percentile, whatever the overlap made it look.

    The refusal names the SAMPLING as well as the count, because "only 2
    readings" on a store holding seventy bars reads as a collection failure and
    is not one — the bars are there and two of their windows share no data.
    """
    _universe(store)
    _ohlc_bars(store, CONTRACT_ID, 70)

    cells = {c.indicator: c for c in realised_vol_family(_inputs(store, config))}
    for name in (
        "realised_vol_30d_percentile",
        "parkinson_vol_30d_percentile",
        "garman_klass_vol_30d_percentile",
    ):
        cell = cells[name]
        assert cell.status != STATUS_OK
        assert "NON-OVERLAPPING" in (cell.reason or ""), cell.reason
        assert "grid.realised_vol_percentile_min_independent" in (cell.reason or "")
        # The LEVEL still computes: this refuses a ranking, not a measurement.
        assert cells[name.removesuffix("_percentile")].status == STATUS_OK


def test_the_history_is_anchored_on_the_newest_reading():
    """Walking forwards would drop the present, which is what is being ranked.

    A stride that does not divide the history has to leave a remainder
    somewhere. Leaving it at the OLD end costs the oldest window; leaving it at
    the new end costs the window the percentile exists to place, and nothing in
    the output would say so.
    """
    from obliskiq.compute.indicators import _rolling

    readings = _rolling(list(range(10)), 3, lambda w: float(w[-1]), step=3)
    assert readings == [3.0, 6.0, 9.0], "the last window ends on the newest item"


def test_a_close_only_series_refuses_the_range_leaves_and_says_which_input_is_absent(store, config):
    """No high, no low, no range estimate — and the close-to-close leaf survives."""
    _universe(store)
    _bars(store, CONTRACT_ID, 70)

    cells = {c.indicator: c for c in realised_vol_family(_inputs(store, config))}
    assert cells["realised_vol_30d"].status == STATUS_OK
    for name in ("parkinson_vol_30d", "garman_klass_vol_30d"):
        assert cells[name].status != STATUS_OK
        assert cells[name].reason_code == "no_high_low_range"
        assert cells[f"{name}_percentile"].reason_code == "no_high_low_range"


def test_a_correlation_names_the_overlapping_returns_behind_it(store, config):
    _universe(store)
    _bars(store, CONTRACT_ID, 200)
    _bars(store, BENCHMARK_ID, 200, seed=1.3)

    inputs = _inputs(store, config, benchmark_contract_id=BENCHMARK_ID)
    cells = {c.indicator: c for c in correlation_to_benchmark(inputs)}

    cell = cells.get("correlation_250d")
    assert cell is not None
    if cell.status == STATUS_OK:
        # Admitted below its nominal window, so the label must not claim 250.
        assert cell.n_obs is not None and cell.n_obs < 250
        assert f"{cell.n_obs} overlapping returns" in (cell.window or ""), cell.window


def test_a_shorter_correlation_window_is_fully_covered_and_says_so(store, config):
    _universe(store)
    _bars(store, CONTRACT_ID, 200)
    _bars(store, BENCHMARK_ID, 200, seed=1.3)

    inputs = _inputs(store, config, benchmark_contract_id=BENCHMARK_ID)
    cells = {c.indicator: c for c in correlation_to_benchmark(inputs)}

    cell = cells.get("correlation_30d")
    assert cell is not None and cell.status == STATUS_OK
    assert cell.n_obs == 30
    assert "30 overlapping returns" in (cell.window or "")


def test_the_grid_reads_the_daily_window_not_the_weekly_one(config):
    """The two keys are different numbers and the daily series must use its own.

    data.daily_window_years is also what collect/backfill.py fetches, so asking
    for the weekly span requested history the collector never gathers.
    """
    daily = config.require_int("data.daily_window_years")
    weekly = config.require_int("data.weekly_window_years")
    assert daily != weekly

    import inspect

    from obliskiq.compute import indicators

    source = inspect.getsource(indicators.IndicatorInputs.daily.fget)
    assert "daily_window_years" in source
    assert "weekly_window_years" not in source
