"""The measurement store: every leaf of every entity, as of the session it was built for.

A build upserts its own session, so a rebuild of a past session replaces that session's rows and
nothing else, and a leaf's history is its rows across sessions. Reads are bounded by ``as_of``:
a session's rows are the ones built for it, and a history holds only sessions on or before it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import Sequence
from datetime import date

from ..domain.enums import MarketDataStatus
from ..domain.measurement import EntityKind, Measurement, MeasurementStatus
from ..errors import StoreError
from .db import Store
from .mappers import date_text, read_date, read_stamp, stamp_text

MEASUREMENT_COLUMNS = (
    "entity_kind, entity_id, indicator, leaf, as_of, value, status, reason, reason_code, n_obs, "
    "window, units, source, market_data_status, inputs_hash, computed_at"
)
_COUNT = 16


def _params(row: Measurement) -> tuple[object, ...]:
    return (
        row.entity_kind.value,
        row.entity_id,
        row.indicator,
        row.leaf,
        date_text(row.as_of),
        row.value,
        row.status.value,
        row.reason,
        row.reason_code,
        row.n_obs,
        row.window,
        row.units,
        row.source,
        None if row.market_data_status is None else row.market_data_status.value,
        row.inputs_hash,
        stamp_text(row.computed_at),
    )


def _from_row(row: sqlite3.Row) -> Measurement:
    try:
        return Measurement(
            entity_kind=EntityKind(row["entity_kind"]),
            entity_id=row["entity_id"],
            indicator=row["indicator"],
            leaf=row["leaf"],
            as_of=read_date(row["as_of"], "measurements", "as_of"),
            value=row["value"],
            status=MeasurementStatus(row["status"]),
            reason=row["reason"],
            reason_code=row["reason_code"],
            n_obs=row["n_obs"],
            window=row["window"],
            units=row["units"],
            source=row["source"],
            market_data_status=(
                None
                if row["market_data_status"] is None
                else MarketDataStatus(row["market_data_status"])
            ),
            inputs_hash=row["inputs_hash"],
            computed_at=read_stamp(row["computed_at"], "measurements", "computed_at"),
        )
    except ValueError as error:
        raise StoreError(f"measurements row {tuple(row)[:4]!r} cannot be read: {error}") from None


def upsert_measurements(store: Store, rows: Sequence[Measurement]) -> int:
    """Insert or replace leaves (one per entity, leaf and session); returns how many were given."""
    if not rows:
        return 0
    placeholders = ", ".join("?" * _COUNT)
    with store.transaction():
        store.executemany(
            f"INSERT INTO measurements ({MEASUREMENT_COLUMNS}) VALUES ({placeholders}) "
            "ON CONFLICT (entity_kind, entity_id, leaf, as_of) DO UPDATE SET "
            "indicator = excluded.indicator, value = excluded.value, status = excluded.status, "
            "reason = excluded.reason, reason_code = excluded.reason_code, "
            "n_obs = excluded.n_obs, window = excluded.window, units = excluded.units, "
            "source = excluded.source, market_data_status = excluded.market_data_status, "
            "inputs_hash = excluded.inputs_hash, computed_at = excluded.computed_at",
            [_params(row) for row in rows],
        )
    return len(rows)


def measured_session(store: Store, entity_kind: EntityKind, *, as_of: date) -> date | None:
    """The newest session on or before ``as_of`` with a build of ``entity_kind``."""
    row = store.execute(
        "SELECT MAX(as_of) FROM measurements WHERE entity_kind = ? AND as_of <= ?",
        (entity_kind.value, date_text(as_of)),
    ).fetchone()
    return None if row[0] is None else read_date(row[0], "measurements", "as_of")


def session_measurements(
    store: Store,
    entity_kind: EntityKind,
    *,
    as_of: date,
    leaf: str | None = None,
    leaf_prefix: str | None = None,
    entity_id: str | None = None,
    indicator: str | None = None,
) -> tuple[Measurement, ...]:
    """The rows built for the session ``as_of``, narrowed by whichever of the filters are given,
    ordered by entity and leaf."""
    sql = f"SELECT {MEASUREMENT_COLUMNS} FROM measurements WHERE entity_kind = ? AND as_of = ?"
    params: list[object] = [entity_kind.value, date_text(as_of)]
    for column, value in (("leaf", leaf), ("entity_id", entity_id), ("indicator", indicator)):
        if value is not None:
            sql += f" AND {column} = ?"
            params.append(value)
    if leaf_prefix is not None:
        sql += " AND substr(leaf, 1, ?) = ?"
        params += [len(leaf_prefix), leaf_prefix]
    sql += " ORDER BY entity_id, leaf"
    return tuple(_from_row(row) for row in store.execute(sql, params).fetchall())


def leaf_history(
    store: Store,
    entity_kind: EntityKind,
    entity_id: str,
    leaf: str,
    *,
    as_of: date,
    limit: int | None = None,
) -> tuple[Measurement, ...]:
    """One leaf across the sessions built on or before ``as_of``, newest first.

    A session where the leaf was not measured is returned with its status and reason like any
    other: a gap in the middle of a series is information, and closing it up would make the
    series look continuous when it is not. Compare values only where ``window`` matches.
    """
    if limit is not None and limit <= 0:
        raise StoreError(f"limit must be positive, got {limit}")
    sql = (
        f"SELECT {MEASUREMENT_COLUMNS} FROM measurements WHERE entity_kind = ? AND entity_id = ? "
        "AND leaf = ? AND as_of <= ? ORDER BY as_of DESC"
    )
    params: list[object] = [entity_kind.value, entity_id, leaf, date_text(as_of)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return tuple(_from_row(row) for row in store.execute(sql, params).fetchall())
