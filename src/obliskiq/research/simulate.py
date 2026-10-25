"""The monthly walk-forward simulator: delta execution, the cost model on every leg and tax as
it arises (MASTER_PLAN §1, §5.9; DECISIONS D60).

At each rebalance date, in order:

1. **Income** paid since the last date on the units held is received: the source withholds at
   the policy's rate and the rest is cash; the gross enters the tax ledger.
2. **Tax** of every year whose payment date has come is settled and becomes a liability.
3. The strategy sees the market **as of the date** (nothing published later) and the account's
   **book** (units, values, and the lot engine's answers about the tax a trade would cost or
   defer), and names its target weights of the account's value after that liability; ``None``
   keeps what is held.
4. **Delta execution**: only the change to each target is traded, and only when it exceeds the
   no-trade band; sells first, then the tax is paid, then buys within the cash left (cash never
   goes below zero, and nothing is borrowed). Every leg is priced by ``quant/costs.py`` on the
   instrument's **listing venue**; a leg the cost model cannot price stops the run, because an
   unknown cost is not zero. A sale whose costs would exceed what it raises (a minimum
   commission on a few units) is not made, as no executor would place it: the units stay held
   and the run counts it. Each sale goes through the lot engine (FIFO, the wash-sale rule) and
   each purchase opens a lot whose cost includes the leg's costs.
5. With a **protective stop** (``Simulator.run(..., stop=...)``), every position is watched on
   the daily closes until the next date: the stop trails the highest close since the position
   was opened by a multiple of its recent daily volatility, fixed at the last date, and the
   whole position is sold at the first close at or below it. A close below the stop fills at
   that close, so a gap through the stop is borne in full; a touch inside a session that
   recovers by the close is not seen. The cash waits for the next date.
6. At the **horizon** everything is sold, every year still open is settled and its tax paid:
   what is left is the liquidation value (a position no sale can turn into cash counts as
   nothing).

Money is ``Decimal``; prices and rates read from research history are floats and are converted
exactly (their shortest representation). The series of mark-to-market values is recorded after
each date's trades and tax; at the horizon it records the value before the terminal sale, and
the liquidation value is kept beside it. So is what was held after each date's trades (cash,
units and their prices), from which ``research.daily`` marks the account on every day between
two dates.

A panel may carry a **daily path** (``research.daily``): the base-currency daily returns of its
instruments through each rebalance date. A strategy reads an estimate of their covariance on a
date from it (``MarketView.covariance``), as of that date and nothing later.

A run **before tax** (``SimulationSettings.taxed`` false) trades the same way and records every
lot, but no tax is withheld, settled or paid: it is the same strategy's pre-tax result, so a
report can state every metric before and after tax.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise
from operator import attrgetter
from typing import Final, Protocol

import numpy as np

from ..domain.enums import AssetClass
from ..domain.research import Observation
from ..domain.tax import ProposedTrade, SecurityIdentity, TaxYearSummary
from ..engines.core.trades import affordable, delta_trades, share, units_of, whole
from ..errors import SimulationError
from ..jurisdiction.tax import TaxPolicy
from ..quant.costs import CostBook, LegCost, OrderTerms
from ..quant.covariance import CovarianceSeries, Estimate, EstimatorRule
from ..quant.trend import TrendRule, TrendState, trend_state
from ..tax.lots import LotEngine
from .daily import DailyPath, Holding
from .stats import annualised_log_growth, max_drawdown, years_between

ZERO = Decimal(0)
ONE = Decimal(1)
#: The reference rates' own currency (ECB rates are units of a currency per euro).
EURO: Final = "EUR"
#: Months in a year, for stepping through month ends.
MONTHS_PER_YEAR: Final = 12


def exact(value: float) -> Decimal:
    """A float as the decimal its shortest representation spells."""
    return Decimal(repr(value))


@dataclass(frozen=True, slots=True)
class SimInstrument:
    """What the simulator needs to know to trade, cost and tax one instrument."""

    key: str
    venue: str
    asset_class: AssetClass
    currency: str
    security: SecurityIdentity
    issuer_country: str | None
    market_cap_usd: Decimal | None
    income_country: str | None

    def __post_init__(self) -> None:
        if self.security.key != self.key:
            raise ValueError(
                f"instrument {self.key} carries the tax identity of {self.security.key}"
            )


@dataclass(frozen=True, slots=True)
class Panel:
    """Point-in-time observations: prices in the listing currency and income per unit by
    instrument key, and euro reference rates (units per euro) by currency; and, for a study that
    reads daily risk, the daily path of its instruments."""

    prices: Mapping[str, tuple[Observation, ...]]
    income: Mapping[str, tuple[Observation, ...]]
    fx: Mapping[str, tuple[Observation, ...]]
    daily: DailyPath | None = None


def _visible(rows: Sequence[Observation], as_of: date) -> list[Observation]:
    return [o for o in rows if o.available_on <= as_of and o.observed_on <= as_of]


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


class IndexedSeries:
    """One series' rows sorted by observation then publication, read by bisection."""

    def __init__(self, rows: Sequence[Observation]) -> None:
        ordered = list(rows)
        if any(
            (a.observed_on, a.available_on) > (b.observed_on, b.available_on)
            for a, b in pairwise(ordered)
        ):
            ordered.sort(key=attrgetter("observed_on", "available_on"))
        self.rows = ordered
        self._ordinals = [row.observed_on.toordinal() for row in self.rows]
        #: Every row public on the day it describes: a read then does not depend on ``as_of``.
        self.as_observed = all(row.available_on == row.observed_on for row in self.rows)

    def latest(self, on: date, as_of: date) -> Observation | None:
        """The latest vintage of the latest observation on or before ``on`` public by
        ``as_of``."""
        position = bisect_right(self._ordinals, on.toordinal())
        while position > 0:
            position -= 1
            row = self.rows[position]
            if row.available_on <= as_of:
                return row
        return None

    @property
    def first(self) -> date | None:
        return self.rows[0].observed_on if self.rows else None

    def through(self, on: date) -> int:
        """How many rows describe days on or before ``on`` (an index into ``rows``)."""
        return bisect_right(self._ordinals, on.toordinal())

    def after(self, on: date, through: date) -> list[Observation]:
        """The rows describing days after ``on`` and on or before ``through``."""
        return self.rows[self.through(on) : bisect_left(self._ordinals, through.toordinal() + 1)]


