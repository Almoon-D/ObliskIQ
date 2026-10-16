"""No exit leaves a position unprotected (I5, SESSION_PROMPTS Session 4 item 4).

An exit beside a resting protective stop would oversell the position (I2), so the stop is cut
first: by the exit's size (a partial exit), or cancelled (an exit of all of it, ``flatten``). In
this order:

1. **The exit's other checks first.** :meth:`PretradeGate.check` runs every check of the exit
   with the stops counted at their cut size, before any stop is touched; a refusal there changes
   nothing.
2. **Cut the stops.** A cut is a modify the gate clears (a sell within the position reduces
   risk, so it passes with the kill switch on) or a cancel, which needs no clearance. Each cut is
   recorded in ``stop_resizes`` with the stop's terms before it.
3. **Clear and place the exit**, with the stops at their new size.
4. **Restore.** When the exit is refused, cancelled or expires — at once, or later through
   :meth:`ExitCoordinator.settle` — the stop is brought back to cover what the exit did not
   sell: modified back up, or, when it was cancelled, placed again as a new protective intent.
   Either write passes the gate again. A restore that fails stays ``restore_failed``, alerts the
   owner and is tried again at the next settle.

A downgrade can come between a cut and its restore (downgrades are automatic and never wait),
and the write boundary then refuses the restore: the owner is alerted at once and the restore
waits until a mode can write again. Nothing here runs the exit's executor: Session 5 reprices and
expires exits and calls :meth:`settle` each cycle.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from ..config.schema import ObliskConfig
from ..domain.orders import (
    BrokerOrder,
    ClearanceAction,
    IntentRole,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    StopResize,
    StopResizeState,
)
from ..errors import ExecutionError, ObliskError
from ..ibkr.orders import PlacementResult, PlacementStatus, is_live
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store.orders import StoredIntent
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .boundary import OrderAlert, OrderAlertHook, OrderAlertKind, WriteBoundary
from .intents import new_intent
from .pretrade import PretradeGate, PretradeRefusal
from .state import BROKER_STATES, IntentBook

log = get_logger("execution.exits")

ZERO = Decimal(0)

#: Resizes that still owe a restore, or may.
OPEN_RESIZES = (StopResizeState.RESIZED, StopResizeState.RESTORE_FAILED)

#: Exit states after which the exit can no longer sell: what it did not fill is restored.
_ENDED_UNFILLED = frozenset({IntentState.CANCELLED, IntentState.REJECTED, IntentState.EXPIRED})


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """What one settle did with one cut stop."""

    resize_id: int
    state: StopResizeState
    detail: str


@dataclass(frozen=True, slots=True)
class ExitResult:
    """How an exit went: placed, or refused (``refusal``) with what was done to its stops."""

    exit_intent_id: UUID
    placement: PlacementResult | None
    refusal: PretradeRefusal | None
    resized: tuple[StopResize, ...]
    restored: tuple[RestoreOutcome, ...]
    detail: str

    @property
    def placed(self) -> bool:
        return self.placement is not None and self.placement.status in (
            PlacementStatus.PLACED,
            PlacementStatus.ALREADY_PLACED,
        )


@dataclass(frozen=True, slots=True)
class _Stop:
    stored: StoredIntent
    order: BrokerOrder

    @property
    def remaining(self) -> Decimal:
        order = self.order
        if order.remaining_quantity is not None:
            return order.remaining_quantity
        total = order.total_quantity if order.total_quantity is not None else ZERO
        return total - (order.filled_quantity or ZERO)


class ExitCoordinator:
    """Places exits beside their protective stops, and restores the stops they cut."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        gate: PretradeGate,
        boundary: WriteBoundary,
        book: IntentBook,
        worker: StoreWorker,
        alert: OrderAlertHook | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._gate = gate
        self._boundary = boundary
        self._book = book
        self._worker = worker
        self._alert = alert
        self._now = now

    # ── placing an exit ──

    async def _stops(self, conid: int) -> list[_Stop]:
        """The working protective stops of the instrument, read fresh from the broker."""
        stored = await self._worker.run(repo.intents_in_states, BROKER_STATES)
        stops: list[_Stop] = []
        for entry in stored:
            intent = entry.intent
            if (
                intent.conid != conid
                or intent.role is not IntentRole.PROTECTIVE
                or intent.side is not OrderSide.SELL
            ):
                continue
            orders = await self._worker.run(repo.broker_orders_for_intent, intent.intent_id)
            for order in orders:
                fresh = await self._boundary.sync_order(order.order_id)
                if is_live(fresh):
                    stops.append(_Stop(stored=entry, order=fresh))
        return sorted(stops, key=lambda stop: stop.remaining, reverse=True)

    async def place_exit(self, exit_intent_id: UUID) -> ExitResult:
        """Place a staged exit, cutting the protective stops it would oversell beside."""
        exit_stored = await self._book.get(exit_intent_id)
        intent = exit_stored.intent
        if intent.side is not OrderSide.SELL or intent.terms.conversion:
            raise ExecutionError("an exit is a sell of a position")
        quantity = intent.terms.quantity if intent.terms.quantity is not None else ZERO
        stops = await self._stops(intent.conid)
        cuts: dict[str, Decimal] = {}
        absorb = quantity
        for stop in stops:
            if absorb <= 0:
                break
            cut = min(absorb, stop.remaining)
            cuts[stop.order.order_id] = stop.remaining - cut
            absorb -= cut
        if cuts:
            refusal = await self._gate.check(intent, assume=cuts)
            if refusal is not None:
                return ExitResult(intent.intent_id, None, refusal, (), (), "refused; no stop cut")
        resized: list[StopResize] = []
        for stop in stops:
            target = cuts.get(stop.order.order_id)
            if target is None:
                continue
            done = await self._cut(intent, stop, target)
            if isinstance(done, str):
                restored = await self.settle(exit_intent_id, ended=True)
                return ExitResult(
                    intent.intent_id, None, None, tuple(resized), tuple(restored), done
                )
            resized.append(done)
        clearance = await self._gate.clear(intent)
        if isinstance(clearance, PretradeRefusal):
            restored = await self.settle(exit_intent_id, ended=True)
            return ExitResult(
                intent.intent_id, None, clearance, tuple(resized), tuple(restored), "refused"
            )
        placement = await self._place(exit_intent_id, clearance)
        # Refused before it was sent, the exit will not go: its cuts are restored now.
        restored = await self.settle(exit_intent_id, ended=placement is None)
        detail = "refused before sending" if placement is None else placement.status.value
        return ExitResult(
            intent.intent_id, placement, None, tuple(resized), tuple(restored), detail
        )

    async def _place(
        self, exit_intent_id: UUID, clearance: PretradeClearance
    ) -> PlacementResult | None:
        try:
            return await self._boundary.place(exit_intent_id, clearance)
        except ObliskError as exc:  # refused before sending, or the broker could not be read
            log.warning(
                "the exit was not placed", extra={"intent": str(exit_intent_id), "error": str(exc)}
            )
            return None

    async def _cut(
        self, exit_intent: OrderIntent, stop: _Stop, target: Decimal
    ) -> StopResize | str:
        """Cut one stop to ``target`` remaining (cancel at zero); the cut, or why it failed."""
        order = stop.order
        stop_intent = stop.stored.intent
        filled = order.filled_quantity or ZERO
        if target <= 0:
            answer = await self._boundary.cancel(order.order_id)
            if answer.error is not None:
                return f"the stop {order.order_id} could not be cancelled: {answer.error}"
        else:
            terms = stop_intent.terms.model_copy(update={"quantity": filled + target})
            failure = await self._modify(stop_intent, order.order_id, terms)
            if failure is not None:
                return f"the stop {order.order_id} could not be cut: {failure}"
        now = self._now()
        resize = StopResize(
            exit_intent_id=exit_intent.intent_id,
            stop_intent_id=stop_intent.intent_id,
            stop_order_id=order.order_id,
            original_terms=stop_intent.terms,
            resized_quantity=filled + target if target > 0 else ZERO,
            state=StopResizeState.RESIZED,
            created_at=now,
            updated_at=now,
            detail=f"cut for exit {exit_intent.intent_id}",
        )
        resize_id = await self._worker.run(repo.insert_stop_resize, resize)
        return resize.model_copy(update={"resize_id": resize_id})

    async def _modify(self, intent: OrderIntent, order_id: str, terms: OrderTerms) -> str | None:
        """Clear and send one modify of a stop; ``None`` when the broker took it."""
        clearance = await self._gate.clear(
            intent, action=ClearanceAction.MODIFY, order_id=order_id, terms=terms
        )
        if isinstance(clearance, PretradeRefusal):
            return clearance.reason
        try:
            result = await self._boundary.modify(
                intent.intent_id, order_id=order_id, terms=terms, clearance=clearance
            )
        except ObliskError as exc:
            return str(exc)
        if result.status is not PlacementStatus.PLACED:
            return f"{result.status.value}: {result.detail}"
        return None

    # ── restoring ──

    async def settle(
        self, exit_intent_id: UUID | None = None, *, ended: bool = False
    ) -> list[RestoreOutcome]:
        """Restore every stop cut for an exit that ended without selling (``exit_intent_id``:
        that exit's alone; ``ended``: the caller knows it will not be sent)."""
        if exit_intent_id is None:
            open_resizes = await self._worker.run(repo.stop_resizes_in_states, OPEN_RESIZES)
        else:
            mine = await self._worker.run(repo.stop_resizes_for_exit, exit_intent_id)
            open_resizes = [r for r in mine if r.state in OPEN_RESIZES]
        outcomes = []
        for resize in open_resizes:
            outcome = await self._settle_one(resize, ended=ended)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    async def _exit_state(self, resize: StopResize, *, ended: bool) -> tuple[IntentState, str]:
        exit_stored = await self._book.get(resize.exit_intent_id)
        state = exit_stored.intent.state
        if state in BROKER_STATES:
            orders = await self._worker.run(repo.broker_orders_for_intent, resize.exit_intent_id)
            for order in orders:
                await self._boundary.sync_order(order.order_id)
            state = (await self._book.get(resize.exit_intent_id)).intent.state
        if ended and state in (IntentState.STAGED, IntentState.APPROVED, IntentState.PROPOSED):
            # The exit will not be sent: it ends here, so nothing stays cut for it.
            await self._book.advance(resize.exit_intent_id, IntentState.CANCELLED, "not sent")
            state = IntentState.CANCELLED
        return state, exit_stored.coid

    async def _settle_one(self, resize: StopResize, *, ended: bool) -> RestoreOutcome | None:
        resize_id = resize.resize_id
        if resize_id is None:  # pragma: no cover - read from the store
            raise ExecutionError("a stop resize without an id")
        state, coid = await self._exit_state(resize, ended=ended)
        if state is IntentState.FILLED:
            return await self._mark(resize_id, StopResizeState.CLOSED, "the exit filled")
        if state not in _ENDED_UNFILLED:
            return None  # still working, or its outcome is not known yet
        sold = await self._sold(resize.exit_intent_id, coid)
        original = resize.original_terms.quantity or ZERO
        wanted = original - sold
        if wanted <= 0:
            return await self._mark(resize_id, StopResizeState.CLOSED, "nothing left to protect")
        failure = await self._restore(resize, wanted)
        if failure is None:
            return await self._mark(
                resize_id, StopResizeState.RESTORED, f"restored to {wanted} after the exit ended"
            )
        detail = f"the stop could not be restored to {wanted}: {failure}"
        self._alert_restore(resize, detail)
        return await self._mark(resize_id, StopResizeState.RESTORE_FAILED, detail)

    async def _sold(self, exit_intent_id: UUID, coid: str) -> Decimal:
        """What the exit sold: the larger of its recorded fills and its orders' last report."""
        fills = await self._worker.run(repo.fills_by_coid, [coid])
        recorded = sum((fill.quantity for fill in fills.get(coid, [])), ZERO)
        orders = await self._worker.run(repo.broker_orders_for_intent, exit_intent_id)
        reported = sum((order.filled_quantity or ZERO for order in orders), ZERO)
        return max(recorded, reported)

    async def _restore(self, resize: StopResize, wanted: Decimal) -> str | None:
        """Bring the stop back to ``wanted`` units in total; ``None`` on success."""
        stop_stored = await self._book.get(resize.stop_intent_id)
        stop = stop_stored.intent
        terms = resize.original_terms.model_copy(update={"quantity": wanted})
        if resize.resized_quantity > 0 and stop.state in BROKER_STATES:
            order = await self._boundary.sync_order(resize.stop_order_id)
            if is_live(order):
                return await self._modify(stop, resize.stop_order_id, terms)
        return await self._replace(stop, terms)

    async def _replace(self, old: OrderIntent, terms: OrderTerms) -> str | None:
        """Place the stop again as a new protective intent, for the remaining position."""
        now = self._now()
        if old.expires_at <= now:
            return "the stop's intent has expired; the protective-exit manager re-places it"
        filled = await self._worker.run(repo.fills_for_intent, old.intent_id)
        stop_filled = sum((fill.quantity for fill in filled), ZERO)
        quantity = (terms.quantity or ZERO) - stop_filled
        if quantity <= 0:
            return None
        replacement = new_intent(
            self._settings,
            sleeve=old.sleeve,
            role=IntentRole.PROTECTIVE,
            terms=terms.model_copy(update={"quantity": quantity}),
            reason=old.reason,
            mode=old.mode,
            approval_required=False,
            created_at=now,
            expires_at=old.expires_at,
            thesis_id=old.thesis_id,
            parent_id=old.parent_id,
        )
        await self._book.add(replacement)
        for step in (IntentState.APPROVED, IntentState.STAGED):
            await self._book.advance(replacement.intent_id, step, f"restores stop {old.intent_id}")
        clearance = await self._gate.clear(replacement)
        if isinstance(clearance, PretradeRefusal):
            await self._book.advance(replacement.intent_id, IntentState.REJECTED, clearance.reason)
            return clearance.reason
        placement = await self._place(replacement.intent_id, clearance)
        if placement is None or placement.status is not PlacementStatus.PLACED:
            status = "not sent" if placement is None else placement.status.value
            return f"the replacement stop was {status}"
        return None

    async def _mark(self, resize_id: int, state: StopResizeState, detail: str) -> RestoreOutcome:
        await self._worker.run(
            repo.record_stop_resize_state, resize_id, state=state, at=self._now(), detail=detail
        )
        return RestoreOutcome(resize_id=resize_id, state=state, detail=detail)

    def _alert_restore(self, resize: StopResize, detail: str) -> None:
        alert = OrderAlert(
            kind=OrderAlertKind.RESTORE_FAILED,
            intent_id=resize.stop_intent_id,
            detail=detail,
            at=self._now(),
        )
        log.warning("order alert", extra={"kind": alert.kind.value, "intent": str(alert.intent_id)})
        if self._alert is None:
            return
        try:
            self._alert(alert)
        except Exception:
            log.exception("the order alert hook failed")
