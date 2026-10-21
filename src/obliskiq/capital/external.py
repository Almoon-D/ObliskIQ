"""Holdings the owner keeps at brokers with no readable API (ported from lite's external_positions).

IBKR is the source of truth for everything it can see; these hand-entered rows make whole-book
risk (Session 17) cover the whole book rather than the part with an API. Beyond
``positions.external_data_stale_days`` since a person last confirmed one, a holding is stale, and
whatever is computed over it is reported unreliable, never as a number known to be wrong (lite's
``RiskEngine.external_holdings``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ..config.schema_instruments import PositionsConfig
from ..domain.capital import ExternalHolding

#: The word printed when external holdings are too old to trust (lite's ``UNRELIABLE``).
UNRELIABLE = "UNRELIABLE"


@dataclass(frozen=True, slots=True)
class ExternalBook:
    """The external holdings as of a day, their total and which are stale."""

    as_of: date
    holdings: tuple[ExternalHolding, ...]
    stale: tuple[str, ...]
    stale_days: int

    @property
    def total_base(self) -> Decimal:
        return sum((h.market_value_base for h in self.holdings), Decimal(0))

    @property
    def reliable(self) -> bool:
        return not self.stale

    def label(self, holding: ExternalHolding) -> str:
        return f"{holding.broker}:{holding.symbol}"

    def reason(self) -> str | None:
        """Why figures over this book are unreliable; ``None`` when they are not."""
        if self.reliable:
            return None
        return (
            f"external holdings {list(self.stale)} were last confirmed more than "
            f"{self.stale_days} days ago; a figure over a stale book is a number known to be wrong"
        )


def external_book(
    holdings: Sequence[ExternalHolding], positions: PositionsConfig, *, as_of: date
) -> ExternalBook:
    """The holdings with the stale ones named (lite: older than the cutoff is stale)."""
    stale_days = positions.external_data_stale_days
    cutoff = as_of - timedelta(days=stale_days)
    stale = tuple(f"{h.broker}:{h.symbol}" for h in holdings if h.last_updated < cutoff)
    return ExternalBook(as_of=as_of, holdings=tuple(holdings), stale=stale, stale_days=stale_days)