class PanelIndex:
    """Every price and rate series of a panel, indexed once."""

    def __init__(self, panel: Panel) -> None:
        self.panel = panel
        shared: dict[int, IndexedSeries] = {}

        def indexed(rows: Sequence[Observation]) -> IndexedSeries:
            # Instruments priced on one series (a role's two vehicles) share its index.
            if id(rows) not in shared:
                shared[id(rows)] = IndexedSeries(rows)
            return shared[id(rows)]

        self.prices = {key: indexed(rows) for key, rows in panel.prices.items()}
        self.fx = {currency: indexed(rows) for currency, rows in panel.fx.items()}
        #: Each instrument's closes at its completed month ends, filled as runs ask for them.
        self.completed_months: dict[str, list[tuple[date, float]]] = {}
        #: Trend states by instrument, cash instrument, rule and date: every run on the panel
        #: that asks the same question on the same date gets the same answer.
        self.trends: dict[tuple[str, str, TrendRule, date], TrendState | None] = {}
        #: Listing prices and reference rates by key and date, as read on that date.
        self.listing_prices: dict[tuple[str, date], Decimal | None] = {}
        self.rates: dict[tuple[str, date], Decimal | None] = {}
        #: The daily path's returns of a set of instruments, and the estimates read from them.
        self.daily_series: dict[tuple[str, ...], CovarianceSeries] = {}
        self.estimates: dict[
            tuple[tuple[str, ...], EstimatorRule, bytes, date], Estimate | None
        ] = {}

    def price_series(self, key: str) -> IndexedSeries:
        return self.prices.get(key) or IndexedSeries(())

    def fx_series(self, currency: str) -> IndexedSeries:
        return self.fx.get(currency) or IndexedSeries(())


