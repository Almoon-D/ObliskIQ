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

A thesis may be held as a long option instead of its instrument (``thesis_options.py``): one
that says ``option``, or whose levels describe a fall, is a long call or put when the options
sleeve's gates pass and nothing otherwise; one that says ``auto`` is the option when they pass
and its instrument when they do not.

Entries and exits are intents priced by ``IntentMaker`` and carried by the executor through the
pre-trade gate, a funding conversion first for a foreign currency, as the core's are. An entry
carries its protective stop: a stop-limit at the invalidation (``execution/protective.py``), which
rests at the broker from the first fill wherever orders reach one. Entries wait while the kill
switch is on or reconciliation freezes new risk; exits do not.

:meth:`ThesisRebalancer.survey` only reads, so a dry run shows the plan and writes nothing. Applying
a plan records the book's capital that day (the first reading kept): H13's record divides each
month by the book's capital before it (``scoreboard/h13.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from ..calendars.service import Calendars
from ..capital.allocation import allocation_now
from ..capital.managed import CapitalReader, book_fills
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
    Sleeve,
)
from ..domain.thesis import (
    OPEN_STATES,
    SLOT_STATES,
    Conviction,
    Expression,
    LogKind,
    Thesis,
    ThesisState,
)
from ..engines.thesis.book import Slot
from ..engines.thesis.exits import OVERRIDE, exit_due
from ..engines.thesis.gate import Finding, GateFacts, Reason, ThesisGate, describe
from ..engines.thesis.grading import grade
from ..errors import CalendarError, ObliskError
from ..execution.freeze import new_risk_frozen
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
from ..store import analyst as analyst_repo
from ..store import h13 as h13_repo
from ..store import orders as repo
from ..store import theses as thesis_repo
from ..store import thesis_options as link_repo
from ..store import universe as universe_repo
from ..store.db import Store
from ..store.h13 import BookCapital, record_book_capital
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .book_cash import BookCash
from .breakers import Breakers
from .thesis_intake import find_candidate
from .thesis_options import ThesisOptions
from .thesis_plan import (
    ACTOR,
    CLOSE,
    ENTER,
    ENTRY_REASON,
    EXIT,
    EXPIRE,
    HOLD,
    REFUSE,
    RESOLVE,
    WAIT,
    ZERO,
    BookRead,
    ThesisDecision,
    ThesisPlan,
)
from .vehicle_inputs import market_inputs

__all__ = ["ThesisDecision", "ThesisPlan", "ThesisRebalancer"]

log = get_logger("decision.thesis")

