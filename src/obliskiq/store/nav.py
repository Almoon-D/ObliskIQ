"""``nav_history``: the account's net asset value over time, and its one writer.

Ported from lite's ``store/nav.py``. **One writer**: :func:`record_nav` is the only code that
writes a NAV row (the service's NAV step, ``orchestration/nav.py``, calls it), so two
implementations of the same upsert cannot drift; Session 7 builds the return series on these rows.

**``as_of`` is the date the reading was TAKEN.** Never the date it is about: a series read as a
drawdown or a return must not hold a number the account never had on that day.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..errors import StoreError
from .db import Store
from .mappers import date_text, read_date, read_stamp, stamp_text


@dataclass(frozen=True, slots=True)
class NavReading:
    """One day's reading of the account's net liquidation value, in ``currency``."""

    as_of: date
    nav_base: Decimal
    currency: str
    source: str
    recorded_at: datetime


def record_nav(store: Store, reading: NavReading) -> None:
    """Record a NAV observation, keyed on the day it was read.

    An upsert rather than an insert: re-reading on a day already recorded corrects that day rather
    than failing, and cannot create two observations of which a reader would silently prefer one.
    """
    if not reading.nav_base.is_finite() or reading.nav_base <= 0:
        raise StoreError(f"a NAV of {reading.nav_base} is not a net asset value")
    with store.transaction():
        store.execute(
            "INSERT INTO nav_history (as_of, nav_base, currency, source, recorded_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (as_of) DO UPDATE SET "
            "nav_base = excluded.nav_base, currency = excluded.currency, "
            "source = excluded.source, recorded_at = excluded.recorded_at",
            (
                date_text(reading.as_of),
                str(reading.nav_base),
                reading.currency,
                reading.source,
                stamp_text(reading.recorded_at),
            ),
        )


def nav_series(store: Store, *, since: date, as_of: date) -> list[NavReading]:
    """The readings taken from ``since`` to ``as_of``, oldest first."""
    rows = store.execute(
        "SELECT as_of, nav_base, currency, source, recorded_at FROM nav_history "
        "WHERE as_of >= ? AND as_of <= ? ORDER BY as_of",
        (date_text(since), date_text(as_of)),
    ).fetchall()
    return [
        NavReading(
            as_of=read_date(row["as_of"], "nav_history", "as_of"),
            nav_base=Decimal(row["nav_base"]),
            currency=row["currency"],
            source=row["source"],
            recorded_at=read_stamp(row["recorded_at"], "nav_history", "recorded_at"),
        )
        for row in rows
    ]