class Pricing:
    """Prices and reference rates as of a date, fresh enough or not at all, in the listing
    currency or the base currency."""

    def __init__(
        self,
        instruments: Mapping[str, SimInstrument],
        index: PanelIndex,
        settings: SimulationSettings,
    ) -> None:
        self.instruments = instruments
        self.index = index
        self.settings = settings

    @staticmethod
    def _fresh(series: IndexedSeries, on: date, as_of: date, max_age: int) -> Observation | None:
        found = series.latest(on, as_of)
        if found is None or on - found.observed_on > timedelta(days=max_age):
            return None
        return found

    def listing_price(self, key: str, on: date, as_of: date | None = None) -> Decimal | None:
        if as_of is None and (key, on) in self.index.listing_prices:
            return self.index.listing_prices[(key, on)]
        found = self._fresh(
            self.index.price_series(key), on, on if as_of is None else as_of,
            self.settings.max_price_age_days,
        )  # fmt: skip
        value = None if found is None or found.value <= 0 else exact(found.value)
        if as_of is None:
            self.index.listing_prices[(key, on)] = value
        return value

    def per_euro(self, currency: str, on: date, as_of: date | None = None) -> Decimal | None:
        if currency == EURO:
            return ONE
        if as_of is None and (currency, on) in self.index.rates:
            return self.index.rates[(currency, on)]
        found = self._fresh(
            self.index.fx_series(currency), on, on if as_of is None else as_of,
            self.settings.max_fx_age_days,
        )  # fmt: skip
        value = None if found is None or found.value <= 0 else exact(found.value)
        if as_of is None:
            self.index.rates[(currency, on)] = value
        return value

    def to_base(
        self, amount: Decimal, currency: str, on: date, as_of: date | None = None
    ) -> Decimal | None:
        base = self.settings.base_currency
        if currency == base:
            return amount
        local = self.per_euro(currency, on, as_of)
        target = self.per_euro(base, on, as_of)
        if local is None or target is None:
            return None
        return amount * target / local

    def month_end_closes(self, key: str, as_of: date) -> tuple[tuple[date, float], ...]:
        """The base-currency close of ``key`` at each month end from its first month to the
        month of ``as_of`` (the last one at ``as_of`` itself), each public by ``as_of``. A month
        without a fresh close and rate ends the run: a signal over a gap is not computed.

        For a series and rates public on the day they describe, a completed month's close does
        not depend on ``as_of``, so it is computed once per panel and kept."""
        series = self.index.price_series(key)
        first = series.first
        if first is None:
            return ()
        completed = (as_of.year - first.year) * MONTHS_PER_YEAR + as_of.month - first.month
        current = self._month_end_close(key, series, as_of, as_of)
        if not self._cacheable(key, series):
            return (
                *(
                    self._month_end_close(key, series, self._month(first, k), as_of)
                    for k in range(completed)
                ),
                current,
            )
        done = self.index.completed_months.setdefault(key, [])
        while len(done) < completed:
            done.append(self._month_end_close(key, series, self._month(first, len(done)), as_of))
        return (*done[:completed], current)

    def trend(self, key: str, cash: str, rule: TrendRule, as_of: date) -> TrendState | None:
        """The tilt of ``key`` on ``as_of`` over the month ends it shares with ``cash``
        (``quant.trend``); ``None`` while the history is shorter than the rule needs."""
        memo = (key, cash, rule, as_of)
        if memo not in self.index.trends:
            bills = dict(self.month_end_closes(cash, as_of))
            closes = [
                (day, value) for day, value in self.month_end_closes(key, as_of) if day in bills
            ]
            self.index.trends[memo] = trend_state(
                [value for _, value in closes], [bills[day] for day, _ in closes], rule
            )
        return self.index.trends[memo]

    def covariance(
        self, keys: Sequence[str], rule: EstimatorRule, target: np.ndarray, as_of: date
    ) -> Estimate | None:
        """The estimate of ``keys``' daily covariance from the panel's daily path through
        ``as_of`` (a rebalance date), shrunk toward ``target`` (daily units) as ``rule`` says."""
        daily = self.index.panel.daily
        if daily is None:
            raise SimulationError("the panel has no daily path: no daily risk can be estimated")
        names = tuple(keys)
        prior = np.ascontiguousarray(target, dtype=float)
        memo = (names, rule, prior.tobytes(), as_of)
        if memo not in self.index.estimates:
            if names not in self.index.daily_series:
                columns = daily.returns[:, [daily.column(key) for key in names]]
                if np.isnan(columns).any():
                    raise SimulationError(
                        f"{', '.join(names)} lack a daily price from the start of the daily path"
                    )
                self.index.daily_series[names] = CovarianceSeries(columns)
            series = self.index.daily_series[names]
            self.index.estimates[memo] = series.estimate(daily.rows_through(as_of), prior, rule)
        return self.index.estimates[memo]

    @staticmethod
    def _month(first: date, offset: int) -> date:
        """The last day of the month ``offset`` months after ``first``'s."""
        year, month = divmod(
            first.year * MONTHS_PER_YEAR + first.month - 1 + offset, MONTHS_PER_YEAR
        )
        return month_end(year, month + 1)

    def _cacheable(self, key: str, series: IndexedSeries) -> bool:
        currency = self.instruments[key].currency
        rates = {currency, self.settings.base_currency} - {EURO}
        return series.as_observed and (
            currency == self.settings.base_currency
            or all(self.index.fx_series(c).as_observed for c in rates)
        )

    def _month_end_close(
        self, key: str, series: IndexedSeries, on: date, as_of: date
    ) -> tuple[date, float]:
        found = self._fresh(series, on, as_of, self.settings.max_price_age_days)
        converted = (
            None
            if found is None
            else self.to_base(
                exact(found.value), self.instruments[key].currency, found.observed_on, as_of
            )
        )
        if found is None or converted is None or converted <= ZERO:
            raise SimulationError(f"{key} has no month-end close on {on} within the allowed age")
        return month_end(on.year, on.month), float(converted)


