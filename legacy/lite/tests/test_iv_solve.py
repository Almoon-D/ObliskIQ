"""Solving an ATM implied vol the route never served.

The strongest test here is the ROUND TRIP: price a chain from a known sigma with
this repository's own pricer, throw the sigma away, and check the solver hands it
back. Everything else in this file guards a way the number could be quietly wrong
rather than absent — a blended percentile series, today's rate on a past session,
a non-converged sigma reported as data.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.frames import underlying_snapshots
from obliskiq.compute.iv_solve import (
    SKIP_NO_RATE,
    SOURCE_SOLVED_BSM_EUROPEAN,
    SolveInputs,
    solve_session,
)
from obliskiq.quant.bsm import BsmInputs, bsm_price

SESSION = date(2026, 9, 2)
UNDERLYING = 1
SPOT = 100.0
#: Inside numerics.implied_vol.solve's [min_dte, max_dte] band.
DTE = 120
EXPIRY = SESSION + timedelta(days=DTE)
TRUE_SIGMA = 0.24
RATE_PERCENT = 4.0


def _seed_universe(store, currency="USD"):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, sector) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (UNDERLYING, "TEST", currency, "ETF", "us", "broad"),
        )


def _seed_rate(store, *, reference: date, release: date | None = None, series="DTB3"):
    """A risk-free rate row, with the vintage dates the point-in-time read uses."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value) "
            "VALUES (?, 'fred', ?, ?, ?)",
            (
                series,
                reference.isoformat(),
                None if release is None else release.isoformat(),
                RATE_PERCENT,
            ),
        )


def _seed_spot(store, *, session=SESSION, last=SPOT):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, source) "
            "VALUES (?, ?, ?, 'marketdata_app')",
            (session.isoformat(), UNDERLYING, last),
        )


def _seed_chain(store, config, *, sigma=TRUE_SIGMA, session=SESSION, expiry=EXPIRY, strikes=None):
    """A chain priced FROM ``sigma``, carrying no implied vol — the vendor case.

    Prices come from this repository's own ``bsm_price``, so a solver that
    disagrees with them disagrees with the pricer the rest of the build uses.
    """
    bsm = BsmInputs.from_config(config)
    strikes = strikes if strikes is not None else [88.0, 92.0, 96.0, 100.0, 104.0, 108.0, 112.0]
    rows = []
    for strike in strikes:
        is_call = strike >= SPOT
        fair = float(
            bsm_price(
                SPOT,
                strike,
                (expiry - session).days,
                sigma,
                RATE_PERCENT / 100.0,
                0.0,
                is_call,
                bsm,
            )
        )
        rows.append(
            (
                session.isoformat(),
                UNDERLYING,
                expiry.isoformat(),
                strike,
                "C" if is_call else "P",
                # A tight two-sided book whose MID is the fair value. Spread and
                # open interest clear liquidity hygiene so the test measures the
                # solve rather than the filters.
                fair * 0.999,
                fair * 1.001,
                None,  # iv: the whole point — the vendor served none
                None,  # delta: historical vendor chains carry none either
                5000,
                1000,
            )
        )
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right, "
            "bid, ask, iv, delta, open_interest, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'marketdata_app')",
            rows,
        )


@pytest.fixture()
def solvable(store, config):
    _seed_universe(store)
    _seed_rate(store, reference=SESSION, release=SESSION)
    _seed_spot(store)
    _seed_chain(store, config)
    return store


# ── the round trip ──


def test_the_solver_recovers_the_sigma_the_chain_was_priced_from(solvable, config):
    """Price at 24% vol, discard it, solve: 24% comes back.

    This is the test that says the number is RIGHT rather than merely present.
    """
    report = solve_session(solvable, config, as_of=SESSION)
    assert report.solved == 1

    snapshots = underlying_snapshots(solvable, UNDERLYING, until=SESSION)
    solved = snapshots[-1].implied_vol_solved
    assert solved == pytest.approx(TRUE_SIGMA, abs=1e-4)


