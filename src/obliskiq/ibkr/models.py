"""What the read client returns: frozen, typed readings of IBKR's responses.

These are the route's facts as decoded, not domain objects: a collector or the order path maps
them onto the domain. ``None`` always means "not reported", never zero; money that reaches the
order path (cash, NAV, positions) is ``Decimal``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..domain.base import Conid, CurrencyCode, UtcDatetime
from ..domain.enums import MarketDataStatus

#: Money as decoded: exact, and finite because the models refuse NaN and infinity.
type Amount = Decimal


class IbkrModel(BaseModel):
    """Base for the read models: immutable, no unknown fields, no non-finite numbers."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


# ── portfolio ──


class Account(IbkrModel):
    account_id: str = Field(min_length=1)
    currency: CurrencyCode | None
    account_type: str | None

    def __repr__(self) -> str:
        return f"Account(<masked>, currency={self.currency!r})"


class Position(IbkrModel):
    conid: Conid
    quantity: Amount
    market_price: Amount | None
    market_value: Amount | None
    average_cost: Amount | None
    currency: CurrencyCode | None
    asset_class: str | None
    description: str | None
    listing_exchange: str | None


class CashBalance(IbkrModel):
    """Cash in one currency, as the ledger reports it."""

    currency: CurrencyCode
    cash: Amount
    settled_cash: Amount | None


class NavReading(IbkrModel):
    """The account's net liquidation value, in the configured base currency only."""

    nav: Amount
    currency: CurrencyCode


class Ledger(IbkrModel):
    """``/portfolio/{account}/ledger``: the NAV in base currency and cash per currency.

    ``nav`` is ``None`` (with ``nav_refusal`` saying why) when the base row is missing or its
    currency is not the configured base currency: a NAV in another currency is refused, never
    converted.
    """

    nav: NavReading | None
    nav_refusal: str | None
    base_currency: CurrencyCode | None
    balances: tuple[CashBalance, ...]


# ── market data ──


class LastKind(StrEnum):
    """What the ``last`` price is: a trade, the previous close (``C``), or halted (``H``)."""

    TRADE = "trade"
    PRIOR_CLOSE = "prior_close"
    HALTED = "halted"


class DelayBasis(StrEnum):
    """Where a quote's delay figure comes from."""

    REALTIME = "realtime"
    DOCUMENTED_BOUND = "documented_bound"


class Quote(IbkrModel):
    """One decoded snapshot row."""

    conid: Conid
    last: float | None
    last_kind: LastKind | None
    bid: float | None
    ask: float | None
    crossed: bool
    volume: int | None
    open_interest: int | None
    implied_vol: float | None
    historical_vol_30d: float | None
    implied_vol_percentile: float | None
    dividend_yield: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    market_data_status: MarketDataStatus | None
    status_flag: str | None
    delay_seconds: int | None
    delay_basis: DelayBasis | None
    updated_at: UtcDatetime | None

    @property
    def mid(self) -> float | None:
        """The mid of a two-sided, uncrossed book; ``None`` otherwise."""
        if self.bid is None or self.ask is None or self.crossed:
            return None
        return (self.bid + self.ask) / 2

    @property
    def priced(self) -> bool:
        return self.bid is not None or self.ask is not None or self.last is not None


class HistoryBar(IbkrModel):
    start: UtcDatetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None


class History(IbkrModel):
    """``/iserver/marketdata/history`` for one conid. Never assumed to hold what was asked for."""

    conid: Conid
    bars: tuple[HistoryBar, ...]
    status_flag: str | None
    market_data_status: MarketDataStatus | None


# ── contracts ──


class ContractSection(IbkrModel):
    sec_type: str
    months: tuple[str, ...]
    exchanges: tuple[str, ...]


class BondIssuer(IbkrModel):
    issuer_id: str
    name: str | None