class MarketView:
    """What a strategy may see on ``as_of``: only observations public by then."""

    def __init__(self, source: Panel | Pricing, as_of: date) -> None:
        self._pricing = source if isinstance(source, Pricing) else None
        self._index = source.index if isinstance(source, Pricing) else PanelIndex(source)
        self._as_of = as_of

    @property
    def as_of(self) -> date:
        return self._as_of

    def prices(self, key: str) -> tuple[tuple[date, float], ...]:
        rows = _visible(self._index.panel.prices.get(key, ()), self._as_of)
        latest: dict[date, Observation] = {}
        for row in rows:
            known = latest.get(row.observed_on)
            if known is None or row.available_on > known.available_on:
                latest[row.observed_on] = row
        return tuple((day, latest[day].value) for day in sorted(latest))

    def _priced(self) -> Pricing:
        if self._pricing is None:
            raise SimulationError("a view over a bare panel has no base-currency closes")
        return self._pricing

    def month_end_closes(self, key: str) -> tuple[tuple[date, float], ...]:
        """``key``'s base-currency close at each month end through ``as_of`` (see
        :meth:`Pricing.month_end_closes`)."""
        return self._priced().month_end_closes(key, self._as_of)

    def trend(self, key: str, cash: str, rule: TrendRule) -> TrendState | None:
        """``key``'s trend state on ``as_of`` against ``cash`` (see :meth:`Pricing.trend`)."""
        return self._priced().trend(key, cash, rule, self._as_of)

    def covariance(
        self, keys: Sequence[str], rule: EstimatorRule, target: np.ndarray
    ) -> Estimate | None:
        """The estimate of ``keys``' daily covariance as of ``as_of`` (see
        :meth:`Pricing.covariance`)."""
        return self._priced().covariance(keys, rule, target, self._as_of)

    def close(self, key: str) -> Decimal | None:
        """``key``'s base-currency price on ``as_of`` if a fresh one exists, else ``None``."""
        pricing = self._priced()
        listing = pricing.listing_price(key, self._as_of)
        if listing is None:
            return None
        return pricing.to_base(listing, pricing.instruments[key].currency, self._as_of)


class Book:
    """What a strategy may know of the account on a date: units, base-currency values, the
    value it may invest (after the tax now owed), and the lot engine's answers about a trade.
    The tax questions run on a copy of the engine (DECISIONS D74), so asking changes nothing."""

    def __init__(
        self,
        units: Mapping[str, Decimal],
        price: Callable[[str], Decimal],
        investable: Decimal,
        engine: LotEngine,
        on: date,
    ) -> None:
        self.units = dict(units)
        self._price = price
        self.investable = investable
        self._engine = engine
        self.on = on

    def price(self, key: str) -> Decimal:
        """The base-currency price of one unit of ``key``."""
        return self._price(key)

    def value(self, key: str) -> Decimal:
        quantity = self.units.get(key, ZERO)
        return quantity * self._price(key) if quantity > ZERO else ZERO

    def weight(self, key: str) -> Decimal:
        """``key``'s share of the investable value."""
        return self.value(key) / self.investable if self.investable > ZERO else ZERO

    def basis(self, key: str) -> Decimal:
        """What a sale of every unit counts against: the lots' cost and the losses they carry."""
        return self._engine.basis(key)

    def units_for(self, key: str, value: Decimal) -> Decimal:
        """The whole units of ``key`` that ``value`` buys or sells, rounded down."""
        return whole(value / self._price(key))

    def sales_tax(self, sales: Sequence[tuple[str, Decimal]]) -> Decimal:
        """The tax that selling each ``(key, units)`` now, together, would add to the year
        (negative: saved); a loss on one sale offsets a gain on another as the year would."""
        trades = [
            ProposedTrade(
                security=key, side="sell", quantity=units, price=self._price(key), on=self.on
            )
            for key, units in sales
            if units > ZERO
        ]
        return self._engine.trial_tax(trades)

    def purchase_defers_loss(self, key: str) -> bool:
        """Whether buying ``key`` now would defer a loss under the wash-sale rule."""
        return self._engine.purchase_defers_loss(key, self.on)


