"""The option surface per underlying (MASTER_PLAN §5.7): delta x tenor x implied vol, the
percentile, skew and term slope, liquidity and theta, each leaf measured or absent with why, a
stale chain making its leaves stale, and the build point in time.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, timedelta

import pytest
from options_rig import AS_OF, SPOT, SPY, STAMP, seed_options

from obliskiq.config import ObliskConfig
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Snapshot
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.grid.option_surface import (
    SURFACE,
    OptionSurfaceBuilder,
    surface_leaves,
    surface_view,
    tenor_leaf,
    wing_leaf,
)
from obliskiq.store import Store, repos

OK = MeasurementStatus.OK
NA = MeasurementStatus.NA


def build(store: Store, settings: ObliskConfig, *, as_of: date = AS_OF) -> dict[str, Measurement]:
    builder = OptionSurfaceBuilder(store, settings)
    rows = builder.build(as_of=as_of, computed_at=STAMP)
    builder.write(rows)
    return {row.leaf: row for row in rows if row.entity_id == str(SPY)}


def seed_rate(store: Store) -> None:
    repos.upsert_macro(
        store,
        [
            MacroObservation(
                series_id="DTB3",
                source="fred",
                reference_date=AS_OF - timedelta(days=1),
                release_date=AS_OF,
                value=4.0,
                frequency="daily",
                units="%",
                collected_at=STAMP,
            )
        ],
    )


def test_every_leaf_is_written_in_every_branch(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    leaves = build(store, shipped_settings)
    assert set(leaves) == set(surface_leaves(shipped_settings))
    assert all(row.indicator == SURFACE for row in leaves.values())
    assert all(row.entity_kind is EntityKind.INSTRUMENT for row in leaves.values())


def test_the_surface_reads_each_tenor_and_wing_off_the_smile(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    seed_rate(store)
    leaves = build(store, shipped_settings)
    atm = leaves[tenor_leaf(95, "iv_atm")]
    assert atm.status is OK
    assert atm.value == pytest.approx(0.20, abs=0.01)
    assert "97d (target 95d)" in (atm.window or "")
    put25 = leaves[wing_leaf(95, 0.25, calls=False)]
    call25 = leaves[wing_leaf(95, 0.25, calls=True)]
    assert put25.status is OK
    assert call25.status is OK
    assert put25.value is not None
    assert call25.value is not None
    assert put25.value > call25.value, "an equity skew bids the put wing"
    skew = leaves[tenor_leaf(95, "skew")]
    assert skew.status is OK
    assert skew.value is not None
    assert skew.value < 0
    theta = leaves[tenor_leaf(180, "theta_share")]
    assert theta.status is OK
    assert theta.value is not None
    assert theta.value < 0
    assert leaves[tenor_leaf(95, "liquid_share")].value == 1.0
    assert leaves[tenor_leaf(95, "open_interest")].value == 9 * 2000
    spread = leaves[tenor_leaf(95, "spread_median")]
    assert spread.status is OK
    assert spread.value is not None
    assert 0 < spread.value < 0.05
    slope = leaves["term_slope"]
    assert slope.status is OK
    assert slope.window == "97d vs 178d"


def test_a_tenor_with_no_expiry_near_it_says_so(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    leaves = build(store, shipped_settings)
    middle = leaves[tenor_leaf(135, "iv_atm")]
    assert middle.status is NA
    assert middle.reason_code == "no_expiry_near_tenor"
    assert "within 35d of 135d" in (middle.reason or "")


def test_theta_without_a_rate_is_absent_with_the_series(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    leaves = build(store, shipped_settings)
    theta = leaves[tenor_leaf(95, "theta_share")]
    assert theta.status is NA
    assert theta.reason_code == "risk_free_rate_absent"


def test_theta_reads_the_collected_dividend_yield_and_no_rate_without_a_series(
    store: Store, shipped_settings: ObliskConfig, make_snapshot: Callable[..., Snapshot]
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    seed_rate(store)
    repos.upsert_snapshots(
        store,
        [
            make_snapshot(
                snapshot_date=AS_OF,
                conid=SPY,
                last=SPOT,
                bid=SPOT - 0.01,
                ask=SPOT + 0.01,
                dividend_yield=0.013,
                dividend_yield_basis="trailing 12 months",
            )
        ],
    )
    theta = build(store, shipped_settings)[tenor_leaf(95, "theta_share")]
    assert theta.status is OK
    assert "dividend yield collected (trailing 12 months)" in (theta.reason or "")
    solve = shipped_settings.numerics.implied_vol.solve
    rates = {k: v for k, v in solve.risk_free_series.items() if k != "USD"}
    numerics = shipped_settings.numerics.model_copy(
        update={
            "implied_vol": shipped_settings.numerics.implied_vol.model_copy(
                update={"solve": solve.model_copy(update={"risk_free_series": rates})}
            )
        }
    )
    no_series = build(store, shipped_settings.model_copy(update={"numerics": numerics}))
    assert no_series[tenor_leaf(95, "theta_share")].reason_code == "risk_free_rate_absent"


def test_a_wing_the_band_does_not_reach_is_absent_not_substituted(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    assert build(store, shipped_settings)[wing_leaf(95, 0.25, calls=True)].status is OK
    store.execute("DELETE FROM chain_snapshots WHERE strike > 525")
    leaves = build(store, shipped_settings)
    far = leaves[wing_leaf(95, 0.25, calls=True)]
    assert far.status is NA
    assert far.reason_code == "no_smile_after_hygiene"
    assert "does not reach" in (far.reason or "")
    assert leaves[tenor_leaf(95, "skew")].status is NA


def test_the_percentile_waits_for_its_snapshots(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    leaves = build(store, shipped_settings)
    percentile = leaves["iv_percentile"]
    assert percentile.status is NA
    assert percentile.reason_code == "iv_percentile_unavailable"


def test_an_underlying_without_a_chain_is_absent_everywhere(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    repos.upsert_bars(store, [])
    store.execute("DELETE FROM chain_snapshots")
    leaves = build(store, shipped_settings)
    assert all(row.status is NA for row in leaves.values())
    assert leaves[tenor_leaf(95, "iv_atm")].reason_code == "no_option_chain"


def test_a_stale_chain_makes_its_leaves_stale(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    window = shipped_settings.grid.freshness_days.option_chain
    later = AS_OF + timedelta(days=window + 1)
    leaves = build(store, shipped_settings, as_of=later)
    measured = [row for row in leaves.values() if row.value is not None]
    assert measured
    assert all(row.status is MeasurementStatus.STALE for row in measured)


def test_the_build_is_point_in_time_and_reproducible(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_options(store, shipped_settings, iv_percentile=None)
    first = build(store, shipped_settings)
    later = AS_OF + timedelta(days=1)
    seed_options(store, shipped_settings, as_of=later, iv_percentile=None, open_interest=1)
    again = build(store, shipped_settings)
    assert {k: (v.value, v.inputs_hash) for k, v in first.items()} == {
        k: (v.value, v.inputs_hash) for k, v in again.items()
    }


def test_the_view_reads_back_the_newest_build(store: Store, shipped_settings: ObliskConfig) -> None:
    assert surface_view(store, as_of=AS_OF) == {}
    seed_options(store, shipped_settings, iv_percentile=None)
    build(store, shipped_settings)
    view = surface_view(store, as_of=AS_OF + timedelta(days=3))
    assert set(view[SPY]) == set(surface_leaves(shipped_settings))


def test_the_tenors_sit_inside_the_entry_window_the_collector_buys(
    shipped_settings: ObliskConfig,
) -> None:
    tenor = shipped_settings.tenor
    tenors = shipped_settings.grid.option_surface.tenors_days
    assert tenor.entry_dte_min <= min(tenors)
    assert max(tenors) <= tenor.entry_dte_max
