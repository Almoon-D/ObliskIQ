"""The monthly walk-forward simulator: delta execution, the cost model on every leg and tax as
it arises (MASTER_PLAN §1, §5.9; DECISIONS D60).

At each rebalance date, in order:

1. **Income** paid since the last date on the units held is received: the source withholds at
   the policy's rate and the rest is cash; the gross enters the tax ledger.
2. **Tax** of every year whose payment date has come is settled and becomes a liability.
3. The strategy sees the market **as of the date** (nothing published later) and names its
   target weights of the account's value after that liability; ``None`` keeps what is held.
4. **Delta execution**: only the change to each target is traded, and only when it exceeds the
   no-trade band; sells first, then the tax is paid, then buys within the cash left (cash never
   goes below zero, and nothing is borrowed). Every leg is priced by ``quant/costs.py`` on the
   instrument's **listing venue**; a leg the cost model cannot price stops the run, because an
   unknown cost is not zero. Each sale goes through the lot engine (FIFO, the wash-sale rule)
   and each purchase opens a lot whose cost includes the leg's costs.
5. At the **horizon** everything is sold, every year still open is settled and its tax paid:
   what is left is the liquidation value.

Money is ``Decimal``; prices and rates read from research history are floats and are converted
exactly (their shortest representation). The series of mark-to-market values is recorded after
each date's trades and tax; at the horizon it records the value before the terminal sale, and
the liquidation value is kept beside it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise
from typing import Final, Protocol

from ..domain.enums import AssetClass
from ..domain.research import Observation
from ..domain.tax import SecurityIdentity, TaxYearSummary
from ..errors import SimulationError
from ..jurisdiction.tax import TaxPolicy
from ..quant.costs import CostBook, LegCost, OrderTerms
from ..tax.lots import LotEngine
from .stats import annualised_log_growth, max_drawdown, years_between

ZERO = Decimal(0)
ONE = Decimal(1)
#: The reference rates' own currency (ECB rates are units of a currency per euro).
EURO: Final = "EUR"
#: Halvings in the search for the share of a buy list the cash covers (2^-30 of the list).
SCALE_BISECTIONS: Final = 30


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
    instrument key, and euro reference rates (units per euro) by currency."""

    prices: Mapping[str, tuple[Observation, ...]]
    income: Mapping[str, tuple[Observation, ...]]
    fx: Mapping[str, tuple[Observation, ...]]


def _visible(rows: Sequence[Observation], as_of: date) -> list[Observation]:
    return [o for o in rows if o.available_on <= as_of and o.observed_on <= as_of]


class MarketView:
    """What a strategy may see on ``as_of``: only observations public by then."""

    def __init__(self, panel: Panel, as_of: date) -> None:
        self._panel = panel
        self._as_of = as_of

    @property
    def as_of(self) -> date:
        return self._as_of

    def prices(self, key: str) -> tuple[tuple[date, float], ...]:
        rows = _visible(self._panel.prices.get(key, ()), self._as_of)
        latest: dict[date, Observation] = {}
        for row in rows:
            known = latest.get(row.observed_on)
            if known is None or row.available_on > known.available_on:
                latest[row.observed_on] = row
        return tuple((day, latest[day].value) for day in sorted(latest))


