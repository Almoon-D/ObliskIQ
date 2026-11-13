"""The thesis book's rebalancer: entries and each thesis's own exits, as intents (MASTER_PLAN
§5.5, §5.11; DECISIONS D156, D159).

The book works on its share of investable capital (``capital/budgets.py``: ``thesis.capital_share``
in shadow and paper, nothing on the live account until the capital allocation rule gives it one),
in the satellite sleeve. With no idea it holds nothing and a cycle places nothing. At each
decision cycle, for the theses still open, in order of arrival:

1. a **stock candidate** not pinned yet is resolved through IBKR's contract search
   (``ibkr/identity.py``), pinned for its thesis and described to the order path's directory; one
   that does not resolve is refused;
2. a **candidate** is judged by the gate (``engines/thesis/gate.py``) on what was read this cycle:
   the mandate's prohibited products, the jurisdiction's eligibility, a conid another book holds,
   a fresh quote, the stored liquidity, the caps, the gain-to-loss ratio and a free slot. A
   permanent finding refuses it; any other withholds it, with the reason, until its entry window
   ends. One that passes is **approved** (its size fixed) and its entry is made;
3. an **approved** thesis without a fill is entered again while its entry rule still holds, and
   expires when its window ends or its own levels are passed before it filled;
4. an **active** thesis is exited when its target, its invalidation or its horizon is reached
   (``engines/thesis/exits.py``); one whose position is gone (the protective stop filled, its
   exit filled, the reservation planner sold it) is **closed** and graded.

Entries and exits are intents priced by ``IntentMaker`` and carried by the executor through the
pre-trade gate, a funding conversion first for a foreign currency, as the core's are. An entry
carries its protective stop: a stop-limit at the invalidation (``execution/protective.py``), which
rests at the broker from the first fill wherever orders reach one. Entries wait while the kill
switch is on or reconciliation freezes new risk; exits do not.

:meth:`ThesisRebalancer.survey` only reads, so a dry run shows the plan and writes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..calendars.service import Calendars
from ..capital.budgets import book_allocation
from ..capital.managed import CapitalReader, CapitalView, book_fills
from ..config.schema import ObliskConfig
from ..data.candidates import resolve_candidate
from ..domain.instrument import TradedInstrument
from ..domain.orders import (
    ExecutionMode,
    Fill,
    IntentRole,
    OrderIntent,
    OrderSide,
    ProtectiveSpec,
    ReconcileStatus,
    Sleeve,
)
from ..domain.thesis import OPEN_STATES, SLOT_STATES, LogKind, Thesis, ThesisState
from ..engines.thesis.book import Slot
from ..engines.thesis.exits import OVERRIDE, exit_due
from ..engines.thesis.gate import Finding, GateFacts, Reason, ThesisGate, describe
from ..engines.thesis.grading import grade
from ..errors import CalendarError, ObliskError
from ..execution.account import CashRead
from ..execution.instruments import (
    UniverseDirectory,
    describe_pinned,
    prohibited_reasons,
)
from ..execution.intent_maker import IntentMaker
from ..execution.killswitch import KillSwitch
from ..execution.protective import protective_spec
from ..ibkr.client import Secdef
from ..jurisdiction.eligibility import EligibilityPolicy
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store import theses as thesis_repo
from ..store import universe as universe_repo
from ..store.db import Store
from ..store.orders import IntentFill
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .book_cash import BookCash
from .rebalance_core import minimum_trade
from .thesis_intake import find_candidate
from .vehicle_inputs import market_inputs
from .vehicle_selector import VehicleMarket

log = get_logger("decision.thesis")

ZERO = Decimal(0)

#: Who the book's own log entries are written by.
ACTOR = "thesis book"
#: The reason an entry intent gives.
ENTRY_REASON = "thesis"
#: What the plan does with one thesis.
RESOLVE, REFUSE, EXPIRE, WAIT, ENTER, HOLD, EXIT, CLOSE = (
    "resolve",
    "refuse",
    "expire",
    "wait",
    "enter",
    "hold",
    "exit",
    "close",
)


@dataclass(frozen=True, slots=True)
class ThesisDecision:
    """What the plan does with one thesis this cycle, and why."""

    thesis: Thesis
    action: str
    reason: str
    findings: tuple[Finding, ...] = ()
    quantity: Decimal = ZERO
    size_base: Decimal | None = None
    held: Decimal = ZERO
    fills: tuple[Fill, ...] = ()
    #: An approved thesis whose entry has filled becomes active when the plan is applied.
    opened: bool = False

    @property
    def trades(self) -> bool:
        return self.action in (ENTER, EXIT)


@dataclass
class ThesisPlan:
    """A survey of the thesis book: its capital, every open thesis's decision, and why nothing
    is entered."""

    mode: ExecutionMode
    at: datetime
    capital: Decimal | None = None
    decisions: list[ThesisDecision] = field(default_factory=list)
    blocked: str | None = None
    entries_blocked: str | None = None

    @property
    def trades(self) -> list[ThesisDecision]:
        return [decision for decision in self.decisions if decision.trades]

    def render(self) -> str:
        lines = [f"thesis book ({self.mode.value}) at {self.at.isoformat()}"]
        if self.capital is not None:
            lines.append(f"  the book's capital {self.capital:.2f}")
        if self.blocked is not None:
            lines.append(f"  nothing is decided: {self.blocked}")
        if self.entries_blocked is not None:
            lines.append(f"  no entry now: {self.entries_blocked}")
        if not self.decisions and self.blocked is None:
            lines.append("  no open thesis: the book holds nothing")
        for d in self.decisions:
            t = d.thesis
            trade = f" {d.quantity}" if d.trades else ""
            lines.append(
                f"  {t.ref} {t.instrument.symbol} ({t.source.value}, {t.conviction.value}, "
                f"{t.state.value}) held {d.held}: {d.action}{trade} ({d.reason})"
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Read:
    """What one survey read for every thesis."""

    view: CapitalView
    cash: CashRead
    capital: Decimal
    spendable: Decimal
    minimum: Decimal
    fills: Sequence[IntentFill]
    busy: Sequence[OrderIntent]
    market: Mapping[int, VehicleMarket]
    owners: Mapping[int, str]
    today: date
    at: datetime


class ThesisRebalancer:
    """Plans the thesis book's entries and exits and makes them intents."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        capital: CapitalReader,
        directory: UniverseDirectory,
        calendars: Calendars,
        worker: StoreWorker,
        maker: IntentMaker,
        kill: KillSwitch,
        eligibility: EligibilityPolicy,
        secdef: Secdef,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._capital = capital
        self._directory = directory
        self._calendars = calendars
        self._worker = worker
        self._maker = maker
        self._kill = kill
        self._eligibility = eligibility
        self._secdef = secdef
        self._now = now
        self._gate = ThesisGate(settings.thesis)
        self._liquidity_sessions = settings.thesis.liquidity.sessions
        self._sleeve = Sleeve.SATELLITE
        self._cash = BookCash(
            settings, capital=capital, directory=directory, worker=worker, now=now
        )
        self._sessions = Decimal(settings.execution.collar.sessions_per_year)

    # ── the survey ──

    async def survey(self, mode: ExecutionMode) -> ThesisPlan:
        """The plan for the book now: reads only, writes nothing to the store."""
        at = self._now()
        today = at.date()
        plan = ThesisPlan(mode=mode, at=at)
        await describe_pinned(self._directory, self._worker)
        theses = await self._worker.run(thesis_repo.theses, as_of=today, states=OPEN_STATES)
        if not theses:
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
        spendable = await self._cash.spendable(mode, view, cash)
        if isinstance(spendable, str):
            plan.blocked = spendable
            return plan
        if await self._kill.is_on():
            plan.entries_blocked = "the kill switch is on"
        latest = await self._worker.run(repo.latest_reconciliation)
        if latest is not None and latest.status is not ReconcileStatus.GREEN:
            plan.entries_blocked = f"reconciliation is {latest.status.value}: new risk is frozen"
        allocation = book_allocation(self._settings, view.investable, mode)
        plan.capital = allocation.satellite
        busy, _ = await self._cash.pending(mode)
        conids = [t.instrument.conid for t in theses if t.instrument.conid is not None]
        read = _Read(
            view=view,
            cash=cash,
            capital=allocation.satellite,
            spendable=max(ZERO, spendable - allocation.cash_floor),
            minimum=minimum_trade(self._settings.trading, view.managed),
            fills=book_fills(await self._worker.run(repo.intent_fills), mode),
            busy=[i for i in busy if i.role in (IntentRole.ENTRY, IntentRole.EXIT)],
            market=await self._worker.run(
                market_inputs, conids, as_of=today, sessions=self._liquidity_sessions
            ),
            owners={
                a.conid: str(a.owner) for a in await self._worker.run(repo.holding_assignments)
            },
            today=today,
            at=at,
        )
        slots = {t.thesis_id: _slot(t) for t in theses if t.state in SLOT_STATES}
        spent = ZERO
        for thesis in theses:
            others = tuple(slot for owner, slot in slots.items() if owner != thesis.thesis_id)
            decision = await self._decide(thesis, read, others, spent, plan)
            plan.decisions.append(decision)
            if decision.action == ENTER and decision.size_base is not None:
                spent += decision.size_base
                slots[thesis.thesis_id] = _slot(thesis, decision.size_base)
        return plan

    def _position(self, thesis: Thesis, read: _Read) -> tuple[Decimal, tuple[Fill, ...], str]:
        """The units ``thesis`` holds, the fills that made them and the last sale's reason."""
        since = thesis.approved_at
        conid = thesis.instrument.conid
        mine = [
            entry
            for entry in read.fills
            if since is not None and entry.fill.conid == conid and entry.fill.executed_at >= since
        ]
        held = sum(
            (e.fill.quantity if e.fill.side is OrderSide.BUY else -e.fill.quantity for e in mine),
            ZERO,
        )
        sales = [entry.reason for entry in mine if entry.fill.side is OrderSide.SELL]
        return held, tuple(entry.fill for entry in mine), sales[-1] if sales else OVERRIDE

    async def _decide(
        self, thesis: Thesis, read: _Read, slots: tuple[Slot, ...], spent: Decimal, plan: ThesisPlan
    ) -> ThesisDecision:
        instrument = thesis.instrument
        conid = instrument.conid
        if thesis.mode is not None and thesis.mode is not plan.mode:
            return ThesisDecision(
                thesis, HOLD, f"it belongs to the {thesis.mode.value} record, not this mode's"
            )
        if conid is None:
            if read.today > thesis.enter_by:
                return ThesisDecision(thesis, EXPIRE, "its entry window ended before it resolved")
            return ThesisDecision(thesis, RESOLVE, "its stock candidate is not pinned yet")
        held, fills, last_sale = self._position(thesis, read)
        working = [i for i in read.busy if i.conid == conid]
        bought = any(fill.side is OrderSide.BUY for fill in fills)
        if bought and held <= ZERO:
            if working:
                return ThesisDecision(thesis, HOLD, "an order still works on it", fills=fills)
            return ThesisDecision(thesis, CLOSE, last_sale, fills=fills, opened=True)
        described = self._directory.describe(conid)
        value = await self._capital.valuer.unit(conid, read.cash)
        reference = (
            None
            if described is None or value.unit is None or value.rate is None
            else value.unit / (described.multiplier * value.rate)
        )
        if bought:
            return self._manage(thesis, held, fills, working, reference, read)
        if read.today > thesis.enter_by and not working:
            return ThesisDecision(thesis, EXPIRE, "its entry window ended without a fill")
        if working:
            waits = (Finding(Reason.WORKING, "its entry order is working"),)
            return ThesisDecision(thesis, WAIT, describe(waits), waits)
        if described is None:
            waits = (
                Finding(
                    Reason.NOT_DESCRIBED,
                    f"conid {conid} is not described to the order path (a universe instrument "
                    "needs its pin and description)",
                ),
            )
            return ThesisDecision(thesis, WAIT, describe(waits), waits)
        return self._enter(thesis, described, reference, value.rate, read, slots, spent, plan)

    def _manage(
        self,
        thesis: Thesis,
        held: Decimal,
        fills: tuple[Fill, ...],
        working: Sequence[OrderIntent],
        reference: Decimal | None,
        read: _Read,
    ) -> ThesisDecision:
        """A thesis with a position: hold it, or leave by one of its own exits."""
        opened = thesis.state is ThesisState.APPROVED

        def decided(action: str, reason: str, quantity: Decimal = ZERO) -> ThesisDecision:
            return ThesisDecision(
                thesis, action, reason, quantity=quantity, held=held, fills=fills, opened=opened
            )

        if working:
            return decided(HOLD, "an order still works on it")
        due = exit_due(thesis, reference, read.today)
        if due is not None:
            return decided(EXIT, due, held)
        if reference is None:
            return decided(HOLD, "no fresh quote: its levels are not judged")
        return decided(HOLD, f"at {reference} inside its levels")

    def _enter(
        self,
        thesis: Thesis,
        described: TradedInstrument,
        reference: Decimal | None,
        rate: Decimal | None,
        read: _Read,
        slots: tuple[Slot, ...],
        spent: Decimal,
        plan: ThesisPlan,
    ) -> ThesisDecision:
        """A thesis without a position: the gate, then what stands between it and an order."""
        conid = described.conid
        verdict = self._eligibility.assess(described, underlying=None)
        found = read.market.get(conid)
        facts = GateFacts(
            capital=read.capital,
            base_currency=self._settings.account.base_currency,
            prohibited=tuple(prohibited_reasons(described, None, self._settings)),
            ineligible=None if verdict.eligible else verdict.reason,
            held_elsewhere=self._held_elsewhere(thesis, read, slots),
            reference=reference,
            rate=rate,
            daily_value=None if found is None else found.average_daily_value,
            sessions=0 if found is None else found.sessions,
            slots=slots,
            minimum=read.minimum,
        )
        judged = self._gate.evaluate(thesis, facts)
        if judged.permanent:
            action = REFUSE if thesis.state is ThesisState.CANDIDATE else EXPIRE
            return ThesisDecision(thesis, action, describe(judged.permanent), judged.permanent)
        waits = list(judged.findings)
        if plan.entries_blocked is not None:
            waits.append(Finding(Reason.ENTRIES_BLOCKED, plan.entries_blocked))
        try:
            venue = self._calendars.venue_for_ibkr(described.listing_exchange)
            status = self._calendars.tradability(venue, read.at, conid=conid)
            if not status.ok:
                waits.append(
                    Finding(
                        Reason.VENUE_NOT_TRADABLE,
                        f"{venue} is not tradable now ({status.reason})",
                    )
                )
        except CalendarError as exc:
            waits.append(Finding(Reason.VENUE_NOT_TRADABLE, f"its venue cannot be judged: {exc}"))
        size = judged.size_base
        if size is not None and size > read.spendable - spent:
            waits.append(
                Finding(
                    Reason.NO_CASH,
                    f"its size {size:.2f} is above the {max(ZERO, read.spendable - spent):.2f} "
                    "the book may spend now",
                )
            )
        if waits or size is None or judged.quantity is None:
            return ThesisDecision(thesis, WAIT, describe(waits), tuple(waits))
        return ThesisDecision(
            thesis,
            ENTER,
            f"the gate passed: {judged.quantity} at about {reference}",
            quantity=judged.quantity,
            size_base=size,
        )

    def _held_elsewhere(self, thesis: Thesis, read: _Read, slots: tuple[Slot, ...]) -> str | None:
        """Who else holds or claims the thesis's conid: another book, the owner, another thesis."""
        conid = thesis.instrument.conid
        symbol = thesis.instrument.symbol
        if any(slot.conid == conid for slot in slots):
            return f"another thesis already holds a slot on {symbol}"
        holding = next((h for h in read.view.holdings if h.conid == conid), None)
        if holding is not None and holding.sleeve is not self._sleeve:
            return f"the {holding.sleeve.value} book holds {symbol}"
        owner = None if conid is None else read.owners.get(conid)
        if owner is not None and owner != self._sleeve.value:
            return f"{symbol} is assigned to {owner}, not to the thesis book's sleeve"
        return None

    # ── applying the plan ──

    async def apply(self, plan: ThesisPlan) -> list[OrderIntent]:
        """Carry out ``plan``: pins, changes of state, grades, and an intent for every trade,
        priced now. A trade that cannot be priced is logged and left for the next cycle."""
        made: list[OrderIntent] = []
        for decision in plan.decisions:
            thesis = decision.thesis
            if decision.opened and thesis.state is ThesisState.APPROVED:
                thesis = await self._move(thesis, ThesisState.ACTIVE, "its entry filled")
            if decision.action == RESOLVE:
                await self._resolve(thesis)
            elif decision.action == REFUSE:
                await self._move(thesis, ThesisState.REFUSED, decision.reason)
            elif decision.action == EXPIRE:
                await self._move(thesis, ThesisState.EXPIRED, decision.reason)
            elif decision.action == WAIT:
                await self._note(thesis, LogKind.WITHHELD, decision.reason)
            elif decision.action == CLOSE:
                await self._close(thesis, decision)
            elif decision.action in (ENTER, EXIT):
                intent = await self._trade(thesis, decision, plan)
                if intent is not None:
                    made.append(intent)
        return made

    async def _move(self, thesis: Thesis, to: ThesisState, detail: str) -> Thesis:
        return await self._worker.run(
            thesis_repo.move, thesis.thesis_id, to, at=self._now(), actor=ACTOR, detail=detail
        )

    async def _note(self, thesis: Thesis, kind: LogKind, detail: str) -> None:
        """Log ``detail`` once: an unchanged reason is not written again every cycle."""

        def write(store: Store) -> None:
            latest = thesis_repo.latest_log(store, thesis.thesis_id)
            if latest is None or latest.kind is not kind or latest.detail != detail:
                thesis_repo.append_log(
                    store, thesis.thesis_id, at=self._now(), actor=ACTOR, kind=kind, detail=detail
                )

        await self._worker.run(write)

    async def _resolve(self, thesis: Thesis) -> None:
        """Pin a stock candidate through IBKR's contract search, or refuse its thesis."""
        key = thesis.instrument.candidate_key or ""
        today = self._now().date()
        found = await self._worker.run(
            lambda store: find_candidate(self._settings, store, key, as_of=today)
        )
        if found is None:
            await self._move(
                thesis,
                ThesisState.REFUSED,
                f"{Reason.UNRESOLVED.value}: no stock candidate {key!r} any more",
            )
            return
        try:
            resolution = await resolve_candidate(
                self._secdef, found[0], at=self._now(), pinned_for=thesis.ref
            )
        except ObliskError as exc:  # the broker could not be asked: the next cycle asks again
            await self._note(
                thesis, LogKind.WITHHELD, f"{Reason.UNRESOLVED.value}: not resolved yet: {exc}"
            )
            return
        await self._worker.run(universe_repo.record_resolution, resolution)
        conid, symbol = resolution.conid, resolution.symbol
        exchange, currency = resolution.listing_exchange, resolution.currency
        if conid is None or symbol is None or exchange is None or currency is None:
            await self._move(
                thesis,
                ThesisState.REFUSED,
                f"{Reason.UNRESOLVED.value}: {resolution.failure}",
            )
            return
        await self._worker.run(
            thesis_repo.pin_instrument,
            thesis.thesis_id,
            conid=conid,
            symbol=symbol,
            listing_exchange=exchange,
            currency=currency,
            at=self._now(),
            actor=ACTOR,
        )
        await describe_pinned(self._directory, self._worker)

    def _stop(self, thesis: Thesis) -> Callable[[float | None], ProtectiveSpec]:
        """The protective stop an entry's fills get: a stop-limit at the invalidation, its limit
        offset from the quote's volatility (``execution/protective.py``)."""

        def spec(sigma: float | None) -> ProtectiveSpec:
            daily = None if sigma is None else Decimal(repr(sigma)) / self._sessions.sqrt()
            return protective_spec(
                self._settings, thesis.invalidation, daily_sigma=daily, gap_quantile=None
            )

        return spec

    async def _trade(
        self, thesis: Thesis, decision: ThesisDecision, plan: ThesisPlan
    ) -> OrderIntent | None:
        conid = thesis.instrument.conid
        assert conid is not None  # only a pinned thesis trades
        entering = decision.action == ENTER
        if entering and thesis.state is ThesisState.CANDIDATE:
            assert decision.size_base is not None  # an entry carries the gate's size
            thesis = await self._worker.run(
                thesis_repo.approve,
                thesis.thesis_id,
                mode=plan.mode,
                size_base=decision.size_base,
                at=self._now(),
                actor=ACTOR,
                detail=f"approved at {decision.size_base:.2f}: {decision.reason}",
            )
        intent = await self._maker.make(
            conid=conid,
            side=OrderSide.BUY if entering else OrderSide.SELL,
            quantity=decision.quantity,
            sleeve=self._sleeve,
            reason=ENTRY_REASON if entering else decision.reason,
            mode=plan.mode,
            thesis_id=thesis.ref,
            protective=self._stop(thesis) if entering else None,
        )
        what = "entry" if entering else f"exit ({decision.reason})"
        if isinstance(intent, str):
            log.warning("a thesis trade is not made", extra={"thesis": thesis.ref, "why": intent})
            await self._note(
                thesis, LogKind.WITHHELD, f"{Reason.NOT_PRICED.value}: {what} not made: {intent}"
            )
            return None
        await self._note(
            thesis, LogKind.ORDER, f"{what} of {decision.quantity}: intent {intent.intent_id}"
        )
        return intent

    async def _close(self, thesis: Thesis, decision: ThesisDecision) -> None:
        """A thesis whose position is gone: closed for the reason of its last sale, and graded."""
        at = self._now()
        graded = grade(thesis, decision.fills, exit_reason=decision.reason, at=at)

        def write(store: Store) -> None:
            with store.transaction():
                thesis_repo.move(
                    store,
                    thesis.thesis_id,
                    ThesisState.CLOSED,
                    at=at,
                    actor=ACTOR,
                    detail=(
                        f"closed by {decision.reason}: payoff {graded.payoff:.2%} against the "
                        f"{graded.stated_payoff:.2%} stated, {graded.days_held} of "
                        f"{graded.horizon_days} days"
                    ),
                    exit_reason=decision.reason,
                )
                thesis_repo.record_grade(store, graded)

        await self._worker.run(write)


def _slot(thesis: Thesis, size: Decimal | None = None) -> Slot:
    instrument = thesis.instrument
    committed = size if size is not None else thesis.size_base
    assert instrument.conid is not None  # a slot is a pinned thesis
    assert committed is not None  # and carries its size
    return Slot(
        conid=instrument.conid,
        size_base=committed,
        sector=instrument.sector,
        country=instrument.country,
        currency=instrument.currency,
    )
