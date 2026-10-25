"""The core's rebalancer: the core engine's target portfolio against the book, as intents
(MASTER_PLAN §5.4, §5.15).

The core holds the variant ``core.variant`` names (``engines/core/variants.py``) on its budget
(``capital/budgets.py``): investable capital (managed less reserved less unmanaged,
``capital/managed.py``) times the core's share, which keeps the budgets of the sleeves that do not
trade yet and leaves the cash floor as cash. At each decision cycle:

1. every role's **vehicles** are chosen through the vehicle selector and the swap-partner rule
   (``decision/core_vehicles.py``), the book's held vehicles kept; a vehicle without a quote this
   cycle is not bought;
2. the **trend** is read on the last completed month end from the market store's bars when the
   variant tilts (``engines/core/history.py``);
3. the **engine** decides the target portfolio and its trades (``engines/core/engine.py``): a start,
   a band breach (every role back to its target, the only time the core sells), cash beyond the
   targets deployed into the roles below theirs, or nothing;
4. each trade passes the **live guards**, and a trade that fails one is left, with its reason: an
   instrument nobody describes, an identity IBKR did not confirm, a venue not tradable now, an
   intent of this mode already working on it, a sale while an open reservation is not yet covered
   by the book's cash (the reservation planner chooses those sales, ``capital/planner.py``), a buy
   that would take a loss the two-month rule still holds open (the live tax lots, DECISIONS D110:
   the rebalancer never buys back inside an open window; a tax-aware variant's engine has already
   sent it to the swap partner when one would not), and a trade below the minimum trade
   (``trading.min_trade``) or its own commission. Buys are made within
   what the book may spend now: the smaller of the account's cash and the book's own cash, less
   the open withdrawal reservations, the part of a deposit not yet released for deployment
   (``capital/deploy.py``), every open buy's commitment and ``execution.pretrade.cash_floor``,
   scaled by one factor as the engine scales them. A band's buys that wait for its sales'
   proceeds are bought from that cash at a later cycle.

The book is not rebalanced at all when a held position cannot be valued, the capital cannot be
read, the kill switch is on or reconciliation freezes new risk. Holdings are the broker's
positions the registry assigns to the core; in shadow mode, the core's simulated fills.

Before it trades, every vehicle a core role may hold is confirmed against IBKR's contract search
(:func:`confirm_vehicles`); one that is not confirmed is never traded, with why, until it is.

Each trade becomes an intent priced by ``Pricer.initial`` (``execution/intent_maker.py``), an entry
for a buy and an exit (reason ``rebalance``) for a sale, without protective stops (MASTER_PLAN
§5.4: core positions carry none). The executor clears, funds, places, reprices and reconciles them.
:meth:`CoreRebalancer.survey` only reads, so a dry run shows the plan and writes nothing; each
survey's portfolio is noted on the core's StrategyValue (``capital/core_value.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

from ..calendars.service import Calendars
from ..capital.budgets import SleeveBudgets
from ..capital.core_value import CoreStrategyValue
from ..capital.deploy import held_back
from ..capital.managed import BookHolding, CapitalReader, CapitalView
from ..config.schema import ObliskConfig
from ..config.schema_runtime import TradingConfig
from ..domain.core import (
    PlannedTrade,
    RoleStatus,
    RoleTarget,
    TargetPortfolio,
    Trigger,
    VehicleTarget,
)
from ..domain.instrument import TradedInstrument
from ..domain.orders import (
    TERMINAL_STATES,
    ExecutionMode,
    IntentState,
    OrderIntent,
    OrderSide,
    ReconcileStatus,
    Sleeve,
)
from ..engines.core.engine import UNPRICEABLE, BookState, RoleChoice, Sizing, plan_portfolio
from ..engines.core.history import HistoryView, VehicleHistory, last_month_end, read_month_ends
from ..engines.core.trades import affordable
from ..engines.core.variants import Variant, build_variant
from ..errors import CalendarError, CoreError, ObliskError, TaxError
from ..execution.account import CashRead
from ..execution.instruments import InstrumentDirectory
from ..execution.intent_maker import IntentMaker
from ..execution.killswitch import KillSwitch
from ..ibkr.client import Secdef
from ..ibkr.identity import resolve_identity
from ..logging_setup import get_logger
from ..quant.costs import CostBook
from ..quant.costs import OrderTerms as CostTerms
from ..store import capital as capital_repo
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..tax.book import TaxBook, TaxHooks
from ..util.timeutil import utc_now
from .core_vehicles import (
    CoreVehicleChooser,
    CoreVehicles,
    Correlations,
    CorrelationSource,
    Unmeasured,
)
from .vehicle_inputs import market_inputs, windows
from .vehicle_selector import SelectorInputs, VehicleMarket, VehicleSelector, resolve_exposure

log = get_logger("decision.rebalance")

ZERO = Decimal(0)

#: The reason every rebalancing intent gives (an allowed exit reason, discipline).
REASON = "rebalance"

#: Actions a role's decision may take.
HOLD, BUY, SELL, SKIP = "hold", "buy", "sell", "skip"

#: The sleeves that trade in this build: the core alone, so the other budgets stay in it until a
#: layer's activation condition holds (D65, D66; Sessions 12, 15 and 17).
ACTIVE_SLEEVES = frozenset({Sleeve.CORE})

#: What a correlation reads when no research history was handed in.
NO_RESEARCH = "no research history was handed to the rebalancer"


@dataclass(frozen=True, slots=True)
class RoleDecision:
    """What the plan does with one vehicle of a role: hold, buy, sell or skip, and why.
    ``weight`` is its target share of the core's budget and ``target`` that share's value; a role
    with no vehicle to report has no ``conid``."""

    role: str
    conid: int | None
    symbol: str
    weight: Decimal
    held: Decimal
    value: Decimal | None
    target: Decimal
    action: str
    quantity: Decimal
    notional: Decimal
    reason: str


@dataclass
class RebalancePlan:
    """A survey of the book: the capital, the core's target portfolio, every vehicle's decision,
    and why nothing trades."""

    mode: ExecutionMode
    at: datetime
    capital: CapitalView | None = None
    core_capital: Decimal | None = None
    spendable: Decimal | None = None
    portfolio: TargetPortfolio | None = None
    decisions: list[RoleDecision] = field(default_factory=list)
    blocked: str | None = None

    @property
    def trades(self) -> list[RoleDecision]:
        return [d for d in self.decisions if d.action in (BUY, SELL)]

    def render(self) -> str:
        lines = [f"rebalance plan ({self.mode.value}) at {self.at.isoformat()}"]
        if self.capital is not None:
            c = self.capital
            lines.append(
                f"  managed capital {c.managed:.2f} ({c.mode}: {c.detail}); account NAV "
                f"{c.nav:.2f}; reserved {c.reserved:.2f}; unmanaged {c.unmanaged:.2f}; "
                f"investable {c.investable:.2f}"
            )
        if self.core_capital is not None:
            lines.append(f"  the core's budget {self.core_capital:.2f}")
        if self.spendable is not None:
            lines.append(f"  the book may spend {self.spendable:.2f}")
        portfolio = self.portfolio
        if portfolio is not None:
            trend = (
                ""
                if portfolio.trend_as_of is None
                else f", trend as of {portfolio.trend_as_of.isoformat()}"
            )
            lines.append(f"  variant {portfolio.variant}: {portfolio.trigger.value}{trend}")
            for role in portfolio.roles:
                lines.append(
                    f"  role {role.role} ({role.status.value}): weight {role.static_weight:.4f} "
                    f"x scale {role.scale.normalize()} -> target {role.target_weight:.4f}, held "
                    f"{role.held_weight:.4f}: {role.reason}"
                )
            lines += [f"  note: {note}" for note in portfolio.notes]
        if self.blocked is not None:
            lines.append(f"  nothing trades: {self.blocked}")
        for d in self.decisions:
            value = "unvalued" if d.value is None else f"{d.value:.2f}"
            trade = f" {d.quantity} ({d.notional:.2f})" if d.action in (BUY, SELL) else ""
            lines.append(
                f"  {d.role:<22} {d.symbol:<6} weight {d.weight:.4f} held {d.held} value {value} "
                f"target {d.target:.2f}: {d.action}{trade} ({d.reason})"
            )
        return "\n".join(lines)


def minimum_trade(trading: TradingConfig, managed: Decimal) -> Decimal:
    """The smallest trade by ``trading.min_trade``, in the base currency."""
    min_trade = trading.min_trade
    parts = []
    if min_trade.absolute_base is not None:
        parts.append(min_trade.absolute_base)
    if min_trade.fraction_of_managed is not None:
        parts.append(min_trade.fraction_of_managed * managed)
    return max(parts) if min_trade.combine == "max" else min(parts)


def core_vehicle_conids(
    settings: ObliskConfig, selector: VehicleSelector, *, today: date
) -> list[int]:
    """Every vehicle a core role may hold: the pinned universe instruments of each role that pass
    the selector's gates."""
    found: dict[int, None] = {}
    for role in settings.core.roles:
        selection = selector.select(
            resolve_exposure(settings, role), SelectorInputs(as_of=today, rates={})
        )
        found.update(dict.fromkeys(v.conid for v in selection.ranked))
    return list(found)