def test_the_solved_value_is_labelled_european_and_carries_its_q_tier(solvable, config):
    """A number inverted through a European model on an American-style ETF is
    labelled as such, or it is indistinguishable from an exchange quote."""
    solve_session(solvable, config, as_of=SESSION)
    source = underlying_snapshots(solvable, UNDERLYING, until=SESSION)[-1].implied_vol_solved_source
    assert source.startswith(SOURCE_SOLVED_BSM_EUROPEAN)
    assert "european" in source
    # No yield was collected or declared for this instrument, and the tier says so
    # rather than letting a zero read as a measured non-payer.
    assert source.endswith("q_none_declared")


def test_the_vendor_column_is_left_untouched(solvable, config):
    """The separate column is the whole mechanism. If the solve ever wrote
    ``implied_vol``, the percentile series would silently mix two methods."""
    solve_session(solvable, config, as_of=SESSION)
    latest = underlying_snapshots(solvable, UNDERLYING, until=SESSION)[-1]
    assert latest.implied_vol is None
    assert latest.implied_vol_solved is not None


# ── the point-in-time rate ──


def test_a_session_with_no_rate_known_by_then_is_not_solved(store, config):
    """The rate is published AFTER the session being solved.

    macro.risk_free_rate.on_failure is `error` and never a literal fallback, so
    the underlying is left unsolved rather than discounted at a rate that did not
    exist yet.
    """
    _seed_universe(store)
    _seed_rate(store, reference=SESSION, release=SESSION + timedelta(days=5))
    _seed_spot(store)
    _seed_chain(store, config)

    report = solve_session(store, config, as_of=SESSION)
    assert report.solved == 0
    assert report.skipped.get(SKIP_NO_RATE) == 1


def test_a_rate_for_a_later_reference_date_is_not_used_either(store, config):
    _seed_universe(store)
    _seed_rate(store, reference=SESSION + timedelta(days=3), release=SESSION)
    _seed_spot(store)
    _seed_chain(store, config)

    report = solve_session(store, config, as_of=SESSION)
    assert report.solved == 0
    assert report.skipped.get(SKIP_NO_RATE) == 1


# ── non-convergence is NULL and counted ──


def test_an_unsolvable_book_writes_no_value_and_is_counted(store, config):
    """Prices below discounted intrinsic have no implied volatility.

    The solver rejects them before iterating, so nothing converges, nothing is
    written, and the count is visible rather than swallowed.
    """
    _seed_universe(store)
    _seed_rate(store, reference=SESSION, release=SESSION)
    _seed_spot(store)
    # A deep in-the-money call quoted at a cent: far under discounted intrinsic.
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right, "
            "bid, ask, iv, delta, open_interest, volume, source) "
            "VALUES (?, ?, ?, ?, 'C', 0.005, 0.015, NULL, NULL, 5000, 1000, 'marketdata_app')",
            (SESSION.isoformat(), UNDERLYING, EXPIRY.isoformat(), 90.0),
        )

    report = solve_session(store, config, as_of=SESSION)
    assert report.solved == 0
    assert report.attempted_contracts == 1
    assert report.converged_contracts == 0
    assert underlying_snapshots(store, UNDERLYING, until=SESSION)[-1].implied_vol_solved is None


def test_a_run_past_the_non_convergence_ceiling_is_degraded(store, config):
    _seed_universe(store)
    _seed_rate(store, reference=SESSION, release=SESSION)
    _seed_spot(store)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right, "
            "bid, ask, iv, delta, open_interest, volume, source) "
            "VALUES (?, ?, ?, ?, 'C', 0.005, 0.015, NULL, NULL, 5000, 1000, 'marketdata_app')",
            (SESSION.isoformat(), UNDERLYING, EXPIRY.isoformat(), 90.0),
        )
    report = solve_session(store, config, as_of=SESSION)
    inputs = SolveInputs.from_config(config)
    assert report.non_converged_fraction == 1.0
    assert report.is_degraded(inputs)
    assert "did not" in report.summary(inputs)


