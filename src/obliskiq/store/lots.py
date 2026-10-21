"""The lot engine's state in the store: its open lots, its open loss windows, its ledger and its lot
numbering, and every fill the live tax book took with the realisations it made.

The state is the system's own and is saved and read whole (DECISIONS D46, D74): a save replaces
the lots, the windows and the ledger in one transaction, together with the fill that changed
them, so a reader never sees half of one engine's state and a fill is never taken twice. Amounts
are decimals written as text.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from ..domain.tax import (
    CarryForward,
    LedgerState,
    LedgerYear,
    OpenLossWindow,
    Realisation,
    TaxLot,
    TaxYearSummary,
)
from ..errors import StoreError
from .db import Store
from .mappers import date_text, read_date, read_stamp, stamp_text

type FillOutcome = Literal["applied", "held", "incomplete"]

_COLUMNS = "lot_id, security, acquired_on, quantity, cost, deferred_loss, replacement_units"


def _decimal(text: str, column: str, table: str = "tax_lots") -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StoreError(f"{table}.{column} holds {text!r}, which is not a decimal") from None


def save_tax_lots(store: Store, lots: Sequence[TaxLot], *, saved_at: datetime) -> None:
    """Replace every open lot with ``lots``."""
    with store.transaction():
        store.execute("DELETE FROM tax_lots")
        store.executemany(
            f"INSERT INTO tax_lots ({_COLUMNS}, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    lot.lot_id,
                    lot.security,
                    date_text(lot.acquired_on),
                    str(lot.quantity),
                    str(lot.cost),
                    str(lot.deferred_loss),
                    str(lot.replacement_units),
                    stamp_text(saved_at),
                )
                for lot in lots
            ],
        )


def tax_lots(store: Store) -> list[TaxLot]:
    """The open lots, oldest first within each security."""
    rows = store.execute(
        f"SELECT {_COLUMNS} FROM tax_lots ORDER BY security, acquired_on, lot_id"
    ).fetchall()
    lots: list[TaxLot] = []
    for row in rows:
        try:
            lots.append(
                TaxLot(
                    lot_id=row["lot_id"],
                    security=row["security"],
                    acquired_on=read_date(row["acquired_on"], "tax_lots", "acquired_on"),
                    quantity=_decimal(row["quantity"], "quantity"),
                    cost=_decimal(row["cost"], "cost"),
                    deferred_loss=_decimal(row["deferred_loss"], "deferred_loss"),
                    replacement_units=_decimal(row["replacement_units"], "replacement_units"),
                )
            )
        except ValueError as exc:
            raise StoreError(f"tax_lots row {row['lot_id']!r} is invalid: {exc}") from None
    return lots


# ── the whole state ──


@dataclass(frozen=True, slots=True)
class TaxState:
    """Everything the live lot engine holds."""

    lots: tuple[TaxLot, ...]
    windows: tuple[OpenLossWindow, ...]
    ledger: LedgerState
    sequence: int


@dataclass(frozen=True, slots=True)
class TakenFill:
    """A fill the tax book took, and what became of it."""

    execution_id: str
    outcome: FillOutcome
    detail: str
    taken_at: datetime


def _save_state(store: Store, state: TaxState, *, saved_at: datetime) -> None:
    save_tax_lots(store, state.lots, saved_at=saved_at)
    store.execute("DELETE FROM tax_loss_windows")
    store.executemany(
        "INSERT INTO tax_loss_windows (position, security, sold_on, closes_on, loss_per_unit, "
        "units) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                position,
                w.security,
                date_text(w.sold_on),
                date_text(w.closes_on),
                str(w.loss_per_unit),
                str(w.units),
            )
            for position, w in enumerate(state.windows, start=1)
        ],
    )
    store.execute("DELETE FROM tax_ledger_years")
    store.executemany(
        "INSERT INTO tax_ledger_years (tax_year, income, gains, withheld, foreign_income) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (y.tax_year, str(y.income), str(y.gains), str(y.withheld), str(y.foreign_income))
            for y in state.ledger.years
        ],
    )
    store.execute("DELETE FROM tax_carry")
    store.executemany(
        "INSERT INTO tax_carry (position, component, arising_year, amount) VALUES (?, ?, ?, ?)",
        [
            (position, c.component, c.arising_year, str(c.amount))
            for position, c in enumerate(state.ledger.carried, start=1)
        ],
    )
    store.execute("DELETE FROM tax_settled")
    store.executemany(
        "INSERT INTO tax_settled (tax_year, summary) VALUES (?, ?)",
        [(s.tax_year, s.model_dump_json()) for s in state.ledger.settled],
    )
    store.execute(
        "INSERT INTO tax_engine (singleton, lot_sequence, saved_at) VALUES (1, ?, ?) "
        "ON CONFLICT (singleton) DO UPDATE SET lot_sequence = excluded.lot_sequence, "
        "saved_at = excluded.saved_at",
        (state.sequence, stamp_text(saved_at)),
    )


def save_tax_state(store: Store, state: TaxState, *, saved_at: datetime) -> None:
    """Replace the whole saved state with ``state``."""
    with store.transaction():
        _save_state(store, state, saved_at=saved_at)


def record_taken_fill(
    store: Store,
    taken: TakenFill,
    *,
    state: TaxState | None,
    realisations: Sequence[Realisation] = (),
) -> None:
    """Record that the tax book took a fill and, when it changed the engine, the new state and
    the realisations it made, all in one transaction. A fill taken before is refused."""
    with store.transaction():
        if state is not None:
            _save_state(store, state, saved_at=taken.taken_at)
        store.execute(
            "INSERT INTO tax_fills (execution_id, outcome, detail, taken_at) VALUES (?, ?, ?, ?)",
            (taken.execution_id, taken.outcome, taken.detail, stamp_text(taken.taken_at)),
        )
        store.executemany(
            "INSERT INTO tax_realisations (execution_id, security, lot_id, acquired_on, "
            "disposed_on, quantity, proceeds, cost, released, deferred) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    taken.execution_id,
                    r.security,
                    r.lot_id,
                    date_text(r.acquired_on),
                    date_text(r.disposed_on),
                    str(r.quantity),
                    str(r.proceeds),
                    str(r.cost),
                    str(r.released),
                    str(r.deferred),
                )
                for r in realisations
            ],
        )


def taken_fills(store: Store) -> dict[str, TakenFill]:
    """Every fill the tax book took, by execution id."""
    rows = store.execute(
        "SELECT execution_id, outcome, detail, taken_at FROM tax_fills ORDER BY taken_at"
    ).fetchall()
    return {
        row["execution_id"]: TakenFill(
            execution_id=row["execution_id"],
            outcome=row["outcome"],
            detail=row["detail"],
            taken_at=read_stamp(row["taken_at"], "tax_fills", "taken_at"),
        )
        for row in rows
    }


def load_tax_state(store: Store) -> TaxState:
    """The saved state; an empty one when nothing was ever saved."""
    with store.read():
        lots = tax_lots(store)
        windows = [
            _windowed(row)
            for row in store.execute(
                "SELECT security, sold_on, closes_on, loss_per_unit, units FROM tax_loss_windows "
                "ORDER BY position"
            ).fetchall()
        ]
        years = [
            LedgerYear(
                tax_year=row["tax_year"],
                income=_decimal(row["income"], "income", "tax_ledger_years"),
                gains=_decimal(row["gains"], "gains", "tax_ledger_years"),
                withheld=_decimal(row["withheld"], "withheld", "tax_ledger_years"),
                foreign_income=_decimal(
                    row["foreign_income"], "foreign_income", "tax_ledger_years"
                ),
            )
            for row in store.execute(
                "SELECT tax_year, income, gains, withheld, foreign_income FROM tax_ledger_years "
                "ORDER BY tax_year"
            ).fetchall()
        ]
        carried = [
            CarryForward(
                component=row["component"],
                arising_year=row["arising_year"],
                amount=_decimal(row["amount"], "amount", "tax_carry"),
            )
            for row in store.execute(
                "SELECT component, arising_year, amount FROM tax_carry ORDER BY position"
            ).fetchall()
        ]
        settled = [
            TaxYearSummary.model_validate(json.loads(row["summary"]))
            for row in store.execute("SELECT summary FROM tax_settled ORDER BY tax_year").fetchall()
        ]
        engine = store.execute("SELECT lot_sequence FROM tax_engine").fetchone()
    return TaxState(
        lots=tuple(lots),
        windows=tuple(windows),
        ledger=LedgerState(years=tuple(years), carried=tuple(carried), settled=tuple(settled)),
        sequence=0 if engine is None else int(engine["lot_sequence"]),
    )


def _windowed(row: sqlite3.Row) -> OpenLossWindow:
    table = "tax_loss_windows"
    try:
        return OpenLossWindow(
            security=row["security"],
            sold_on=read_date(row["sold_on"], table, "sold_on"),
            closes_on=read_date(row["closes_on"], table, "closes_on"),
            loss_per_unit=_decimal(row["loss_per_unit"], "loss_per_unit", table),
            units=_decimal(row["units"], "units", table),
        )
    except ValueError as exc:
        raise StoreError(f"a tax_loss_windows row is invalid: {exc}") from None


def realisations_between(store: Store, *, since: date, until: date) -> list[Realisation]:
    """The realisations disposed of from ``since`` to ``until``, in the order they were made."""
    rows = store.execute(
        "SELECT security, lot_id, acquired_on, disposed_on, quantity, proceeds, cost, released, "
        "deferred FROM tax_realisations WHERE disposed_on >= ? AND disposed_on <= ? "
        "ORDER BY realisation_id",
        (date_text(since), date_text(until)),
    ).fetchall()
    return [
        Realisation(
            security=row["security"],
            lot_id=row["lot_id"],
            acquired_on=read_date(row["acquired_on"], "tax_realisations", "acquired_on"),
            disposed_on=read_date(row["disposed_on"], "tax_realisations", "disposed_on"),
            quantity=_decimal(row["quantity"], "quantity", "tax_realisations"),
            proceeds=_decimal(row["proceeds"], "proceeds", "tax_realisations"),
            cost=_decimal(row["cost"], "cost", "tax_realisations"),
            released=_decimal(row["released"], "released", "tax_realisations"),
            deferred=_decimal(row["deferred"], "deferred", "tax_realisations"),
        )
        for row in rows
    ]
