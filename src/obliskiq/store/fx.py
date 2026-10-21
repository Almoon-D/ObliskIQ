"""``fx_rates``: one unit of a currency in the base currency, as the broker's ledger gave it on a
day. A fill is valued at the rate of its trade date (art. 35's values on the day; DECISIONS D110),
so each cash read records the day's rates; a later reading of the day replaces an earlier one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..errors import StoreError
from .db import Store
from .mappers import date_text, read_date, read_stamp, stamp_text


@dataclass(frozen=True, slots=True)
class FxRate:
    """What one unit of ``currency`` was worth in the base currency on ``on``."""

    currency: str
    on: date
    rate_to_base: Decimal
    source: str
    observed_at: datetime


def record_rates(store: Store, rates: Iterable[FxRate]) -> int:
    """Upsert the day's rates; a rate that is not positive is refused."""
    rows = list(rates)
    for rate in rows:
        if not rate.rate_to_base.is_finite() or rate.rate_to_base <= 0:
            raise StoreError(f"a rate of {rate.rate_to_base} for {rate.currency} is not a rate")
    with store.transaction():
        store.executemany(
            "INSERT INTO fx_rates (currency, on_date, rate_to_base, source, observed_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (currency, on_date) DO UPDATE SET "
            "rate_to_base = excluded.rate_to_base, source = excluded.source, "
            "observed_at = excluded.observed_at",
            [
                (
                    rate.currency,
                    date_text(rate.on),
                    str(rate.rate_to_base),
                    rate.source,
                    stamp_text(rate.observed_at),
                )
                for rate in rows
            ],
        )
    return len(rows)


def rate_on(store: Store, currency: str, on: date) -> FxRate | None:
    """The rate recorded for ``currency`` on ``on``; ``None`` when that day has none."""
    row = store.execute(
        "SELECT currency, on_date, rate_to_base, source, observed_at FROM fx_rates "
        "WHERE currency = ? AND on_date = ?",
        (currency, date_text(on)),
    ).fetchone()
    if row is None:
        return None
    return FxRate(
        currency=row["currency"],
        on=read_date(row["on_date"], "fx_rates", "on_date"),
        rate_to_base=Decimal(row["rate_to_base"]),
        source=row["source"],
        observed_at=read_stamp(row["observed_at"], "fx_rates", "observed_at"),
    )
