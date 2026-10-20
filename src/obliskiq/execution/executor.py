"""The executor (MASTER_PLAN §5.12): an intent becomes a filled, protected, reconciled position.

Each :meth:`Executor.cycle`:

1. **Sync.** The live orders and the trades are read; each order's intent and each fill recorded.
2. **Settle exits** (I5): stops cut for an exit that ended unfilled are restored
   (``ExitCoordinator.settle``; a ``restore_failed`` one is tried again).
3. **Expire.** An intent past its ``expires_at`` ends: one not yet sent expires; a working order is
   cancelled first. A protective stop is left resting: it protects the position, and its intent
   only governs its modifies.
4. **Follow fills.** A new holding is assigned to the sleeve whose entry filled it, and an entry
   that carries a protective spec gets its stop, resized with each fill
   (``ProtectiveManager.protect``). The stop is placed on the first fill: IBKR's attached orders
   are not used (DECISIONS D99).
5. **Reprice or cancel.** A working entry or exit at a patient limit is repriced toward the touch
   every ``pricing.reprice_interval_minutes``, each a modify the gate clears for its new terms;
   after the last reprice, or when its venue's window ends before the next reprice would, it is
   cancelled — still inside the window, so a cancelled exit's stops are restored there (I5) — and
   retried at the next tradable window by a successor intent priced afresh. An entry's reprice is
   refused while the kill switch is on; a stop is never touched here. In shadow mode an unfilled
   simulated order is repriced on the same clock (its terms change; nothing is sent) and given up
   the same way (DECISIONS D103).
6. **Place.** Proposed intents that need no approval are approved and approved ones staged (one
   that adds risk waits while reconciliation freezes new risk). Staged buys in a foreign currency
   are funded first (``CurrencyManager.fund``): a buy whose conversion has not filled is not
   placed. Each is cleared and placed — an exit through ``ExitCoordinator.place_exit`` — or, in
   shadow mode, checked (``PretradeGate.check(simulated=True)``) and filled by ``ShadowFiller``.
   A refusal that may pass later leaves the intent staged until it expires; any other rejects
   it. A submitted intent whose outcome is unknown is placed again once the grace period has
   passed, its cOID looked up first.
7. **Protect.** The stop check (``ProtectiveManager.verify``) and the gap repricing.
8. **Repatriate** foreign cash above its buffer.
9. **Reconcile** (``Reconciler.run``): drift freezes new risk.

:meth:`Executor.resume` runs once after a restart: every intent that may be at the broker is looked
up by its cOID; one found is followed, one not found is placed again by the next cycle only once
the grace period has passed, the lookup first. A crash after a placement never sends a second
order. One process runs the executor (DECISIONS D47).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.orders import (
    ApprovalDecision,
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderIntent,
    OrderSide,
    ReconcileRun,
    ReconcileStatus,
)
from ..errors import CalendarError, ObliskError
from ..ibkr.models import Algo, ContractRules
from ..ibkr.orders import OrderClient, PlacementStatus, is_live
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store.orders import StoredIntent
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .account import AccountReader
from .boundary import OrderAlert, OrderAlertHook, OrderAlertKind, WriteBoundary, raise_alert
from .currency import ConversionOutcome, CurrencyManager
from .exits import ExitCoordinator
from .instruments import InstrumentDirectory
from .intents import new_intent
from .modes import Modes
from .pretrade import PretradeGate, PretradeRefusal, Reason
from .pricing import Pricer, cost_venue
from .protective import ProtectiveManager, ProtectOutcome
from .reconcile import Reconciler
from .shadow import ShadowFiller
from .state import BROKER_STATES, IntentBook

log = get_logger("execution.executor")

ZERO = Decimal(0)

#: The fact every reprice is recorded under, so the reprices done can be counted.
REPRICED = "repriced"
#: The fact an order cancelled for want of a fill carries: a successor follows at the next window.
RETRY_NEXT_WINDOW = "cancelled to retry at the next tradable window"

#: Refusals no later moment can lift: the intent is rejected. Any other leaves it staged.
PERMANENT_REFUSALS: Final = frozenset(
    {
        Reason.INTENT_UNKNOWN,
        Reason.INTENT_STATE,
        Reason.INTENT_EXPIRED,
        Reason.TERMS_MISMATCH,
        Reason.CONVERSION_PAIR,
        Reason.PAPER_GUARD,
        Reason.PROFILE_MISMATCH,
        Reason.INSTRUMENT_UNKNOWN,
        Reason.PROHIBITED_PRODUCT,
        Reason.INELIGIBLE,
        Reason.MANDATORY_CLOSE,
        Reason.OPTION_EXPIRED,
        Reason.PROTECTIVE_SPEC,
        Reason.HOLDING_UNMANAGED,
        Reason.HOLDING_OTHER_SLEEVE,
        Reason.WRITTEN_OPTION,
        Reason.ORDER_NOTIONAL_CAP,
    }
)

#: Roles whose intents their own managers place: the executor never stages or places them.
_MANAGED_ROLES = frozenset({IntentRole.PROTECTIVE, IntentRole.CONVERSION})


class ContractReader(Protocol):
    """A contract's trading rules and the algos IBKR offers on it."""

    async def rules(
        self, conid: int, *, exchange: str | None = None, is_buy: bool = True
    ) -> ContractRules: ...

    async def algos(self, conid: int) -> tuple[Algo, ...]: ...


