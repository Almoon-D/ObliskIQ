"""The fifteen families (``grid/indicators.py``), each against a store built for the rule.

Ported from lite's ``tests/test_compute_indicators.py`` and ``tests/test_window_honesty.py``:
a fixed horizon needs its full window, a percentile of rolling readings counts independent ones,
the base-currency leg is the right way up, the term structure refuses a tenor pair it was not
asked for, a structural gap is inapplicable and a collection gap n/a, and every family emits the
same leaves in every branch.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
from datetime import date, timedelta

import pytest
from measure_rig import (
    AS_OF,
    DAX,
    QQQ,
    SPX,
    STAMP,
    XIU,
    business_days,
    chain,
    cot_reports,
    daily_series,
    instrument,
    make_bars,
    monthly_series,
    observation,
)

from obliskiq.config import ObliskConfig
from obliskiq.data.macro.catalog import MacroCatalog
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.market import Bar, Snapshot
from obliskiq.domain.measurement import MeasurementStatus
from obliskiq.grid import indicators
from obliskiq.grid.cells import Cell
from obliskiq.grid.estimators import close_to_close
from obliskiq.grid.frames import MacroReader, cpi_yoy, market_spec
from obliskiq.grid.indicators import CellInputs, CellInstrument, GridParams
from obliskiq.quant.bsm import BsmInputs
from obliskiq.store import Store, repos
from obliskiq.store import sources as sources_repo

OK, NA, STALE, INAPPLICABLE = (
    MeasurementStatus.OK,
    MeasurementStatus.NA,
    MeasurementStatus.STALE,
    MeasurementStatus.INAPPLICABLE,
)


def inputs(
    store: Store,
    settings: ObliskConfig,
    *,
    market: str = "us",
    sector: str = "broad",
    conid: int | None = SPX,
    asset_class: AssetClass = AssetClass.INDEX,
    options_unavailable: str | None = None,
    benchmark: int | None = None,
    as_of: date = AS_OF,
) -> CellInputs:
    return CellInputs(
        store=store,
        settings=settings,
        params=GridParams.from_settings(settings),
        reader=MacroReader(store, MacroCatalog.from_settings(settings), as_of),
        as_of=as_of,
        market=market,
        sector=sector,
        spec=market_spec(settings.universe, market),
        instrument=None
        if conid is None
        else CellInstrument(
            symbol="X",
            conid=conid,
            asset_class=asset_class,
            options_unavailable_reason=options_unavailable,
        ),
        benchmark=benchmark,
        benchmark_market="us",
    )


def leaves(cells: list[Cell]) -> dict[str, Cell]:
    return {cell.leaf: cell for cell in cells}


def seed_bars(store: Store, conid: int, count: int, **kwargs: object) -> None:
    repos.upsert_instrument(store, instrument(conid))
    repos.upsert_bars(
        store, make_bars(conid, business_days(AS_OF, count), seed=conid % 97, **kwargs)
    )  # type: ignore[arg-type]


# ── 1. returns ──


def test_a_fixed_horizon_needs_its_full_window(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 30)
    found = leaves(indicators.returns(inputs(store, shipped_settings)))
    assert found["return_21d_local"].status is OK
    assert found["return_21d_local"].n_obs == 21
    sixty_three = found["return_63d_local"]
    assert sixty_three.status is NA
    assert sixty_three.reason_code is not None
    assert sixty_three.reason_code.value == "sample_too_short"
    assert sixty_three.window == "63d (30 bars)"
    assert found["return_63d_base"].status is NA


def test_year_to_date_starts_at_the_previous_year_end_close(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 400)
    bars = repos.bar_series(store, SPX, as_of=AS_OF)
    year_end = [bar for bar in bars if bar.session_date < date(AS_OF.year, 1, 1)][-1]
    found = leaves(indicators.returns(inputs(store, shipped_settings)))
    assert found["return_ytd_local"].value == pytest.approx(bars[-1].close / year_end.close - 1.0)


def test_the_base_leg_is_the_right_way_up(store: Store, shipped_settings: ObliskConfig) -> None:
    """EURUSD rising is the euro strengthening: a dollar asset is worth less in euros."""
    seed_bars(store, SPX, 30)
    days = business_days(AS_OF, 30)
    repos.upsert_macro(
        store,
        [
            observation(
                "EURUSD",
                "ecb",
                day,
                1.00 + 0.01 * index,
                release=day,
                frequency="daily",
                units="USD per EUR",
            )
            for index, day in enumerate(days)
        ],
    )
    found = leaves(indicators.returns(inputs(store, shipped_settings)))
    local = found["return_5d_local"].value
    base = found["return_5d_base"].value
    start_day = AS_OF - timedelta(
        days=GridParams.from_settings(shipped_settings).sessions_to_days(5)
    )
    rates = {day: 1.00 + 0.01 * i for i, day in enumerate(days)}
    start_rate = rates[max(day for day in days if day <= start_day)]
    assert local is not None
    assert base == pytest.approx((1.0 + local) * start_rate / rates[AS_OF] - 1.0)
    assert base is not None
    assert base < local


def test_an_fx_series_in_other_units_is_refused_rather_than_inverted(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 30)
    days = business_days(AS_OF, 30)
    repos.upsert_macro(
        store,
        [
            observation("EURUSD", "ecb", d, 1.1, release=d, frequency="daily", units="USD per EUR")
            for d in days
        ],
    )
    settings = shipped_settings.model_copy(
        update={
            "sources": shipped_settings.sources.model_copy(
                update={
                    "macro": shipped_settings.sources.macro.model_copy(
                        update={
                            "series": [
                                s.model_copy(update={"units": "EUR per USD"})
                                if s.id == "EURUSD"
                                else s
                                for s in shipped_settings.sources.macro.series
                            ]
                        }
                    )
                }
            )
        }
    )
    found = leaves(indicators.returns(inputs(store, settings)))
    assert found["return_5d_base"].status is NA
    assert "would invert" in (found["return_5d_base"].reason or "")


def test_a_market_in_the_base_currency_reads_its_return_as_the_base_return(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, DAX, 30)
    found = leaves(indicators.returns(inputs(store, shipped_settings, market="germany", conid=DAX)))
    assert found["return_21d_base"].value == found["return_21d_local"].value
    currency = leaves(
        indicators.currency_return(inputs(store, shipped_settings, market="germany", conid=DAX))
    )
    assert currency["currency_return_12m"].status is INAPPLICABLE


# ── 2-4. range position ──


def test_the_52_week_range_reads_weekly_closes_derived_from_daily_bars(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 400)
    bars = repos.bar_series(store, SPX, as_of=AS_OF)
    weekly: dict[tuple[int, int], float] = {}
    for bar in bars:
        iso = bar.session_date.isocalendar()
        weekly[(iso.year, iso.week)] = bar.close
    year = list(weekly.values())[-52:]
    found = leaves(indicators.distance_from_high(inputs(store, shipped_settings)))
    assert found["distance_from_52w_high"].value == pytest.approx(year[-1] / max(year) - 1.0)
    assert found["distance_from_52w_high"].window == "52w (52 bars)"


def test_a_range_below_the_percentile_floor_is_absent_with_its_count(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 60)
    found = leaves(indicators.distance_from_low(inputs(store, shipped_settings)))
    cell = found["distance_from_52w_low"]
    assert cell.status is NA
    assert "below the 26" in (cell.reason or "")


def test_drawdown_is_the_worst_fall_not_the_distance_from_the_high(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """A series fully recovered from a collapse: zero from its high, a deep drawdown behind."""
    repos.upsert_instrument(store, instrument(SPX))
    days = business_days(AS_OF, 300)
    path = [100.0] * 100 + [60.0] * 100 + [101.0] * 100
    repos.upsert_bars(
        store,
        [
            Bar(conid=SPX, session_date=d, open=p, high=p, low=p, close=p, volume=None, source="t")
            for d, p in zip(days, path, strict=True)
        ],
    )
    cell = inputs(store, shipped_settings)
    assert leaves(indicators.distance_from_high(cell))["distance_from_long_run_high"].value == 0.0
    assert leaves(indicators.drawdown(cell))["max_drawdown_long_run"].value == pytest.approx(-0.4)


# ── 5-6. realised volatility ──


def test_a_realised_vol_percentile_counts_independent_readings(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """Window honesty: about 504 sessions hold sixteen non-overlapping 30-day readings, and
    n_obs says so (lite measured 474 overlapping readings resting on sixteen)."""
    seed_bars(store, SPX, 640)
    found = leaves(indicators.realised_vol(inputs(store, shipped_settings)))
    level = found["realised_vol_30d"]
    percentile = found["realised_vol_30d_percentile"]
    assert level.status is OK
    assert level.n_obs == 30
    assert percentile.status is OK
    held = len(inputs(store, shipped_settings).window_daily)
    assert percentile.n_obs == (held - 1) // 30
    assert percentile.n_obs is not None
    assert percentile.n_obs < 20
    for estimator in ("parkinson_vol_30d", "garman_klass_vol_30d"):
        assert found[estimator].status is OK
        assert found[f"{estimator}_percentile"].n_obs == held // 30


def test_below_the_independent_floor_the_level_stays_and_the_rank_goes(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 120)
    found = leaves(indicators.realised_vol(inputs(store, shipped_settings)))
    assert found["realised_vol_30d"].status is OK
    assert found["realised_vol_30d_percentile"].status is NA
    assert "non-overlapping" in (found["realised_vol_30d_percentile"].reason or "")


def test_a_restated_open_refuses_garman_klass_and_keeps_parkinson(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 120, restate_opens=True)
    found = leaves(indicators.realised_vol(inputs(store, shipped_settings)))
    assert found["garman_klass_vol_30d"].status is NA
    assert found["garman_klass_vol_30d"].reason_code is not None
    assert found["garman_klass_vol_30d"].reason_code.value == "open_restates_previous_close"
    assert found["parkinson_vol_30d"].status is OK


def test_the_rv_term_slope_is_short_over_long_minus_one(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 200)
    found = leaves(indicators.rv_term_structure(inputs(store, shipped_settings)))
    short, long = found["rv_10d"].value, found["rv_90d"].value
    assert short is not None
    assert long is not None
    assert found["rv_term_slope"].value == pytest.approx(short / long - 1.0)
    assert found["rv_term_slope"].window == "10d vs 90d"


# ── 7-9. implied volatility ──


def seed_chain(store: Store, settings: ObliskConfig, expiries: list[int], **kwargs: object) -> None:
    seed_bars(store, QQQ, 200)
    spot = repos.bar_series(store, QQQ, as_of=AS_OF)[-1].close
    repos.upsert_chain_rows(
        store,
        chain(
            QQQ,
            spot,
            session=AS_OF,
            expiries=[AS_OF + timedelta(days=d) for d in expiries],
            bsm=BsmInputs.from_config(settings),
            **kwargs,  # type: ignore[arg-type]
        ),
    )


def qqq(store: Store, settings: ObliskConfig, **kwargs: object) -> CellInputs:
    return inputs(
        store,
        settings,
        sector="technology",
        conid=QQQ,
        asset_class=AssetClass.ETF,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_declared_unreachable_chain_makes_every_option_leaf_inapplicable(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    cell = qqq(store, shipped_settings, options_unavailable="no chain on this route")
    for family in (indicators.implied_vol, indicators.iv_percentile, indicators.iv_hv_spread):
        for leaf in family(cell):
            assert leaf.status is INAPPLICABLE
            assert leaf.reason_code is not None
            assert leaf.reason_code.value == "options_not_available"
    assert set(leaves(indicators.implied_vol(cell))) == set(indicators.IMPLIED_VOL_LEAVES)


def test_the_term_structure_reads_the_two_target_legs(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_chain(store, shipped_settings, [95, 180])
    found = leaves(indicators.implied_vol(qqq(store, shipped_settings)))
    assert found["implied_vol_atm_front"].status is OK
    assert found["implied_vol_term_slope"].window == "95d vs 180d"
    front, back = found["implied_vol_atm_front"].value, found["implied_vol_atm_back"].value
    assert front is not None
    assert back is not None
    assert found["implied_vol_term_slope"].value == pytest.approx(front / back - 1.0)
    assert found["implied_vol_atm"].market_data_status is MarketDataStatus.DELAYED


def test_one_listed_expiry_is_named_before_any_tolerance(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_chain(store, shipped_settings, [95])
    found = leaves(indicators.implied_vol(qqq(store, shipped_settings)))
    for leaf in indicators.TERM_LEAVES:
        assert found[leaf].status is NA
        assert "only one listed expiry" in (found[leaf].reason or "")


def test_a_leg_beyond_its_tolerance_is_refused_with_the_dte_it_reached(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_chain(store, shipped_settings, [95, 120])
    found = leaves(indicators.implied_vol(qqq(store, shipped_settings)))
    reason = found["implied_vol_term_slope"].reason or ""
    assert "back leg" in reason
    assert "120d out" in reason


def test_a_skew_band_that_cannot_reach_the_delta_refuses(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """Without deltas there is no 25-delta strike to interpolate to."""
    seed_chain(store, shipped_settings, [95, 180])
    rows = repos.chain_rows(store, as_of=AS_OF, underlying_conid=QQQ)
    repos.upsert_chain_rows(store, [row.model_copy(update={"delta": None}) for row in rows])
    found = leaves(indicators.implied_vol(qqq(store, shipped_settings)))
    assert found["implied_vol_skew"].status is NA
    assert "does not reach" in (found["implied_vol_skew"].reason or "")


def test_no_chain_is_a_collection_gap_where_one_is_reachable(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, QQQ, 60)
    found = leaves(indicators.implied_vol(qqq(store, shipped_settings)))
    assert {cell.status for cell in found.values()} == {NA}
    assert found["implied_vol_atm"].reason_code is not None
    assert found["implied_vol_atm"].reason_code.value == "no_option_chain"


def snapshot(day: date, iv: float | None, solved: float | None = None) -> Snapshot:
    return Snapshot(
        snapshot_date=day,
        conid=QQQ,
        last=100.0,
        bid=None,
        ask=None,
        volume=None,
        implied_vol=iv,
        implied_vol_source=None if iv is None else "t",
        historical_vol_30d=None,
        dividend_yield=None,
        dividend_yield_basis=None,
        implied_vol_solved=solved,
        implied_vol_solved_source=None
        if solved is None
        else "solved_bsm_european/q_none_collected",
        source="t",
        collected_at=STAMP,
        market_data_status=None,
        market_data_raw=None,
        quote_delay_seconds=None,
    )


def test_the_self_percentile_waits_for_its_accumulated_snapshots(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, QQQ, 400)
    days = business_days(AS_OF, 300)
    repos.upsert_snapshots(store, [snapshot(d, 0.2) for d in days[:100]])
    found = leaves(indicators.iv_percentile(qqq(store, shipped_settings)))
    assert found["iv_percentile"].status is NA
    assert "100 of the 252" in (found["iv_percentile"].reason or "")
    repos.upsert_snapshots(store, [snapshot(d, 0.1 + i / 1000) for i, d in enumerate(days)])
    found = leaves(indicators.iv_percentile(qqq(store, shipped_settings)))
    assert found["iv_percentile"].status is OK
    assert found["iv_percentile"].value == 1.0  # the newest is the highest of a rising series


def test_the_atm_order_is_declared_and_a_solved_vol_says_so(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, QQQ, 60)
    repos.upsert_snapshots(store, [snapshot(AS_OF, 0.21, solved=0.19)])
    vendor = indicators.atm_implied_vol(qqq(store, shipped_settings))
    assert vendor.value == 0.21
    solved_first = shipped_settings.model_copy(
        update={"grid": shipped_settings.grid.model_copy(update={"atm_iv_prefer": "solved"})}
    )
    solved = indicators.atm_implied_vol(qqq(store, solved_first))
    assert solved.value == 0.19
    assert "SOLVED" in solved.provenance
    assert "European" in solved.provenance


def test_iv_minus_hv_subtracts_close_to_close(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_chain(store, shipped_settings, [30, 95, 180])
    cell = qqq(store, shipped_settings)
    found = leaves(indicators.iv_hv_spread(cell))
    atm = indicators.atm_implied_vol(cell).value
    realised = close_to_close(cell.window_daily.close, 30, trading_days_per_year=252.0)
    assert atm is not None
    assert realised is not None
    assert found["iv_minus_hv"].value == pytest.approx(atm - realised)


# ── 10. correlation ──


def test_the_benchmark_cell_is_structurally_inapplicable(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 300)
    found = indicators.correlation_to_benchmark(inputs(store, shipped_settings, benchmark=SPX))
    assert {cell.status for cell in found} == {INAPPLICABLE}


def test_a_shift_needs_independent_readings_in_its_norm(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 640)
    seed_bars(store, XIU, 640)
    found = leaves(
        indicators.correlation_to_benchmark(
            inputs(
                store,
                shipped_settings,
                market="canada",
                conid=XIU,
                asset_class=AssetClass.ETF,
                benchmark=SPX,
            )
        )
    )
    assert found["correlation_30d"].status is OK
    assert found["correlation_shift_30d"].status is OK
    assert found["correlation_shift_30d"].n_obs == 2 * 252 // 30
    # Two years hold two 250-day readings: below the floor of four, the shift is absent.
    assert found["correlation_250d"].status is OK
    assert found["correlation_shift_250d"].status is NA
    assert "independent" in (found["correlation_shift_250d"].reason or "")


# ── 11-13. currency and rates ──


def test_a_stale_cpi_print_makes_the_real_rate_stale(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days = business_days(AS_OF, 30)
    repos.upsert_macro(
        store, daily_series("DFF", "fred", days, seed=1, start=4.0, vol=0.01, units="%", level=True)
    )
    old = [
        o
        for o in monthly_series(
            "CPIAUCSL",
            "fred",
            months=36,
            start=300.0,
            step=0.8,
            units="Index 1982-1984=100",
            lag_days=42,
        )
        if o.release_date is not None and o.release_date <= AS_OF - timedelta(days=90)
    ]
    repos.upsert_macro(store, old)
    found = leaves(indicators.policy_rate(inputs(store, shipped_settings)))
    assert found["policy_rate"].status is OK
    assert found["policy_rate_real"].status is STALE
    assert "printed" in (found["policy_rate_real"].window or "")


def test_a_rebased_cpi_refuses_the_real_rate(store: Store, shipped_settings: ObliskConfig) -> None:
    days = business_days(AS_OF, 30)
    repos.upsert_macro(
        store, daily_series("DFF", "fred", days, seed=1, start=4.0, vol=0.01, units="%", level=True)
    )
    cpi = monthly_series(
        "CPIAUCSL",
        "fred",
        months=24,
        start=300.0,
        step=0.8,
        units="Index 1982-1984=100",
        lag_days=10,
    )
    repos.upsert_macro(
        store,
        [
            o.model_copy(update={"units": "Index 2025=100"}) if i >= 18 else o
            for i, o in enumerate(cpi)
        ],
    )
    found = leaves(indicators.policy_rate(inputs(store, shipped_settings)))
    assert found["policy_rate_real"].status is NA
    assert found["policy_rate_real"].reason_code is not None
    assert found["policy_rate_real"].reason_code.value == "mixed_units_in_window"


def test_the_curve_says_which_slope_it_is(store: Store, shipped_settings: ObliskConfig) -> None:
    days = business_days(AS_OF, 30)
    repos.upsert_macro(
        store,
        daily_series("DGS10", "fred", days, seed=2, start=4.0, vol=0.01, units="%", level=True),
    )
    repos.upsert_macro(
        store,
        daily_series("DGS2", "fred", days, seed=3, start=3.5, vol=0.01, units="%", level=True),
    )
    found = leaves(indicators.yield_curve(inputs(store, shipped_settings)))
    assert found["curve_slope"].window == "10y_minus_2y"
    long, slope = found["long_rate_10y"].value, found["curve_slope"].value
    assert long is not None
    assert slope is not None
    assert 0 < long < 1


def test_a_series_released_after_the_day_is_not_read(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """Point in time: a value published after as_of is absent, whatever its reference date."""
    repos.upsert_macro(
        store,
        [
            observation(
                "DGS10",
                "fred",
                AS_OF - timedelta(days=3),
                4.0,
                release=AS_OF + timedelta(days=1),
                frequency="daily",
                units="%",
            )
        ],
    )
    found = leaves(indicators.yield_curve(inputs(store, shipped_settings)))
    assert found["long_rate_10y"].status is NA


# ── 14-15. turnover and positioning ──


def test_an_index_level_has_no_turnover_and_a_fund_without_volume_is_a_gap(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 120, volume=False)
    index = leaves(indicators.turnover(inputs(store, shipped_settings)))
    assert index["turnover_vs_average"].status is INAPPLICABLE
    fund = leaves(indicators.turnover(inputs(store, shipped_settings, asset_class=AssetClass.ETF)))
    assert fund["turnover_vs_average"].status is NA


def test_positioning_reads_the_released_reports(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    sources_repo.upsert_cot_reports(store, cot_reports("13874A", weeks=30))
    found = leaves(indicators.positioning_cells(inputs(store, shipped_settings)))
    level = found["positioning_percentile"]
    change = found[indicators.positioning_change_leaf(4)]
    assert level.status is NA  # warming up: fewer reports than percentile_min_weeks
    assert change.status is OK
    assert change.value == pytest.approx(4000 / 1_000_000)


def test_a_market_with_no_cftc_contract_is_inapplicable(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    found = indicators.positioning_cells(
        inputs(store, shipped_settings, market="germany", conid=None)
    )
    assert {cell.status for cell in found} == {INAPPLICABLE}


# ── the leaf set ──


def test_every_family_emits_the_same_leaves_whatever_it_finds(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    empty = inputs(store, shipped_settings)
    expected = {
        family: [cell.leaf for cell in compute(empty)]
        for family, compute in indicators.INDICATOR_FUNCTIONS
    }
    seed_bars(store, SPX, 640)
    sources_repo.upsert_cot_reports(store, cot_reports("13874A", weeks=160))
    full = inputs(store, shipped_settings)
    for family, compute in indicators.INDICATOR_FUNCTIONS:
        assert [cell.leaf for cell in compute(full)] == expected[family], family
    assert len(indicators.INDICATOR_FUNCTIONS) == 15


# ── refusals on degenerate inputs ──


def flat_bars(conid: int, count: int, *, close: float, ranged: bool) -> list[Bar]:
    return [
        Bar(
            conid=conid,
            session_date=day,
            open=close if ranged else None,
            high=close if ranged else None,
            low=close if ranged else None,
            close=close,
            volume=None,
            source="t",
        )
        for day in business_days(AS_OF, count)
    ]


def test_a_window_opening_on_a_zero_close_has_no_return_and_no_range(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_instrument(store, instrument(SPX))
    repos.upsert_bars(store, flat_bars(SPX, 400, close=0.0, ranged=False))
    cell = inputs(store, shipped_settings)
    returns = leaves(indicators.returns(cell))
    assert returns["return_21d_local"].status is NA
    assert "not positive" in (returns["return_21d_local"].reason or "")
    for found in indicators.distance_from_high(cell):
        assert found.status is NA
        assert "no usable closes" in (found.reason or "")


def test_a_bar_range_of_nothing_is_no_range_estimate(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_instrument(store, instrument(SPX))
    repos.upsert_bars(store, flat_bars(SPX, 200, close=100.0, ranged=True))
    found = leaves(indicators.realised_vol(inputs(store, shipped_settings)))
    ranges = {
        leaf for leaf, cell in found.items() if "carry no usable range" in (cell.reason or "")
    }
    # Garman-Klass is refused first: an open equal to the previous close is a restated one.
    assert ranges == {"parkinson_vol_30d", "parkinson_vol_30d_percentile"}
    assert {found[leaf].status for leaf in ranges} == {NA}


def test_term_legs_on_one_expiry_or_too_close_are_not_a_term_structure(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_chain(store, shipped_settings, [125, 150])
    close = leaves(indicators.implied_vol(qqq(store, shipped_settings)))
    assert "25d apart" in (close["implied_vol_term_slope"].reason or "")
    terms = shipped_settings.smile.term_structure.model_copy(
        update={"max_target_deviation_days": 60}
    )
    wide = shipped_settings.model_copy(
        update={"smile": shipped_settings.smile.model_copy(update={"term_structure": terms})}
    )
    store.execute("DELETE FROM chain_snapshots")
    seed_chain(store, shipped_settings, [140, 400])
    same = leaves(indicators.implied_vol(qqq(store, wide)))
    assert "same listed expiry (140d)" in (same["implied_vol_term_slope"].reason or "")


def test_a_chain_without_vols_yields_no_term_leg(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_chain(store, shipped_settings, [95, 180], with_iv=False)
    found = leaves(indicators.implied_vol(qqq(store, shipped_settings)))
    assert "front leg" in (found["implied_vol_term_slope"].reason or "")
    assert found["implied_vol_atm_front"].status is NA


def test_a_self_percentile_with_no_recent_implied_vol_is_absent(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days = business_days(AS_OF, 520)
    repos.upsert_instrument(store, instrument(QQQ))
    repos.upsert_snapshots(
        store, [snapshot(d, 0.2 if i < 260 else None, solved=0.2) for i, d in enumerate(days)]
    )
    found = leaves(indicators.iv_percentile(qqq(store, shipped_settings)))
    assert found["iv_percentile"].status is NA
    assert "carries a finite implied vol" in (found["iv_percentile"].reason or "")


def test_iv_minus_hv_needs_a_realised_vol_too(store: Store, shipped_settings: ObliskConfig) -> None:
    seed_bars(store, QQQ, 20)
    spot = repos.bar_series(store, QQQ, as_of=AS_OF)[-1].close
    repos.upsert_chain_rows(
        store,
        chain(
            QQQ,
            spot,
            session=AS_OF,
            expiries=[AS_OF + timedelta(days=d) for d in (95, 180)],
            bsm=BsmInputs.from_config(shipped_settings),
        ),
    )
    found = leaves(indicators.iv_hv_spread(qqq(store, shipped_settings)))
    assert found["iv_minus_hv"].status is NA
    assert "no realised vol" in (found["iv_minus_hv"].reason or "")


def canada(store: Store, settings: ObliskConfig) -> CellInputs:
    return inputs(
        store, settings, market="canada", conid=XIU, asset_class=AssetClass.ETF, benchmark=SPX
    )


def test_a_flat_series_has_no_correlation_and_says_why(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_bars(store, SPX, 300)
    repos.upsert_instrument(store, instrument(XIU))
    repos.upsert_bars(store, flat_bars(XIU, 300, close=50.0, ranged=False))
    found = leaves(indicators.correlation_to_benchmark(canada(store, shipped_settings)))
    for window in shipped_settings.grid.correlation_windows_days:
        assert found[f"correlation_{window}d"].reason_code is not None
        assert found[f"correlation_{window}d"].reason_code.value == "no_dispersion"


def test_a_pair_that_never_moves_apart_has_no_norm_to_shift_from(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """The same prices under two conids correlate at one in every window; the sessions only
    the Canadian leg traded are dropped, never paired with a neighbour."""
    seed_bars(store, SPX, 640)
    repos.upsert_instrument(store, instrument(XIU))
    repos.upsert_bars(
        store,
        [
            bar.model_copy(update={"conid": XIU})
            for bar in repos.bar_series(store, SPX, as_of=AS_OF)
        ],
    )
    for bar in repos.bar_series(store, SPX, as_of=AS_OF)[:10]:
        store.execute(
            "DELETE FROM bars WHERE conid = ? AND session_date = ?",
            (SPX, bar.session_date.isoformat()),
        )
    found = leaves(indicators.correlation_to_benchmark(canada(store, shipped_settings)))
    assert found["correlation_30d"].value == pytest.approx(1.0)
    assert found["correlation_shift_30d"].status is NA
    assert "no dispersion" in (found["correlation_shift_30d"].reason or "")


def test_a_policy_rate_without_a_declared_cpi_keeps_the_real_rate_inapplicable(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_macro(
        store,
        monthly_series(
            "IRSTCI01JPM156N", "fred", months=12, start=0.5, step=0.0, units="%", lag_days=20
        ),
    )
    found = leaves(indicators.policy_rate(inputs(store, shipped_settings, market="japan")))
    assert found["policy_rate"].status is OK
    assert found["policy_rate_real"].status is INAPPLICABLE


def test_a_curve_without_its_second_point_has_no_slope(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_macro(
        store,
        daily_series(
            "DGS10",
            "fred",
            business_days(AS_OF, 30),
            seed=2,
            start=4.0,
            vol=0.01,
            units="%",
            level=True,
        ),
    )
    cell = inputs(store, shipped_settings)
    uncollected = leaves(indicators.yield_curve(cell))
    assert uncollected["long_rate_10y"].status is OK
    assert uncollected["curve_slope"].status is NA
    cell.spec = dataclasses.replace(
        cell.spec, slope_short=None, slope_label=None, slope_gap="no short rate is declared"
    )
    undeclared = leaves(indicators.yield_curve(cell))
    assert undeclared["curve_slope"].status is INAPPLICABLE
    assert "no short rate is declared" in (undeclared["curve_slope"].reason or "")


# ── the macro reader ──


def reader(store: Store, settings: ObliskConfig) -> MacroReader:
    return MacroReader(store, MacroCatalog.from_settings(settings), AS_OF)


def test_the_reader_reads_declared_series_only_and_says_why_one_is_empty(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    assert reader(store, shipped_settings).value_at("NOT_DECLARED", AS_OF) is None
    assert "is not declared" in reader(store, shipped_settings).absence("NOT_DECLARED")
    repos.upsert_macro(
        store,
        [
            observation(
                "DGS10",
                "fred",
                AS_OF - timedelta(days=3),
                4.0,
                release=None,
                frequency="daily",
                units="%",
            )
        ],
    )
    assert "none carries a release date" in reader(store, shipped_settings).absence("DGS10")


def test_cpi_year_on_year_is_read_as_published_or_from_a_print_a_year_back(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    universe = shipped_settings.universe
    japan = cpi_yoy(
        reader(store, shipped_settings), market_spec(universe, "japan"), days_per_year=365.25
    )
    assert japan.value is None
    assert japan.problem == market_spec(universe, "japan").cpi_gap
    repos.upsert_macro(
        store,
        monthly_series("UK_CPI_YOY", "ons", months=3, start=3.2, step=0.0, units="%", lag_days=20),
    )
    uk = cpi_yoy(reader(store, shipped_settings), market_spec(universe, "uk"), days_per_year=365.25)
    assert uk.value == pytest.approx(0.032)
    repos.upsert_macro(
        store,
        monthly_series(
            "CPIAUCSL",
            "fred",
            months=6,
            start=300.0,
            step=0.8,
            units="Index 1982-1984=100",
            lag_days=20,
        ),
    )
    us = cpi_yoy(reader(store, shipped_settings), market_spec(universe, "us"), days_per_year=365.25)
    assert us.value is None
    assert "a year before" in (us.problem or "")