class Strategy(Protocol):
    """Target weights of the account's investable value, or ``None`` to keep what is held."""

    @property
    def name(self) -> str: ...

    def targets(self, view: MarketView, book: Book) -> Mapping[str, Decimal] | None: ...


@dataclass(frozen=True, slots=True)
class StopRule:
    """A trailing protective stop: ``multiple`` times the standard deviation of the daily log
    returns of the last ``volatility_sessions`` sessions, scaled by the square root of
    ``horizon_sessions``, below the highest close since entry (module docstring, step 5)."""

    volatility_sessions: int
    horizon_sessions: int
    multiple: float

    def __post_init__(self) -> None:
        if self.volatility_sessions < 2 or self.horizon_sessions < 1 or self.multiple <= 0:
            raise ValueError("a stop needs two sessions of volatility, a horizon and a multiple")

    def distance(self, closes: Sequence[float]) -> float | None:
        """The stop's log distance below the peak from the last closes; ``None`` while there
        are too few of them or they do not move."""
        if len(closes) < self.volatility_sessions + 1:
            return None
        window = [math.log(b / a) for a, b in pairwise(closes[-self.volatility_sessions - 1 :])]
        mean = sum(window) / len(window)
        variance = sum((r - mean) ** 2 for r in window) / (len(window) - 1)
        if variance <= 0:
            return None
        return self.multiple * math.sqrt(variance) * math.sqrt(self.horizon_sessions)


@dataclass(frozen=True, slots=True)
class SimulationSettings:
    """The mechanics a run uses; ``cost_multiplier`` scales every leg's cost (1 is the model)."""

    base_currency: str
    initial_capital: Decimal
    no_trade_band: Decimal
    max_price_age_days: int
    max_fx_age_days: int
    whole_units: bool
    days_per_year: float
    cost_multiplier: Decimal
    taxed: bool = True


@dataclass(frozen=True, slots=True)
class TaxPayment:
    tax_year: int
    amount: Decimal
    paid_on: date


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """One run of one strategy."""

    strategy: str
    dates: tuple[date, ...]
    nav: tuple[float, ...]
    initial: Decimal
    liquidation_value: Decimal
    years: float
    log_growth: float | None
    max_drawdown: float
    costs: Decimal
    traded: Decimal
    trades: int
    taxes: tuple[TaxPayment, ...]
    withheld: Decimal
    summaries: tuple[TaxYearSummary, ...]
    cost_labels: frozenset[str]
    #: Sales not made because their costs would exceed what they raise.
    unsold: int = 0
    #: Positions sold at their protective stop.
    stops: int = 0
    #: What was held after each date's trades and tax, every date but the horizon.
    holdings: tuple[Holding, ...] = ()

    @property
    def period_log_returns(self) -> tuple[float, ...]:
        return tuple(math.log(b / a) for a, b in pairwise(self.nav))

    @property
    def tax_paid(self) -> Decimal:
        return sum((payment.amount for payment in self.taxes), ZERO)


@dataclass(slots=True)
class _State:
    cash: Decimal
    next_year: int
    units: dict[str, Decimal] = field(default_factory=dict)
    costs: Decimal = ZERO
    traded: Decimal = ZERO
    trades: int = 0
    unsold: int = 0
    stops: int = 0
    withheld: Decimal = ZERO
    labels: set[str] = field(default_factory=set)
    taxes: list[TaxPayment] = field(default_factory=list)
    #: The day each position was opened, for its trailing stop.
    entries: dict[str, date] = field(default_factory=dict)
    holdings: list[Holding] = field(default_factory=list)


