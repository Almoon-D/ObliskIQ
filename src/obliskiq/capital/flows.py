"""The owner's cash-flow ledger: deposits, dated withdrawals and their reservations, and the
classification of a broker cash change nothing explains (MASTER_PLAN §5.15).

* **A deposit** ("I added 1,237.57 € to your cash") raises managed capital — in mode ``cap`` at
  once, in the book; in mode ``whole_account`` through the NAV once the money lands — and is
  deployed toward the targets, optionally in tranches (``capital/deploy.py``).
* **A reservation** ("in 15 days I need 1,000 €") makes investable capital drop at once; the
  reservation planner raises the cash before the due date.
* **A withdrawal** closes a reservation when the owner takes the money.
* **A classification** explains a pending cash event (a dividend, interest, a fee, a deposit the
  owner did not announce): reconciliation then counts it and lifts the freeze on new buys.

Records are kept, never deleted: a flow the broker has not shown may be cancelled, an open
reservation cancelled, each audited. Ids are shown with a letter: ``R`` a reservation, ``F`` a
flow, ``E`` a cash event.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from ..domain.capital import (
    Adjustment,
    AdjustmentKind,
    CashEvent,
    CashEventState,
    CashFlow,
    Deposit,
    ReservationState,
    Withdrawal,
    WithdrawalReservation,
)
from ..errors import ObliskError, StoreError
from ..store import capital as repo
from ..store.db import Store
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

ZERO = Decimal(0)

type RecordKind = Literal["reservation", "flow", "event"]
type Classification = AdjustmentKind | Literal["deposit", "withdrawal"]

#: The letter before each kind of record's number.
PREFIXES: dict[str, RecordKind] = {"R": "reservation", "F": "flow", "E": "event"}


class LedgerRefusal(ObliskError):
    """An owner's ledger command that cannot be carried out, and why."""


def parse_record_id(text: str) -> tuple[RecordKind, int]:
    """``R3`` → (reservation, 3); a bare number is refused, since the kinds share numbers."""
    cleaned = text.strip().upper()
    kind = PREFIXES.get(cleaned[:1])
    number = cleaned[1:]
    if kind is None or not number.isdigit() or int(number) < 1:
        raise LedgerRefusal(
            f"{text!r} is not a record id: write R<number> for a reservation, F<number> for a "
            "flow or E<number> for a cash event, as `obliskiq cash list` shows them"
        )
    return kind, int(number)


def record_label(kind: RecordKind, number: int | None) -> str:
    letter = next(key for key, value in PREFIXES.items() if value == kind)
    return f"{letter}{number}"


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """What `obliskiq cash list` shows."""

    reservations: tuple[WithdrawalReservation, ...]
    flows: tuple[CashFlow, ...]
    events: tuple[CashEvent, ...]


def _positive(amount: Decimal, what: str) -> Decimal:
    if not amount.is_finite() or amount <= ZERO:
        raise LedgerRefusal(f"{what} must be a positive amount, got {amount}")
    return amount


