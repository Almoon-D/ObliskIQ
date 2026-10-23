"""The equity-grid and macro-board builders: freshness per input, the macro blanket, point in
time, reproducibility and the non-finite backstop.

Ported from lite's ``tests/test_compute_grid.py`` and ``tests/test_point_in_time.py`` onto the
measurement store.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from measure_rig import (
    AS_OF,
    QQQ,
    SPX,
    STAMP,
    business_days,
    daily_series,
    instrument,
    make_bars,
    observation,
    seed,
)

from obliskiq.config import ObliskConfig
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.grid.cells import Cell, to_measurement
from obliskiq.grid.equity_grid import EquityGridBuilder, Freshness, stale_reason
from obliskiq.grid.macro_board import MacroBoardBuilder
from obliskiq.grid.vocabulary import Input, Reason, Units
from obliskiq.store import Store, measurements, repos

OK, NA, STALE, INAPPLICABLE = (
    MeasurementStatus.OK,
    MeasurementStatus.NA,
    MeasurementStatus.STALE,
    MeasurementStatus.INAPPLICABLE,
)


def cell(rows: list[Measurement], entity: str) -> dict[str, Measurement]:
    return {row.leaf: row for row in rows if row.entity_id == entity}


def build(store: Store, settings: ObliskConfig, **kwargs: object) -> list[Measurement]:
    return EquityGridBuilder(store, settings).build(as_of=AS_OF, computed_at=STAMP, **kwargs)  # type: ignore[arg-type]


def test_freshness_is_measured_on_the_input_each_family_reads(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """Old bars make the bar families stale and leave a fresh policy rate alone."""
    repos.upsert_instrument(store, instrument(SPX))
    old = business_days(AS_OF - timedelta(days=30), 300)
    repos.upsert_bars(store, make_bars(SPX, old, seed=3, volume=False))
    repos.upsert_macro(
        store,
        daily_series(
            "DFF",
            "fred",
            business_days(AS_OF, 30),
            seed=4,
            start=4.0,
            vol=0.01,
            units="%",
            level=True,
        ),
    )
    us = cell(build(store, shipped_settings), "us/broad")
    assert us["return_21d_local"].status is STALE
    assert us["return_21d_local"].reason_code == "inputs_stale"
    assert "daily_bars" in (us["return_21d_local"].reason or "")
    assert us["policy_rate"].status is OK


def test_a_group_is_satisfied_by_one_current_member() -> None:
    fresh = Freshness(Input.OPTION_CHAIN, AS_OF, AS_OF, 5)
    old = Freshness(Input.UNDERLYING_SNAPSHOT, AS_OF - timedelta(days=30), AS_OF, 5)
    absent = Freshness(Input.DAILY_BARS, None, AS_OF, 5)
    groups = ((Input.OPTION_CHAIN, Input.UNDERLYING_SNAPSHOT), (Input.DAILY_BARS,))
    found = {Input.OPTION_CHAIN: fresh, Input.UNDERLYING_SNAPSHOT: old, Input.DAILY_BARS: absent}
    assert stale_reason(groups, found) is None
    found[Input.OPTION_CHAIN] = Freshness(Input.OPTION_CHAIN, AS_OF - timedelta(days=9), AS_OF, 5)
    reason = stale_reason(groups, found)
    assert reason is not None
    assert "option_chain" in reason  # the freshest member of the failing group is named
    assert not absent.is_stale  # absent is not stale


def test_the_macro_blanket_lifts_on_the_broad_cell_only(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    rows = build(store, shipped_settings)
    broad = cell(rows, "japan/broad")
    tech = cell(rows, "japan/technology")
    assert broad["policy_rate"].reason_code == "macro_series_absent"
    assert tech["policy_rate"].reason_code == "instrument_not_seeded"
    assert broad["return_5d_local"].reason_code == "instrument_not_seeded"


def test_an_unpinned_instrument_is_told_apart_from_an_unseeded_one(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    universe = shipped_settings.universe
    unpinned = [
        (key, c)
        for key, c in universe.cells().items()
        if c.symbol and universe.instrument(c.symbol).conid is None  # type: ignore[union-attr]
    ]
    rows = build(store, shipped_settings)
    for (market, sector), declared in unpinned:
        reason = cell(rows, f"{market}/{sector}")["return_5d_local"].reason or ""
        assert "pin" in reason, declared.symbol


def test_a_build_is_point_in_time(
    open_test_store: Callable[..., Store], tmp_path: Path, shipped_settings: ObliskConfig
) -> None:
    """Perturbing every input dated after as_of changes no row."""
    base, perturbed = (
        open_test_store(tmp_path / "base.db"),
        open_test_store(tmp_path / "perturbed.db"),
    )
    seed(base, shipped_settings)
    seed(perturbed, shipped_settings)
    later = business_days(AS_OF + timedelta(days=40), 20)
    repos.upsert_bars(
        perturbed,
        [b for b in make_bars(QQQ, later, seed=99, start=500.0) if b.session_date > AS_OF],
    )
    repos.upsert_macro(
        perturbed,
        [
            observation(
                "DGS10",
                "fred",
                AS_OF + timedelta(days=1),
                99.0,
                release=AS_OF + timedelta(days=2),
                frequency="daily",
                units="%",
            ),
            observation(
                "EURUSD",
                "ecb",
                AS_OF + timedelta(days=1),
                9.9,
                release=AS_OF + timedelta(days=1),
                frequency="daily",
                units="USD per EUR",
            ),
        ],
    )
    one = build(base, shipped_settings)
    two = build(perturbed, shipped_settings)
    assert [(r.entity_id, r.leaf, r.value, r.status, r.inputs_hash) for r in one] == [
        (r.entity_id, r.leaf, r.value, r.status, r.inputs_hash) for r in two
    ]
    board_one = MacroBoardBuilder(base, shipped_settings).build(as_of=AS_OF, computed_at=STAMP)
    board_two = MacroBoardBuilder(perturbed, shipped_settings).build(as_of=AS_OF, computed_at=STAMP)
    assert [(r.leaf, r.value) for r in board_one] == [(r.leaf, r.value) for r in board_two]


def test_a_rebuild_reproduces_the_rows_and_a_new_input_changes_the_hash(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed(store, shipped_settings)
    first = build(store, shipped_settings)
    again = build(store, shipped_settings)
    assert [(r.leaf, r.value, r.inputs_hash) for r in first] == [
        (r.leaf, r.value, r.inputs_hash) for r in again
    ]
    bars = repos.bar_series(store, SPX, as_of=AS_OF)
    repos.upsert_bars(store, [bars[-1].model_copy(update={"close": bars[-1].close * 1.01})])
    changed = build(store, shipped_settings)
    before = cell(first, "us/broad")["return_5d_local"]
    after = cell(changed, "us/broad")["return_5d_local"]
    assert before.inputs_hash != after.inputs_hash
    assert before.value != after.value


def test_a_write_replaces_its_session_only(store: Store, shipped_settings: ObliskConfig) -> None:
    builder = EquityGridBuilder(store, shipped_settings)
    builder.write(builder.build(as_of=AS_OF, computed_at=STAMP))
    builder.write(builder.build(as_of=AS_OF - timedelta(days=1), computed_at=STAMP))
    builder.write(builder.build(as_of=AS_OF, computed_at=STAMP))
    history = measurements.leaf_history(
        store, EntityKind.EQUITY_CELL, "us/broad", "policy_rate", as_of=AS_OF
    )
    assert [row.as_of for row in history] == [AS_OF, AS_OF - timedelta(days=1)]


def test_a_non_finite_value_never_reaches_the_store_as_a_measurement() -> None:
    bad = Cell.measured("x", "returns", float("inf"), units=Units.DECIMAL)
    row = to_measurement(
        bad,
        entity_kind=EntityKind.EQUITY_CELL,
        entity_id="us/broad",
        as_of=AS_OF,
        source=None,
        fingerprint={},
        computed_at=STAMP,
    )
    assert row.status is NA
    assert row.value is None
    assert row.reason_code == Reason.NON_FINITE.value


def test_an_inapplicable_leaf_needs_a_structural_code() -> None:
    with pytest.raises(ValueError, match="not a structural"):
        Cell.inapplicable("x", "returns", "why", code=Reason.SHORT_SAMPLE)
    with pytest.raises(ValueError, match="structural code"):
        Cell.absent("x", "returns", "why", code=Reason.NO_INSTRUMENT)
