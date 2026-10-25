"""The macro board's leaves per asset class, the views' family reach and correlation flags, and
the census.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest
from measure_rig import (
    AS_OF,
    STAMP,
    business_days,
    curves,
    daily_series,
    monthly_series,
    observation,
)
from options_rig import AS_OF as OPTIONS_AS_OF
from options_rig import SPY, seed_options

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.decision.gates import CandidateGates, Gate
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import Bar, ChainRow
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.grid import views
from obliskiq.grid.census import Census, Verdict
from obliskiq.grid.correlations import leaves_for
from obliskiq.grid.macro_board import MacroBoardBuilder
from obliskiq.store import Store, measurements, repos

OK, NA, STALE, INAPPLICABLE = (
    MeasurementStatus.OK,
    MeasurementStatus.NA,
    MeasurementStatus.STALE,
    MeasurementStatus.INAPPLICABLE,
)


def board(store: Store, settings: ObliskConfig) -> dict[str, dict[str, Measurement]]:
    out: dict[str, dict[str, Measurement]] = {}
    for row in MacroBoardBuilder(store, settings).build(as_of=AS_OF, computed_at=STAMP):
        out.setdefault(row.entity_id, {})[row.leaf] = row
    return out


def test_a_level_cell_reads_its_level_changes_and_rank(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days = business_days(AS_OF, 1400)
    repos.upsert_macro(
        store,
        [
            observation("DGS10", "fred", d, 3.0 + i / 1000, release=d, frequency="daily", units="%")
            for i, d in enumerate(days)
        ],
    )
    cell = board(store, shipped_settings)["us.rate_10y"]
    assert cell["level"].value == pytest.approx((3.0 + 1399 / 1000) / 100)
    assert cell["level"].units == "decimal"
    assert cell["change_3m"].value is not None
    assert cell["change_3m"].value > 0
    assert cell["level_percentile"].value == 1.0  # a rising series is at its high


def test_a_change_needs_an_observation_released_before_its_window(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """Twenty sessions hold no three-month change and fewer observations than a rank needs."""
    days = business_days(AS_OF, 20)
    repos.upsert_macro(
        store,
        daily_series("DGS10", "fred", days, seed=1, start=4.0, vol=0.01, units="%", level=True),
    )
    cell = board(store, shipped_settings)["us.rate_10y"]
    assert cell["level"].status is OK
    assert cell["change_3m"].status is NA
    assert "less than its label" in (cell["change_3m"].reason or "")
    assert cell["level_percentile"].status is NA


def test_the_vol_term_slope_is_short_over_long(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days = business_days(AS_OF, 10)
    repos.upsert_macro(
        store,
        [
            observation("VIXCLS", "fred", d, 20.0, release=d, frequency="daily", units="Index")
            for d in days
        ]
        + [
            observation("VXVCLS", "fred", d, 16.0, release=d, frequency="daily", units="Index")
            for d in days
        ],
    )
    cell = board(store, shipped_settings)["us.vol_index"]
    assert cell["term_slope"].value == pytest.approx(20.0 / 16.0 - 1.0)
    assert cell["level"].units == "index"


def test_the_board_fx_move_is_the_local_currency_against_the_base(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """EURUSD from 1.00 to 1.10 is the euro strengthening: the dollar fell about 9 %."""
    start, end = AS_OF - timedelta(days=400), AS_OF
    repos.upsert_macro(
        store,
        [
            observation(
                "EURUSD", "ecb", start, 1.00, release=start, frequency="daily", units="USD per EUR"
            ),
            observation(
                "EURUSD", "ecb", end, 1.10, release=end, frequency="daily", units="USD per EUR"
            ),
        ],
    )
    cell = board(store, shipped_settings)["us.fx_vs_base"]
    assert cell["currency_return_12m"].value == pytest.approx(1.00 / 1.10 - 1.0)


def test_a_stale_cpi_makes_the_board_real_rate_stale_and_keeps_the_policy_rate(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_macro(
        store,
        daily_series(
            "DFF",
            "fred",
            business_days(AS_OF, 30),
            seed=2,
            start=4.0,
            vol=0.01,
            units="%",
            level=True,
        ),
    )
    cpi = monthly_series(
        "CPIAUCSL",
        "fred",
        months=30,
        start=300.0,
        step=0.8,
        units="Index 1982-1984=100",
        lag_days=42,
    )
    repos.upsert_macro(
        store,
        [
            o
            for o in cpi
            if o.release_date is not None and o.release_date < AS_OF - timedelta(days=60)
        ],
    )
    cell = board(store, shipped_settings)["us.real_rate"]
    assert cell["policy_rate"].status is OK
    assert cell["cpi_yoy"].status is STALE
    assert cell["real_rate"].status is STALE


def test_every_leaf_of_an_unmapped_cell_carries_the_declared_reason(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    cells = board(store, shipped_settings)
    declared = shipped_settings.universe.macro_board.regions["world"].cells["rate_2y"].unmapped
    assert {row.status for row in cells["world.rate_2y"].values()} == {INAPPLICABLE}
    assert all(declared in (row.reason or "") for row in cells["world.rate_2y"].values())


def test_every_cell_of_an_asset_class_carries_the_same_leaves(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    cells = board(store, shipped_settings)
    for asset_class in shipped_settings.universe.macro_board.asset_classes:
        if asset_class == "commodities":
            continue  # a commodities cell's leaves follow its roots
        sets = {
            frozenset(leaves) for ref, leaves in cells.items() if ref.endswith(f".{asset_class}")
        }
        assert len(sets) == 1, asset_class


# ── views ──


def row(
    entity: str, family: str, status: MeasurementStatus, code: str | None = None
) -> Measurement:
    measured = status.has_value
    return Measurement(
        entity_kind=EntityKind.EQUITY_CELL,
        entity_id=entity,
        indicator=family,
        leaf=f"{family}_leaf",
        as_of=AS_OF,
        value=0.1 if measured else None,
        status=status,
        reason=None if status is OK else "r",
        reason_code=None if status is OK else code or "c",
        n_obs=None,
        window=None,
        units=None,
        source=None,
        market_data_status=None,
        inputs_hash="d" * 64,
        computed_at=STAMP,
    )


def test_family_reach_is_counted_in_cells_worst_first() -> None:
    rows = [
        row("us/broad", "implied_vol", OK),
        row("us/energy", "implied_vol", INAPPLICABLE, "options_not_available"),
        row("de/broad", "implied_vol", INAPPLICABLE, "no_instrument_mapped"),
        row("jp/broad", "implied_vol", INAPPLICABLE, "no_instrument_mapped"),
        row("us/broad", "returns", OK),
        row("us/energy", "returns", NA),
        row("de/broad", "returns", INAPPLICABLE, "no_instrument_mapped"),
        row("jp/broad", "returns", STALE),
    ]
    implied, returns = views.family_reach(rows)
    assert (implied.family, implied.cells_reachable, implied.cells_populated) == (
        "implied_vol",
        1,
        1,
    )
    assert (implied.dominant_blocker, implied.blocked_cells) == ("no_instrument_mapped", 2)
    assert (returns.cells_total, returns.cells_reachable, returns.cells_populated) == (4, 3, 2)


def test_a_correlation_shift_is_flagged_beyond_the_configured_deviations(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    names = leaves_for(30)

    def leaf(name: str, value: float) -> Measurement:
        return row("equity:germany|equity:us", "x", OK).model_copy(
            update={"entity_kind": EntityKind.CORRELATION_PAIR, "leaf": name, "value": value}
        )

    measurements.upsert_measurements(store, [leaf(names[0], 0.5), leaf(names[-1], -1.5)])
    (flagged,) = [
        p for p in views.correlation_matrix(store, shipped_settings, as_of=AS_OF) if p.window == 30
    ]
    assert flagged.flagged
    measurements.upsert_measurements(store, [leaf(names[-1], 0.5)])
    (calm,) = [
        p for p in views.correlation_matrix(store, shipped_settings, as_of=AS_OF) if p.window == 30
    ]
    assert not calm.flagged


# ── the census ──


GAP = "no budget is known to this run"


def test_the_census_says_no_data_without_a_chain(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    result = Census(shipped_settings).run(store, as_of=AS_OF, budget_gap=GAP)
    assert result.verdict is Verdict.NO_DATA
    assert "could not" not in result.report()


def test_the_census_never_reports_an_untested_gate_as_passed(
    store: Store,
    shipped_settings: ObliskConfig,
    make_instrument: Callable[..., Instrument],
    make_chain_row: Callable[..., ChainRow],
    make_bar: Callable[..., Bar],
) -> None:
    repos.upsert_instrument(store, make_instrument())
    repos.upsert_bars(store, [make_bar(session_date=AS_OF, close=100.0)])
    expiry = AS_OF + timedelta(days=120)
    repos.upsert_chain_rows(
        store,
        [
            make_chain_row(snapshot_date=AS_OF, expiry=expiry, strike=strike, delta=delta)
            for strike, delta in ((100.0, 0.5), (105.0, 0.4))
        ],
    )
    measurements.upsert_measurements(
        store, [row(f"m{i}/broad", "implied_vol", OK) for i in range(3)]
    )
    result = Census(shipped_settings).run(store, as_of=AS_OF, budget_gap=GAP)
    assert result.verdict is Verdict.UNTESTED
    assert result.contracts_scanned == 2
    assert result.passed_all_gates == 0
    assert result.passed_with_untested == 2
    assert result.untested[Gate.PRICE_CEILING] == 2
    assert result.per_gate.get(Gate.PRICE_CEILING, 0) == 0
    assert "an untested gate is not a passed gate" in result.reason
    assert result.thin_families == ("implied_vol",)
    assert "THIN: implied_vol" in result.report()
    assert "price_ceiling: 0 pass, 2 untested" in result.report()


def test_the_census_counts_a_refusal_per_gate_and_says_thin(
    store: Store,
    shipped_settings: ObliskConfig,
    make_instrument: Callable[..., Instrument],
    make_chain_row: Callable[..., ChainRow],
) -> None:
    repos.upsert_instrument(store, make_instrument())
    repos.upsert_chain_rows(store, [make_chain_row(snapshot_date=AS_OF)])
    result = Census(shipped_settings).run(store, as_of=AS_OF, budget_gap=GAP)
    assert result.verdict is Verdict.THIN
    assert result.per_gate.get(Gate.TENOR, 0) == 0
    assert "below the floor" in result.reason


def test_a_census_given_the_pool_tests_every_gate_and_can_be_viable(
    store: Store, shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    seed_options(store, shipped_settings)
    gates = CandidateGates(shipped_settings)
    pool = gates.affordability(
        budget=Decimal(100_000), penalty_balance=Decimal(0), committed_premium=Decimal(0)
    )
    census = Census(shipped_settings)
    result = census.run(
        store, as_of=OPTIONS_AS_OF, budget_gap=GAP, calendars=calendars, affordability=pool
    )
    assert result.verdict is Verdict.VIABLE, result.report()
    assert result.passed_all_gates >= shipped_settings.census.min_viable_contracts
    assert len(result.survivors) == result.passed_all_gates
    assert result.untested.get(Gate.PRICE_CEILING, 0) == 0
    assert all(name.startswith(f"{SPY}:") for name in result.survivors)
    offline = census.run(store, as_of=OPTIONS_AS_OF, budget_gap=GAP, calendars=calendars)
    assert offline.verdict is Verdict.UNTESTED
    assert offline.passed_all_gates == 0


def test_a_chain_of_no_pinned_instrument_is_no_data(
    store: Store,
    shipped_settings: ObliskConfig,
    make_instrument: Callable[..., Instrument],
    make_chain_row: Callable[..., ChainRow],
) -> None:
    repos.upsert_instrument(store, make_instrument(conid=1, symbol="ZZZ"))
    repos.upsert_chain_rows(store, [make_chain_row(snapshot_date=AS_OF, underlying_conid=1)])
    result = Census(shipped_settings).run(store, as_of=AS_OF, budget_gap=GAP)
    assert result.verdict is Verdict.NO_DATA
    assert "of no instrument the universe pins" in result.reason


# ── board refusals ──


def test_a_level_released_beyond_the_macro_window_is_stale(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days = [d for d in business_days(AS_OF, 200) if d < AS_OF - timedelta(days=60)]
    repos.upsert_macro(
        store,
        daily_series("DGS10", "fred", days, seed=1, start=4.0, vol=0.01, units="%", level=True),
    )
    cell = board(store, shipped_settings)["us.rate_10y"]
    assert cell["level"].status is STALE


def test_a_level_in_units_the_board_cannot_read_is_refused(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    sources = shipped_settings.sources
    series = [
        s.model_copy(update={"units": "Billions of Dollars"}) if s.id == "DGS10" else s
        for s in sources.macro.series
    ]
    settings = shipped_settings.model_copy(
        update={
            "sources": sources.model_copy(
                update={"macro": sources.macro.model_copy(update={"series": series})}
            )
        }
    )
    cell = board(store, settings)["us.rate_10y"]
    assert {row.reason_code for row in cell.values()} == {"units_not_as_declared"}


def test_a_level_carrying_two_units_is_refused(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days = business_days(AS_OF, 30)
    repos.upsert_macro(
        store,
        [
            observation(
                "DGS10", "fred", d, 4.0, release=d, frequency="daily", units="%" if i else "percent"
            )
            for i, d in enumerate(days)
        ],
    )
    cell = board(store, shipped_settings)["us.rate_10y"]
    assert {row.reason_code for row in cell.values()} == {"mixed_units_in_window"}


def test_one_volatility_index_has_no_term_slope(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    data = shipped_settings.model_dump(mode="json")
    data["universe"]["macro_board"]["regions"]["us"]["cells"]["vol_index"].update(
        series=["VIXCLS"], label=None
    )
    settings = ObliskConfig.model_validate(data)
    cell = board(store, settings)["us.vol_index"]
    assert cell["term_slope"].status is INAPPLICABLE
    assert "one volatility index" in (cell["term_slope"].reason or "")


def test_a_curve_change_needs_both_ends_released_before_its_window(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days = business_days(AS_OF, 30)
    for series_id, start in (("DGS10", 4.0), ("DGS2", 3.5)):
        repos.upsert_macro(
            store,
            daily_series(
                series_id, "fred", days, seed=2, start=start, vol=0.01, units="%", level=True
            ),
        )
    cell = board(store, shipped_settings)["us.curve"]
    assert cell["slope"].status is OK
    assert cell["slope_change_3m"].status is NA


def test_a_front_month_that_opened_at_zero_has_no_return(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    cutoff = AS_OF - timedelta(days=80)
    repos.upsert_curve_points(
        store,
        [
            point.model_copy(update={"price": 0.0})
            if point.snapshot_date <= cutoff and point.annualised_roll is None
            else point
            for point in curves()
        ],
    )
    cell = board(store, shipped_settings)["world.commodities"]
    assert cell["front_return_3m_CL"].status is NA
    assert "not positive" in (cell["front_return_3m_CL"].reason or "")


def test_no_correlation_build_is_an_empty_matrix(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    assert views.correlation_matrix(store, shipped_settings, as_of=AS_OF) == []