class Simulator:
    """Runs strategies over one panel with one cost book and one tax policy."""

    def __init__(
        self,
        instruments: Sequence[SimInstrument],
        panel: Panel,
        costs: CostBook,
        policy: TaxPolicy,
        settings: SimulationSettings,
    ) -> None:
        self._instruments = {instrument.key: instrument for instrument in instruments}
        if len(self._instruments) != len(instruments):
            raise ValueError("instrument keys must be unique")
        self._panel = panel
        self._costs = costs
        self._policy = policy
        self._settings = settings
        self._pricing = Pricing(self._instruments, PanelIndex(panel), settings)

    # ── prices and rates ──

    def price(self, key: str, on: date) -> Decimal:
        """The listing-currency price of ``key`` on ``on``; a missing or stale one stops the run."""
        value = self._pricing.listing_price(key, on)
        if value is None:
            raise SimulationError(f"{key} has no price on {on} within the allowed age")
        return value

    def _per_euro(self, currency: str, on: date) -> Decimal:
        value = self._pricing.per_euro(currency, on)
        if value is None:
            raise SimulationError(
                f"no reference rate for {currency} on {on} within the allowed age"
            )
        return value

    def to_base(self, amount: Decimal, currency: str, on: date) -> Decimal:
        base = self._settings.base_currency
        if currency == base:
            return amount
        return amount * self._per_euro(base, on) / self._per_euro(currency, on)

    def _base_price(self, key: str, on: date) -> Decimal:
        return self.to_base(self.price(key, on), self._instruments[key].currency, on)

    def _pricer(self, on: date) -> Callable[[str], Decimal]:
        def price(key: str) -> Decimal:
            return self._base_price(key, on)

        return price

    # ── one leg ──

    def _quote(
        self, key: str, side: str, quantity: Decimal, on: date
    ) -> tuple[Decimal, Decimal, tuple[str, ...]]:
        """A leg's cost and notional in the base currency and what in its cost is unverified,
        without recording it."""
        instrument = self._instruments[key]
        price = self.price(key, on)
        cost: LegCost = self._costs.leg_cost(
            OrderTerms(
                instrument.venue, instrument.asset_class, instrument.currency, quantity, price
            ),
            side="buy" if side == "buy" else "sell",
            on=on,
            issuer_country=instrument.issuer_country,
            market_cap_usd=instrument.market_cap_usd,
        )
        total = cost.total
        if total is None:
            gaps = ", ".join(gap.value for gap in cost.gaps) or "a component without a price"
            raise SimulationError(f"the cost model cannot price a {side} of {key} on {on} ({gaps})")
        leg_cost = self.to_base(total * self._settings.cost_multiplier, instrument.currency, on)
        notional = self.to_base(quantity * price, instrument.currency, on)
        return leg_cost, notional, cost.unverified

    @staticmethod
    def _record(quote: tuple[Decimal, Decimal, tuple[str, ...]], state: _State) -> Decimal:
        """Count a quoted leg as traded; returns its cost in the base currency."""
        leg_cost, notional, unverified = quote
        state.labels.update(unverified)
        state.costs += leg_cost
        state.traded += notional
        state.trades += 1
        return leg_cost

    def _sell(
        self, key: str, quantity: Decimal, on: date, engine: LotEngine, state: _State
    ) -> bool:
        """Sell ``quantity`` of ``key``; a sale whose costs would exceed what it raises is not
        made (the units stay held, the run counts it) and returns ``False``."""
        quote = self._quote(key, "sell", quantity, on)
        proceeds = quantity * self._base_price(key, on) - quote[0]
        if proceeds < ZERO:
            state.unsold += 1
            return False
        self._record(quote, state)
        engine.dispose(key, on=on, quantity=quantity, proceeds=proceeds)
        state.cash += proceeds
        left = state.units[key] - quantity
        if left > ZERO:
            state.units[key] = left
        else:
            del state.units[key]
        return True

    def _buy_cost(self, key: str, quantity: Decimal, on: date) -> Decimal:
        """What a buy would take from cash, without recording it."""
        return quantity * self._base_price(key, on) + self._quote(key, "buy", quantity, on)[0]

    def _buy(self, key: str, quantity: Decimal, on: date, engine: LotEngine, state: _State) -> None:
        cost = self._record(self._quote(key, "buy", quantity, on), state)
        paid = quantity * self._base_price(key, on) + cost
        engine.acquire(key, on=on, quantity=quantity, cost=paid)
        state.cash -= paid
        state.units[key] = state.units.get(key, ZERO) + quantity

    def _units(self, value: Decimal, price: Decimal, rounding: str = ROUND_FLOOR) -> Decimal:
        return units_of(value, price, whole_units=self._settings.whole_units, rounding=rounding)

    # ── a run ──

    def run(
        self, strategy: Strategy, dates: Sequence[date], *, stop: StopRule | None = None
    ) -> SimulationResult:
        if len(dates) < 2 or list(dates) != sorted(set(dates)):
            raise SimulationError(
                "a run needs at least two rebalance dates in strictly rising order"
            )
        engine = LotEngine(self._policy, [i.security for i in self._instruments.values()])
        state = _State(cash=self._settings.initial_capital, next_year=dates[0].year)
        nav: list[float] = []
        previous: date | None = None
        *trading, horizon = dates
        for on, following in pairwise(dates):
            if previous is not None:
                self._receive_income(previous, on, engine, state)
            liability = self._taxes_due(on, engine, state, through=None)
            marked = self._marked(on, state)
            book = Book(state.units, self._pricer(on), max(marked - liability, ZERO), engine, on)
            weights = strategy.targets(MarketView(self._pricing, on), book)
            self._rebalance(weights, marked, liability, on, engine, state)
            nav.append(float(self._marked(on, state)))
            state.holdings.append(
                Holding(
                    cash=float(state.cash),
                    units={key: float(q) for key, q in state.units.items()},
                    prices={key: float(self._base_price(key, on)) for key in state.units},
                )
            )
            state.entries = {key: state.entries.get(key, on) for key in state.units}
            if stop is not None:
                self._watch(stop, on, following, engine, state)
            previous = on
        self._receive_income(trading[-1], horizon, engine, state)
        liability = self._taxes_due(horizon, engine, state, through=None)
        nav.append(float(self._marked(horizon, state)))
        for key in sorted(state.units):
            self._sell(key, state.units[key], horizon, engine, state)
        liability += self._taxes_due(horizon, engine, state, through=horizon.year)
        self._pay(liability, horizon, engine, state)
        initial = self._settings.initial_capital
        years = years_between(dates[0], dates[-1], days_per_year=self._settings.days_per_year)
        return SimulationResult(
            strategy=strategy.name,
            dates=tuple(dates),
            nav=tuple(nav),
            initial=initial,
            liquidation_value=state.cash,
            years=years,
            log_growth=annualised_log_growth(float(initial), float(state.cash), years),
            max_drawdown=max_drawdown(nav),
            costs=state.costs,
            traded=state.traded,
            trades=state.trades,
            taxes=tuple(state.taxes),
            withheld=state.withheld,
            summaries=engine.ledger.settled(),
            cost_labels=frozenset(state.labels),
            unsold=state.unsold,
            stops=state.stops,
            holdings=tuple(state.holdings),
        )

    def _watch(
        self, stop: StopRule, on: date, until: date, engine: LotEngine, state: _State
    ) -> None:
        """Sell each position at the first daily close after ``on`` and through ``until`` at
        or below its trailing stop (module docstring, step 5)."""
        for key in sorted(state.units):
            series = self._pricing.index.price_series(key)
            end = series.through(on)
            # The last closes public by ``on``, newest first, as many as the volatility reads.
            recent: list[float] = []
            index = end - 1
            while index >= 0 and len(recent) <= stop.volatility_sessions:
                if series.rows[index].available_on <= on:
                    recent.append(series.rows[index].value)
                index -= 1
            distance = stop.distance(recent[::-1])
            if distance is None:
                continue
            # The peak runs from the last close on or before the day the position was opened.
            opened = max(series.through(state.entries[key]) - 1, 0)
            peak = max(
                (row.value for row in series.rows[opened:end] if row.available_on <= on),
                default=None,
            )
            if peak is None:
                continue
            for row in series.after(on, until):
                if row.available_on > row.observed_on:
                    continue  # not public on the day it describes: no stop could act on it
                if row.value <= peak * math.exp(-distance):
                    if self._sell(key, state.units[key], row.observed_on, engine, state):
                        state.stops += 1
                        del state.entries[key]
                    break
                peak = max(peak, row.value)

    def _marked(self, on: date, state: _State) -> Decimal:
        """Cash plus every position at its price on ``on``, in the base currency."""
        return state.cash + sum(
            (q * self._base_price(key, on) for key, q in state.units.items()), ZERO
        )

    def _receive_income(self, since: date, on: date, engine: LotEngine, state: _State) -> None:
        for key, quantity in sorted(state.units.items()):
            instrument = self._instruments[key]
            for row in self._panel.income.get(key, ()):
                if since < row.observed_on <= on and row.available_on <= on:
                    gross = self.to_base(
                        quantity * exact(row.value), instrument.currency, row.observed_on
                    )
                    event = engine.receive_income(
                        key,
                        on=row.observed_on,
                        gross=gross,
                        source_country=instrument.income_country,
                    )
                    withheld = event.withheld if self._settings.taxed else ZERO
                    state.cash += gross - withheld
                    state.withheld += withheld

    def _taxes_due(
        self, on: date, engine: LotEngine, state: _State, *, through: int | None
    ) -> Decimal:
        """Settle every open year whose payment date has come (or every year through
        ``through``, at the horizon); returns the tax now owed. A run before tax owes none."""
        if not self._settings.taxed:
            return ZERO
        owed = ZERO
        year = state.next_year
        while year <= on.year and (
            (through is not None and year <= through) or self._policy.payment_date(year) <= on
        ):
            summary = engine.ledger.settle(year)
            if summary.tax_due > ZERO:
                state.taxes.append(TaxPayment(year, summary.tax_due, on))
                owed += summary.tax_due
            year += 1
        state.next_year = year
        return owed

    def _pay(self, liability: Decimal, on: date, engine: LotEngine, state: _State) -> None:
        if liability <= ZERO:
            return
        if state.cash < liability:
            self._raise_cash(liability - state.cash, on, engine, state)
        state.cash -= liability

    def _raise_cash(self, shortfall: Decimal, on: date, engine: LotEngine, state: _State) -> None:
        """Sell across holdings in proportion to value until ``shortfall`` more cash is held;
        if the leg costs leave it short, sell whole positions, smallest first."""
        target = state.cash + shortfall
        values = {key: q * self._base_price(key, on) for key, q in state.units.items()}
        held = sum(values.values(), ZERO)
        if held <= ZERO:
            raise SimulationError(f"the account cannot pay {shortfall} of tax on {on}")
        for key in sorted(values):
            share = shortfall * values[key] / held
            units = min(
                state.units[key], self._units(share, self._base_price(key, on), ROUND_CEILING)
            )
            self._sell(key, units, on, engine, state)
        for key in sorted(state.units, key=lambda k: values[k]):
            if state.cash >= target:
                break
            self._sell(key, state.units[key], on, engine, state)
        if state.cash < target:
            raise SimulationError(f"the account cannot pay {shortfall} of tax on {on}")

    def _rebalance(
        self,
        weights: Mapping[str, Decimal] | None,
        marked: Decimal,
        liability: Decimal,
        on: date,
        engine: LotEngine,
        state: _State,
    ) -> None:
        prices = {key: self._base_price(key, on) for key in state.units}
        if weights is None:
            weights = (
                {key: share(q * prices[key], marked) for key, q in state.units.items()}
                if marked > ZERO
                else {}
            )
        self._check(weights)
        for key in weights:
            prices.setdefault(key, self._base_price(key, on))
        investable = max(marked - liability, ZERO)
        sells, buys = delta_trades(
            weights,
            state.units,
            prices,
            investable,
            band=self._settings.no_trade_band,
            whole_units=self._settings.whole_units,
        )
        for key, quantity in sells:
            self._sell(key, quantity, on, engine, state)
        self._pay(liability, on, engine, state)
        plan = affordable(
            buys,
            state.cash,
            prices,
            lambda key, quantity: self._buy_cost(key, quantity, on),
            whole_units=self._settings.whole_units,
        )
        for key, quantity in plan:
            self._buy(key, quantity, on, engine, state)

    def _check(self, weights: Mapping[str, Decimal]) -> None:
        unknown = sorted(set(weights) - set(self._instruments))
        if unknown:
            raise SimulationError(
                f"the strategy names instruments the study does not hold: {unknown}"
            )
        if any(w < ZERO or not w.is_finite() for w in weights.values()):
            raise SimulationError("a target weight is negative or not finite: no shorting")
        if sum(weights.values(), ZERO) > ONE:
            raise SimulationError("target weights sum above 1: no leverage")