def test_a_run_with_nothing_to_attempt_is_not_degraded(store, config):
    """An empty store is a young store, not a broken run."""
    _seed_universe(store)
    report = solve_session(store, config, as_of=SESSION)
    assert report.attempted_contracts == 0
    assert report.non_converged_fraction == 0.0
    assert not report.is_degraded(SolveInputs.from_config(config))


# ── bounds ──


def test_an_expiry_outside_the_dte_band_is_not_solved(store, config):
    """Outside [min_dte, max_dte] the European approximation is not tolerable,
    and labelling a value does not make it acceptable."""
    _seed_universe(store)
    _seed_rate(store, reference=SESSION, release=SESSION)
    _seed_spot(store)
    _seed_chain(store, config, expiry=SESSION + timedelta(days=7))

    report = solve_session(store, config, as_of=SESSION)
    assert report.solved == 0


def test_a_chain_the_vendor_already_priced_is_left_alone(store, config):
    """Inverting a mid price to second-guess a served implied vol produces a
    worse answer to a question already answered."""
    _seed_universe(store)
    _seed_rate(store, reference=SESSION, release=SESSION)
    _seed_spot(store)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right, "
            "bid, ask, iv, delta, open_interest, volume, source) "
            "VALUES (?, ?, ?, ?, 'C', 5.0, 5.1, 0.21, 0.5, 5000, 1000, 'marketdata_app')",
            (SESSION.isoformat(), UNDERLYING, EXPIRY.isoformat(), 100.0),
        )
    report = solve_session(store, config, as_of=SESSION)
    assert report.solved == 0
    assert underlying_snapshots(store, UNDERLYING, until=SESSION)[-1].implied_vol_solved is None


# ── the two methods never meet in one series ──
#
# These are the tests for the constraint that made the solved value a SEPARATE
# column rather than a tagged one. A percentile series whose early history is
# solved and whose recent history is exchange-quoted has a methodology break at
# the join, and iv_percentile would report a regime shift that did not happen.


def _percentile_inputs(store, config, prefer="vendor"):
    import copy

    from obliskiq.compute.indicators import IndicatorInputs
    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["data"]["iv_percentile_source"] = "self"
    data["data"]["atm_iv_prefer"] = prefer
    # One session is enough to accumulate for these tests; the real floor is a
    # year and would need 252 seeded rows to say anything about column choice.
    data["data"]["iv_self_source_min_sessions"] = 1
    return IndicatorInputs.bind(
        store,
        Config(data, config.source),
        as_of=SESSION,
        market="us",
        sector="broad",
        contract_id=UNDERLYING,
        symbol="TEST",
        market_spec={"currency": "USD", "native_index": "SPX"},
        benchmark_contract_id=None,
        benchmark_market="us",
    )


def test_the_self_percentile_never_reads_the_solved_column(store, config):
    """Ten sessions of solved implied vol accumulate NOTHING for the vendor series.

    Two independent mechanisms have to hold for this, and both are asserted here:
    ``underlying_snapshot_count`` counts only rows with a non-null ``implied_vol``,
    so the solved rows never reach the switch-on threshold; and
    ``_self_iv_percentile`` reads ``implied_vol`` alone, so they could not be
    ranked even if they did. Either one failing would let a solved series masquerade
    as a vendor one and put a methodology break inside a percentile.
    """
    from obliskiq.compute.indicators import _self_iv_percentile

    _seed_universe(store)
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, "
            "implied_vol_solved, implied_vol_solved_source, source) "
            "VALUES (?, ?, NULL, ?, 'solved_bsm_european/q_none_declared', 'marketdata_app')",
            [
                ((SESSION - timedelta(days=n)).isoformat(), UNDERLYING, 0.20 + n * 0.001)
                for n in range(10)
            ],
        )
    from obliskiq.compute.frames import underlying_snapshot_count

    # Mechanism one: ten solved sessions accumulate zero countable snapshots.
    assert underlying_snapshot_count(store, UNDERLYING, until=SESSION) == 0

    # Mechanism two: nothing is ranked, and the reason names the accumulation.
    value, reason = _self_iv_percentile(_percentile_inputs(store, config))
    assert value is None
    assert "0 of the 1 snapshots" in reason

    # And the solved values really are in the store — the test would otherwise
    # pass against an empty table and prove nothing.
    assert underlying_snapshots(store, UNDERLYING, until=SESSION)[-1].implied_vol_solved is not None


