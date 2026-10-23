"""The measurement store's repository, and the solved implied vol's column-targeted writer.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, timedelta

import pytest
from measure_rig import AS_OF, STAMP

from obliskiq.domain.market import ChainRow, Snapshot
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.errors import StoreError
from obliskiq.store import Store, measurements, repos


def row(**overrides: object) -> Measurement:
    fields: dict[str, object] = {
        "entity_kind": EntityKind.EQUITY_CELL,
        "entity_id": "us/broad",
        "indicator": "returns",
        "leaf": "return_5d_local",
        "as_of": AS_OF,
        "value": 0.01,
        "status": MeasurementStatus.OK,
        "reason": None,
        "reason_code": None,
        "n_obs": 5,
        "window": "5d (6 bars)",
        "units": "decimal",
        "source": "bars:416904",
        "market_data_status": None,
        "inputs_hash": "b" * 64,
        "computed_at": STAMP,
    }
    fields.update(overrides)
    return Measurement(**fields)  # type: ignore[arg-type]


def test_a_measurement_round_trips(store: Store) -> None:
    written = row()
    measurements.upsert_measurements(store, [written])
    assert measurements.session_measurements(store, EntityKind.EQUITY_CELL, as_of=AS_OF) == (
        written,
    )


def test_a_session_read_is_that_session_and_filters_narrow_it(store: Store) -> None:
    measurements.upsert_measurements(
        store,
        [
            row(),
            row(leaf="return_21d_local"),
            row(entity_id="us/energy"),
            row(as_of=AS_OF - timedelta(days=1)),
            row(entity_kind=EntityKind.MACRO_CELL, entity_id="us.rate_10y", leaf="level"),
        ],
    )
    session = measurements.session_measurements(store, EntityKind.EQUITY_CELL, as_of=AS_OF)
    assert len(session) == 3
    assert {r.as_of for r in session} == {AS_OF}
    one = measurements.session_measurements(
        store, EntityKind.EQUITY_CELL, as_of=AS_OF, leaf="return_5d_local", entity_id="us/energy"
    )
    assert len(one) == 1
    prefixed = measurements.session_measurements(
        store, EntityKind.EQUITY_CELL, as_of=AS_OF, leaf_prefix="return_21"
    )
    assert [r.leaf for r in prefixed] == ["return_21d_local"]


def test_the_measured_session_is_the_newest_on_or_before_the_day(store: Store) -> None:
    measurements.upsert_measurements(
        store, [row(as_of=AS_OF - timedelta(days=3)), row(as_of=AS_OF + timedelta(days=1))]
    )
    kind = EntityKind.EQUITY_CELL
    assert measurements.measured_session(store, kind, as_of=AS_OF) == AS_OF - timedelta(days=3)
    assert measurements.measured_session(store, EntityKind.MACRO_CELL, as_of=AS_OF) is None


def test_a_leaf_history_is_newest_first_bounded_and_keeps_its_gaps(store: Store) -> None:
    gap = row(
        as_of=AS_OF - timedelta(days=1),
        value=None,
        status=MeasurementStatus.NA,
        reason="no bars",
        reason_code="sample_too_short",
    )
    measurements.upsert_measurements(
        store,
        [row(), gap, row(as_of=AS_OF - timedelta(days=2)), row(as_of=AS_OF + timedelta(days=1))],
    )
    history = measurements.leaf_history(
        store, EntityKind.EQUITY_CELL, "us/broad", "return_5d_local", as_of=AS_OF
    )
    assert [r.as_of for r in history] == [AS_OF - timedelta(days=d) for d in (0, 1, 2)]
    assert history[1].status is MeasurementStatus.NA
    limited = measurements.leaf_history(
        store, EntityKind.EQUITY_CELL, "us/broad", "return_5d_local", as_of=AS_OF, limit=1
    )
    assert len(limited) == 1
    with pytest.raises(StoreError, match="positive"):
        measurements.leaf_history(
            store, EntityKind.EQUITY_CELL, "us/broad", "x", as_of=AS_OF, limit=0
        )


def test_an_upsert_replaces_its_leaf_and_session(store: Store) -> None:
    measurements.upsert_measurements(store, [row()])
    measurements.upsert_measurements(store, [row(value=0.02)])
    (only,) = measurements.session_measurements(store, EntityKind.EQUITY_CELL, as_of=AS_OF)
    assert only.value == 0.02


def test_a_corrupt_stored_row_raises_rather_than_reading_as_a_default(store: Store) -> None:
    measurements.upsert_measurements(store, [row()])
    store.execute("PRAGMA ignore_check_constraints = 1")
    store.execute("UPDATE measurements SET status = 'weird'")
    store.execute("PRAGMA ignore_check_constraints = 0")
    with pytest.raises(StoreError, match="cannot be read"):
        measurements.session_measurements(store, EntityKind.EQUITY_CELL, as_of=AS_OF)


def test_a_domain_measurement_refuses_an_incoherent_leaf() -> None:
    with pytest.raises(ValueError, match="value exactly when ok"):
        row(value=None)
    with pytest.raises(ValueError, match="says why"):
        row(value=None, status=MeasurementStatus.NA)
    with pytest.raises(ValueError, match="finite"):
        row(value=float("nan"))


# ── the solved implied vol ──


def test_the_solved_writer_touches_only_its_two_columns(
    store: Store, make_instrument: Callable[..., object], make_snapshot: Callable[..., Snapshot]
) -> None:
    repos.upsert_instrument(store, make_instrument())  # type: ignore[arg-type]
    snapshot = make_snapshot(snapshot_date=AS_OF)
    repos.upsert_snapshots(store, [snapshot])
    repos.upsert_solved_implied_vol(
        store,
        conid=snapshot.conid,
        snapshot_date=AS_OF,
        value=0.23,
        method="solved",
        route="r",
        at=STAMP,
    )
    (stored,) = repos.snapshots(store, snapshot.conid, as_of=AS_OF)
    assert stored.implied_vol == snapshot.implied_vol
    assert stored.source == snapshot.source
    assert (stored.implied_vol_solved, stored.implied_vol_solved_source) == (0.23, "solved")


def test_a_session_with_a_chain_and_no_snapshot_gets_a_row_holding_only_the_solve(
    store: Store, make_instrument: Callable[..., object]
) -> None:
    repos.upsert_instrument(store, make_instrument())  # type: ignore[arg-type]
    repos.upsert_solved_implied_vol(
        store,
        conid=756733,
        snapshot_date=AS_OF,
        value=0.2,
        method="solved",
        route="vendor",
        at=STAMP,
    )
    (stored,) = repos.snapshots(store, 756733, as_of=AS_OF)
    assert stored.implied_vol is None
    assert stored.source == "vendor"
    assert stored.implied_vol_solved == 0.2


def test_a_recollected_quote_keeps_the_session_solve(
    store: Store, make_instrument: Callable[..., object], make_snapshot: Callable[..., Snapshot]
) -> None:
    repos.upsert_instrument(store, make_instrument())  # type: ignore[arg-type]
    repos.upsert_snapshots(store, [make_snapshot(snapshot_date=AS_OF)])
    repos.upsert_solved_implied_vol(
        store, conid=756733, snapshot_date=AS_OF, value=0.2, method="solved", route="r", at=STAMP
    )
    repos.upsert_snapshots(store, [make_snapshot(snapshot_date=AS_OF, last=101.0)])
    (stored,) = repos.snapshots(store, 756733, as_of=AS_OF)
    assert stored.last == 101.0
    assert stored.implied_vol_solved == 0.2


def test_chain_underlyings_are_bounded_by_the_day(
    store: Store, make_instrument: Callable[..., object], make_chain_row: Callable[..., ChainRow]
) -> None:
    repos.upsert_instrument(store, make_instrument())  # type: ignore[arg-type]
    repos.upsert_chain_rows(store, [make_chain_row(snapshot_date=date(2026, 10, 8))])
    assert repos.chain_underlyings(store, as_of=date(2026, 10, 8)) == (756733,)
    assert repos.chain_underlyings(store, as_of=date(2026, 10, 7)) == ()
