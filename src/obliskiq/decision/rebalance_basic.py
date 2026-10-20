"""The basic rebalancer (Session 6's walking skeleton): targets against holdings, as intents.

The skeleton's static book (``core.skeleton_targets``) is held at its weights of managed capital
(``capital/``). At each decision cycle every role is valued at its quote's reference price in the
base currency, and a role whose weight has left its **inner band** (``trading.inner_band``: the
smaller of an absolute width and a share of its target) is traded back to its target:

* in **integer units**, rounded toward zero, a sale never beyond what the sleeve holds;
* never below the **minimum trade** (``trading.min_trade``), nor below the order's own commission
  (the commission floor); an order whose commission the cost model cannot price is not made,
  since the floor cannot be checked;
* buys within what the book may spend: the smaller of the account's cash and the book's own cash
  under its capital (managed capital less what the sleeve holds), less every open buy's
  commitment and ``execution.pretrade.cash_floor``, all in the base currency; the largest
  shortfall is bought first.

A role is left alone, with its reason, when its venue is not tradable now, an intent of this mode
already works on it, or it has no price. The book is not rebalanced at all when a held position
cannot be valued, the NAV cannot be read, the kill switch is on or reconciliation freezes new risk.
A core holding the targets no longer name has a target of zero. Holdings are the broker's
positions the registry assigns to the sleeve; in shadow mode, the sleeve's simulated fills.

Before it trades, each target's pinned conid is confirmed against IBKR's contract search
(:func:`confirm_targets`: the symbol on its listing venue in its currency must resolve to that
conid); a role that is not confirmed is skipped, with why, until it is.

Each trade becomes an intent priced by ``Pricer.initial`` (a patient limit inside the mid, or an
enabled algo with a limit cap), an entry for a buy and an exit (reason ``rebalance``) for a sale,
without protective stops (MASTER_PLAN §5.4: core positions carry none). The executor clears,
funds, places, reprices and reconciles them. :meth:`BasicRebalancer.survey` only reads, so a dry
run shows the plan and writes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from ..calendars.service import Calendars
from ..capital.managed import ManagedCapital, managed_capital
from ..config.schema import ObliskConfig
from ..config.schema_runtime import TradingConfig
from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument
from ..domain.orders import (
    TERMINAL_STATES,
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderTerms,
    ReconcileStatus,
    Sleeve,
    TimeInForce,
)
from ..errors import CalendarError, ObliskError
from ..execution.account import AccountReader, CashRead
from ..execution.executor import ContractReader
from ..execution.instruments import InstrumentDirectory
from ..execution.intents import new_intent
from ..execution.killswitch import KillSwitch
from ..execution.pricing import Pricer
from ..execution.state import IntentBook
from ..ibkr.client import Secdef
from ..ibkr.identity import resolve_identity
from ..ibkr.orders import SMART_ROUTER
from ..logging_setup import get_logger
from ..quant.costs import CostBook
from ..quant.costs import OrderTerms as CostTerms
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

log = get_logger("decision.rebalance")

ZERO = Decimal(0)

#: The reason every rebalancing intent gives (an allowed exit reason, discipline).
REASON = "rebalance"

#: Actions a role's decision may take.
HOLD, BUY, SELL, SKIP = "hold", "buy", "sell", "skip"


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
    capital: ManagedCapital | None = None
    spendable: Decimal | None = None
    decisions: list[RoleDecision] = field(default_factory=list)
    blocked: str | None = None

    @property
    def trades(self) -> list[RoleDecision]:
        return [d for d in self.decisions if d.action in (BUY, SELL)]

    def render(self) -> str:
        lines = [f"rebalance plan ({self.mode.value}) at {self.at.isoformat()}"]
        if self.capital is not None:
            lines.append(
                f"  managed capital {self.capital.managed:.2f} ({self.capital.mode}: "
                f"{self.capital.detail}); account NAV {self.capital.nav:.2f}"
            )
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

    def row(
        self,
        managed: Decimal,
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
            target=self.weight * managed,
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
        reader: AccountReader,
        contracts: ContractReader,
        directory: InstrumentDirectory,
        calendars: Calendars,
        worker: StoreWorker,
        book: IntentBook,
        pricer: Pricer,
        costs: CostBook,
        kill: KillSwitch,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._contracts = contracts
        self._directory = directory
        self._calendars = calendars
        self._worker = worker
        self._book = book
        self._pricer = pricer
        self._costs = costs
        self._kill = kill
        self._now = now
        self._sleeve = Sleeve.CORE
        self._base = settings.account.base_currency
        self._trading = settings.trading
        self._floor = settings.execution.pretrade.cash_floor
        self._refused: dict[int, str] = {}

    def refuse(self, refused: dict[int, str]) -> None:
        """Roles whose conid is not confirmed (:func:`confirm_targets`): skipped, with why."""
        self._refused = dict(refused)

    # ── reading the book ──

    async def holdings(self, mode: ExecutionMode) -> dict[int, Decimal]:
        """Units the sleeve holds by conid: the broker's positions the registry assigns to it, or
        in shadow mode its simulated fills."""
        if mode is ExecutionMode.SHADOW:
            held: dict[int, Decimal] = {}
            for stored in await self._worker.run(repo.intents_in_states, list(IntentState)):
                intent = stored.intent
                if intent.mode is not ExecutionMode.SHADOW or intent.sleeve is not self._sleeve:
                    continue
                for fill in await self._worker.run(repo.fills_for_intent, intent.intent_id):
                    signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
                    held[fill.conid] = held.get(fill.conid, ZERO) + signed
            return {conid: units for conid, units in held.items() if units != ZERO}
        owners = await self._worker.run(repo.holding_assignments)
        ours = sorted(a.conid for a in owners if a.owner == self._sleeve)
        positions = await self._reader.positions()
        return {c: positions.quantity(c) for c in ours if positions.quantity(c) != ZERO}

    async def _rate(self, currency: str, cash: CashRead) -> Decimal | None:
        """One unit of ``currency`` in the base currency: the ledger's rate, else a pair's quote."""
        if currency == self._base:
            return Decimal(1)
        line = cash.line(currency)
        if line is not None and line.rate_to_base is not None:
            return line.rate_to_base
        for pair in self._settings.execution.currency.pairs:
            if {pair.base, pair.quote} != {currency, self._base}:
                continue
            try:
                quote = await self._reader.quote(pair.conid, option=False)
            except ObliskError:
                return None
            reference = None if quote is None else quote.evidence.reference()
            if reference is None:
                return None
            return reference if currency == pair.base else 1 / reference
        return None

    async def _role(
        self, role: str, conid: int, weight: Decimal, held: Decimal, cash: CashRead
    ) -> _Role:
        instrument = self._directory.describe(conid)
        unit = rate = None
        if instrument is not None:
            rate = await self._rate(instrument.currency, cash)
            try:
                quote = await self._reader.quote(
                    conid, option=instrument.asset_class is AssetClass.OPTION
                )
            except ObliskError:
                quote = None
            reference = None if quote is None else quote.evidence.reference()
            if reference is not None and rate is not None:
                unit = reference * instrument.multiplier * rate
        return _Role(role, conid, weight, instrument, held, unit, rate)

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
            rate = None if instrument is None else await self._rate(instrument.currency, cash)
            quantity = intent.terms.quantity
            if instrument is None or rate is None or quantity is None:
                return None
            fills = await self._worker.run(repo.fills_for_intent, intent.intent_id)
            left = max(ZERO, quantity - sum((fill.quantity for fill in fills), ZERO))
            total += left * intent.terms.price.limit * instrument.multiplier * rate
        return total

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
            cash = await self._reader.cash()
            held = await self.holdings(mode)
        except ObliskError as exc:
            plan.blocked = f"the account cannot be read: {exc}"
            return plan
        capital = managed_capital(self._settings.capital, cash.nav)
        if isinstance(capital, str):
            plan.blocked = capital
            return plan
        plan.capital = capital
        targets = self._settings.core.skeleton_targets
        named = {target.conid for target in targets}
        wanted = [(t.role, t.conid, t.weight) for t in targets]
        wanted += [(f"unlisted {c}", c, ZERO) for c in sorted(held) if c not in named]
        roles = [
            await self._role(name, conid, weight, held.get(conid, ZERO), cash)
            for name, conid, weight in wanted
        ]
        unvalued = [r.role for r in roles if r.held != ZERO and r.unit is None]
        if unvalued:
            plan.blocked = f"a held role has no price: {', '.join(unvalued)}"
            return plan
        holdings_value = sum((r.held * r.unit for r in roles if r.unit is not None), ZERO)
        busy, buys = await self._pending(mode)
        committed = await self._committed(buys, cash)
        if committed is None:
            plan.blocked = "an open buy cannot be valued in the base currency"
            return plan
        account_cash = ZERO
        for line in cash.lines:
            rate = await self._rate(line.currency, cash)
            if rate is None:
                plan.blocked = f"the {line.currency} cash cannot be valued in {self._base}"
                return plan
            account_cash += line.cash * rate
        book_cash = capital.managed - holdings_value
        plan.spendable = max(ZERO, min(account_cash, book_cash) - committed - self._floor)
        plan.decisions = self._decide(roles, busy, capital.managed, plan.spendable, at)
        return plan

    def _decide(
        self,
        roles: Sequence[_Role],
        busy: set[int],
        managed: Decimal,
        spendable: Decimal,
        at: datetime,
    ) -> list[RoleDecision]:
        band = self._trading.inner_band
        least = minimum_trade(self._trading, managed)
        decided: dict[int, RoleDecision] = {}
        short: list[tuple[Decimal, _Role, str]] = []
        for role in roles:
            if role.instrument is None:
                decided[role.conid] = role.row(managed, SKIP, "no instrument describes the conid")
                continue
            if role.conid in self._refused:
                why = self._refused[role.conid]
                decided[role.conid] = role.row(managed, SKIP, f"not confirmed: {why}")
                continue
            if role.unit is None:
                decided[role.conid] = role.row(managed, SKIP, "no price")
                continue
            value = role.held * role.unit
            target = role.weight * managed
            if abs(value - target) <= min(band.absolute, band.relative * role.weight) * managed:
                decided[role.conid] = role.row(managed, HOLD, "inside its band")
                continue
            if role.conid in busy:
                decided[role.conid] = role.row(managed, SKIP, "an intent already works on it")
                continue
            try:
                venue = self._calendars.venue_for_ibkr(role.instrument.listing_exchange)
            except CalendarError as exc:
                decided[role.conid] = role.row(managed, SKIP, str(exc))
                continue
            closed = self._closed(venue, role.conid, at)
            if closed is not None:
                decided[role.conid] = role.row(managed, SKIP, closed)
                continue
            if value > target:
                count = min(_whole_units(value - target, role.unit), role.held)
                decided[role.conid] = self._sized(role, SELL, count, least, managed, venue)
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
            made = self._sized(role, BUY, count, least, managed, venue, why_none=why)
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
        managed: Decimal,
        venue: str,
        *,
        why_none: str = "the gap is less than one whole unit",
    ) -> RoleDecision:
        if count <= ZERO:
            return role.row(managed, SKIP, f"{action}: {why_none}")
        unit = role.unit if role.unit is not None else ZERO
        notional = count * unit
        commission = self._commission(role, count, venue)
        if commission is None:
            return role.row(
                managed, SKIP, f"{action}: no commission schedule, so no minimum can be checked"
            )
        floor = max(least, commission)
        if notional < floor:
            return role.row(
                managed, SKIP, f"{action} of {notional:.2f} is below the minimum trade {floor:.2f}"
            )
        return role.row(managed, action, "back to its target", count, notional)

    # ── making the intents ──

    async def apply(self, plan: RebalancePlan) -> list[OrderIntent]:
        """An intent for every trade of ``plan``, priced now; a trade that cannot be priced is
        logged and left for the next cycle."""
        made: list[OrderIntent] = []
        now = self._now()
        expires = now + timedelta(minutes=self._trading.intent_ttl_minutes)
        for trade in plan.trades:
            instrument = self._directory.describe(trade.conid)
            if instrument is None:
                continue
            side = OrderSide.BUY if trade.action == BUY else OrderSide.SELL
            try:
                venue = self._calendars.venue_for_ibkr(instrument.listing_exchange)
                quote = await self._reader.quote(
                    trade.conid, option=instrument.asset_class is AssetClass.OPTION
                )
                rules = await self._contracts.rules(trade.conid, is_buy=side is OrderSide.BUY)
                algos = await self._contracts.algos(trade.conid)
            except (CalendarError, ObliskError) as exc:
                log.warning(
                    "a trade cannot be priced", extra={"role": trade.role, "error": str(exc)}
                )
                continue
            if quote is None:
                log.warning("a trade has no quote", extra={"role": trade.role})
                continue
            priced = self._pricer.initial(
                instrument,
                side,
                quote.evidence,
                sigma=quote.sigma,
                rules=rules,
                algos=algos,
                venue=venue,
            )
            if priced.price is None:
                log.warning(
                    "a trade is not priced", extra={"role": trade.role, "why": priced.detail}
                )
                continue
            intent = new_intent(
                self._settings,
                sleeve=self._sleeve,
                role=IntentRole.ENTRY if side is OrderSide.BUY else IntentRole.EXIT,
                terms=OrderTerms(
                    conid=trade.conid,
                    side=side,
                    quantity=trade.quantity,
                    price=priced.price,
                    tif=TimeInForce.DAY,
                    venue=SMART_ROUTER,
                ),
                reason=REASON,
                mode=plan.mode,
                approval_required=False,
                created_at=now,
                expires_at=expires,
            )
            await self._book.add(intent)
            made.append(intent)
        return made