def test_the_solved_value_does_not_outrank_a_vendor_quote_by_default(store, config):
    """data.atm_iv_prefer defaults to `vendor`: an exchange-quoted figure beats an
    inversion of a mid price through a European model."""
    from obliskiq.compute.indicators import _atm_implied_vol

    _seed_universe(store)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, implied_vol, "
            "implied_vol_solved, implied_vol_solved_source, source) "
            "VALUES (?, ?, ?, 0.31, 0.19, 'solved_bsm_european/q_none_declared', "
            "'marketdata_app')",
            (SESSION.isoformat(), UNDERLYING, SPOT),
        )
    value, provenance, _ = _atm_implied_vol(_percentile_inputs(store, config, prefer="vendor"))
    assert value == pytest.approx(0.31)
    assert "vendor implied vol" in provenance


def test_the_ordering_is_configurable_and_says_which_it_used(store, config):
    from obliskiq.compute.indicators import _atm_implied_vol

    _seed_universe(store)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, implied_vol, "
            "implied_vol_solved, implied_vol_solved_source, source) "
            "VALUES (?, ?, ?, 0.31, 0.19, 'solved_bsm_european/q_none_declared', "
            "'marketdata_app')",
            (SESSION.isoformat(), UNDERLYING, SPOT),
        )
    value, provenance, _ = _atm_implied_vol(_percentile_inputs(store, config, prefer="solved"))
    assert value == pytest.approx(0.19)
    assert "SOLVED" in provenance
    # The limitation travels with the number, on every cell that reads it.
    assert "EUROPEAN" in provenance


def test_a_winning_source_does_not_read_the_ones_below_it(store, config):
    """The ordering is lazy, and this pins it.

    `_atm_implied_vol` runs once per cell per family across a 156-cell grid.
    Reading all three sources eagerly cost two extra store round trips on every
    cell the first source already answered, and the chain read is the expensive
    one. With `solved` preferred and a solved value present, the chain must not
    be touched at all.
    """
    from obliskiq.compute import indicators

    _seed_universe(store)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, implied_vol, "
            "implied_vol_solved, implied_vol_solved_source, source) "
            "VALUES (?, ?, ?, 0.31, 0.19, 'solved_bsm_european/q_none_declared', "
            "'marketdata_app')",
            (SESSION.isoformat(), UNDERLYING, SPOT),
        )

    chain_reads = []
    real = indicators.latest_chain_date

    def counting(*args, **kwargs):
        chain_reads.append(1)
        return real(*args, **kwargs)

    indicators.latest_chain_date = counting
    try:
        value, _, _ = indicators._atm_implied_vol(
            _percentile_inputs(store, config, prefer="solved")
        )
    finally:
        indicators.latest_chain_date = real

    assert value == pytest.approx(0.19)
    assert chain_reads == [], "the chain was read even though the solved value won"


def test_the_snapshot_behind_two_sources_is_read_once(store, config):
    """Vendor and solved both come off the underlying snapshot. One read, not two."""
    from obliskiq.compute import indicators

    _seed_universe(store)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, source) "
            "VALUES (?, ?, ?, 'marketdata_app')",
            (SESSION.isoformat(), UNDERLYING, SPOT),
        )

    reads = []
    real = indicators.underlying_snapshots

    def counting(*args, **kwargs):
        reads.append(1)
        return real(*args, **kwargs)

    indicators.underlying_snapshots = counting
    try:
        # Neither column is populated, so BOTH snapshot-backed sources are reached
        # and both come back empty — the case that would double-read.
        value, _, _ = indicators._atm_implied_vol(
            _percentile_inputs(store, config, prefer="vendor")
        )
    finally:
        indicators.underlying_snapshots = real

    assert value is None
    assert len(reads) == 1, f"the underlying snapshot was read {len(reads)} times, expected 1"
