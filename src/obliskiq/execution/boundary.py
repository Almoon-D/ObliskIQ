"""The broker-write boundary: the one place an intent becomes a broker write.

Before a placement or a modify, and again immediately before it is sent (the order client's last
check, after the cOID lookup):

1. **Mode.** The current mode, read from the store, must be the intent's and permit writes
   under this profile (``shadow`` never does; ``paper`` needs profile ``paper``; the live levels
   none until the promotion gate exists), and the account guard must run under the same
   profile. A mode that cannot be read refuses. An expired intent is never sent or confirmed,
   and a modify must name an order placed for its intent.
2. **Paper guard.** Under profile ``paper`` the account must carry a paper prefix.
3. **Kill switch**, for an order that adds risk (I3, I4): ON refuses it. Every order adds risk
   except a sell the gate cleared as reducing it (the clearance model refuses that effect on a
   buy or a conversion), so with the switch ON a reducing sell still passes.
4. **Clearance (I1).** Spent by the order client in the store, bound to this intent, action,
   broker order and terms, before anything is sent.

``cancel`` needs no clearance and is allowed in every mode, with the kill switch on or off: only
the paper guard applies. Every outcome is written to the intent's history; a declined warning, an
unknown outcome and a refused cOID alert the owner. Refusals carry a reason code
(``WriteRefusedError.code``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from ..domain.orders import (
    BrokerOrder,
    ExecutionMode,
    IntentState,
    OrderTerms,
    PretradeClearance,
    RiskEffect,
)
from ..errors import WriteRefusedError
from ..ibkr.errors import AccountGuardError, IbkrError
from ..ibkr.guard import AccountGuard
from ..ibkr.order_decode import CancelAnswer
from ..ibkr.orders import (
    CANCELLED_STATUS,
    FILLED_STATUS,
    OrderClient,
    PlacementResult,
    PlacementStatus,
)
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store.orders import StoredIntent
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .killswitch import KillSwitch
from .modes import Modes, write_refusal
from .replies import ReplyPolicy
from .state import BROKER_STATES, IntentBook, can_transition

log = get_logger("execution.boundary")

#: The status recorded for an order the broker took without saying its status.
UNREPORTED_STATUS = "unreported"


class OrderAlertKind(StrEnum):
    WARNING_DECLINED = "warning_declined"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DUPLICATE_COID = "duplicate_coid"
    #: An option close the gate refused for want of a real-time quote (it may be exercised).
    OPTION_CLOSE_BLOCKED = "option_close_blocked"
    #: A protective stop cut for an exit could not be restored (I5).
    RESTORE_FAILED = "restore_failed"


@dataclass(frozen=True, slots=True)
class OrderAlert:
    """Something the owner must hear about an order."""

    kind: OrderAlertKind
    intent_id: UUID
    detail: str
    at: datetime


type OrderAlertHook = Callable[[OrderAlert], None]


def state_for(order: BrokerOrder) -> IntentState:
    """The intent state a broker report stands for. Anything not final is still working."""
    status = order.status.strip().lower()
    if status == FILLED_STATUS:
        return IntentState.FILLED
    if status == CANCELLED_STATUS:
        return IntentState.CANCELLED
    if (order.filled_quantity or Decimal(0)) > 0:
        return IntentState.PARTIALLY_FILLED
    return IntentState.WORKING


def adds_risk(clearance: PretradeClearance) -> bool:
    """Whether the cleared order adds risk: all but a sell the gate cleared as reducing it."""
    return clearance.effect is not RiskEffect.REDUCES


class WriteBoundary:
    """Places, modifies and cancels orders for stored intents, and records what happened."""

    def __init__(
        self,
        *,
        client: OrderClient,
        book: IntentBook,
        worker: StoreWorker,
        modes: Modes,
        kill_switch: KillSwitch,
        guard: AccountGuard,
        replies: ReplyPolicy,
        alert: OrderAlertHook | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = client
        self._book = book
        self._worker = worker
        self._modes = modes
        self._kill = kill_switch
        self._guard = guard
        self._replies = replies
        self._alert = alert
        self._now = now

    # ── the checks ──

    def _paper_guard(self) -> None:
        """Profile paper ⇒ a paper-prefix account: the one check a cancel needs."""
        try:
            self._guard.check_prefix()
        except AccountGuardError as exc:
            raise WriteRefusedError("paper_guard", str(exc)) from None

    async def check_write(
        self, *, adds_risk: bool, intent_mode: ExecutionMode, expires_at: datetime
    ) -> None:
        """Expiry, mode (the intent's, permitted under this profile), paper guard and, for an
        order that adds risk, the kill switch."""
        if self._now() >= expires_at:
            raise WriteRefusedError("intent_expired", "the intent has expired")
        try:
            mode = await self._modes.current()
        except Exception as exc:
            raise WriteRefusedError(
                "mode_unreadable", f"the current mode cannot be read ({type(exc).__name__})"
            ) from None
        refusal = write_refusal(mode, self._modes.profile)
        if refusal is not None:
            raise WriteRefusedError("mode_forbids_write", refusal)
        if mode is not intent_mode:
            raise WriteRefusedError(
                "intent_mode",
                f"the intent was made for {intent_mode.value}; the mode is {mode.value}",
            )
        if self._modes.profile != self._guard.profile:
            raise WriteRefusedError(
                "profile_mismatch",
                f"the modes run under profile {self._modes.profile}, the account guard under "
                f"{self._guard.profile}",
            )
        self._paper_guard()
        if adds_risk and await self._kill.is_on():
            raise WriteRefusedError("kill_switch_on", "the kill switch blocks orders that add risk")

    # ── writes ──

    async def place(self, intent_id: UUID, clearance: PretradeClearance) -> PlacementResult:
        """Place a staged intent (or retry a submitted one, after its cOID is looked up)."""
        stored = await self._book.get(intent_id)
        intent = stored.intent
        if clearance.intent_id != intent_id:
            raise WriteRefusedError("clearance_mismatch", "the clearance is for another intent")
        if intent.state not in (IntentState.STAGED, IntentState.SUBMITTED):
            raise WriteRefusedError(
                "intent_state", f"an intent in state {intent.state.value} is not placed"
            )
        first_attempt = intent.state is IntentState.STAGED
        if self._now() >= intent.expires_at:
            if first_attempt:
                await self._book.advance(intent_id, IntentState.EXPIRED, "expired before sending")
            raise WriteRefusedError("intent_expired", "the intent expired before it was sent")
        risky = adds_risk(clearance)
        mode, expires_at = intent.mode, intent.expires_at
        await self.check_write(adds_risk=risky, intent_mode=mode, expires_at=expires_at)
        if first_attempt:
            await self._book.advance(intent_id, IntentState.SUBMITTED, "sending to the broker")
        try:
            result = await self._client.place_idempotent(
                coid=stored.coid,
                terms=intent.terms,
                clearance=clearance,
                decide=self._replies.decider(clearance, self._now),
                last_check=lambda: self.check_write(
                    adds_risk=risky, intent_mode=mode, expires_at=expires_at
                ),
            )
        except WriteRefusedError as exc:
            if first_attempt:
                state = (
                    IntentState.EXPIRED if exc.code == "intent_expired" else IntentState.REJECTED
                )
                await self._book.advance(intent_id, state, f"refused: {exc}")
            else:
                await self._book.fact(intent_id, f"retry refused before sending: {exc}")
            raise
        except IbkrError as exc:
            await self._book.fact(
                intent_id, f"not sent: the broker could not be read ({type(exc).__name__})"
            )
            raise
        await self._settle_placement(stored, result)
        return result

    async def modify(
        self,
        intent_id: UUID,
        *,
        order_id: str,
        terms: OrderTerms,
        clearance: PretradeClearance,
    ) -> PlacementResult:
        """Change a working order to ``terms``: same instrument, side and kind of sizing."""
        stored = await self._book.get(intent_id)
        intent = stored.intent
        if clearance.intent_id != intent_id:
            raise WriteRefusedError("clearance_mismatch", "the clearance is for another intent")
        if intent.state not in BROKER_STATES:
            raise WriteRefusedError(
                "intent_state", f"an intent in state {intent.state.value} has no order to modify"
            )
        if (terms.conid, terms.side, terms.conversion, terms.quantity is None) != (
            intent.conid,
            intent.side,
            intent.terms.conversion,
            intent.terms.quantity is None,
        ):
            raise WriteRefusedError(
                "modify_changes_order", "a modify keeps the instrument, the side and the kind"
            )
        if await self._worker.run(repo.intent_for_order, order_id) != intent_id:
            raise WriteRefusedError(
                "order_not_intents", f"order {order_id} was not placed for this intent"
            )
        risky = adds_risk(clearance)
        mode, expires_at = intent.mode, intent.expires_at
        await self.check_write(adds_risk=risky, intent_mode=mode, expires_at=expires_at)
        try:
            result = await self._client.modify(
                order_id=order_id,
                coid=stored.coid,
                terms=terms,
                clearance=clearance,
                decide=self._replies.decider(clearance, self._now),
                last_check=lambda: self.check_write(
                    adds_risk=risky, intent_mode=mode, expires_at=expires_at
                ),
            )
        except WriteRefusedError as exc:
            await self._book.fact(intent_id, f"modify refused: {exc}")
            raise
        await self._record_replies(intent_id, result)
        if result.status is PlacementStatus.PLACED:
            await self._book.fact(intent_id, f"modified order {order_id}", terms=terms)
        else:
            await self._book.fact(
                intent_id, f"modify not applied ({result.status.value}): {result.detail}"
            )
            self._alert_for(intent_id, result)
        return result

    async def cancel(self, order_id: str) -> CancelAnswer:
        """Cancel one order: no clearance, any mode, the kill switch on or off; the paper guard
        alone applies. The order is then read back, so a cancel that lost a race to a fill
        records the fill."""
        self._paper_guard()
        answer = await self._client.cancel(order_id)
        try:
            await self.sync_order(order_id)
        except Exception as exc:  # the cancel was sent; a failed read-back must not hide it
            log.warning(
                "the order could not be read back after a cancel",
                extra={"order_id": order_id, "error": type(exc).__name__},
            )
        return answer

    async def sync_order(self, order_id: str) -> BrokerOrder:
        """Read one order's status from the broker and bring its intent up to date."""
        order = await self._client.status(order_id)
        await self._worker.run(repo.upsert_broker_orders, [order])
        intent_id = await self._worker.run(repo.intent_for_order, order_id)
        if intent_id is not None:
            await self._move(intent_id, state_for(order), f"broker status {order.status}")
        return order

    # ── recording ──

    async def _move(self, intent_id: UUID, target: IntentState, detail: str) -> None:
        """Advance the intent to ``target`` when that is a legal move; a stale or repeated
        report changes nothing."""
        current = (await self._book.get(intent_id)).intent.state
        if current is target:
            return
        if not can_transition(current, target):
            log.warning(
                "a broker report the intent cannot follow",
                extra={"intent": str(intent_id), "from": current.value, "to": target.value},
            )
            return
        await self._book.advance(intent_id, target, detail)

    async def _record_replies(self, intent_id: UUID, result: PlacementResult) -> None:
        for reply in result.replies:
            verdict = "confirmed" if reply.confirmed else "declined"
            ids = ", ".join(reply.message_ids) or "no message id"
            await self._book.fact(intent_id, f"warning ({ids}) {verdict}: {reply.reason}")

    def _alert_for(self, intent_id: UUID, result: PlacementResult) -> None:
        kinds = {
            PlacementStatus.DECLINED: OrderAlertKind.WARNING_DECLINED,
            PlacementStatus.UNKNOWN: OrderAlertKind.OUTCOME_UNKNOWN,
            PlacementStatus.DUPLICATE: OrderAlertKind.DUPLICATE_COID,
        }
        kind = kinds.get(result.status)
        if kind is None:
            return
        alert = OrderAlert(kind=kind, intent_id=intent_id, detail=result.detail, at=self._now())
        log.warning("order alert", extra={"kind": kind.value, "intent": str(intent_id)})
        if self._alert is None:
            return
        try:
            self._alert(alert)
        except Exception:
            log.exception("the order alert hook failed")

    async def _settle_placement(self, stored: StoredIntent, result: PlacementResult) -> None:
        intent = stored.intent
        intent_id = intent.intent_id
        await self._record_replies(intent_id, result)
        status = result.status
        if status is PlacementStatus.PLACED and result.order_id is not None:
            order = BrokerOrder(
                order_id=result.order_id,
                coid=stored.coid,
                conid=intent.conid,
                side=intent.side,
                sec_type=None,
                order_type=None,
                status=result.order_status or UNREPORTED_STATUS,
                total_quantity=intent.terms.quantity,
                filled_quantity=None,
                remaining_quantity=None,
                average_price=None,
                tif=intent.terms.tif.value,
                observed_at=self._now(),
            )
            await self._worker.run(repo.upsert_broker_orders, [order])
            await self._move(intent_id, state_for(order), f"placed as order {order.order_id}")
            return
        if status is PlacementStatus.ALREADY_PLACED and result.lookup is not None:
            lookup = result.lookup
            await self._worker.run(repo.upsert_broker_orders, list(lookup.orders))
            await self._worker.run(repo.upsert_fills, list(lookup.fills))
            target = self._state_from_lookup(intent.terms, result)
            await self._move(intent_id, target, "found at the broker under its cOID")
            return
        if status is PlacementStatus.REJECTED or (
            status is PlacementStatus.DECLINED and result.cleanup_verified
        ):
            await self._book.advance(
                intent_id, IntentState.REJECTED, f"{status.value}: {result.detail}"
            )
        else:
            # Duplicate, not sent, unknown, or declined with an order perhaps still listed: the
            # intent stays submitted, its cOID to be looked up.
            await self._book.fact(intent_id, f"{status.value}: {result.detail}")
        self._alert_for(intent_id, result)

    @staticmethod
    def _state_from_lookup(terms: OrderTerms, result: PlacementResult) -> IntentState:
        lookup = result.lookup
        if lookup is not None and lookup.orders:
            return state_for(lookup.orders[0])
        filled = sum((fill.quantity for fill in lookup.fills), Decimal(0)) if lookup else Decimal(0)
        if terms.quantity is not None and filled >= terms.quantity:
            return IntentState.FILLED
        return IntentState.PARTIALLY_FILLED