#: The modes of the live account: H13's holdbacks apply there alone.
_LIVE = frozenset({ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE})


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
        breakers: Breakers,
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
        self._breakers = breakers
        self._now = now
        self._reconcile_age = timedelta(seconds=settings.execution.reconcile.max_age_seconds)
        self._gate = ThesisGate(settings.thesis)
        self._liquidity_sessions = settings.thesis.liquidity.sessions
        self._sleeve = Sleeve.SATELLITE
        self._cash = BookCash(
            settings, capital=capital, directory=directory, worker=worker, now=now
        )
        self._sessions = Decimal(settings.execution.collar.sessions_per_year)
        # The absolute minimum trade alone: the share of managed capital beside it is the core's
        # inertia, and a normal idea is a smaller share of the book than that by construction.
        self._minimum = settings.trading.min_trade.absolute_base or ZERO
        self._options = ThesisOptions(
            settings,
            capital=capital,
            directory=directory,
            calendars=calendars,
            worker=worker,
            eligibility=eligibility,
            gate=self._gate,
            now=now,
        )

    # ── the survey ──

    async def survey(self, mode: ExecutionMode, *, rehearsal: bool = False) -> ThesisPlan:
        """The plan for the book now: reads only, writes nothing to the store. ``rehearsal``: a
        dry run, planned as the cycle that reconciles first would plan."""
        at = self._now()
        today = at.date()
        plan = ThesisPlan(mode=mode, at=at)
        await describe_pinned(self._directory, self._worker)
        await self._options.describe()
        theses = await self._worker.run(thesis_repo.theses, as_of=today, states=OPEN_STATES)
        twins = await self._worker.run(link_repo.open_twins)
        if not theses and not twins:
            plan.capital = await self._idle_capital(mode)
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
        frozen_risk = new_risk_frozen(
            latest, now=at, max_age=self._reconcile_age, rehearsal=rehearsal
        )
        if frozen_risk is not None:
            plan.entries_blocked = frozen_risk
        frozen = (await self._breakers.read(mode, view, cash)).buys_refused
        if frozen is not None:
            plan.entries_blocked = frozen
        allocation = await allocation_now(self._worker, self._settings, view.investable, mode)
        plan.capital = allocation.thesis_book
        busy, _ = await self._cash.pending(mode)
        conids = [t.instrument.conid for t in theses if t.instrument.conid is not None]
        read = BookRead(
            view=view,
            cash=cash,
            capital=allocation.thesis_book,
            spendable=max(ZERO, spendable - allocation.cash_floor),
            minimum=self._minimum,
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
            holdbacks=await self._worker.run(h13_repo.holdbacks) if mode in _LIVE else None,
            segments=await self._worker.run(analyst_repo.thesis_segments),
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
        plan.twins = await self._options.twins(read, plan)
        return plan

    async def _idle_capital(self, mode: ExecutionMode) -> Decimal | None:
        """The book's capital when it has nothing to decide: read for H13's record (its capital
        before each month), never a reason to block the cycle."""
        try:
            cash = await self._capital.cash()
            view = await self._capital.read(mode, cash=cash)
        except ObliskError:
            return None
        if isinstance(view, str):
            return None
        allocation = await allocation_now(self._worker, self._settings, view.investable, mode)
        return allocation.thesis_book

    def _position(self, thesis: Thesis, read: BookRead) -> tuple[Decimal, tuple[Fill, ...], str]:
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
        self,
        thesis: Thesis,
        read: BookRead,
        slots: tuple[Slot, ...],
        spent: Decimal,
        plan: ThesisPlan,
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
        described = self._directory.describe(conid)
        value = await self._capital.valuer.unit(conid, read.cash)
        reference = (
            None
            if described is None or value.unit is None or value.rate is None
            else value.unit / (described.multiplier * value.rate)
        )
        held_back = _held_back(thesis, read)
        if held_back is not None and thesis.state is ThesisState.CANDIDATE:
            finding = Finding(Reason.HELD_BACK, held_back, permanent=True)
            return ThesisDecision(thesis, REFUSE, describe((finding,)), (finding,))
        if thesis.as_option:
            return await self._options.held(thesis, reference, read, plan)
        held, fills, last_sale = self._position(thesis, read)
        working = [i for i in read.busy if i.conid == conid]
        bought = any(fill.side is OrderSide.BUY for fill in fills)
        if bought and held <= ZERO:
            if working:
                return ThesisDecision(thesis, HOLD, "an order still works on it", fills=fills)
            return ThesisDecision(
                thesis, CLOSE, last_sale, fills=fills, opened=True, rate=value.rate
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
        note = None
        if thesis.held_as is None and thesis.expression is not Expression.SHARES:
            option = await self._options.enter(
                thesis, described, reference, value.rate, read, slots, spent, plan
            )
            if isinstance(option, ThesisDecision):
                return option
            declared = self._settings.universe.pinned(conid)
            rises = thesis.expression is Expression.AUTO and not thesis.falls
            if not rises:
                return self._not_an_option(thesis, option)
            if declared is not None and not declared.tradable:
                why = declared.data_only_reason or "it is not a tradable candidate"
                option.append(
                    Finding(
                        Reason.NOT_TRADABLE, f"{declared.symbol} itself cannot be ordered: {why}"
                    )
                )
                return self._not_an_option(thesis, option)
            note = f"not held as an option: {describe(option)}"
        decision = self._enter(thesis, described, reference, value.rate, read, slots, spent, plan)
        if note is None:
            return decision
        if decision.action == WAIT:
            # One log entry while it waits, not two that take turns.
            return replace(decision, reason=f"{decision.reason}; {note}")
        return replace(decision, note=note)

    @staticmethod
    def _not_an_option(thesis: Thesis, found: list[Finding]) -> ThesisDecision:
        """A thesis that is an option or nothing, and is not one now: refused when it never can
        be, otherwise withheld with why."""
        permanent = tuple(finding for finding in found if finding.permanent)
        if permanent:
            action = REFUSE if thesis.state is ThesisState.CANDIDATE else EXPIRE
            return ThesisDecision(thesis, action, describe(permanent), permanent)
        return ThesisDecision(thesis, WAIT, describe(found), tuple(found))

    def _manage(
        self,
        thesis: Thesis,
        held: Decimal,
        fills: tuple[Fill, ...],
        working: Sequence[OrderIntent],
        reference: Decimal | None,
        read: BookRead,
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
        # A held position is always priced here: the capital view refuses the survey otherwise.
        return decided(HOLD, f"at {reference} inside its levels")

    def _enter(
        self,
        thesis: Thesis,
        described: TradedInstrument,
        reference: Decimal | None,
        rate: Decimal | None,
        read: BookRead,
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
            sized_as=_sized_as(thesis, read),
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

    def _held_elsewhere(
        self, thesis: Thesis, read: BookRead, slots: tuple[Slot, ...]
    ) -> str | None:
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
        if plan.capital is not None:
            reading = BookCapital(plan.mode, plan.at.date(), plan.capital, plan.at)
            await self._worker.run(record_book_capital, reading)
        await self._options.close_twins(plan)
        for decision in plan.decisions:
            thesis = decision.thesis
            await self._options.apply(decision)
            if decision.note is not None:
                await self._note(thesis, LogKind.WITHHELD, decision.note)
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
            elif decision.action == CLOSE and decision.option is not None:
                await self._options.close(thesis, decision)
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
        # A broker that cannot be read blocks the survey before this; a search that answers
        # and finds no one contract is final.
        resolution = await resolve_candidate(
            self._secdef, found[0], at=self._now(), pinned_for=thesis.ref
        )
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
        conid = decision.conid
        assert conid is not None  # only a pinned thesis trades
        entering = decision.action == ENTER
        as_option = decision.option is not None
        if entering and thesis.state is ThesisState.CANDIDATE and as_option:
            thesis = await self._options.approve(decision, plan)
        elif entering and thesis.state is ThesisState.CANDIDATE:
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
            # An option carries no stop: its loss is its premium.
            protective=self._stop(thesis) if entering and not as_option else None,
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
        if decision.rate is not None:
            gain = (graded.exit_price - graded.entry_price) * graded.quantity * decision.rate
            await self._options.repay(decision, gain)


def _held_back(thesis: Thesis, read: BookRead) -> str | None:
    """Why H13 holds the thesis's source (or analyst segment) back on the live account."""
    held = read.holdbacks
    if held is None:
        return None
    source = held.sources.get(thesis.source.value)
    if source is not None:
        return f"its source {thesis.source.value} is no longer traded on the account: {source}"
    segment = read.segments.get(thesis.thesis_id)
    found = None if segment is None else held.segments.get(segment)
    if found is not None:
        return f"its analyst segment is no longer traded on the account: {found}"
    return None


def _sized_as(thesis: Thesis, read: BookRead) -> Conviction | None:
    """``normal`` when H13 holds the thesis's conviction level back on the live account."""
    held = read.holdbacks
    if held is None or thesis.conviction.value not in held.convictions:
        return None
    return Conviction.NORMAL


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
