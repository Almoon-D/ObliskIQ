"""The lot engine's open lots in the store.

Lots are the system's own state and are saved and read whole (DECISIONS D46): a save replaces
every open lot in one transaction, so a reader never sees half of one engine's state. Amounts
are decimals written as text.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ..domain.tax import TaxLot
from ..errors import StoreError
from .db import Store
from .mappers import date_text, read_date, stamp_text

_COLUMNS = "lot_id, security, acquired_on, quantity, cost, deferred_loss, replacement_units"


def _decimal(text: str, column: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StoreError(f"tax_lots.{column} holds {text!r}, which is not a decimal") from None


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
