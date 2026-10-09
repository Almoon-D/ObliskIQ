"""Market data as the store holds it: bars, snapshots, option chains and curves.

Prices, volatilities and yields are decimals (0.32 is 32 %), never percents. A missing value is
``None``: the store never holds a placeholder, and a reader never meets a number nobody
measured. Every row names the route that supplied it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from typing import Self

from pydantic import NonNegativeFloat, NonNegativeInt, PositiveFloat, model_validator

from .base import Conid, CurrencyCode, DomainModel, NonEmptyText, UtcDatetime
from .enums import MarketDataStatus, OptionRight


class _QuotedRow(DomainModel):
    """A row captured from a market-data route, with the state the quote was taken in.

    ``market_data_raw`` is the route's own flag, verbatim and never constrained; the decoded
    ``market_data_status`` is ``None`` when the flag is absent or not recognised.
    """

    source: NonEmptyText
    collected_at: UtcDatetime
    market_data_status: MarketDataStatus | None
    market_data_raw: NonEmptyText | None
    quote_delay_seconds: NonNegativeInt | None


class Bar(DomainModel):
    """One daily bar, keyed by the listing venue's session date.

    A bar without a close carries no return and no range, so it is not a bar; the other
    fields may be missing (a listing may restate the previous close instead of an open).
    """

    conid: Conid
    session_date: date
    open: NonNegativeFloat | None
    high: NonNegativeFloat | None
    low: NonNegativeFloat | None
    close: NonNegativeFloat
    volume: NonNegativeFloat | None
    source: NonEmptyText


class Snapshot(_QuotedRow):
    """The state of one underlying on one session.

    ``implied_vol`` is a figure an exchange or vendor quoted; ``implied_vol_solved`` is one
    this system inverted out of a mid price. They are separate fields because a series that
    mixes the two has a methodology break at the join.
    """

    snapshot_date: date
    conid: Conid
    last: NonNegativeFloat | None
    bid: NonNegativeFloat | None
    ask: NonNegativeFloat | None
    volume: NonNegativeInt | None
    implied_vol: NonNegativeFloat | None
    implied_vol_source: NonEmptyText | None
    historical_vol_30d: NonNegativeFloat | None
    dividend_yield: float | None
    dividend_yield_basis: NonEmptyText | None
    implied_vol_solved: NonNegativeFloat | None
    implied_vol_solved_source: NonEmptyText | None

    @model_validator(mode="after")
    def _basis_travels_with_its_figure(self) -> Self:
        if (self.dividend_yield is None) != (self.dividend_yield_basis is None):
            raise ValueError("dividend_yield and dividend_yield_basis are set together")
        if (self.implied_vol_solved is None) != (self.implied_vol_solved_source is None):
            raise ValueError("implied_vol_solved and implied_vol_solved_source are set together")
        return self


class ChainRow(_QuotedRow):
    """One strike of one expiry on one session."""

    snapshot_date: date
    underlying_conid: Conid
    expiry: date
    strike: PositiveFloat
    right: OptionRight
    option_conid: Conid | None
    option_symbol: NonEmptyText | None
    option_exchange: NonEmptyText | None
    bid: NonNegativeFloat | None
    ask: NonNegativeFloat | None
    last: NonNegativeFloat | None
    iv: NonNegativeFloat | None
    delta: float | None
    open_interest: NonNegativeInt | None
    volume: NonNegativeInt | None
    sigma_used: NonNegativeFloat | None
    sigma_source: NonEmptyText | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.sigma_used is None) != (self.sigma_source is None):
            raise ValueError("sigma_used and sigma_source are set together")
        if self.delta is not None and not -1.0 <= self.delta <= 1.0:
            raise ValueError("delta must lie in [-1, 1]")
        return self

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread_pct_of_mid(self) -> float | None:
        """Bid-ask spread as a fraction of mid; ``None`` when either side is absent.

        A crossed book (``ask < bid``) stays a negative number rather than folding into
        ``None``: a consumer must be able to tell "the spread is broken" from "the spread
        could not be measured", and treat a negative reading as a failure, never a pass.
        """
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid


class CurvePoint(_QuotedRow):
    """One expiry of a futures curve on one session; curves are data, never traded."""

    snapshot_date: date
    root: NonEmptyText
    expiry: date
    price: NonNegativeFloat | None
    annualised_roll: float | None
    conid: Conid | None
    exchange: NonEmptyText | None
    currency: CurrencyCode | None
