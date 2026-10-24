"""The HAR-RV forecast and the overnight-gap distribution, and the instrument leaves built from
them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from measure_rig import AS_OF, STAMP, business_days, earnings, make_bars

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import AssetClass, InstrumentRole
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import Bar
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.grid import har
from obliskiq.grid.frames import BarFrame
from obliskiq.grid.gaps import Gap, flag_earnings, gaps, restated_share
from obliskiq.grid.har import daily_variances, fit_har
from obliskiq.grid.instrument_measures import InstrumentMeasures, quantile_name
from obliskiq.store import Store, repos
from obliskiq.store import sources as sources_repo

OK, NA, INAPPLICABLE = MeasurementStatus.OK, MeasurementStatus.NA, MeasurementStatus.INAPPLICABLE
FCX = 7089


def har_closes(count: int, *, seed: int = 5) -> list[float]:
    """Returns whose variance follows a HAR recursion, so the fit has structure to find."""
    rng = np.random.default_rng(seed)
    variance = np.full(count, 1e-4)
    returns = np.zeros(count)
    for t in range(22, count):
        week = variance[t - 5 : t].mean()
        month = variance[t - 22 : t].mean()
        variance[t] = 1e-5 + 0.35 * variance[t - 1] + 0.3 * week + 0.25 * month
        returns[t] = rng.normal(0.0, math.sqrt(variance[t]))
    return list(100.0 * np.exp(np.cumsum(returns)))


def test_daily_variances_are_squared_log_returns_of_positive_closes() -> None:
    found = daily_variances([100.0, 110.0, 0.0, 99.0])
    assert found == pytest.approx([math.log(1.1) ** 2, math.log(99 / 110) ** 2])
    assert len(daily_variances([100.0, 0.0])) == 0


def test_a_fit_on_har_structured_returns_forecasts_a_plausible_vol() -> None:
    fit = fit_har(
        har_closes(600),
        horizon=21,
        weekly=5,
        monthly=22,
        min_sessions=252,
        trading_days_per_year=252,
    )
    assert fit.problem is None
    assert fit.forecast_vol is not None
    assert 0.05 < fit.forecast_vol < 0.6
    assert fit.training_rows == 599 - 22 - 21 + 1
    assert len(fit.coefficients) == 4


def test_too_little_history_is_no_forecast() -> None:
    fit = fit_har(
        har_closes(100),
        horizon=21,
        weekly=5,
        monthly=22,
        min_sessions=252,
        trading_days_per_year=252,
    )
    assert fit.forecast_vol is None
    assert "below the 252" in (fit.problem or "")


def test_a_constant_variance_is_a_singular_design() -> None:
    closes = [100.0 * math.exp(0.01 * ((-1) ** i)) for i in range(400)]
    fit = fit_har(
        closes, horizon=21, weekly=5, monthly=22, min_sessions=252, trading_days_per_year=252
    )
    assert fit.forecast_vol is None
    assert "singular" in (fit.problem or "")


def test_a_forecast_below_zero_is_refused_never_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    def negative(design: np.ndarray, targets: np.ndarray, rcond: None) -> tuple[np.ndarray, ...]:
        return (np.array([-1.0, 0.0, 0.0, 0.0]),)

    monkeypatch.setattr(har.np.linalg, "lstsq", negative)
    fit = fit_har(
        har_closes(600),
        horizon=21,
        weekly=5,
        monthly=22,
        min_sessions=252,
        trading_days_per_year=252,
    )
    assert fit.forecast_vol is None
    assert "not positive" in (fit.problem or "")


def test_the_fit_reads_only_what_it_is_given() -> None:
    """Point in time is the caller's bound: a fit on history up to a day is the same whatever
    follows it."""
    closes = har_closes(700)
    one = fit_har(
        closes[:600], horizon=21, weekly=5, monthly=22, min_sessions=252, trading_days_per_year=252
    )
    two = fit_har(
        closes[:600], horizon=21, weekly=5, monthly=22, min_sessions=252, trading_days_per_year=252
    )
    assert one == two


# ── gaps ──


def frame(opens: list[float | None], closes: list[float]) -> BarFrame:
    days = business_days(AS_OF, len(closes))
    return BarFrame.of(
        1,
        [
            Bar(
                conid=1,
                session_date=d,
                open=o,
                high=None,
                low=None,
                close=c,
                volume=None,
                source="t",
            )
            for d, o, c in zip(days, opens, closes, strict=True)
        ],
    )


def test_a_gap_is_the_log_move_from_one_close_to_the_next_open() -> None:
    found = gaps(frame([None, 105.0, None, 90.0], [100.0, 104.0, 100.0, 95.0]))
    assert [round(g[2], 6) for g in found] == [round(math.log(1.05), 6), round(math.log(0.9), 6)]


def test_the_restated_share_counts_opens_equal_to_the_previous_close() -> None:
    assert restated_share(frame([None, 100.0, 101.0], [100.0, 102.0, 103.0])) == 0.5
    assert restated_share(frame([None], [100.0])) is None


def test_an_earnings_gap_is_the_one_whose_window_holds_the_report(calendars: Calendars) -> None:
    thursday, friday = date(2026, 10, 15), date(2026, 10, 16)
    after_close = datetime(2026, 10, 15, 21, 5, tzinfo=UTC)
    raw = [(date(2026, 10, 14), thursday, 0.01), (thursday, friday, -0.08)]
    found = flag_earnings(raw, [after_close], calendars=calendars, venue="XNYS")
    assert [g.earnings for g in found] == [False, True]
    intraday = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    assert [
        g.earnings for g in flag_earnings(raw, [intraday], calendars=calendars, venue="XNYS")
    ] == [
        False,
        False,
    ]


def test_a_window_the_calendar_cannot_place_is_unplaced_not_guessed(calendars: Calendars) -> None:
    long_ago = [(date(2020, 1, 6), date(2020, 1, 7), 0.02)]
    report = [datetime(2020, 1, 6, 21, 0, tzinfo=UTC)]
    assert flag_earnings(long_ago, report, calendars=calendars, venue="XNYS") == [
        Gap(date(2020, 1, 7), 0.02, None)
    ]


def test_quantile_leaf_names() -> None:
    assert [quantile_name(q) for q in (0.01, 0.05, 0.5, 0.95, 0.99, 0.025)] == [
        "q1",
        "q5",
        "q50",
        "q95",
        "q99",
        "q2_5",
    ]


# ── the instrument leaves ──


def seed_stock(
    store: Store,
    make_instrument: Callable[..., Instrument],
    count: int = 520,
    **bar_kwargs: object,
) -> None:
    repos.upsert_instrument(
        store,
        make_instrument(
            conid=FCX,
            symbol="FCX",
            listing_exchange="NYSE",
            asset_class=AssetClass.STOCK,
            roles={InstrumentRole.DATA},
            option_exchange=None,
        ),
    )
    repos.upsert_bars(store, make_bars(FCX, business_days(AS_OF, count), seed=11, **bar_kwargs))  # type: ignore[arg-type]


def stock_leaves(
    store: Store, settings: ObliskConfig, calendars: Calendars
) -> dict[str, Measurement]:
    declared = settings.universe.instrument("FCX")
    assert declared is not None
    rows = InstrumentMeasures(store, settings, calendars).for_instrument(
        declared, FCX, as_of=AS_OF, computed_at=STAMP
    )
    assert {row.entity_kind for row in rows} == {EntityKind.INSTRUMENT}
    return {row.leaf: row for row in rows}


def test_a_stock_without_earnings_dates_is_a_collection_gap(
    store: Store,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    make_instrument: Callable[..., Instrument],
) -> None:
    seed_stock(store, make_instrument)
    found = stock_leaves(store, shipped_settings, calendars)
    assert found["gap_q5"].status is OK
    assert found["gap_ex_earnings_q5"].status is NA
    assert found["gap_earnings_min"].reason_code == "earnings_dates_absent"
    assert found["har_rv_forecast_21d"].status is OK


def test_a_stock_with_earnings_dates_measures_both_sides(
    store: Store,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    make_instrument: Callable[..., Instrument],
) -> None:
    seed_stock(store, make_instrument)
    reports = [datetime(2026, 7, 23, 11, 0, tzinfo=UTC), datetime(2026, 4, 23, 11, 0, tzinfo=UTC)]
    sources_repo.save_earnings_dates(store, earnings("FCX", reports))
    found = stock_leaves(store, shipped_settings, calendars)
    assert found["gap_ex_earnings_q50"].status is OK
    assert found["gap_earnings_min"].status is OK
    assert found["gap_earnings_min"].n_obs == 2
    assert "earnings:FCX" in (found["gap_q5"].source or "")


def test_restated_opens_refuse_the_gap_distribution(
    store: Store,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    make_instrument: Callable[..., Instrument],
) -> None:
    seed_stock(store, make_instrument, restate_opens=True)
    found = stock_leaves(store, shipped_settings, calendars)
    assert found["gap_q5"].reason_code == "open_restates_previous_close"


def test_old_bars_make_the_instrument_leaves_stale(
    store: Store,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    make_instrument: Callable[..., Instrument],
) -> None:
    seed_stock(store, make_instrument)
    later = AS_OF + timedelta(days=30)
    declared = shipped_settings.universe.instrument("FCX")
    assert declared is not None
    rows = InstrumentMeasures(store, shipped_settings, calendars).for_instrument(
        declared, FCX, as_of=later, computed_at=STAMP
    )
    assert {row.status for row in rows if row.leaf == "har_rv_forecast_21d"} == {
        MeasurementStatus.STALE
    }


def test_a_short_history_has_no_forecast_and_too_few_gaps(
    store: Store,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    make_instrument: Callable[..., Instrument],
) -> None:
    seed_stock(store, make_instrument, count=40)
    found = stock_leaves(store, shipped_settings, calendars)
    assert found["har_rv_forecast_21d"].status is NA
    assert found["har_rv_r2_21d"].status is NA
    assert found["gap_q5"].reason_code == "sample_too_short"
    assert "below the 60" in (found["gap_q5"].reason or "")


def test_too_few_gaps_known_not_to_span_a_report_leave_that_side_absent(
    store: Store,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    make_instrument: Callable[..., Instrument],
) -> None:
    seed_stock(store, make_instrument, count=62)
    reports = [datetime(2026, 8, 5, 21, 0, tzinfo=UTC), datetime(2026, 9, 9, 21, 0, tzinfo=UTC)]
    sources_repo.save_earnings_dates(store, earnings("FCX", reports))
    found = stock_leaves(store, shipped_settings, calendars)
    assert found["gap_q5"].status is OK
    assert found["gap_ex_earnings_q5"].status is NA
    assert "known not to span a report" in (found["gap_ex_earnings_q5"].reason or "")
    assert found["gap_earnings_max"].n_obs == 2


def test_a_venue_the_calendars_do_not_know_places_no_earnings_gap(
    store: Store,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    make_instrument: Callable[..., Instrument],
) -> None:
    seed_stock(store, make_instrument)
    reports = [datetime(2026, 7, 23, 11, 0, tzinfo=UTC)]
    sources_repo.save_earnings_dates(store, earnings("FCX", reports))
    declared = shipped_settings.universe.instrument("FCX")
    assert declared is not None
    rows = InstrumentMeasures(store, shipped_settings, calendars).for_instrument(
        declared.model_copy(update={"listing_exchange": "NOWHERE"}),
        FCX,
        as_of=AS_OF,
        computed_at=STAMP,
    )
    found = {row.leaf: row for row in rows}
    assert found["gap_q5"].status is OK
    assert found["gap_ex_earnings_q5"].status is NA
    assert "spans a known earnings report" in (found["gap_earnings_min"].reason or "")