class Strategy(Protocol):
    """Target weights of the account's value, or ``None`` to keep what is held."""

    @property
    def name(self) -> str: ...

    def targets(
        self, view: MarketView, holdings: Mapping[str, Decimal]
    ) -> Mapping[str, Decimal] | None: ...


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
    withheld: Decimal = ZERO
    labels: set[str] = field(default_factory=set)
    taxes: list[TaxPayment] = field(default_factory=list)


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

    # ── prices and rates ──

    @staticmethod
    def _latest(rows: Sequence[Observation], on: date, max_age: int) -> float | None:
        """The latest vintage of the latest observation public on ``on``, if fresh enough."""
        visible = _visible(rows, on)
        if not visible:
            return None
        found = max(visible, key=lambda row: (row.observed_on, row.available_on))
        if on - found.observed_on > timedelta(days=max_age):
            return None
        return found.value

    def price(self, key: str, on: date) -> Decimal:
        """The listing-currency price of ``key`` on ``on``; a missing or stale one stops the run."""
        value = self._latest(self._panel.prices.get(key, ()), on, self._settings.max_price_age_days)
        if value is None or value <= 0:
            raise SimulationError(f"{key} has no price on {on} within the allowed age")
        return exact(value)

    def _per_euro(self, currency: str, on: date) -> Decimal:
        if currency == EURO:
            return ONE
        value = self._latest(self._panel.fx.get(currency, ()), on, self._settings.max_fx_age_days)
        if value is None or value <= 0:
            raise SimulationError(
                f"no reference rate for {currency} on {on} within the allowed age"
            )
        return exact(value)

    def to_base(self, amount: Decimal, currency: str, on: date) -> Decimal:
        base = self._settings.base_currency
        if currency == base:
            return amount
        return amount * self._per_euro(base, on) / self._per_euro(currency, on)

    def _base_price(self, key: str, on: date) -> Decimal:
        return self.to_base(self.price(key, on), self._instruments[key].currency, on)

    # ── one leg ──

    def _leg(self, key: str, side: str, quantity: Decimal, on: date, state: _State) -> Decimal:
        """Trade ``quantity`` of ``key``; returns the leg's cost in the base currency."""
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
        state.labels.update(cost.unverified)
        leg_cost = self.to_base(total * self._settings.cost_multiplier, instrument.currency, on)
        notional = self.to_base(quantity * price, instrument.currency, on)
        state.costs += leg_cost
        state.traded += notional
        state.trades += 1
        return leg_cost

    def _sell(
        self, key: str, quantity: Decimal, on: date, engine: LotEngine, state: _State
    ) -> None:
        cost = self._leg(key, "sell", quantity, on, state)
        proceeds = quantity * self._base_price(key, on) - cost
        if proceeds < ZERO:
            raise SimulationError(f"a sale of {quantity} {key} on {on} costs more than it raises")
        engine.dispose(key, on=on, quantity=quantity, proceeds=proceeds)
        state.cash += proceeds
        left = state.units[key] - quantity
        if left > ZERO:
            state.units[key] = left
        else:
            del state.units[key]

    def _buy_cost(self, key: str, quantity: Decimal, on: date) -> Decimal:
        """What a buy would take from cash, without recording it."""
        scratch = _State(cash=ZERO, next_year=on.year)
        return quantity * self._base_price(key, on) + self._leg(key, "buy", quantity, on, scratch)

    def _buy(self, key: str, quantity: Decimal, on: date, engine: LotEngine, state: _State) -> None:
        cost = self._leg(key, "buy", quantity, on, state)
        paid = quantity * self._base_price(key, on) + cost
        engine.acquire(key, on=on, quantity=quantity, cost=paid)
        state.cash -= paid
        state.units[key] = state.units.get(key, ZERO) + quantity

    def _units(self, value: Decimal, price: Decimal, rounding: str = ROUND_FLOOR) -> Decimal:
        units = value / price
        return units.to_integral_value(rounding=rounding) if self._settings.whole_units else units

    # ── a run ──

    def run(self, strategy: Strategy, dates: Sequence[date]) -> SimulationResult:
        if len(dates) < 2 or list(dates) != sorted(set(dates)):
            raise SimulationError(
                "a run needs at least two rebalance dates in strictly rising order"
            )
        engine = LotEngine(self._policy, [i.security for i in self._instruments.values()])
        state = _State(cash=self._settings.initial_capital, next_year=dates[0].year)
        nav: list[float] = []
        previous: date | None = None
        *trading, horizon = dates
        for on in trading:
            if previous is not None:
                self._receive_income(previous, on, engine, state)
            liability = self._taxes_due(on, engine, state, through=None)
            weights = strategy.targets(MarketView(self._panel, on), dict(state.units))
            self._rebalance(weights, self._marked(on, state), liability, on, engine, state)
            nav.append(float(self._marked(on, state)))
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
        )

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
                    state.cash += gross - event.withheld
                    state.withheld += event.withheld

    def _taxes_due(
        self, on: date, engine: LotEngine, state: _State, *, through: int | None
    ) -> Decimal:
        """Settle every open year whose payment date has come (or every year through
        ``through``, at the horizon); returns the tax now owed."""
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
                {key: q * prices[key] / marked for key, q in state.units.items()}
                if marked > ZERO
                else {}
            )
        self._check(weights)
        for key in weights:
            prices.setdefault(key, self._base_price(key, on))
        investable = max(marked - liability, ZERO)
        band = self._settings.no_trade_band
        sells: list[tuple[str, Decimal]] = []
        buys: list[tuple[str, Decimal]] = []
        for key in sorted(set(weights) | set(state.units)):
            target_value = investable * weights.get(key, ZERO)
            target = self._units(target_value, prices[key])
            delta = target - state.units.get(key, ZERO)
            if delta == ZERO or (target > ZERO and abs(delta) * prices[key] < band * target_value):
                continue
            (sells if delta < ZERO else buys).append((key, abs(delta)))
        for key, quantity in sells:
            self._sell(key, quantity, on, engine, state)
        self._pay(liability, on, engine, state)
        plan = self._affordable(buys, state.cash, on, prices)
        for key, quantity in plan:
            self._buy(key, quantity, on, engine, state)

    def _affordable(
        self,
        buys: list[tuple[str, Decimal]],
        cash: Decimal,
        on: date,
        prices: Mapping[str, Decimal],
    ) -> list[tuple[str, Decimal]]:
        """The buys scaled by one common factor, the largest whose cost (notional and every
        leg's cost) the cash covers; found by bisection, since a minimum commission makes the
        cost of a smaller order more than proportional."""

        def scaled(factor: Decimal) -> list[tuple[str, Decimal]]:
            plan = [(key, self._units(q * factor * prices[key], prices[key])) for key, q in buys]
            return [(key, q) for key, q in plan if q > ZERO]

        def cost(plan: list[tuple[str, Decimal]]) -> Decimal:
            return sum((self._buy_cost(key, q, on) for key, q in plan), ZERO)

        if cost(buys) <= cash:
            return buys
        low, high = ZERO, ONE
        for _ in range(SCALE_BISECTIONS):
            middle = (low + high) / 2
            if cost(scaled(middle)) <= cash:
                low = middle
            else:
                high = middle
        return scaled(low)

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