class ContractMatch(IbkrModel):
    """One row of ``/iserver/secdef/search``. ``description`` carries the listing venue."""

    conid: Conid
    symbol: str
    company_name: str | None
    description: str | None
    sections: tuple[ContractSection, ...]
    issuers: tuple[BondIssuer, ...]

    def section(self, sec_type: str) -> ContractSection | None:
        return next((s for s in self.sections if s.sec_type == sec_type), None)

    @property
    def has_options(self) -> bool:
        return self.section("OPT") is not None


class ContractInfo(IbkrModel):
    """``/iserver/contract/{conid}/info``."""

    conid: Conid
    symbol: str | None
    sec_type: str | None
    exchange: str | None
    listing_exchange: str | None
    valid_exchanges: tuple[str, ...]
    currency: CurrencyCode | None
    company_name: str | None
    trading_class: str | None
    local_symbol: str | None
    multiplier: float | None
    maturity_date: str | None
    underlying_conid: Conid | None


class ContractIdentity(IbkrModel):
    """A contract pinned by conid, listing venue and currency."""

    conid: Conid
    symbol: str
    listing_exchange: str
    currency: CurrencyCode


class IncrementRule(IbkrModel):
    lower_edge: float
    increment: float = Field(gt=0)


class ContractRules(IbkrModel):
    """``/iserver/contract/rules``: what an order on the contract may look like."""

    conid: Conid
    is_buy: bool
    order_types: tuple[str, ...]
    order_types_outside_rth: tuple[str, ...]
    cash_quantity_order_types: tuple[str, ...]
    tif_types: tuple[str, ...]
    algo_eligible: bool | None
    size_increment: float | None
    increment_rules: tuple[IncrementRule, ...]
    price_magnifier: int | None
    cash_currency: CurrencyCode | None
    error: str | None


class AlgoParameter(IbkrModel):
    parameter_id: str
    name: str | None
    value_class: str | None
    required: bool | None
    legal_values: tuple[str, ...]


class Algo(IbkrModel):
    algo_id: str
    name: str | None
    parameters: tuple[AlgoParameter, ...]


class ScheduledSession(IbkrModel):
    """One regular (liquid-hours) session of a contract."""

    day: date
    open: UtcDatetime
    close: UtcDatetime


class TradingSchedule(IbkrModel):
    """``/contract/trading-schedule``: regular sessions for the days IBKR returned."""

    conid: Conid
    exchange: str | None
    timezone: str | None
    sessions: tuple[ScheduledSession, ...]
    first_day: date | None
    last_day: date | None


class ScanType(IbkrModel):
    code: str
    name: str | None
    instruments: tuple[str, ...]


class ScannerParams(IbkrModel):
    scan_types: tuple[ScanType, ...]
    instruments: tuple[str, ...]
    filters: tuple[str, ...]
    locations: tuple[str, ...]


class ScannerContract(IbkrModel):
    conid: Conid
    symbol: str | None
    listing_exchange: str | None
    sec_type: str | None
    company_name: str | None


class FuturesRung(IbkrModel):
    """One rung of a futures ladder. Data only: a future is never a tradable candidate."""

    root: str
    conid: Conid
    expiry: date
    symbol: str | None


class OptionStrikes(IbkrModel):
    call: tuple[float, ...]
    put: tuple[float, ...]


class BondFilter(IbkrModel):
    name: str
    values: tuple[str, ...]


__all__ = [
    "Account",
    "Algo",
    "AlgoParameter",
    "BondFilter",
    "BondIssuer",
    "CashBalance",
    "ContractIdentity",
    "ContractInfo",
    "ContractMatch",
    "ContractRules",
    "ContractSection",
    "DelayBasis",
    "FuturesRung",
    "History",
    "HistoryBar",
    "IncrementRule",
    "LastKind",
    "Ledger",
    "NavReading",
    "OptionStrikes",
    "Position",
    "Quote",
    "ScanType",
    "ScannerContract",
    "ScannerParams",
    "ScheduledSession",
    "TradingSchedule",
]