@dataclass(frozen=True, slots=True)
class Step:
    """What a cycle did with one intent."""

    intent_id: UUID
    action: str
    detail: str


@dataclass
class CycleReport:
    """One cycle: what it did with each intent, the stops, the conversions and the
    reconciliation it ended with."""

    steps: list[Step] = field(default_factory=list)
    protective: list[ProtectOutcome] = field(default_factory=list)
    conversions: list[ConversionOutcome] = field(default_factory=list)
    reconcile: ReconcileRun | None = None

    def add(self, intent_id: UUID, action: str, detail: str) -> None:
        self.steps.append(Step(intent_id, action, detail))


def adds_risk(intent: OrderIntent) -> bool:
    """Every buy and conversion adds risk; a sell reduces it (the gate decides it exactly)."""
    return intent.side is OrderSide.BUY or intent.terms.conversion


class Executor:
    """Runs intents through funding, pricing, placement, protection and reconciliation."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        orders: OrderClient,
        gate: PretradeGate,
        boundary: WriteBoundary,
        book: IntentBook,
        worker: StoreWorker,
        reader: AccountReader,
        contracts: ContractReader,
        directory: InstrumentDirectory,
        calendars: Calendars,
        modes: Modes,
        coordinator: ExitCoordinator,
        currency: CurrencyManager,
        pricer: Pricer,
        protective: ProtectiveManager,
        reconciler: Reconciler,
        shadow: ShadowFiller,
        alert: OrderAlertHook | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._orders = orders
        self._gate = gate
        self._boundary = boundary
        self._book = book
        self._worker = worker
        self._reader = reader
        self._contracts = contracts
        self._directory = directory
        self._calendars = calendars
        self._modes = modes
        self._coordinator = coordinator
        self._currency = currency
        self._pricer = pricer
        self._protective = protective
        self._reconciler = reconciler
        self._shadow = shadow
        self._alert = alert
        self._now = now
        self._held = frozenset(settings.execution.approval.sleeves)
        self._grace = timedelta(seconds=settings.execution.reconcile.grace_seconds)
        self._interval = timedelta(minutes=settings.execution.pricing.reprice_interval_minutes)
        self._since = now()

    # ── after a restart ──

    async def resume(self) -> list[Step]:
        """Look up by its cOID every intent that may be at the broker, and follow what is found."""
        steps: list[Step] = []
        stored = await self._worker.run(repo.intents_in_states, BROKER_STATES)
        for entry in stored:
            intent = entry.intent
            if intent.mode is ExecutionMode.SHADOW:
                continue
            try:
                lookup = await self._orders.find(entry.coid)
            except ObliskError as exc:
                steps.append(Step(intent.intent_id, "unresolved", f"lookup failed: {exc}"))
                self._raise(intent.intent_id, f"the lookup by cOID failed: {exc}")
                continue
            if lookup.found:
                await self._worker.run(repo.upsert_fills, list(lookup.fills))
                await self._boundary.record_orders(list(lookup.orders))
                state = (await self._book.get(intent.intent_id)).intent.state
                steps.append(Step(intent.intent_id, "found", f"at the broker, {state.value}"))
            elif intent.state is IntentState.SUBMITTED:
                steps.append(
                    Step(intent.intent_id, "not found", "placed again after the grace period")
                )
            else:
                steps.append(Step(intent.intent_id, "unresolved", "not at the broker"))
                self._raise(
                    intent.intent_id,
                    f"the {intent.state.value} intent is not listed at the broker under its cOID",
                )
        return steps

    # ── the cycle ──

    async def cycle(self) -> CycleReport:
        report = CycleReport()
        started = self._now()
        await self._sync()
        await self._coordinator.settle()
        await self._expire(report)
        await self._follow()
        await self._work(report)
        await self._retry(report)
        await self._place(report)
        report.protective = await self._protective.verify()
        await self._protective.reprice_gaps()
        report.conversions += await self._currency.repatriate()
        report.reconcile = await self._reconciler.run()
        self._since = started
        return report

    async def _sync(self) -> None:
        try:
            live = await self._reader.live_orders()
            trades = await self._reader.trades()
        except ObliskError as exc:
            log.warning("the cycle's sync read failed", extra={"error": str(exc)})
            return
        await self._worker.run(repo.upsert_fills, list(trades))
        await self._boundary.record_orders(list(live.orders))

    # ── expiry ──

    async def _expire(self, report: CycleReport) -> None:
        now = self._now()
        before = (IntentState.PROPOSED, IntentState.APPROVED, IntentState.STAGED)
        candidates = await self._worker.run(repo.intents_in_states, (*before, *BROKER_STATES))
        for stored in candidates:
            intent = stored.intent
            if intent.expires_at > now or intent.role is IntentRole.PROTECTIVE:
                continue
            if intent.state in before or intent.mode is ExecutionMode.SHADOW:
                await self._book.advance(intent.intent_id, IntentState.EXPIRED, "expired")
                report.add(intent.intent_id, "expired", "expired before it filled")
                continue
            await self._cancel_all(stored, report, "expired")
            if (await self._book.get(intent.intent_id)).intent.state in BROKER_STATES:
                listed = await self._live_orders_of(stored)
                if not listed:
                    await self._book.advance(
                        intent.intent_id, IntentState.EXPIRED, "expired; nothing working"
                    )

    async def _live_orders_of(self, stored: StoredIntent) -> list[str]:
        orders = await self._worker.run(repo.broker_orders_for_intent, stored.intent.intent_id)
        return [order.order_id for order in orders if is_live(order)]

    async def _cancel_all(self, stored: StoredIntent, report: CycleReport, why: str) -> None:
        for order_id in await self._live_orders_of(stored):
            try:
                answer = await self._boundary.cancel(order_id)
            except ObliskError as exc:
                report.add(stored.intent.intent_id, "cancel failed", f"{why}: {exc}")
                continue
            detail = answer.message or answer.error or "sent"
            report.add(stored.intent.intent_id, "cancelled", f"{why}: {detail}")
        if stored.intent.role is IntentRole.EXIT:
            await self._coordinator.settle(stored.intent.intent_id)

    # ── fills ──

    async def _follow(self) -> None:
        """New holdings assigned to their sleeves; entries with a spec protected."""
        states = (IntentState.WORKING, IntentState.PARTIALLY_FILLED, IntentState.FILLED)
        entries = await self._worker.run(repo.intents_in_states, states)
        conids: set[int] = set()
        for stored in entries:
            intent = stored.intent
            if intent.role is not IntentRole.ENTRY or intent.mode is ExecutionMode.SHADOW:
                continue
            if intent.state is IntentState.FILLED:
                history = await self._book.history(intent.intent_id)
                if not history or history[-1].at < self._since:
                    continue  # filled before the previous cycle: followed then
            if not await self._worker.run(repo.fills_for_intent, intent.intent_id):
                continue
            await self._reconciler.register(intent.conid)
            if intent.protective is not None:
                conids.add(intent.conid)
        for conid in sorted(conids):
            await self._protective.protect(conid)

    # ── repricing ──

    async def _work(self, report: CycleReport) -> None:
        working = await self._worker.run(
            repo.intents_in_states, (IntentState.WORKING, IntentState.PARTIALLY_FILLED)
        )
        for stored in working:
            intent = stored.intent
            if (
                intent.role not in (IntentRole.ENTRY, IntentRole.EXIT)
                or intent.mode is ExecutionMode.SHADOW
                or not isinstance(intent.terms.price, LimitPrice)
            ):
                continue
            await self._work_one(stored, report)
        await self._work_shadow(report)

    async def _work_shadow(self, report: CycleReport) -> None:
        """A simulated order still unfilled is repriced toward the touch on the same clock as a
        real one, then given up and retried at the next window: nothing reaches the broker, so
        each reprice only changes the terms the next simulation fills against."""
        try:
            mode = await self._modes.current()
        except Exception:  # the mode unreadable: nothing is worked
            return
        if mode is not ExecutionMode.SHADOW:
            return
        submitted = await self._worker.run(repo.intents_in_states, [IntentState.SUBMITTED])
        for stored in submitted:
            intent = stored.intent
            price = intent.terms.price
            instrument = self._directory.describe(intent.conid)
            if (
                intent.mode is not ExecutionMode.SHADOW
                or intent.role not in (IntentRole.ENTRY, IntentRole.EXIT)
                or not isinstance(price, LimitPrice)
                or instrument is None
            ):
                continue
            now = self._now()
            if self._closing(intent.conid, instrument.listing_exchange, now):
                await self._give_up_shadow(stored, report, "its venue's window is ending")
                continue
            history = await self._book.history(intent.intent_id)
            # The clock runs from the last reprice or state change; the facts each simulation
            # records ("shadow: …") do not reset it.
            moves = [
                e
                for e in history
                if e.detail.startswith(REPRICED)
                or (e.from_state is not None and e.from_state is not e.to_state)
            ]
            done = sum(e.detail.startswith(REPRICED) for e in history)
            if now - (moves[-1].at if moves else intent.created_at) < self._interval:
                continue
            if done >= self._pricer.max_reprices:
                await self._give_up_shadow(stored, report, f"unfilled after {done} reprices")
                continue
            try:
                quote = await self._reader.quote(intent.conid, option=self._is_option(intent))
                rules = await self._contracts.rules(
                    intent.conid, is_buy=intent.side is OrderSide.BUY
                )
            except ObliskError as exc:
                report.add(intent.intent_id, "not repriced", f"cannot read: {exc}")
                continue
            if quote is None:
                report.add(intent.intent_id, "not repriced", "no quote")
                continue
            step = self._pricer.reprice(
                intent.side, price.limit, quote.evidence, sigma=quote.sigma, rules=rules, done=done
            )
            if step.price is None:
                report.add(intent.intent_id, "not repriced", step.detail)
                continue
            terms = intent.terms.model_copy(update={"price": step.price})
            await self._book.fact(intent.intent_id, f"{REPRICED}: {step.detail}", terms=terms)
            report.add(intent.intent_id, "repriced in shadow", step.detail)

    async def _give_up_shadow(self, stored: StoredIntent, report: CycleReport, why: str) -> None:
        intent_id = stored.intent.intent_id
        await self._book.fact(intent_id, f"{RETRY_NEXT_WINDOW}: {why}")
        await self._book.advance(intent_id, IntentState.CANCELLED, f"shadow: {why}")
        report.add(intent_id, "cancelled in shadow", why)

    async def _work_one(self, stored: StoredIntent, report: CycleReport) -> None:
        intent = stored.intent
        instrument = self._directory.describe(intent.conid)
        orders = await self._live_orders_of(stored)
        if instrument is None or not orders:
            return
        now = self._now()
        if self._closing(intent.conid, instrument.listing_exchange, now):
            # Given up while the window is still open, so an exit's stop is restored in it.
            await self._give_up(stored, report, "its venue's window is ending")
            return
        history = await self._book.history(intent.intent_id)
        moves = [e for e in history if e.detail.startswith(REPRICED) or e.from_state is not None]
        last = moves[-1].at if moves else intent.created_at
        done = sum(e.detail.startswith(REPRICED) for e in history)
        if now - last < self._interval:
            return
        if done >= self._pricer.max_reprices:
            await self._give_up(stored, report, f"unfilled after {done} reprices")
            return
        await self._reprice(stored, orders[-1], done, report)

    async def _reprice(
        self, stored: StoredIntent, order_id: str, done: int, report: CycleReport
    ) -> None:
        intent = stored.intent
        price = intent.terms.price
        try:
            quote = await self._reader.quote(intent.conid, option=self._is_option(intent))
            rules = await self._contracts.rules(intent.conid, is_buy=intent.side is OrderSide.BUY)
        except ObliskError as exc:
            report.add(intent.intent_id, "not repriced", f"cannot read: {exc}")
            return
        if quote is None:
            report.add(intent.intent_id, "not repriced", "no quote")
            return
        step = self._pricer.reprice(
            intent.side, price.limit, quote.evidence, sigma=quote.sigma, rules=rules, done=done
        )
        if step.price is None:
            report.add(intent.intent_id, "not repriced", step.detail)
            return
        terms = intent.terms.model_copy(update={"price": step.price})
        clearance = await self._gate.clear(
            intent, action=ClearanceAction.MODIFY, order_id=order_id, terms=terms
        )
        if isinstance(clearance, PretradeRefusal):
            report.add(intent.intent_id, "not repriced", clearance.reason)
            return
        try:
            result = await self._boundary.modify(
                intent.intent_id, order_id=order_id, terms=terms, clearance=clearance
            )
        except ObliskError as exc:
            report.add(intent.intent_id, "not repriced", str(exc))
            return
        if result.status is PlacementStatus.PLACED:
            await self._book.fact(intent.intent_id, f"{REPRICED}: {step.detail}")
            report.add(intent.intent_id, "repriced", step.detail)
        else:
            report.add(intent.intent_id, "not repriced", f"{result.status.value}: {result.detail}")

    def _closing(self, conid: int, exchange: str, now: datetime) -> bool:
        """Whether the venue's window has ended, or ends before the next reprice would."""
        try:
            venue = self._calendars.venue_for_ibkr(exchange)
            status = self._calendars.tradability(venue, now, conid=conid)
        except CalendarError:
            return True
        if status.window is None:  # not tradable now
            return True
        return now + self._interval >= status.window.end

    async def _give_up(self, stored: StoredIntent, report: CycleReport, why: str) -> None:
        """Cancel an order that did not fill, to be retried at the next tradable window."""
        await self._book.fact(stored.intent.intent_id, f"{RETRY_NEXT_WINDOW}: {why}")
        await self._cancel_all(stored, report, why)

    def _is_option(self, intent: OrderIntent) -> bool:
        instrument = self._directory.describe(intent.conid)
        return instrument is not None and instrument.asset_class is AssetClass.OPTION

    # ── retries at the next window ──

    async def _retry(self, report: CycleReport) -> None:
        now = self._now()
        cancelled = await self._worker.run(repo.intents_in_states, [IntentState.CANCELLED])
        for stored in cancelled:
            intent = stored.intent
            if intent.expires_at <= now or intent.role not in (IntentRole.ENTRY, IntentRole.EXIT):
                continue
            history = await self._book.history(intent.intent_id)
            if not any(event.detail.startswith(RETRY_NEXT_WINDOW) for event in history):
                continue
            siblings = await self._worker.run(repo.intents_for_conid, intent.conid)
            if any(s.intent.parent_id == intent.intent_id for s in siblings):
                continue
            await self._successor(stored, report)

    async def _successor(self, stored: StoredIntent, report: CycleReport) -> None:
        intent = stored.intent
        instrument = self._directory.describe(intent.conid)
        quantity = intent.terms.quantity
        if instrument is None or quantity is None:
            return
        now = self._now()
        if self._closing(intent.conid, instrument.listing_exchange, now):
            return  # waits for the next window
        try:
            venue = self._calendars.venue_for_ibkr(instrument.listing_exchange)
            quote = await self._reader.quote(intent.conid, option=self._is_option(intent))
            rules = await self._contracts.rules(intent.conid, is_buy=intent.side is OrderSide.BUY)
            algos = await self._contracts.algos(intent.conid)
        except (CalendarError, ObliskError) as exc:
            report.add(intent.intent_id, "not retried", str(exc))
            return
        fills = await self._worker.run(repo.fills_for_intent, intent.intent_id)
        remaining = quantity - sum((fill.quantity for fill in fills), ZERO)
        if quote is None or remaining <= 0:
            return
        priced = self._pricer.initial(
            instrument,
            intent.side,
            quote.evidence,
            sigma=quote.sigma,
            rules=rules,
            algos=algos,
            venue=venue,
        )
        if priced.price is None:
            report.add(intent.intent_id, "not retried", priced.detail)
            return
        successor = new_intent(
            self._settings,
            sleeve=intent.sleeve,
            role=intent.role,
            terms=intent.terms.model_copy(update={"quantity": remaining, "price": priced.price}),
            reason=intent.reason,
            mode=intent.mode,
            approval_required=intent.approval_required,
            created_at=now,
            expires_at=intent.expires_at,
            thesis_id=intent.thesis_id,
            parent_id=intent.intent_id,
            protective=intent.protective,
        )
        await self._book.add(successor)
        decision = await self._worker.run(repo.approval_for, intent.intent_id)
        if decision is not None and decision.approved:
            await self._worker.run(
                repo.record_approval,
                ApprovalDecision(
                    intent_id=successor.intent_id,
                    approved=True,
                    actor=decision.actor,
                    reason=f"carried over from {intent.intent_id}: {decision.reason}",
                    at=now,
                ),
            )
        report.add(successor.intent_id, "retried", f"follows {intent.intent_id}: {priced.detail}")

    # ── placing ──

    async def _place(self, report: CycleReport) -> None:
        await self._stage(report)
        try:
            mode = await self._modes.current()
        except Exception as exc:  # the mode unreadable: nothing is placed
            log.warning("no placement: the mode cannot be read", extra={"error": str(exc)})
            return
        staged = await self._worker.run(repo.intents_in_states, [IntentState.STAGED])
        staged = [s for s in staged if s.intent.role not in _MANAGED_ROLES]
        if mode is ExecutionMode.SHADOW:
            shadow = await self._worker.run(
                repo.intents_in_states, [IntentState.STAGED, IntentState.SUBMITTED]
            )
            for stored in shadow:
                if stored.intent.mode is ExecutionMode.SHADOW:
                    await self._simulate(stored, report)
            return
        live = [s for s in staged if s.intent.mode is not ExecutionMode.SHADOW]
        funding = await self._currency.fund([s.intent for s in live])
        report.conversions += funding.conversions
        for stored in live:
            intent_id = stored.intent.intent_id
            if intent_id not in funding.ready:
                report.add(intent_id, "waiting", funding.waiting.get(intent_id, "not funded"))
                continue
            await self._place_one(stored, report)
        await self._retry_unknown(report)

    async def _stage(self, report: CycleReport) -> None:
        latest = await self._worker.run(repo.latest_reconciliation)
        frozen = latest is not None and latest.status is not ReconcileStatus.GREEN
        proposed = await self._worker.run(repo.intents_in_states, [IntentState.PROPOSED])
        for stored in proposed:
            intent = stored.intent
            if intent.role in _MANAGED_ROLES:
                continue
            if intent.approval_required or intent.sleeve in self._held:
                continue  # the approval queue moves it
            await self._book.advance(intent.intent_id, IntentState.APPROVED, "needs no approval")
        approved = await self._worker.run(repo.intents_in_states, [IntentState.APPROVED])
        for stored in approved:
            intent = stored.intent
            if intent.role in _MANAGED_ROLES:
                continue
            if frozen and adds_risk(intent):
                report.add(intent.intent_id, "frozen", "reconciliation freezes new risk")
                continue
            await self._book.advance(intent.intent_id, IntentState.STAGED, "staged")

    async def _place_one(self, stored: StoredIntent, report: CycleReport) -> None:
        intent = stored.intent
        if intent.role is IntentRole.EXIT:
            result = await self._coordinator.place_exit(intent.intent_id)
            if result.refusal is not None:
                await self._refused(stored, result.refusal, report)
            else:
                report.add(
                    intent.intent_id, "placed" if result.placed else "not placed", result.detail
                )
            return
        clearance = await self._gate.clear(intent)
        if isinstance(clearance, PretradeRefusal):
            await self._refused(stored, clearance, report)
            return
        try:
            placement = await self._boundary.place(intent.intent_id, clearance)
        except ObliskError as exc:
            report.add(intent.intent_id, "not placed", str(exc))
            return
        report.add(intent.intent_id, placement.status.value, placement.detail or "sent")

    async def _refused(
        self, stored: StoredIntent, refusal: PretradeRefusal, report: CycleReport
    ) -> None:
        intent = stored.intent
        permanent = any(Reason(code) in PERMANENT_REFUSALS for code in refusal.codes)
        current = (await self._book.get(intent.intent_id)).intent.state
        if permanent and current is IntentState.STAGED:
            await self._book.advance(intent.intent_id, IntentState.REJECTED, refusal.reason)
            report.add(intent.intent_id, "rejected", refusal.reason)
        else:
            report.add(intent.intent_id, "waiting", refusal.reason)

    async def _retry_unknown(self, report: CycleReport) -> None:
        """A submitted intent the broker does not list, past the grace period: placed again with a
        fresh clearance, its cOID looked up first (the order client does it)."""
        now = self._now()
        submitted = await self._worker.run(repo.intents_in_states, [IntentState.SUBMITTED])
        for stored in submitted:
            intent = stored.intent
            if intent.mode is ExecutionMode.SHADOW or intent.role in _MANAGED_ROLES:
                continue
            history = await self._book.history(intent.intent_id)
            if history and now - history[-1].at < self._grace:
                continue
            await self._place_one(stored, report)

    # ── shadow ──

    async def _simulate(self, stored: StoredIntent, report: CycleReport) -> None:
        intent = stored.intent
        refusal = await self._gate.check(intent, assume={}, simulated=True)
        if refusal is not None:
            await self._refused(stored, refusal, report)
            return
        instrument = self._directory.describe(intent.conid)
        try:
            quote = await self._reader.quote(intent.conid, option=self._is_option(intent))
            venue = None if instrument is None else cost_venue(instrument, self._calendars)
        except (CalendarError, ObliskError) as exc:
            report.add(intent.intent_id, "not simulated", str(exc))
            return
        if instrument is None or quote is None or venue is None:
            report.add(intent.intent_id, "not simulated", "no instrument or quote")
            return
        outcome = self._shadow.simulate(
            intent.terms,
            quote.evidence,
            coid=stored.coid,
            asset_class=instrument.asset_class,
            venue=venue,
            currency=instrument.currency,
            at=self._now(),
            multiplier=instrument.multiplier,
        )
        if intent.state is IntentState.STAGED:
            await self._book.advance(intent.intent_id, IntentState.SUBMITTED, "simulated in shadow")
        if outcome.fill is None:
            await self._book.fact(intent.intent_id, f"shadow: {outcome.reason}")
            report.add(intent.intent_id, "shadow, unfilled", outcome.reason)
            return
        await self._worker.run(repo.upsert_fills, [outcome.fill])
        await self._book.advance(intent.intent_id, IntentState.FILLED, outcome.reason)
        report.add(
            intent.intent_id, "shadow fill", f"{outcome.fill.quantity} at {outcome.fill.price}"
        )

    # ── alerts ──

    def _raise(self, intent_id: UUID | None, detail: str) -> None:
        raise_alert(
            self._alert,
            OrderAlert(
                kind=OrderAlertKind.RESUME_UNRESOLVED,
                intent_id=intent_id,
                detail=detail,
                at=self._now(),
            ),
        )