class CashLedger:
    """Records the owner's flows and reservations, each checked before it is written."""

    def __init__(
        self, worker: StoreWorker, *, base_currency: str, now: Callable[[], datetime] = utc_now
    ) -> None:
        self._worker = worker
        self._base = base_currency
        self._now = now

    # ── building records (nothing is written) ──

    def deposit(
        self,
        amount: Decimal,
        *,
        currency: str | None,
        on: date | None,
        actor: str,
        note: str | None,
    ) -> Deposit:
        at = self._now()
        day = at.date() if on is None else on
        if day > at.date():
            raise LedgerRefusal("a deposit is recorded once it is made, not before")
        try:
            return Deposit(
                amount=_positive(amount, "a deposit"),
                currency=currency or self._base,
                on=day,
                recorded_at=at,
                recorded_by=actor,
                note=note,
            )
        except ValueError as exc:
            raise LedgerRefusal(str(exc)) from None

    def reservation(
        self, amount: Decimal, *, currency: str | None, by: date, reason: str | None, actor: str
    ) -> WithdrawalReservation:
        at = self._now()
        if by < at.date():
            raise LedgerRefusal(f"a withdrawal due on {by.isoformat()} is already past")
        try:
            return WithdrawalReservation(
                amount=_positive(amount, "a withdrawal"),
                currency=currency or self._base,
                due_date=by,
                reason=reason or "withdrawal",
                created_at=at,
                created_by=actor,
                updated_at=at,
            )
        except ValueError as exc:
            raise LedgerRefusal(str(exc)) from None

    # ── writing ──

    async def record_deposit(self, deposit: Deposit) -> Deposit:
        made = await self._worker.run(repo.record_flow, deposit)
        assert isinstance(made, Deposit)
        return made

    async def record_reservation(self, reservation: WithdrawalReservation) -> WithdrawalReservation:
        return await self._worker.run(repo.record_reservation, reservation)

    async def open_reservation(self, reservation_id: int) -> WithdrawalReservation:
        reservation = await self._worker.run(repo.get_reservation, reservation_id)
        if reservation is None:
            raise LedgerRefusal(f"there is no reservation R{reservation_id}")
        if reservation.state is not ReservationState.OPEN:
            raise LedgerRefusal(f"reservation R{reservation_id} is {reservation.state.value}")
        return reservation

    def withdrawal_for(self, reservation: WithdrawalReservation, *, actor: str) -> Withdrawal:
        """The withdrawal that ends ``reservation`` today (nothing is written)."""
        at = self._now()
        return Withdrawal(
            amount=reservation.amount,
            currency=reservation.currency,
            on=at.date(),
            recorded_at=at,
            recorded_by=actor,
            reservation_id=reservation.reservation_id,
            note=reservation.reason,
        )

    async def record_withdrawal(self, withdrawal: Withdrawal) -> Withdrawal:
        """Record the withdrawal and close its reservation, together."""
        reservation_id = withdrawal.reservation_id
        if reservation_id is None:
            raise LedgerRefusal("a withdrawal ends a reservation")
        at = self._now()

        def write(store: Store) -> CashFlow:
            with store.transaction():
                repo.change_reservation(
                    store,
                    reservation_id,
                    at=at,
                    actor=withdrawal.recorded_by,
                    detail=f"withdrawn: {withdrawal.amount} {withdrawal.currency}",
                    state=ReservationState.WITHDRAWN,
                )
                return repo.record_flow(store, withdrawal)

        try:
            made = await self._worker.run(write)
        except StoreError as exc:
            raise LedgerRefusal(str(exc)) from None
        assert isinstance(made, Withdrawal)
        return made

    async def cancel(self, kind: RecordKind, number: int, *, actor: str) -> str:
        """Cancel an open reservation or a flow the broker has not shown; what was done."""
        at = self._now()
        try:
            if kind == "reservation":
                await self.open_reservation(number)
                await self._worker.run(
                    lambda store: repo.change_reservation(
                        store,
                        number,
                        at=at,
                        actor=actor,
                        detail="cancelled by the owner",
                        state=ReservationState.CANCELLED,
                    )
                )
                return f"reservation R{number} cancelled: investable capital is restored"
            if kind == "flow":
                await self._worker.run(repo.cancel_flow, number, at=at)
                return f"flow F{number} cancelled"
        except StoreError as exc:
            raise LedgerRefusal(str(exc)) from None
        raise LedgerRefusal("a cash event is not cancelled; classify it (`obliskiq cash classify`)")

    async def pending_event(self, event_id: int) -> CashEvent:
        event = await self._worker.run(repo.get_cash_event, event_id)
        if event is None:
            raise LedgerRefusal(f"there is no cash event E{event_id}")
        if event.state is not CashEventState.PENDING:
            raise LedgerRefusal(f"cash event E{event_id} is already classified")
        return event

    def classification(
        self, event: CashEvent, kind: Classification, *, actor: str, in_book: bool, note: str | None
    ) -> CashFlow:
        """The flow that explains ``event`` (nothing is written): an adjustment of its amount, or
        a deposit or withdrawal the broker already shows."""
        at = self._now()
        common: dict[str, Any] = {
            "currency": event.currency,
            "on": at.date(),
            "recorded_at": at,
            "recorded_by": actor,
            "matched_at": at,
            "in_book": in_book,
            "note": note,
        }
        if kind == "deposit":
            if event.amount <= ZERO:
                raise LedgerRefusal(f"E{event.event_id} is money out; it is not a deposit")
            return Deposit.model_validate(common | {"amount": event.amount})
        if kind == "withdrawal":
            if event.amount >= ZERO:
                raise LedgerRefusal(f"E{event.event_id} is money in; it is not a withdrawal")
            return Withdrawal.model_validate(common | {"amount": -event.amount})
        return Adjustment.model_validate(
            common
            | {
                "category": AdjustmentKind(kind),
                "amount": event.amount,
                "event_id": event.event_id,
            }
        )

    async def classify(self, event: CashEvent, flow: CashFlow, *, actor: str) -> CashFlow:
        if event.event_id is None:  # a stored event always has one
            raise LedgerRefusal("the cash event has no id")
        try:
            return await self._worker.run(
                repo.classify_event, event.event_id, flow, at=self._now(), actor=actor
            )
        except StoreError as exc:
            raise LedgerRefusal(str(exc)) from None

    async def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(
            reservations=tuple(await self._worker.run(repo.reservations)),
            flows=tuple(await self._worker.run(repo.cash_flows)),
            events=tuple(await self._worker.run(repo.cash_events)),
        )
