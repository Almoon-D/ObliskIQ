"""The macro board's leaves per asset class, the views' family reach and correlation flags, and
the census.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import timedelta

import pytest
from measure_rig import AS_OF, STAMP, business_days, daily_series, monthly_series, observation

from obliskiq.config import ObliskConfig
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import ChainRow
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


def test_the_census_says_no_data_without_a_chain(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    result = Census(shipped_settings).run(store, as_of=AS_OF)
    assert result.verdict is Verdict.NO_DATA
    assert "could not" not in result.report()


def test_the_census_never_reports_an_untested_gate_as_passed(
    store: Store,
    shipped_settings: ObliskConfig,
    make_instrument: Callable[..., Instrument],
    make_chain_row: Callable[..., ChainRow],
) -> None:
    repos.upsert_instrument(store, make_instrument())
    repos.upsert_chain_rows(store, [make_chain_row(snapshot_date=AS_OF)])
    measurements.upsert_measurements(
        store, [row(f"m{i}/broad", "implied_vol", OK) for i in range(3)]
    )
    result = Census(shipped_settings).run(store, as_of=AS_OF)
    assert result.verdict is Verdict.UNTESTED
    assert result.contracts_scanned == 1
    assert "none is reported passed" in result.reason
    assert result.thin_families == ("implied_vol",)
    assert "THIN: implied_vol" in result.report()
