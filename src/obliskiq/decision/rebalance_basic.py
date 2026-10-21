"""The basic rebalancer (Session 6's walking skeleton): targets against holdings, as intents.

The skeleton's static book (``core.skeleton_targets``) is held at its weights of the core's budget
(``capital/budgets.py``): investable capital (managed less reserved less unmanaged,
``capital/managed.py``) times the core's share, which keeps the budgets of the sleeves that do not
trade yet and leaves the cash floor as cash. At each decision cycle every role is valued at its
quote's reference price in the base currency, and a role whose weight has left its **inner band**
(``trading.inner_band``: the smaller of an absolute width and a share of its target) is traded back
to its target:

* in **integer units**, rounded toward zero, a sale never beyond what the sleeve holds;
* never below the **minimum trade** (``trading.min_trade``), nor below the order's own commission
  (the commission floor); an order whose commission the cost model cannot price is not made,
  since the floor cannot be checked;
* buys within what the book may spend: the smaller of the account's cash and the book's own cash,
  less the open withdrawal reservations, the part of a deposit not yet released for deployment
  (``capital/deploy.py``), every open buy's commitment and ``execution.pretrade.cash_floor``, all
  in the base currency; the largest shortfall is bought first;
* no buy that would take a loss the two-month rule still holds open (the live tax lots), so a
  loss the reservation planner or the owner realised is not deferred by the rebalancer;
* no sale while an open reservation is not yet covered by the book's cash: the reservation
  planner (``capital/planner.py``) chooses those sales, by their tax, trading and strategy cost.

A role is left alone, with its reason, when its venue is not tradable now, an intent of this mode
already works on it, or it has no price. The book is not rebalanced at all when a held position
cannot be valued, the capital cannot be read, the kill switch is on or reconciliation freezes new
risk. A core holding the targets no longer name has a target of zero. Holdings are the broker's
positions the registry assigns to the sleeve; in shadow mode, the sleeve's simulated fills.

Before it trades, each target's pinned conid is confirmed against IBKR's contract search
(:func:`confirm_targets`: the symbol on its listing venue in its currency must resolve to that
conid); a role that is not confirmed is skipped, with why, until it is.

Each trade becomes an intent priced by ``Pricer.initial`` (``execution/intent_maker.py``), an entry
for a buy and an exit (reason ``rebalance``) for a sale, without protective stops (MASTER_PLAN
§5.4: core positions carry none). The executor clears, funds, places, reprices and reconciles
them. :meth:`BasicRebalancer.survey` only reads, so a dry run shows the plan and writes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from ..calendars.service import Calendars
from ..capital.budgets import SleeveBudgets
from ..capital.deploy import held_back
from ..capital.managed import CapitalReader, CapitalView
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
from ..tax.book import TaxBook, TaxHooks
from ..util.timeutil import utc_now

log = get_logger("decision.rebalance")

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


async def confirm_targets(
    settings: ObliskConfig, directory: InstrumentDirectory, secdef: Secdef
) -> dict[int, str]:
    """Each skeleton target whose pinned conid IBKR's contract search does not confirm, with why
    (an empty mapping: every one is confirmed)."""
    refused: dict[int, str] = {}
    for target in settings.core.skeleton_targets:
        instrument = directory.describe(target.conid)
        if instrument is None:
            refused[target.conid] = "no instrument describes it"
            continue
        try:
            identity = await resolve_identity(
                secdef,
                instrument.symbol,
                listing_exchange=instrument.listing_exchange,
                currency=instrument.currency,
            )
        except ObliskError as exc:
            refused[target.conid] = f"its identity cannot be confirmed: {exc}"
            continue
        if identity.conid != target.conid:
            refused[target.conid] = (
                f"IBKR resolves {instrument.symbol} on {instrument.listing_exchange} to conid "
                f"{identity.conid}, not the pinned {target.conid}"
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


class BasicRebalancer:
    """Plans the skeleton book's trades and makes them intents."""

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
        self._now = now
        self._sleeve = Sleeve.CORE
        self._base = settings.account.base_currency
        self._trading = settings.trading
        self._floor = settings.execution.pretrade.cash_floor
        self._budgets = SleeveBudgets(settings.capital)
        self._refused: dict[int, str] = {}

    def refuse(self, refused: dict[int, str]) -> None:
        """Roles whose conid is not confirmed (:func:`confirm_targets`): skipped, with why."""
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
        targets = self._settings.core.skeleton_targets
        named = {target.conid for target in targets}
        roles: list[_Role] = []
        for target in targets:
            holding = held.get(target.conid)
            value = (
                holding.unit
                if holding is not None
                else await self._capital.valuer.unit(target.conid, cash)
            )
            units = ZERO if holding is None else holding.units
            roles.append(_Role.valued(target.role, target.weight, units, value))
        roles += [
            _Role.valued(f"unlisted {conid}", ZERO, holding.units, holding.unit)
            for conid, holding in sorted(held.items())
            if conid not in named
        ]
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
        plan.decisions = self._decide(
            roles,
            busy,
            base,
            view.managed,
            plan.spendable,
            at,
            hooks=await self._hooks(),
            sells_paused=unfunded,
        )
        return plan

    def _decide(
        self,
        roles: Sequence[_Role],
        busy: set[int],
        base: Decimal,
        managed: Decimal,
        spendable: Decimal,
        at: datetime,
        *,
        hooks: TaxHooks | None,
        sells_paused: bool,
    ) -> list[RoleDecision]:
        band = self._trading.inner_band
        least = minimum_trade(self._trading, managed)
        decided: dict[int, RoleDecision] = {}
        short: list[tuple[Decimal, _Role, str]] = []
        for role in roles:
            if role.instrument is None:
                decided[role.conid] = role.row(base, SKIP, "no instrument describes the conid")
                continue
            if role.conid in self._refused:
                why = self._refused[role.conid]
                decided[role.conid] = role.row(base, SKIP, f"not confirmed: {why}")
                continue
            if role.unit is None:
                decided[role.conid] = role.row(base, SKIP, "no price")
                continue
            value = role.held * role.unit
            target = role.weight * base
            if abs(value - target) <= min(band.absolute, band.relative * role.weight) * base:
                decided[role.conid] = role.row(base, HOLD, "inside its band")
                continue
            if role.conid in busy:
                decided[role.conid] = role.row(base, SKIP, "an intent already works on it")
                continue
            if value > target and sells_paused:
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
            if value > target:
                count = min(_whole_units(value - target, role.unit), role.held)
                decided[role.conid] = self._sized(role, SELL, count, least, base, venue)
            elif hooks is not None and hooks.purchase_defers_loss(role.conid, at.date()):
                decided[role.conid] = role.row(
                    base,
                    SKIP,
                    "buy: it would defer a loss the two-month rule still holds open",
                )
            else:
                short.append((target - value, role, venue))
        # Buys: the largest shortfall first, within what the book may spend.
        for shortfall, role, venue in sorted(short, key=lambda item: item[0], reverse=True):
            unit = role.unit if role.unit is not None else ZERO
            wanted = _whole_units(shortfall, unit)
            count = min(wanted, _whole_units(spendable, unit))
            why = (
                "the gap is less than one whole unit" if wanted <= ZERO else "no cash for one unit"
            )
            made = self._sized(role, BUY, count, least, base, venue, why_none=why)
            if made.action == BUY:
                spendable -= made.notional
            decided[role.conid] = made
        return [decided[role.conid] for role in roles]

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