async def confirm_vehicles(
    directory: InstrumentDirectory, secdef: Secdef, conids: Iterable[int]
) -> dict[int, str]:
    """Each vehicle whose pinned conid IBKR's contract search does not confirm, with why (an
    empty mapping: every one is confirmed)."""
    refused: dict[int, str] = {}
    for conid in conids:
        instrument = directory.describe(conid)
        if instrument is None:
            refused[conid] = "no instrument describes it"
            continue
        try:
            identity = await resolve_identity(
                secdef,
                instrument.symbol,
                listing_exchange=instrument.listing_exchange,
                currency=instrument.currency,
            )
        except ObliskError as exc:
            refused[conid] = f"its identity cannot be confirmed: {exc}"
            continue
        if identity.conid != conid:
            refused[conid] = (
                f"IBKR resolves {instrument.symbol} on {instrument.listing_exchange} to conid "
                f"{identity.conid}, not the pinned {conid}"
            )
    return refused


@dataclass(frozen=True, slots=True)
class _Priced:
    """A vehicle the plan may trade: what describes it, its unit in the base currency and the
    rate behind it."""

    instrument: TradedInstrument
    unit: Decimal
    rate: Decimal


class CoreRebalancer:
    """Plans the core's trades toward its variant and makes them intents."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        capital: CapitalReader,
        directory: InstrumentDirectory,
        calendars: Calendars,
        worker: StoreWorker,
        maker: IntentMaker,
        costs: CostBook,
        kill: KillSwitch,
        tax: TaxBook,
        chooser: CoreVehicleChooser,
        value: CoreStrategyValue | None = None,
        correlations: CorrelationSource | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._capital = capital
        self._directory = directory
        self._calendars = calendars
        self._worker = worker
        self._maker = maker
        self._costs = costs
        self._kill = kill
        self._tax = tax
        self._chooser = chooser
        self._value = value
        self._correlations = correlations
        self._now = now
        self._sleeve = Sleeve.CORE
        self._base = settings.account.base_currency
        self._trading = settings.trading
        self._floor = settings.execution.pretrade.cash_floor
        self._band = settings.research.simulation.no_trade_band_fraction
        self._budgets = SleeveBudgets(settings.capital)
        self._variant: Variant = build_variant(settings)
        self._refused: dict[int, str] = {}

    @property
    def variant(self) -> Variant:
        return self._variant

    def vehicle_conids(self, today: date) -> list[int]:
        """Every vehicle a core role may hold (:func:`core_vehicle_conids`): what a start
        confirms."""
        return core_vehicle_conids(self._settings, self._chooser.selector, today=today)

    def refuse(self, refused: Mapping[int, str]) -> None:
        """Vehicles whose conid is not confirmed (:func:`confirm_vehicles`): never traded."""
        self._refused = dict(refused)

    # ── reading the book ──

    async def holdings(self, mode: ExecutionMode) -> dict[int, Decimal]:
        """Units the core holds by conid: the broker's positions the registry assigns to it, or
        in shadow mode its simulated fills."""
        held = await self._capital.holdings(mode)
        return {conid: units for conid, (sleeve, units) in held.items() if sleeve is self._sleeve}

    async def _pending(self, mode: ExecutionMode) -> tuple[set[int], list[OrderIntent]]:
        """Conids an intent of this mode still works on (a cancelled one waiting to be retried
        at the next window included), and every open buy of this mode."""
        now = self._now()
        busy: set[int] = set()
        buys: list[OrderIntent] = []
        open_states = [state for state in IntentState if state not in TERMINAL_STATES]
        for stored in await self._worker.run(repo.intents_in_states, open_states):
            intent = stored.intent
            if intent.mode is not mode:
                continue
            busy.add(intent.conid)
            if intent.side is OrderSide.BUY and not intent.terms.conversion:
                buys.append(intent)
        for stored in await self._worker.run(repo.intents_in_states, [IntentState.CANCELLED]):
            intent = stored.intent
            if intent.mode is mode and intent.expires_at > now and not intent.terms.conversion:
                busy.add(intent.conid)
        return busy, buys

    async def _committed(self, buys: Sequence[OrderIntent], cash: CashRead) -> Decimal | None:
        """What the open buys still commit, in the base currency (``None``: one is unvalued)."""
        total = ZERO
        for intent in buys:
            instrument = self._directory.describe(intent.conid)
            rate = (
                None
                if instrument is None
                else await self._capital.valuer.rate(instrument.currency, cash)
            )
            quantity = intent.terms.quantity
            if instrument is None or rate is None or quantity is None:
                return None
            fills = await self._worker.run(repo.fills_for_intent, intent.intent_id)
            left = max(ZERO, quantity - sum((fill.quantity for fill in fills), ZERO))
            total += left * intent.terms.price.limit * instrument.multiplier * rate
        return total

    async def _held_back(self, cash: CashRead) -> Decimal | None:
        """The part of the deposits not released for deployment yet, in the base currency."""
        flows = await self._worker.run(capital_repo.cash_flows)
        held = held_back(flows, self._settings.capital, today=self._now().date())
        return await self._capital.valuer.cash_value(held, cash)

    async def _hooks(self) -> TaxHooks | None:
        try:
            return await self._tax.hooks()
        except TaxError as exc:
            log.warning("the tax lots cannot be read", extra={"error": str(exc)})
            return None

    async def _spendable(
        self, mode: ExecutionMode, view: CapitalView, cash: CashRead
    ) -> Decimal | str:
        """What the book may spend now, or why it cannot be said."""
        _, buys = await self._pending(mode)
        committed = await self._committed(buys, cash)
        if committed is None:
            return "an open buy cannot be valued in the base currency"
        account_cash = ZERO
        for line in cash.lines:
            rate = await self._capital.valuer.rate(line.currency, cash)
            if rate is None:
                return f"the {line.currency} cash cannot be valued in {self._base}"
            account_cash += line.cash * rate
        held_back_now = await self._held_back(cash)
        if held_back_now is None:
            return f"a deposit cannot be valued in {self._base}"
        book_cash = view.book_cash - view.reserved - held_back_now
        return max(ZERO, min(account_cash, book_cash) - committed - self._floor)

    async def _measured(self, today: date) -> Correlations:
        """The research correlations handed in, read for today; unmeasured, with why, when there
        are none or they cannot be read."""
        if self._correlations is None:
            return Unmeasured(NO_RESEARCH)
        try:
            return await self._correlations.read(today)
        except ObliskError as exc:
            log.warning("the research correlations cannot be read", extra={"error": str(exc)})
            return Unmeasured(f"the research correlations cannot be read: {exc}")

    async def _selector_inputs(self, cash: CashRead, today: date) -> SelectorInputs:
        """The rates, trading windows and stored market inputs the selector ranks on."""
        universe = self._settings.universe
        candidates = [
            declared
            for declared in universe.instruments
            if declared.exposures.core_roles and declared.conid is not None
        ]
        currencies = (
            {declared.currency for declared in candidates}
            | {feed.monthly_cost.currency for feed in self._settings.market_data.feeds}
            | {schedule.currency for schedule in self._settings.costs.commissions}
        )
        rates: dict[str, Decimal] = {}
        for currency in sorted(currencies):
            rate = await self._capital.valuer.rate(currency, cash)
            if rate is not None:
                rates[currency] = rate
        exchanges = self._settings.ibkr_exchanges
        venues = {
            exchanges[d.listing_exchange].venue
            for d in candidates
            if d.listing_exchange in exchanges
        }
        try:
            overlaps = windows(self._settings, self._calendars, venues, today)
        except CalendarError as exc:  # a venue the calendars do not cover: unmeasured
            log.warning("trading windows not read", extra={"error": str(exc)})
            overlaps = {}
        market: Mapping[int, VehicleMarket] = await self._worker.run(
            market_inputs,
            [d.conid for d in candidates if d.conid is not None],
            as_of=today,
            sessions=self._settings.vehicles.ranking.liquidity_sessions,
        )
        return SelectorInputs(
            as_of=today, rates=rates, windows=overlaps, market=market, tracking={}
        )

    async def _price(
        self, conids: Iterable[int], held: Mapping[int, BookHolding], cash: CashRead
    ) -> tuple[dict[int, _Priced], dict[int, str]]:
        """One unit of each vehicle in the base currency: a held one at the capital view's
        value, any other from a fresh quote; and why a vehicle has none (it is not traded this
        cycle)."""
        priced: dict[int, _Priced] = {}
        missing: dict[int, str] = {}
        for conid in conids:
            holding = held.get(conid)
            value = (
                holding.unit
                if holding is not None
                else await self._capital.valuer.unit(conid, cash)
            )
            if value.instrument is None:
                missing[conid] = "no instrument describes it"
                continue
            if value.unit is None or value.rate is None:
                missing[conid] = "no price"
                continue
            # A held vehicle is always valued (the capital view priced it); one whose listing
            # maps to no venue is not bought.
            priced[conid] = _Priced(value.instrument, value.unit, value.rate)
            if self._venue(value.instrument) is None:
                missing[conid] = (
                    f"its listing exchange {value.instrument.listing_exchange} maps to no venue"
                )
        return priced, missing

    async def _history(
        self, vehicles: CoreVehicles, priced: Mapping[int, _Priced], today: date
    ) -> HistoryView:
        month = last_month_end(today)
        if not self._variant.tilts:
            return HistoryView({}, month)
        wanted: dict[str, VehicleHistory] = {}
        for choice in vehicles.choices.values():
            for conid in (choice.primary, choice.partner, *choice.held):
                if conid is None:
                    continue
                instrument = (
                    priced[conid].instrument if conid in priced else self._directory.describe(conid)
                )
                if instrument is not None:
                    wanted[str(conid)] = VehicleHistory(conid, instrument.currency)
        history = await self._worker.run(read_month_ends, self._settings, wanted, as_of=month)
        return HistoryView(history, month)

    def _leg_cost(
        self, priced: _Priced, venue: str | None, quantity: Decimal, side: str, today: date,
        market: Mapping[int, VehicleMarket],
    ) -> Decimal | None:  # fmt: skip
        """A leg's spread, slippage, commission and tax, in the base currency."""
        instrument = priced.instrument
        if venue is None:
            return None
        found = market.get(instrument.conid)
        terms = CostTerms(
            venue=venue,
            asset_class=instrument.asset_class,
            currency=instrument.currency,
            quantity=quantity,
            price=priced.unit / (instrument.multiplier * priced.rate),
        )
        leg = self._costs.leg_cost(
            terms,
            side="buy" if side == BUY else "sell",
            on=today,
            average_daily_value=None if found is None else found.average_daily_value,
        )
        return None if leg.total is None else leg.total * priced.rate

    def _venue(self, instrument: TradedInstrument) -> str | None:
        try:
            return self._calendars.venue_for_ibkr(instrument.listing_exchange)
        except CalendarError:
            return None

    # ── the survey ──

    async def survey(self, mode: ExecutionMode) -> RebalancePlan:
        """The plan for the book now: reads only, writes nothing."""
        at = self._now()
        today = at.date()
        plan = RebalancePlan(mode=mode, at=at)
        if await self._kill.is_on():
            plan.blocked = "the kill switch is on: no new decision"
            return plan
        latest = await self._worker.run(repo.latest_reconciliation)
        if latest is not None and latest.status is not ReconcileStatus.GREEN:
            plan.blocked = f"reconciliation is {latest.status.value}: new risk is frozen"
            return plan
        try:
            cash = await self._capital.cash()
        except ObliskError as exc:
            plan.blocked = f"the account cannot be read: {exc}"
            return plan
        view = await self._capital.read(mode, cash=cash)
        if isinstance(view, str):
            plan.blocked = view
            return plan
        plan.capital = view
        budget = self._budgets.allocate(view.investable, active=ACTIVE_SLEEVES).core
        plan.core_capital = budget
        spendable = await self._spendable(mode, view, cash)
        if isinstance(spendable, str):
            plan.blocked = spendable
            return plan
        plan.spendable = spendable
        held = {h.conid: h for h in view.holdings if h.sleeve is self._sleeve}
        inputs = await self._selector_inputs(cash, today)
        vehicles = self._chooser.choose(
            self._variant.roles,
            {conid: h.value or ZERO for conid, h in held.items()},
            inputs,
            await self._measured(today),
            partners=self._variant.tilts,
            refused=self._refused,
        )
        wanted = {
            conid
            for choice in vehicles.choices.values()
            for conid in (choice.primary, choice.partner, *choice.held)
            if conid is not None
        }
        priced, missing = await self._price(sorted(wanted), held, cash)
        choices = {
            role: _close_missing(choice, missing) for role, choice in vehicles.choices.items()
        }
        history = await self._history(vehicles, priced, today)
        venues = {conid: self._venue(p.instrument) for conid, p in priced.items()}

        def cost(side: str) -> Callable[[int, Decimal], Decimal | None]:
            def leg(conid: int, quantity: Decimal) -> Decimal | None:
                found = priced.get(conid)
                if found is None:
                    return None
                charge = self._leg_cost(found, venues[conid], quantity, side, today, inputs.market)
                if charge is None or side == SELL:
                    return charge
                return quantity * found.unit + charge

            return leg

        hooks = await self._hooks()
        state = BookState(
            units={conid: h.units for conid, h in held.items()},
            prices={conid: p.unit for conid, p in priced.items()},
            budget=budget,
            cash=spendable,
            on=today,
        )
        least = minimum_trade(self._trading, view.managed)
        sizing = Sizing(
            no_trade_band=self._band, minimum=least, buy_cost=cost(BUY), sell_cost=cost(SELL)
        )
        try:
            portfolio = plan_portfolio(self._variant, choices, history, state, hooks, sizing)
        except CoreError as exc:
            plan.blocked = f"the core cannot decide: {exc}"
            return plan
        plan.portfolio = portfolio
        if self._value is not None:
            self._value.note(portfolio)
        busy, _ = await self._pending(mode)
        plan.decisions = self._decide(
            portfolio,
            priced,
            venues,
            busy,
            spendable,
            least,
            at,
            sizing,
            sells_paused=view.unfunded > ZERO,
            hooks=hooks,
        )
        return plan

    def _decide(
        self,
        portfolio: TargetPortfolio,
        priced: Mapping[int, _Priced],
        venues: Mapping[int, str | None],
        busy: set[int],
        spendable: Decimal,
        least: Decimal,
        at: datetime,
        sizing: Sizing,
        *,
        sells_paused: bool,
        hooks: TaxHooks | None,
    ) -> list[RoleDecision]:
        trades = {trade.conid: trade for trade in portfolio.trades}
        rows: dict[int, RoleDecision] = {}
        buys: list[tuple[VehicleTarget, PlannedTrade]] = []
        for vehicle in portfolio.vehicles:
            found = priced.get(vehicle.conid)
            trade = trades.get(vehicle.conid)
            if trade is None:
                rows[vehicle.conid] = (
                    _row(portfolio, vehicle, found, SKIP, "an intent already works on it")
                    if vehicle.conid in busy
                    else _untraded(portfolio, vehicle, found)
                )
                continue
            refusal = self._refusal(
                trade, found, venues, busy, at, sells_paused=sells_paused, hooks=hooks
            )
            if refusal is not None:
                rows[vehicle.conid] = _row(portfolio, vehicle, found, SKIP, refusal)
            elif trade.side is OrderSide.SELL:
                rows[vehicle.conid] = self._sized(portfolio, vehicle, found, trade, venues, least)
            else:
                buys.append((vehicle, trade))
        # Buys: within what the book may spend now, scaled by one factor as the engine scales
        # them; a band's buys beyond it wait for its sales' proceeds.
        prices = {str(trade.conid): priced[trade.conid].unit for _, trade in buys}

        def buy_cost(key: str, quantity: Decimal) -> Decimal:
            charge = sizing.buy_cost(int(key), quantity)
            return UNPRICEABLE if charge is None else charge

        now = dict(
            affordable(
                [(str(trade.conid), trade.quantity) for _, trade in buys],
                spendable,
                prices,
                buy_cost,
                whole_units=True,
            )
        )
        for vehicle, trade in buys:
            found = priced[trade.conid]
            count = now.get(str(trade.conid), ZERO)
            if count <= ZERO:
                wait = (
                    "waits for its sales' proceeds"
                    if portfolio.trigger is Trigger.BAND
                    else "no cash for one unit"
                )
                rows[vehicle.conid] = _row(
                    portfolio, vehicle, found, SKIP, f"buy of {trade.quantity}: {wait}"
                )
                continue
            sized = trade.model_copy(update={"quantity": count, "notional": count * found.unit})
            made = self._sized(portfolio, vehicle, found, sized, venues, least)
            if made.action == BUY and count < trade.quantity:
                made = replace(
                    made, reason=f"{made.reason}; {trade.quantity - count} wait for cash"
                )
            rows[vehicle.conid] = made
        out = [rows[v.conid] for v in portfolio.vehicles]
        named = {v.role for v in portfolio.vehicles}
        out += [
            self._role_row(portfolio, role) for role in portfolio.roles if role.role not in named
        ]
        return out

    def _role_row(self, portfolio: TargetPortfolio, role: RoleTarget) -> RoleDecision:
        """A role no vehicle row reports (none priced, confirmed or proposable): skipped, why."""
        conid = role.vehicles[0] if role.vehicles else None
        instrument = None if conid is None else self._directory.describe(conid)
        idle = role.status is RoleStatus.FILLED and role.target_weight == ZERO
        return RoleDecision(
            role=role.role,
            conid=conid,
            symbol="-" if instrument is None else instrument.symbol,
            weight=role.target_weight,
            held=ZERO,
            value=None,
            target=role.target_weight * portfolio.budget,
            action=HOLD if idle else SKIP,
            quantity=ZERO,
            notional=ZERO,
            reason=f"no target now; {role.reason}" if idle else role.reason,
        )

    def _refusal(
        self,
        trade: PlannedTrade,
        found: _Priced | None,
        venues: Mapping[int, str | None],
        busy: set[int],
        at: datetime,
        *,
        sells_paused: bool,
        hooks: TaxHooks | None,
    ) -> str | None:
        """Why a planned trade is not made now, or ``None`` when it may be."""
        side = "sell" if trade.side is OrderSide.SELL else "buy"
        if found is None:
            return f"{side}: no instrument describes the conid or it has no price"
        if trade.conid in self._refused:
            return f"not confirmed: {self._refused[trade.conid]}"
        if trade.conid in busy:
            return "an intent already works on it"
        if trade.side is OrderSide.SELL and sells_paused:
            return (
                "sell: an open reservation is not yet covered; the reservation planner chooses "
                "those sales"
            )
        if (
            trade.side is OrderSide.BUY
            and hooks is not None
            and hooks.purchase_defers_loss(trade.conid, at.date())
        ):
            return "buy: it would defer a loss the two-month rule still holds open"
        venue = venues.get(trade.conid)
        if venue is None:
            return f"{side}: its listing exchange maps to no venue"
        status = self._calendars.tradability(venue, at, conid=trade.conid)
        if not status.ok:
            return f"{venue} is not tradable now ({status.reason})"
        return None

    def _sized(
        self,
        portfolio: TargetPortfolio,
        vehicle: VehicleTarget,
        found: _Priced | None,
        trade: PlannedTrade,
        venues: Mapping[int, str | None],
        least: Decimal,
    ) -> RoleDecision:
        action = BUY if trade.side is OrderSide.BUY else SELL
        venue = venues.get(trade.conid)
        if found is None or venue is None:  # pragma: no cover - refused before sizing
            return _row(portfolio, vehicle, found, SKIP, f"{action}: it cannot be priced")
        commission = self._commission(found, trade.quantity, venue)
        if commission is None:
            return _row(
                portfolio,
                vehicle,
                found,
                SKIP,
                f"{action}: no commission schedule, so no minimum can be checked",
            )
        floor = max(least, commission)
        if trade.notional < floor:
            return _row(
                portfolio,
                vehicle,
                found,
                SKIP,
                f"{action} of {trade.notional:.2f} is below the minimum trade {floor:.2f}",
            )
        return _row(
            portfolio,
            vehicle,
            found,
            action,
            f"toward its target ({portfolio.trigger.value})",
            trade.quantity,
            trade.notional,
        )

    def _commission(self, priced: _Priced, units: Decimal, venue: str) -> Decimal | None:
        """The order's commission in the base currency (the floor no trade may sit below)."""
        instrument = priced.instrument
        estimate = self._costs.commission(
            CostTerms(
                venue=venue,
                asset_class=instrument.asset_class,
                currency=instrument.currency,
                quantity=units,
                price=priced.unit / (instrument.multiplier * priced.rate),
            )
        )
        return None if estimate.amount is None else estimate.amount * priced.rate

    # ── making the intents ──

    async def apply(self, plan: RebalancePlan) -> list[OrderIntent]:
        """An intent for every trade of ``plan``, priced now; a trade that cannot be priced is
        logged and left for the next cycle."""
        made: list[OrderIntent] = []
        for trade in plan.trades:
            if trade.conid is None:  # pragma: no cover - a trade always names its vehicle
                continue
            intent = await self._maker.make(
                conid=trade.conid,
                side=OrderSide.BUY if trade.action == BUY else OrderSide.SELL,
                quantity=trade.quantity,
                sleeve=self._sleeve,
                reason=REASON,
                mode=plan.mode,
            )
            if isinstance(intent, str):
                log.warning("a trade is not made", extra={"role": trade.role, "why": intent})
                continue
            made.append(intent)
        return made


