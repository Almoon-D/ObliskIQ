"""The core rebalancer: the core engine's target portfolio against the book, as intents
(MASTER_PLAN §5.4, §5.12; DECISIONS D104, D140).

Each cycle the roles are filled through the vehicle selector (``core_vehicles.py``; a vehicle held
stays), the core's book is read at the quotes' reference prices in the base currency, and the core
engine (``engines/core``) answers with a :class:`~obliskiq.domain.portfolio.TargetPortfolio`: the
variant the owner chose (``core.variant``), the trend tilt read at the last completed month end,
and whether a role left its **outer band** (``core.bands``). The core's budget is investable
capital (managed less reserved less unmanaged, ``capital/managed.py``) times the core's share
(``capital/budgets.py``), which keeps the budgets of the sleeves that do not trade yet and leaves
the cash floor as cash.

* Inside the outer bands **only new cash is spent**: a vehicle further under its target than the
  **inner band** (``trading.inner_band``: the smaller of an absolute width and a share of its
  target) is bought, the largest shortfall first. Nothing is sold: sells wait for the outer band.
* Beyond the outer band, or when the target names no vehicle a held position fills, **every
  vehicle is traded back to its target**, sells included, unless the change is smaller than the
  no-trade band of the simulator (``research.simulation.no_trade_band_fraction``): the same delta
  execution the test priced.

Trades are in **integer units**, rounded toward zero, a sale never beyond what the sleeve holds;
never below the **minimum trade** (``trading.min_trade``), nor below the order's own commission
(the commission floor); an order whose commission the cost model cannot price is not made, since
the floor cannot be checked; buys within what the book may spend: the smaller of the account's
cash and the book's own cash, less the open withdrawal reservations, the part of a deposit not yet
released for deployment (``capital/deploy.py``), every open buy's commitment and
``execution.pretrade.cash_floor``, all in the base currency. No buy takes a loss the two-month
rule still holds open (the live tax lots); no sale while an open reservation is not yet covered by
the book's cash: the reservation planner (``capital/planner.py``) chooses those sales, by their
tax, trading and strategy cost.

A vehicle is left alone, with its reason, when its venue is not tradable now, an intent of this
mode already works on it, its contract is not confirmed against IBKR's contract search, or it has
no price. The book is not rebalanced at all when a held position cannot be valued (the capital
reader says so), the capital cannot be read, the kill switch is on, reconciliation freezes new
risk, or the engine cannot form a target (it says why). Holdings are the broker's positions the
registry assigns to the sleeve; in shadow mode, the sleeve's simulated fills.

Each trade becomes an intent priced by ``Pricer.initial`` (``execution/intent_maker.py``), an entry
for a buy and an exit (reason ``rebalance``) for a sale, without protective stops (MASTER_PLAN
§5.4: core positions carry none). The executor clears, funds, places, reprices and reconciles
them. :meth:`CoreRebalancer.survey` only reads, so a dry run shows the plan and writes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from ..calendars.service import Calendars
from ..capital.budgets import SleeveBudgets
from ..capital.deploy import held_back
from ..capital.managed import BookHolding, CapitalReader, CapitalView
from ..capital.valuation import UnitValue
from ..config.schema import ObliskConfig
from ..config.schema_runtime import TradingConfig
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
from ..domain.portfolio import TargetPortfolio, TargetStatus
from ..engines.core.engine import CoreEngine
from ..engines.core.mechanism import RoleVehicles
from ..engines.core.trades import Deltas, plan_deltas
from ..engines.core.variants import Variant
from ..errors import CalendarError, ObliskError, TaxError
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
from ..tax.book import TaxBook, TaxHooks, security_key
from ..util.timeutil import utc_now
from .core_book import LiveBook
from .core_closes import StoredCloses, last_month_end
from .core_value import LatestTarget
from .core_vehicles import VehicleChoice, VehicleSource

log = get_logger("decision.core_rebalancer")

ZERO = Decimal(0)

#: The reason every rebalancing intent gives (an allowed exit reason, discipline).
REASON = "rebalance"

#: Actions a role's decision may take.
HOLD, BUY, SELL, SKIP = "hold", "buy", "sell", "skip"

#: The sleeves that trade in this build: the core alone, so the other budgets stay in it until a
#: layer's activation condition holds (D65, D66; Sessions 12, 15 and 17).
ACTIVE_SLEEVES = frozenset({Sleeve.CORE})


@dataclass(frozen=True, slots=True)
class RoleDecision:
    """What the plan does with one role: hold, buy, sell or skip, and why."""

    role: str
    conid: int
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
    """A survey of the book: the capital, every role's decision, and why nothing trades."""

    mode: ExecutionMode
    at: datetime
    capital: CapitalView | None = None
    core_capital: Decimal | None = None
    spendable: Decimal | None = None
    decisions: list[RoleDecision] = field(default_factory=list)
    blocked: str | None = None
    #: The core engine's target for this survey (``None`` when it was not reached).
    portfolio: TargetPortfolio | None = None
    #: Contracts IBKR's contract search does not confirm, with why: neither bought nor sold.
    refused: dict[int, str] = field(default_factory=dict)
    #: Roles no vehicle fills this cycle, with why (their weight is held in cash).
    unfilled: dict[str, str] = field(default_factory=dict)

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
        if self.blocked is not None:
            lines.append(f"  nothing trades: {self.blocked}")
        if self.portfolio is not None:
            p = self.portfolio
            lines.append(
                f"  core variant {p.variant}: "
                + (
                    "outside an outer band, every vehicle goes to its target"
                    if p.rebalance
                    else "inside the outer bands, only new cash is spent"
                )
            )
            lines += [f"  note: {reason}" for reason in p.reasons]
        lines += [f"  unfilled {role}: {why}" for role, why in sorted(self.unfilled.items())]
        for d in self.decisions:
            value = "unvalued" if d.value is None else f"{d.value:.2f}"
            trade = f" {d.quantity} ({d.notional:.2f})" if d.action in (BUY, SELL) else ""
            lines.append(
                f"  {d.role:<16} {d.symbol:<6} weight {d.weight} held {d.held} value {value} "
                f"target {d.target:.2f}: {d.action}{trade} ({d.reason})"
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Role:
    """A role being decided: its target, what is held, and one unit's value in the base
    currency with the rate behind it (``None`` when it has no price)."""

    role: str
    conid: int
    weight: Decimal
    instrument: TradedInstrument | None
    held: Decimal
    unit: Decimal | None
    rate: Decimal | None

    @classmethod
    def valued(cls, role: str, weight: Decimal, held: Decimal, value: UnitValue) -> _Role:
        return cls(role, value.conid, weight, value.instrument, held, value.unit, value.rate)

    def row(
        self,
        base: Decimal,
        action: str,
        reason: str,
        quantity: Decimal = ZERO,
        notional: Decimal = ZERO,
    ) -> RoleDecision:
        return RoleDecision(
            role=self.role,
            conid=self.conid,
            symbol="?" if self.instrument is None else self.instrument.symbol,
            weight=self.weight,
            held=self.held,
            value=None if self.unit is None else self.held * self.unit,
            target=self.weight * base,
            action=action,
            quantity=quantity,
            notional=notional,
            reason=reason,
        )


def _whole_units(value: Decimal, unit: Decimal) -> Decimal:
    if unit <= ZERO or value <= ZERO:
        return ZERO
    return (value / unit).to_integral_value(rounding=ROUND_FLOOR)


async def confirm_vehicles(
    directory: InstrumentDirectory, secdef: Secdef, conids: Sequence[int]
) -> dict[int, str]:
    """Each of ``conids`` whose pinned identity IBKR's contract search does not confirm, with why
    (an empty mapping: every one is confirmed)."""
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


def minimum_trade(trading: TradingConfig, managed: Decimal) -> Decimal:
    """The smallest trade by ``trading.min_trade``, in the base currency."""
    min_trade = trading.min_trade
    parts = []
    if min_trade.absolute_base is not None:
        parts.append(min_trade.absolute_base)
    if min_trade.fraction_of_managed is not None:
        parts.append(min_trade.fraction_of_managed * managed)
    return max(parts) if min_trade.combine == "max" else min(parts)


class CoreRebalancer:
    """Plans the core's trades toward the engine's target and makes them intents."""

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
        engine: CoreEngine,
        vehicles: VehicleSource,
        closes: StoredCloses,
        latest: LatestTarget | None = None,
        secdef: Secdef | None = None,
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
        self._engine = engine
        self._vehicles = vehicles
        self._closes = closes
        self._secdef = secdef
        self._now = now
        self._sleeve = Sleeve.CORE
        self._base = settings.account.base_currency
        self._trading = settings.trading
        self._floor = settings.execution.pretrade.cash_floor
        self._no_trade = settings.research.simulation.no_trade_band_fraction
        self._budgets = SleeveBudgets(settings.capital)
        self._confirmed: set[int] = set()
        self._refused: dict[int, str] = {}
        #: The latest target, for the reservation planner's view of the core.
        self.latest = latest if latest is not None else LatestTarget()

    def refuse(self, refused: dict[int, str]) -> None:
        """Contracts that are not confirmed (:func:`confirm_vehicles`): skipped, with why."""
        self._refused = dict(refused)

    async def _confirm(self, conids: Sequence[int]) -> dict[int, str]:
        """Confirm each chosen contract once against IBKR's contract search. Returns the contracts
        left unconfirmed by a failure of the search itself: they wait this survey and are tried
        again at the next."""
        fresh = [c for c in conids if c not in self._confirmed and c not in self._refused]
        if not fresh or self._secdef is None:
            return {}
        try:
            refused = await confirm_vehicles(self._directory, self._secdef, fresh)
        except Exception as exc:  # the search itself failed: nothing is confirmed this survey
            return {c: f"the confirmation failed: {type(exc).__name__}" for c in fresh}
        self._refused.update(refused)
        self._confirmed.update(c for c in fresh if c not in refused)
        return {}

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

    def _closed(self, venue: str, conid: int, at: datetime) -> str | None:
        """Why the venue is not tradable now for ``conid``, or ``None`` when it is."""
        status = self._calendars.tradability(venue, at, conid=conid)
        return None if status.ok else f"{venue} is not tradable now ({status.reason})"

    def _commission(self, role: _Role, units: Decimal, venue: str) -> Decimal | None:
        """The order's commission in the base currency (the floor no trade may sit below)."""
        instrument, unit, rate = role.instrument, role.unit, role.rate
        if instrument is None or unit is None or rate is None:  # a priced role carries all three
            return None
        estimate = self._costs.commission(
            CostTerms(
                venue=venue,
                asset_class=instrument.asset_class,
                currency=instrument.currency,
                quantity=units,
                price=unit / (instrument.multiplier * rate),
            )
        )
        return None if estimate.amount is None else estimate.amount * rate

    # ── the survey ──

    def _roles(self, variant: Variant, held: Mapping[int, BookHolding]) -> list[str]:
        """The roles to fill: the variant's weighted roles, the cash role, and the roles of the
        vehicles the core holds."""
        roles = [role for role, weight in variant.weights.items() if weight > ZERO]
        roles.append(self._settings.core.cash_role)
        for conid in sorted(held):
            declared = self._settings.universe.pinned(conid)
            if declared is not None:
                roles += [
                    r for r in declared.exposures.core_roles if r in self._settings.core.roles
                ]
        return list(dict.fromkeys(roles))

    async def survey(self, mode: ExecutionMode) -> RebalancePlan:
        """The plan for the book now: reads only, writes nothing."""
        at = self._now()
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
        allocation = self._budgets.allocate(view.investable, active=ACTIVE_SLEEVES)
        base = allocation.core
        plan.core_capital = base
        held = {h.conid: h for h in view.holdings if h.sleeve is self._sleeve}
        busy, buys = await self._pending(mode)
        committed = await self._committed(buys, cash)
        if committed is None:
            plan.blocked = "an open buy cannot be valued in the base currency"
            return plan
        account_cash = ZERO
        for line in cash.lines:
            rate = await self._capital.valuer.rate(line.currency, cash)
            if rate is None:
                plan.blocked = f"the {line.currency} cash cannot be valued in {self._base}"
                return plan
            account_cash += line.cash * rate
        held_back_now = await self._held_back(cash)
        if held_back_now is None:
            plan.blocked = f"a deposit cannot be valued in {self._base}"
            return plan
        book_cash = view.book_cash - view.reserved - held_back_now
        plan.spendable = max(ZERO, min(account_cash, book_cash) - committed - self._floor)
        unfunded = view.unfunded > ZERO
        target = await self._target(plan, held, cash, base, at)
        if target is None:
            return plan
        portfolio, units = target
        roles = self._vehicle_roles(portfolio, held, units)
        plan.decisions = self._decide(
            portfolio,
            roles,
            busy,
            base,
            view.managed,
            plan.spendable,
            at,
            refused=plan.refused,
            hooks=await self._hooks(),
            sells_paused=unfunded,
        )
        return plan

    async def _target(
        self,
        plan: RebalancePlan,
        held: Mapping[int, BookHolding],
        cash: CashRead,
        base: Decimal,
        at: datetime,
    ) -> tuple[TargetPortfolio, dict[int, UnitValue]] | None:
        """The engine's target for the core's book, with every vehicle's unit value; ``None``
        (the plan says why) when none can be formed."""
        engine = self._engine
        variant = engine.variants.get(engine.variants.chosen)
        choice = await self._vehicles.choose(
            self._roles(variant, held),
            held=set(held),
            aware=variant.tax_aware,
            today=at.date(),
        )
        plan.unfilled = choice.unfilled
        transient = await self._confirm(sorted(choice.conids.values()))
        named = set(choice.conids.values()) | set(held)
        plan.refused = {
            **{c: why for c, why in self._refused.items() if c in named},
            **transient,
        }
        units = {conid: holding.unit for conid, holding in held.items()}
        for conid in sorted(choice.conids.values()):
            if conid not in units:
                units[conid] = await self._capital.valuer.unit(conid, cash)
        vehicles = self._priced(choice, units, plan)
        hooks = await self._hooks()
        book = LiveBook(
            {security_key(conid): holding.units for conid, holding in held.items()},
            {security_key(c): v.unit for c, v in units.items() if v.unit is not None},
            base,
            hooks=hooks,
            on=at.date(),
        )
        closes = None
        if variant.tilts:
            primaries = {rv.primary: int(rv.primary) for rv in vehicles.values()}
            closes = await self._closes.view(primaries, through=last_month_end(at.date()))
        portfolio = engine.portfolio(vehicles=vehicles, book=book, view=closes, as_of=at.date())
        plan.portfolio = self.latest.portfolio = portfolio
        if portfolio.status is TargetStatus.NOT_EVALUABLE:
            plan.blocked = "the core's target is not evaluable: " + "; ".join(portfolio.reasons)
            return None
        return portfolio, units

    @staticmethod
    def _priced(
        choice: VehicleChoice, units: Mapping[int, UnitValue], plan: RebalancePlan
    ) -> dict[str, RoleVehicles]:
        """The chosen vehicles that have a price: a role whose primary has none is unfilled this
        cycle, a partner without one is not used."""
        vehicles: dict[str, RoleVehicles] = {}
        for role, found in choice.vehicles.items():
            if units[int(found.primary)].unit is None:
                plan.unfilled[role] = "its vehicle has no price"
                continue
            partner = found.partner
            if partner is not None and units[int(partner)].unit is None:
                partner = None
            vehicles[role] = RoleVehicles(found.primary, partner)
        return vehicles

    def _vehicle_roles(
        self,
        portfolio: TargetPortfolio,
        held: Mapping[int, BookHolding],
        units: Mapping[int, UnitValue],
    ) -> list[_Role]:
        """Every vehicle the target names or the core holds, with what is held and its price."""
        weights = {int(v.key): v.weight for v in portfolio.vehicles}
        roles_of = {int(v.key): v.role for v in portfolio.vehicles}
        out: list[_Role] = []
        for conid in [*weights, *sorted(set(held) - set(weights))]:
            holding = held.get(conid)
            label = roles_of.get(conid) or f"unlisted {conid}"
            out.append(
                _Role.valued(
                    label,
                    weights.get(conid, ZERO),
                    ZERO if holding is None else holding.units,
                    units[conid],
                )
            )
        return out

    def _decide(
        self,
        portfolio: TargetPortfolio,
        roles: Sequence[_Role],
        busy: set[int],
        base: Decimal,
        managed: Decimal,
        spendable: Decimal,
        at: datetime,
        *,
        refused: Mapping[int, str],
        hooks: TaxHooks | None,
        sells_paused: bool,
    ) -> list[RoleDecision]:
        band = self._trading.inner_band
        least = minimum_trade(self._trading, managed)
        deltas = self._deltas(roles, base)
        sells, buys = dict(deltas.sells), dict(deltas.buys)
        decided: dict[int, RoleDecision] = {}
        short: list[tuple[Decimal, _Role, str, Decimal]] = []
        for role in roles:
            if role.instrument is None or role.unit is None:
                why = "no instrument describes the conid" if role.instrument is None else "no price"
                decided[role.conid] = role.row(base, SKIP, why)
                continue
            if role.conid in refused:
                why = refused[role.conid]
                decided[role.conid] = role.row(base, SKIP, f"not confirmed: {why}")
                continue
            key = security_key(role.conid)
            value = role.held * role.unit
            target = role.weight * base
            if portfolio.rebalance:
                sell, buy = sells.get(key, ZERO), buys.get(key, ZERO)
                if sell == ZERO and buy == ZERO:
                    decided[role.conid] = role.row(base, HOLD, "inside its band")
                    continue
            else:
                sell = ZERO
                if value > target:
                    why = "inside the outer bands: a sale waits for the outer band"
                    decided[role.conid] = role.row(base, HOLD, why)
                    continue
                if target - value <= min(band.absolute, band.relative * role.weight) * base:
                    decided[role.conid] = role.row(base, HOLD, "inside its band")
                    continue
                buy = _whole_units(target - value, role.unit)
            if role.conid in busy:
                decided[role.conid] = role.row(base, SKIP, "an intent already works on it")
                continue
            if sell > ZERO and sells_paused:
                decided[role.conid] = role.row(
                    base,
                    SKIP,
                    "sell: an open reservation is not yet covered; the reservation planner "
                    "chooses those sales",
                )
                continue
            try:
                venue = self._calendars.venue_for_ibkr(role.instrument.listing_exchange)
            except CalendarError as exc:
                decided[role.conid] = role.row(base, SKIP, str(exc))
                continue
            closed = self._closed(venue, role.conid, at)
            if closed is not None:
                decided[role.conid] = role.row(base, SKIP, closed)
                continue
            if sell > ZERO:
                decided[role.conid] = self._sized(
                    role, SELL, min(sell, role.held), least, base, venue
                )
            elif hooks is not None and hooks.purchase_defers_loss(role.conid, at.date()):
                decided[role.conid] = role.row(
                    base,
                    SKIP,
                    "buy: it would defer a loss the two-month rule still holds open",
                )
            else:
                short.append((target - value, role, venue, buy))
        # Buys: the largest shortfall first, within what the book may spend.
        for _gap, role, venue, wanted in sorted(short, key=lambda item: item[0], reverse=True):
            unit = role.unit if role.unit is not None else ZERO
            count = min(wanted, _whole_units(spendable, unit))
            why = (
                "the gap is less than one whole unit" if wanted <= ZERO else "no cash for one unit"
            )
            made = self._sized(role, BUY, count, least, base, venue, why_none=why)
            if made.action == BUY:
                spendable -= made.notional
            decided[role.conid] = made
        return [decided[role.conid] for role in roles]

    def _deltas(self, roles: Sequence[_Role], base: Decimal) -> Deltas:
        """The trades that bring every priced vehicle to its target, by the simulator's delta
        execution (``engines/core/trades.py``)."""
        priced = [r for r in roles if r.unit is not None]
        return plan_deltas(
            {security_key(r.conid): r.weight for r in priced},
            {security_key(r.conid): r.held for r in priced},
            {security_key(r.conid): r.unit for r in priced if r.unit is not None},
            base,
            band=self._no_trade,
            whole_units=True,
        )

    def _sized(
        self,
        role: _Role,
        action: str,
        count: Decimal,
        least: Decimal,
        base: Decimal,
        venue: str,
        *,
        why_none: str = "the gap is less than one whole unit",
    ) -> RoleDecision:
        if count <= ZERO:
            return role.row(base, SKIP, f"{action}: {why_none}")
        unit = role.unit if role.unit is not None else ZERO
        notional = count * unit
        commission = self._commission(role, count, venue)
        if commission is None:
            return role.row(
                base, SKIP, f"{action}: no commission schedule, so no minimum can be checked"
            )
        floor = max(least, commission)
        if notional < floor:
            return role.row(
                base, SKIP, f"{action} of {notional:.2f} is below the minimum trade {floor:.2f}"
            )
        return role.row(base, action, "back to its target", count, notional)

    # ── making the intents ──

    async def apply(self, plan: RebalancePlan) -> list[OrderIntent]:
        """An intent for every trade of ``plan``, priced now; a trade that cannot be priced is
        logged and left for the next cycle."""
        made: list[OrderIntent] = []
        for trade in plan.trades:
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
