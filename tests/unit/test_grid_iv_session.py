"""The session's implied-vol solve (``grid/iv_session.py``), ported from lite's
``tests/test_iv_solve.py``: it inverts a chain the route priced without vols, writes only its own
column with its method, never solves a priced-with-vols chain, never discounts at a rate unknown
on the chain's session, and counts what did not converge.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import timedelta

import pytest
from measure_rig import (
    AS_OF,
    STAMP,
    XIU,
    business_days,
    chain,
    instrument,
    make_bars,
    monthly_series,
    observation,
)

from obliskiq.config import ObliskConfig
from obliskiq.domain.market import Snapshot
from obliskiq.grid.iv_session import SessionSolver, Skip
from obliskiq.quant.bsm import BsmInputs
from obliskiq.quant.iv_solve import SolveInputs
from obliskiq.store import Store, repos


def seed_xiu(
    store: Store, settings: ObliskConfig, *, with_iv: bool = False, rate: bool = True
) -> float:
    repos.upsert_instrument(store, instrument(XIU))
    bars = make_bars(XIU, business_days(AS_OF, 60), seed=3)
    repos.upsert_bars(store, bars)
    spot = bars[-1].close
    repos.upsert_chain_rows(
        store,
        chain(
            XIU,
            spot,
            session=AS_OF,
            expiries=[AS_OF + timedelta(days=120)],
            bsm=BsmInputs.from_config(settings),
            with_iv=with_iv,
            rate=0.0275,
        ),
    )
    if rate:
        repos.upsert_macro(
            store,
            monthly_series(
                "IRSTCI01CAM156N", "fred", months=12, start=2.75, step=0.0, units="%", lag_days=20
            ),
        )
    return spot


def solved(store: Store) -> Snapshot | None:
    rows = repos.snapshots(store, XIU, as_of=AS_OF)
    return rows[-1] if rows else None


def test_a_chain_priced_without_vols_is_solved_at_the_money(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_xiu(store, shipped_settings)
    report = SessionSolver(store, shipped_settings).run(as_of=AS_OF, at=STAMP)
    assert report.solved == 1
    row = solved(store)
    assert row is not None
    assert row.implied_vol is None  # the vendor's column is untouched
    assert row.implied_vol_solved == pytest.approx(0.22, abs=0.01)
    assert row.implied_vol_solved_source == "solved_bsm_european/q_none_collected"
    assert not report.is_degraded(SolveInputs.from_config(shipped_settings))


def test_a_chain_the_route_priced_with_vols_is_not_solved(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_xiu(store, shipped_settings, with_iv=True)
    report = SessionSolver(store, shipped_settings).run(as_of=AS_OF, at=STAMP)
    assert report.skipped == {Skip.VENDOR_SERVED: 1}
    assert solved(store) is None


def test_no_rate_known_on_the_session_leaves_the_underlying_unsolved(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_xiu(store, shipped_settings, rate=False)
    # A rate published after the chain's session does not exist for it.
    repos.upsert_macro(
        store,
        [
            observation(
                "IRSTCI01CAM156N",
                "fred",
                AS_OF - timedelta(days=40),
                2.75,
                release=AS_OF + timedelta(days=3),
                frequency="monthly",
                units="%",
            )
        ],
    )
    report = SessionSolver(store, shipped_settings).run(as_of=AS_OF, at=STAMP)
    assert report.skipped == {Skip.NO_RATE: 1}
    assert solved(store) is None


def test_a_collected_dividend_yield_is_used_and_named(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    spot = seed_xiu(store, shipped_settings)
    repos.upsert_snapshots(
        store,
        [
            Snapshot(
                snapshot_date=AS_OF,
                conid=XIU,
                last=spot,
                bid=None,
                ask=None,
                volume=None,
                implied_vol=None,
                implied_vol_source=None,
                historical_vol_30d=None,
                dividend_yield=0.03,
                dividend_yield_basis="trailing",
                implied_vol_solved=None,
                implied_vol_solved_source=None,
                source="t",
                collected_at=STAMP,
                market_data_status=None,
                market_data_raw=None,
                quote_delay_seconds=None,
            )
        ],
    )
    SessionSolver(store, shipped_settings).run(as_of=AS_OF, at=STAMP)
    row = solved(store)
    assert row is not None
    assert row.implied_vol_solved_source == "solved_bsm_european/q_collected"
    assert row.dividend_yield == 0.03


def test_a_dry_run_writes_nothing(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_xiu(store, shipped_settings)
    report = SessionSolver(store, shipped_settings).run(as_of=AS_OF, at=STAMP, write=False)
    assert report.solved == 1
    assert solved(store) is None


def test_a_book_that_does_not_converge_is_counted_and_degrades_the_run(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_xiu(store, shipped_settings)
    rows = repos.chain_rows(store, as_of=AS_OF, underlying_conid=XIU)
    # Prices below intrinsic have no implied vol.
    repos.upsert_chain_rows(
        store, [row.model_copy(update={"bid": 0.01, "ask": 0.02}) for row in rows]
    )
    report = SessionSolver(store, shipped_settings).run(as_of=AS_OF, at=STAMP)
    assert report.attempted_contracts > 0
    assert report.converged_contracts < report.attempted_contracts
    assert report.is_degraded(SolveInputs.from_config(shipped_settings))


def test_an_underlying_without_a_spot_is_skipped(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_xiu(store, shipped_settings)
    store.execute("DELETE FROM bars")
    report = SessionSolver(store, shipped_settings).run(as_of=AS_OF, at=STAMP)
    assert report.skipped == {Skip.NO_SPOT: 1}
