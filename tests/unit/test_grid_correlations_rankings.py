"""The cross-asset correlation matrix and the ranked views.

Ported from lite's ``tests/test_compute_rankings.py``: legs not markets, one leg per shared
series, rates differenced and prices log-returned (and the sign of that difference pinned, the
mutation lite's re-audit found passing every test), mixed frequencies refused, the norm floor on
independent readings; views ordered on one leaf, conditions that say ``None`` rather than false,
trajectories served and never zero, and the reach every row carries.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import timedelta

import pytest
from measure_rig import AS_OF, DAX, SPX, STAMP, business_days, instrument, make_bars, observation

from obliskiq.config import ObliskConfig
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.grid.correlations import (
    ASSET_EQUITY,
    ASSET_FX,
    ASSET_RATE,
    CorrelationMatrix,
    Leg,
    pair_id,
)
from obliskiq.grid.rankings import Dimension, RankingRow, Rankings
from obliskiq.quant.stats import TRANSFORM_LEVEL_CHANGE, TRANSFORM_LOG_RETURN, paired_changes
from obliskiq.store import Store, measurements, repos

OK, NA = MeasurementStatus.OK, MeasurementStatus.NA


# ── legs and pairs ──


def test_a_shared_series_is_one_leg_owned_by_its_first_claimant(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    found = CorrelationMatrix(store, shipped_settings).legs()
    ecb = [leg for leg in found if leg.series == "ECB_DFR"]
    assert len(ecb) == 1
    assert ecb[0].market == "eurozone"  # first in sorted order among its claimants
    assert len({leg.key for leg in found}) == len(found)


def test_only_the_admitted_class_pairs_are_computed() -> None:
    legs = [
        Leg(ASSET_EQUITY, "a", "a"),
        Leg(ASSET_EQUITY, "b", "b"),
        Leg(ASSET_FX, "EURUSD", "us"),
        Leg(ASSET_FX, "EURJPY", "japan"),
        Leg(ASSET_RATE, "DGS10", "us"),
        Leg(ASSET_RATE, "ECB_DFR", "eurozone"),
    ]
    pairs = {
        frozenset((left.asset_class, right.asset_class))
        for left, right in CorrelationMatrix.pairs(legs)
    }
    assert frozenset({ASSET_FX}) not in pairs
    assert frozenset({ASSET_RATE}) not in pairs
    assert pairs == {
        frozenset({ASSET_EQUITY}),
        frozenset({ASSET_EQUITY, ASSET_FX}),
        frozenset({ASSET_EQUITY, ASSET_RATE}),
    }


def test_a_rate_is_differenced_and_a_price_log_returned() -> None:
    assert Leg(ASSET_RATE, "DGS10", "us").transform == TRANSFORM_LEVEL_CHANGE
    assert Leg(ASSET_FX, "EURUSD", "us").transform == TRANSFORM_LOG_RETURN
    assert Leg(ASSET_EQUITY, "us", "us").transform == TRANSFORM_LOG_RETURN


def test_the_sign_of_a_rate_change_is_pinned() -> None:
    """Flipping the level change's sign inverts every equity-against-rates reading; lite's re-audit
    found that mutation passing all its tests. A rising rate is a positive change."""
    own, other = paired_changes(
        [100.0, 101.0, 102.0],
        [1.0, 1.5, 1.2],
        left_transform=TRANSFORM_LOG_RETURN,
        right_transform=TRANSFORM_LEVEL_CHANGE,
    )
    assert other == pytest.approx([0.5, -0.3])
    assert own[0] > 0


def seed_pair(store: Store, *, rate_frequency: str = "daily", rate_units: str = "%") -> None:
    days = business_days(AS_OF, 640)
    for conid in (SPX, DAX):
        repos.upsert_instrument(store, instrument(conid))
        repos.upsert_bars(store, make_bars(conid, days, seed=conid % 31, volume=False))
    repos.upsert_macro(
        store,
        [
            observation(
                "DGS10",
                "fred",
                d,
                4.0 + 0.001 * (i % 7),
                release=d,
                frequency=rate_frequency,
                units=rate_units,
            )
            for i, d in enumerate(days)
        ],
    )


def matrix_rows(store: Store, settings: ObliskConfig) -> dict[str, dict[str, Measurement]]:
    rows = CorrelationMatrix(store, settings).build(as_of=AS_OF, computed_at=STAMP)
    out: dict[str, dict[str, Measurement]] = {}
    for row in rows:
        out.setdefault(row.entity_id, {})[row.leaf] = row
    return out


def test_an_equity_pair_is_measured_against_its_own_norm(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_pair(store)
    pair = matrix_rows(store, shipped_settings)[
        pair_id(Leg(ASSET_EQUITY, "germany", "germany"), Leg(ASSET_EQUITY, "us", "us"))
    ]
    assert pair["correlation_30d"].status is OK
    assert pair["shift_30d"].status is OK
    assert pair["shift_30d"].n_obs == 2 * 252 // 30
    assert pair["correlation_250d"].status is OK
    assert pair["shift_250d"].status is NA  # two independent 250-day readings, floor four


def test_a_missing_leg_names_the_side_that_is_missing(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_pair(store)
    rows = matrix_rows(store, shipped_settings)
    japan = rows[pair_id(Leg(ASSET_EQUITY, "japan", "japan"), Leg(ASSET_EQUITY, "us", "us"))]
    assert japan["correlation_30d"].status is NA
    assert "japan equities" in (japan["correlation_30d"].reason or "")


def test_mixed_frequencies_are_refused_never_resampled(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_pair(store, rate_frequency="monthly")
    rows = matrix_rows(store, shipped_settings)
    pair = rows[pair_id(Leg(ASSET_EQUITY, "us", "us"), Leg(ASSET_RATE, "DGS10", "us"))]
    assert pair["correlation_30d"].status is NA
    assert pair["correlation_30d"].reason_code == "mixed_frequency"


def test_a_units_change_inside_the_window_refuses_the_pair(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_pair(store)
    days = business_days(AS_OF, 10)
    repos.upsert_macro(
        store,
        [
            observation("DGS10", "fred", d, 0.04, release=d, frequency="daily", units="decimal")
            for d in days
        ],
    )
    pair = matrix_rows(store, shipped_settings)[
        pair_id(Leg(ASSET_EQUITY, "us", "us"), Leg(ASSET_RATE, "DGS10", "us"))
    ]
    assert pair["correlation_30d"].reason_code == "mixed_units_in_window"


# ── the ranked views ──


def leaf(
    entity: str,
    name: str,
    value: float | None,
    *,
    status: MeasurementStatus = OK,
    window: str | None = None,
) -> Measurement:
    absent = value is None
    return Measurement(
        entity_kind=EntityKind.EQUITY_CELL,
        entity_id=entity,
        indicator="x",
        leaf=name,
        as_of=AS_OF,
        value=value,
        status=MeasurementStatus.NA if absent else status,
        reason="r" if absent or status is not OK else None,
        reason_code="c" if absent or status is not OK else None,
        n_obs=None,
        window=window,
        units="decimal",
        source=None,
        market_data_status=None,
        inputs_hash="c" * 64,
        computed_at=STAMP,
    )


type Ranked = dict[Dimension, list[RankingRow]]


@pytest.fixture
def rank(shipped_settings: ObliskConfig) -> Callable[[list[Measurement]], Ranked]:
    def run(rows: list[Measurement]) -> Ranked:
        out: Ranked = {}
        for row in Rankings(shipped_settings).rank(rows):
            out.setdefault(row.dimension, []).append(row)
        return out

    return run


def test_most_extended_orders_on_the_distance_from_the_high(rank: Callable[..., Ranked]) -> None:
    rows = [
        leaf("us/broad", "distance_from_52w_high", -0.10),
        leaf("us/energy", "distance_from_52w_high", -0.01),
        leaf("us/utilities", "distance_from_52w_high", None),
    ]
    view = rank(rows)[Dimension.MOST_EXTENDED]
    assert [(row.market, row.sector) for row in view] == [("us", "energy"), ("us", "broad")]
    assert view[0].reachable == 3
    assert view[0].ranked_over == 2


def test_widest_iv_hv_ranks_on_magnitude_and_keeps_the_sign(rank: Callable[..., Ranked]) -> None:
    rows = [leaf("us/broad", "iv_minus_hv", 0.02), leaf("us/energy", "iv_minus_hv", -0.08)]
    view = rank(rows)[Dimension.WIDEST_IV_HV]
    assert view[0].value == -0.08


def test_a_stale_leaf_is_ranked_and_says_so(rank: Callable[..., Ranked]) -> None:
    rows = [leaf("us/broad", "distance_from_52w_high", -0.1, status=MeasurementStatus.STALE)]
    view = rank(rows)[Dimension.MOST_EXTENDED]
    assert view[0].status is MeasurementStatus.STALE


def test_an_unmeasured_condition_is_none_never_false(rank: Callable[..., Ranked]) -> None:
    rows = [
        leaf("us/broad", "parkinson_vol_30d_percentile", 0.1),
        leaf("us/broad", "realised_vol_30d_percentile", 0.5),
    ]
    (row,) = rank(rows)[Dimension.MOST_COMPRESSED]
    assert row.conditions["parkinson_vol_compressed"] is True
    assert row.conditions["realised_vol_compressed"] is False
    assert row.conditions["iv_percentile_low"] is None
    assert row.meets_full_definition is None


def test_the_trajectory_is_served_never_zero_and_never_the_ranked_leaf(
    rank: Callable[..., Ranked],
) -> None:
    rows = [
        leaf("us/broad", "distance_from_52w_high", -0.05),
        leaf("us/broad", "return_21d_local", 0.02, window="21d (22 bars)"),
        leaf("us/broad", "return_63d_local", None),
        leaf("us/broad", "rv_term_slope", -0.1, status=MeasurementStatus.STALE),
    ]
    (row,) = rank(rows)[Dimension.MOST_EXTENDED]
    assert set(row.trajectory) == {"return_21d_local", "rv_term_slope"}
    assert row.trajectory["rv_term_slope"].status is MeasurementStatus.STALE
    assert row.trajectory["return_21d_local"].window == "21d (22 bars)"
    assert "distance_from_52w_high" not in row.trajectory


def test_a_thin_single_market_view_is_labelled_not_suppressed(rank: Callable[..., Ranked]) -> None:
    view = rank([leaf("us/technology", "iv_minus_hv", 0.03)])[Dimension.WIDEST_IV_HV]
    assert len(view) == 1
    assert "ONE market" in view[0].detail
    assert "INSUFFICIENT REACH" in view[0].detail


def test_the_correlation_shift_view_ranks_every_window_together(
    rank: Callable[..., Ranked],
) -> None:
    rows = [
        leaf("us/energy", "correlation_shift_30d", 0.5),
        leaf("us/energy", "correlation_shift_90d", -2.5),
        leaf("germany/broad", "correlation_shift_30d", 1.0),
    ]
    view = rank(rows)[Dimension.LARGEST_CORRELATION_SHIFT]
    assert [row.leaf for row in view] == [
        "correlation_shift_90d",
        "correlation_shift_30d",
        "correlation_shift_30d",
    ]
    assert view[0].reachable == 2  # cells, not leaves


def test_rankings_read_the_newest_built_session(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    assert Rankings(shipped_settings).build(store, as_of=AS_OF) == []
    old = leaf("us/broad", "distance_from_52w_high", -0.2).model_copy(
        update={"as_of": AS_OF - timedelta(days=1)}
    )
    measurements.upsert_measurements(store, [old])
    (row,) = Rankings(shipped_settings).build(store, as_of=AS_OF)
    assert row.value == -0.2


# ── refusals the matrix states ──


def rate_pair(rows: dict[str, dict[str, Measurement]]) -> dict[str, Measurement]:
    return rows[pair_id(Leg(ASSET_EQUITY, "us", "us"), Leg(ASSET_RATE, "DGS10", "us"))]


def test_an_equity_leg_whose_index_has_no_bars_is_absent(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_pair(store)
    store.execute("DELETE FROM bars WHERE conid = ?", (DAX,))
    rows = matrix_rows(store, shipped_settings)
    germany = rows[pair_id(Leg(ASSET_EQUITY, "germany", "germany"), Leg(ASSET_EQUITY, "us", "us"))]
    assert "has no bars for its native index" in (germany["correlation_30d"].reason or "")


def test_a_leg_with_no_declared_cadence_or_two_cadences_is_refused(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_pair(store)
    days = business_days(AS_OF, 640)
    undeclared = [
        observation("DGS10", "fred", d, 4.0, release=d, frequency="daily", units="%").model_copy(
            update={"frequency": None}
        )
        for d in days
    ]
    repos.upsert_macro(store, undeclared)
    found = rate_pair(matrix_rows(store, shipped_settings))["correlation_30d"]
    assert "declares no frequency" in (found.reason or "")
    repos.upsert_macro(
        store,
        [
            observation("DGS10", "fred", d, 4.0, release=d, frequency=cadence, units="%")
            for d, cadence in zip(days, ["daily", "weekly"] * 320, strict=True)
        ],
    )
    found = rate_pair(matrix_rows(store, shipped_settings))["correlation_30d"]
    assert "more than one frequency" in (found.reason or "")


def test_a_short_or_flat_rate_leg_has_no_correlation(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_pair(store)
    store.execute("DELETE FROM macro")
    days = business_days(AS_OF, 640)
    repos.upsert_macro(
        store,
        [
            observation("DGS10", "fred", d, 4.0, release=d, frequency="daily", units="%")
            for d in days
        ],
    )
    flat = rate_pair(matrix_rows(store, shipped_settings))["correlation_30d"]
    assert flat.reason_code == "no_dispersion"
    store.execute("DELETE FROM macro")
    repos.upsert_macro(
        store,
        [
            observation("DGS10", "fred", d, 4.0 + i / 100, release=d, frequency="daily", units="%")
            for i, d in enumerate(days[-20:])
        ],
    )
    short = rate_pair(matrix_rows(store, shipped_settings))["correlation_30d"]
    assert short.reason_code == "sample_too_short"


def test_a_row_meets_its_full_definition_only_when_every_condition_held() -> None:
    def row(conditions: dict[str, bool | None]) -> RankingRow:
        return RankingRow(
            dimension=Dimension.MOST_EXTENDED,
            rank=1,
            market="us",
            sector="broad",
            leaf="distance_from_high_52w",
            value=0.0,
            window=None,
            status=OK,
            market_data_status=None,
            detail="",
            reachable=1,
            ranked_over=1,
            conditions=conditions,
        )

    assert row({}).meets_full_definition is None
    assert row({"a": True, "b": True}).meets_full_definition is True
    assert row({"a": True, "b": False}).meets_full_definition is False