def _untraded(
    portfolio: TargetPortfolio, vehicle: VehicleTarget, found: _Priced | None
) -> RoleDecision:
    """A vehicle the portfolio does not trade: inside the bands, at its target, or short of it
    by a unit the cash does not cover at the scale every buy shares."""
    trigger = portfolio.trigger
    if trigger is Trigger.HOLD:
        return _row(portfolio, vehicle, found, HOLD, "inside its band")
    withheld = portfolio.withheld_for(vehicle.conid)
    if withheld is not None:
        side = "buy" if withheld.side is OrderSide.BUY else "sell"
        return _row(
            portfolio, vehicle, found, SKIP, f"{side} of {withheld.quantity}: {withheld.reason}"
        )
    short = vehicle.weight * portfolio.budget - vehicle.held_units * vehicle.price
    if vehicle.price > ZERO and short >= vehicle.price:
        return _row(
            portfolio,
            vehicle,
            found,
            SKIP,
            f"below its target ({trigger.value}): the cash covers no unit of it at the scale "
            "every buy shares",
        )
    if short > ZERO:
        return _row(
            portfolio, vehicle, found, HOLD, f"within one unit of its target ({trigger.value})"
        )
    return _row(portfolio, vehicle, found, HOLD, f"at its target ({trigger.value})")


def _row(
    portfolio: TargetPortfolio,
    vehicle: VehicleTarget,
    found: _Priced | None,
    action: str,
    reason: str,
    quantity: Decimal = ZERO,
    notional: Decimal = ZERO,
) -> RoleDecision:
    return RoleDecision(
        role=vehicle.role,
        conid=vehicle.conid,
        symbol="?" if found is None else found.instrument.symbol,
        weight=vehicle.weight,
        held=vehicle.held_units,
        value=None if found is None else vehicle.held_units * found.unit,
        target=vehicle.weight * portfolio.budget,
        action=action,
        quantity=quantity,
        notional=notional,
        reason=reason,
    )


def _close_missing(choice: RoleChoice, missing: Mapping[int, str]) -> RoleChoice:
    """A choice whose vehicles that cannot be priced or placed this cycle are not bought."""
    named = [conid for conid in (choice.primary, choice.partner, *choice.held) if conid is not None]
    closing = {conid: missing[conid] for conid in named if conid in missing}
    if not closing:
        return choice
    return replace(
        choice,
        closed=choice.closed | frozenset(closing),
        reason="; ".join(
            [choice.reason, *(f"conid {conid}: {why}" for conid, why in closing.items())]
        ),
    )
