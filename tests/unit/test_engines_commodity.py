"""The commodity engine (lite's ``tests/test_compute_commodity.py``, its curve, positioning, cycle
and inventory parts, ported): the curve decides the wrapper, positioning conditions and cites its
window, cycle position reads the long history only, inventory is compared with the same week of
prior years; and the contango check the ``es`` eligibility policy reads (Session 8's
``ContangoCheck``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_engines import FUTURES_TRACKING_WRAPPER
from obliskiq.config.schema_universe import FuturesRootConfig
from obliskiq.data.macro.catalog import MacroCatalog
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import CurvePoint
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.domain.sources import CotReport
from obliskiq.engines.commodity.engine import (
    SHAPE_BACKWARDATION,
    SHAPE_CONTANGO,
    SHAPE_FLAT,
    CommodityEngine,
    CurveContangoCheck,
    week_distance,
    wrapper_view,
)
from obliskiq.grid.frames import MacroReader
from obliskiq.store import Store, repos
from obliskiq.store import sources as sources_repo

AS_OF = date(2026, 10, 22)
STAMP = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)


def curve(
    make_curve_point: Callable[..., CurvePoint], roll: float | None, *, on: date = AS_OF
) -> list[CurvePoint]:
    return [
        make_curve_point(
            snapshot_date=on, expiry=on + timedelta(days=30), price=70.0, annualised_roll=None
        ),
        make_curve_point(
            snapshot_date=on, expiry=on + timedelta(days=60), price=69.0, annualised_roll=roll
        ),
    ]


@pytest.fixture
def engine(store: Store, shipped_settings: ObliskConfig) -> CommodityEngine:
    return CommodityEngine(store, shipped_settings)


def root(settings: ObliskConfig, name: str) -> FuturesRootConfig:
    return next(r for r in settings.universe.futures_roots if r.root == name)


def build_root(
    engine: CommodityEngine, store: Store, settings: ObliskConfig, name: str = "CL"
) -> dict[str, Measurement]:
    reader = MacroReader(store, MacroCatalog.from_settings(settings), AS_OF)
    rows = engine.for_root(root(settings, name), reader=reader, as_of=AS_OF, computed_at=STAMP)
    return {row.leaf: row for row in rows}


# ── the curve and the wrapper ──


def test_a_steep_contango_rejects_the_futures_wrapper(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: CommodityEngine,
    make_curve_point: Callable[..., CurvePoint],
) -> None:
    ceiling = (
        shipped_settings.commodities.term_structure.contango_max_annualised_for_futures_wrapper
    )
    repos.upsert_curve_points(store, curve(make_curve_point, -(ceiling * 3)))
    shape = engine.curve_shape("CL", as_of=AS_OF)
    assert shape.shape == SHAPE_CONTANGO
    choice = engine.wrapper(shape)
    assert choice.rejected == FUTURES_TRACKING_WRAPPER
    assert FUTURES_TRACKING_WRAPPER not in choice.alternatives
    assert choice.recommended == shipped_settings.commodities.preferred_wrapper_order[0]


def test_backwardation_and_a_shallow_contango_keep_every_wrapper(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: CommodityEngine,
    make_curve_point: Callable[..., CurvePoint],
) -> None:
    order = tuple(shipped_settings.commodities.preferred_wrapper_order)
    repos.upsert_curve_points(store, curve(make_curve_point, 0.08))
    back = engine.curve_shape("CL", as_of=AS_OF)
    assert back.shape == SHAPE_BACKWARDATION
    assert back.contango == pytest.approx(-0.08)
    assert engine.wrapper(back).rejected is None
    assert engine.wrapper(back).alternatives == order
    repos.upsert_curve_points(store, curve(make_curve_point, -0.01))
    assert engine.wrapper(engine.curve_shape("CL", as_of=AS_OF)).rejected is None


def test_a_negligible_roll_is_flat(
    store: Store, engine: CommodityEngine, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(store, curve(make_curve_point, 0.0005))
    assert engine.curve_shape("CL", as_of=AS_OF).shape == SHAPE_FLAT


def test_a_single_listed_expiry_has_no_shape_and_no_wrapper(
    store: Store, engine: CommodityEngine, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(store, curve(make_curve_point, -0.2)[:1])
    shape = engine.curve_shape("CL", as_of=AS_OF)
    assert shape.shape is None
    assert shape.contango is None
    choice = engine.wrapper(shape)
    assert choice.recommended is None
    assert "cannot be used" in choice.reason


def test_a_rung_with_no_roll_is_unclassified_never_flat(
    store: Store, engine: CommodityEngine, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(store, curve(make_curve_point, None))
    shape = engine.curve_shape("CL", as_of=AS_OF)
    assert shape.shape is None
    assert "no annualised roll" in shape.reason


def test_an_old_curve_decides_nothing(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: CommodityEngine,
    make_curve_point: Callable[..., CurvePoint],
) -> None:
    window = shipped_settings.grid.freshness_days.daily_bars
    repos.upsert_curve_points(
        store, curve(make_curve_point, 0.02, on=AS_OF - timedelta(days=window + 1))
    )
    shape = engine.curve_shape("CL", as_of=AS_OF)
    assert shape.stale is not None
    assert engine.wrapper(shape).recommended is None
    leaves = build_root(engine, store, shipped_settings)
    assert leaves["contango_annualised"].status is MeasurementStatus.STALE


def test_the_curve_read_is_point_in_time(
    store: Store, engine: CommodityEngine, make_curve_point: Callable[..., CurvePoint]
) -> None:
    repos.upsert_curve_points(store, curve(make_curve_point, 0.05, on=AS_OF))
    repos.upsert_curve_points(store, curve(make_curve_point, -0.5, on=AS_OF + timedelta(days=1)))
    assert engine.curve_shape("CL", as_of=AS_OF).shape == SHAPE_BACKWARDATION


# ── the contango check ──


def test_the_check_passes_inside_the_ceiling_and_fails_beyond_it(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: CommodityEngine,
    make_curve_point: Callable[..., CurvePoint],
) -> None:
    check = CurveContangoCheck(engine, as_of=AS_OF)
    repos.upsert_curve_points(store, curve(make_curve_point, -0.02))
    assert check.assess("CL").passes
    repos.upsert_curve_points(store, curve(make_curve_point, -0.20))
    verdict = check.assess("CL")
    assert not verdict.passes
    assert "beyond" in verdict.reason


def test_the_check_fails_closed_without_a_curve(engine: CommodityEngine) -> None:
    verdict = CurveContangoCheck(engine, as_of=AS_OF).assess("CL")
    assert not verdict.passes
    assert "cannot be judged" in verdict.reason


# ── positioning, cycle and inventory ──


def cot(report_date: date, net_long: int) -> CotReport:
    return CotReport(
        contract_code="067651",
        report_date=report_date,
        release_date=report_date + timedelta(days=3),
        market_name="WTI",
        open_interest=1_000_000,
        noncomm_long=500_000 + net_long,
        noncomm_short=500_000,
        comm_long=None,
        comm_short=None,
        nonrept_long=None,
        nonrept_short=None,
        source="cftc_socrata",
        collected_at=STAMP,
    )


def test_positioning_cites_its_window_and_an_extreme_needs_confirmation(
    store: Store, shipped_settings: ObliskConfig, engine: CommodityEngine
) -> None:
    weeks = shipped_settings.sources.positioning.percentile_window_weeks
    reports = [cot(AS_OF - timedelta(days=7 * (weeks - i) + 6), i) for i in range(weeks)]
    sources_repo.upsert_cot_reports(store, reports)
    leaf = build_root(engine, store, shipped_settings)["positioning_percentile"]
    assert leaf.status is MeasurementStatus.OK
    assert leaf.value == 1.0
    assert f"{weeks} weekly reports" in leaf.window
    assert "price confirmation" in leaf.reason


def test_positioning_warming_up_is_absent(
    store: Store, shipped_settings: ObliskConfig, engine: CommodityEngine
) -> None:
    sources_repo.upsert_cot_reports(store, [cot(AS_OF - timedelta(days=10), 5)])
    leaf = build_root(engine, store, shipped_settings)["positioning_percentile"]
    assert leaf.status is MeasurementStatus.NA
    assert "warming up" in leaf.reason


def test_a_root_with_no_cot_series_says_so(
    store: Store, shipped_settings: ObliskConfig, engine: CommodityEngine
) -> None:
    leaf = build_root(engine, store, shipped_settings, "BZ")["positioning_percentile"]
    assert leaf.status is MeasurementStatus.INAPPLICABLE


def monthly(
    series: str, units: str, values: list[float], *, source: str = "fred", step: int = 30
) -> list[MacroObservation]:
    count = len(values)
    return [
        MacroObservation(
            series_id=series,
            source=source,
            reference_date=AS_OF - timedelta(days=step * (count - 1 - i)),
            release_date=AS_OF - timedelta(days=step * (count - 1 - i)),
            value=value,
            frequency="monthly",
            units=units,
            collected_at=STAMP,
        )
        for i, value in enumerate(values)
    ]


def test_cycle_position_uses_the_long_window_and_never_borrows(
    store: Store, shipped_settings: ObliskConfig, engine: CommodityEngine
) -> None:
    years = shipped_settings.sources.macro.commodity_history.window_years
    # 25 years of a falling price: only the window's years are ranked.
    values = [200.0 - i * 0.5 for i in range(25 * 12)]
    repos.upsert_macro(store, monthly("DCOILWTICO", "$ per Barrel", values))
    leaf = build_root(engine, store, shipped_settings)["cycle_percentile"]
    assert leaf.status is MeasurementStatus.OK
    assert f"{years}y" in leaf.window
    days = round(years * shipped_settings.numerics.bsm.day_count_basis)
    assert leaf.n_obs == days // 30 + 1 < len(values)
    assert leaf.value == pytest.approx(1 / leaf.n_obs)


def test_a_root_with_no_long_history_refuses(
    store: Store, shipped_settings: ObliskConfig, engine: CommodityEngine
) -> None:
    leaf = build_root(engine, store, shipped_settings)["cycle_percentile"]
    assert leaf.status is MeasurementStatus.NA
    assert "DCOILWTICO" in leaf.reason


def weekly(values: list[tuple[date, float]]) -> list[MacroObservation]:
    return [
        MacroObservation(
            series_id="WCESTUS1",
            source="eia",
            reference_date=day,
            release_date=day,
            value=value,
            frequency="weekly",
            units="MBBL",
            collected_at=STAMP,
        )
        for day, value in values
    ]


def test_inventory_is_measured_against_the_seasonal_band_of_prior_years(
    store: Store, shipped_settings: ObliskConfig, engine: CommodityEngine
) -> None:
    rows: list[tuple[date, float]] = []
    for back in range(1, 5):  # four prior years, the same week and its neighbours
        for shift in (-7, 0, 7):
            rows.append((AS_OF - timedelta(days=364 * back + shift), 400.0 + back))
    # The current year's neighbouring weeks sit far from the band and must not join it.
    rows += [
        (AS_OF - timedelta(days=7), 900.0),
        (AS_OF - timedelta(days=14), 900.0),
        (AS_OF, 430.0),
    ]
    repos.upsert_macro(store, weekly(rows))
    leaves = build_root(engine, store, shipped_settings)
    deviation = leaves["inventory_deviation_sd"]
    assert deviation.status is MeasurementStatus.OK
    assert deviation.n_obs == 12
    assert deviation.value > 0
    assert leaves["inventory_level"].value == 430.0


def test_a_thin_seasonal_band_reports_na(
    store: Store, shipped_settings: ObliskConfig, engine: CommodityEngine
) -> None:
    repos.upsert_macro(store, weekly([(AS_OF - timedelta(days=364), 400.0), (AS_OF, 430.0)]))
    leaf = build_root(engine, store, shipped_settings)["inventory_deviation_sd"]
    assert leaf.status is MeasurementStatus.NA
    assert "floor" in leaf.reason


def test_the_rolling_band_when_seasonal_adjustment_is_off(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    history = shipped_settings.commodities.history.model_copy(update={"seasonal_adjustment": False})
    commodities = shipped_settings.commodities.model_copy(update={"history": history})
    settings = shipped_settings.model_copy(update={"commodities": commodities})
    repos.upsert_macro(
        store, weekly([(AS_OF - timedelta(days=7 * i), 400.0 + i) for i in range(6, -1, -1)])
    )
    leaf = build_root(CommodityEngine(store, settings), store, settings)["inventory_deviation_sd"]
    assert leaf.status is MeasurementStatus.OK
    assert "rolling window" in leaf.reason


def test_iso_weeks_wrap_across_the_year() -> None:
    assert week_distance(1, 52) == 1
    assert week_distance(10, 12) == 2


# ── the build and the view ──


def test_every_tracked_root_is_built_and_viewed(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: CommodityEngine,
    make_curve_point: Callable[..., CurvePoint],
) -> None:
    repos.upsert_curve_points(store, curve(make_curve_point, -0.2))
    rows = engine.build(as_of=AS_OF, computed_at=STAMP)
    engine.write(rows)
    tracked = {r.root for r in shipped_settings.universe.futures_roots if r.tracked}
    assert {row.entity_id for row in rows} == tracked
    assert all(row.entity_kind is EntityKind.COMMODITY_ROOT for row in rows)
    view = {
        name: (status, choice)
        for name, status, choice in wrapper_view(store, shipped_settings, as_of=AS_OF)
    }
    status, choice = view["CL"]
    assert status is MeasurementStatus.OK
    assert choice.rejected == FUTURES_TRACKING_WRAPPER
    assert view["BZ"][1].recommended is None


def test_an_untracked_root_is_not_built(store: Store, shipped_settings: ObliskConfig) -> None:
    roots = [
        root.model_copy(update={"tracked": root.root != "CL"})
        for root in shipped_settings.universe.futures_roots
    ]
    universe = shipped_settings.universe.model_copy(update={"futures_roots": roots})
    settings = shipped_settings.model_copy(update={"universe": universe})
    rows = CommodityEngine(store, settings).build(as_of=AS_OF, computed_at=STAMP)
    assert rows
    assert "CL" not in {row.entity_id for row in rows}
