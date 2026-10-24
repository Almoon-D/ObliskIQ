"""The sleeve's pool and its penalty balance (lite's ``compute/penalty.py``).

The rule the sleeve turns on: a loss costs its premium **multiplied by**
``option_sleeve.loss_penalty_multiple``, held as a continuous balance rather than a lock::

    penalty_balance += loss_penalty_multiple x option_loss
    penalty_balance -= qualifying realised gain        (one for one, floored at 0)
    available = max(0, budget - penalty_balance - committed_premium)

* **The budget is a pool, not a per-position allowance.** ``committed_premium`` is the premium
  tied up by positions still open; it is released by the position closing, never by a second
  row. Budget left unspent stays unspent.
* **The penalty is booked at detection** (``zombie_test.book_penalty_at``), not at the close, so
  holding a dead position to expiry never postpones it. The close reconciles the realised loss
  against what detection booked and adjusts by the difference only.
* **Recovery is on realised gains, never on NAV**, and only a non-option position's gain on an
  exit in ``option_sleeve.qualifying_exit_reasons`` counts: an ``override`` gain never does, nor
  a win inside the sleeve itself, which is what is being earned back.

Every amount is in the base currency. The balance is replayed from the events on every read;
``penalty_balance_after`` is written on each event as the audit trail and :meth:`verify` checks
the two against each other.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ...config.schema import ObliskConfig
from ...domain.options import Conversion, LedgerEvent, LedgerEventType
from ...errors import ObliskError
from ...store import options as options_repo
from ...store.db import Store
from ...util.timeutil import utc_now

#: The floor: a balance never goes negative, so gains beyond it buy no credit against a loss.
ZERO = Decimal(0)
#: The events that book a position's loss.
LOSS_EVENTS = (LedgerEventType.LOSS_DETECTED, LedgerEventType.LOSS_CLOSED)


class PenaltyError(ObliskError):
    """A penalty-ledger write was refused."""


@dataclass(frozen=True, slots=True)
class SleeveStatus:
    """The pool as of a day: the gross budget, the penalty, the commitments and what is left."""

    as_of: date
    budget: Decimal
    penalty_balance: Decimal
    committed_premium: Decimal
    available: Decimal
    loss_penalty_multiple: Decimal
    qualifying_exit_reasons: tuple[str, ...]
    recovery_basis: str

    @property
    def gains_required(self) -> Decimal:
        """Qualifying realised gains still needed to clear the penalty."""
        return self.penalty_balance

    @property
    def is_locked(self) -> bool:
        return self.available <= ZERO

    @property
    def locked_by(self) -> str | None:
        """Why nothing can be opened: the penalty reopens on qualifying gains only, commitments
        the moment a position closes."""
        if not self.is_locked:
            return None
        penalty = self.penalty_balance > ZERO
        committed = self.committed_premium > ZERO
        if penalty and committed:
            return "penalty_and_commitments"
        if penalty:
            return "penalty"
        if committed:
            return "commitments"
        return "no_budget"


def replay(events: Sequence[LedgerEvent]) -> Decimal:
    """The balance the events leave, each floored at zero in turn."""
    running = ZERO
    for event in events:
        running = max(ZERO, running + event.penalty_added)
    return running


class PenaltyLedger:
    """Reads and writes the penalty ledger, and derives the sleeve's pool."""

    def __init__(
        self, store: Store, settings: ObliskConfig, *, now: Callable[[], datetime] = utc_now
    ) -> None:
        self._store = store
        sleeve = settings.option_sleeve
        self._multiple = sleeve.loss_penalty_multiple
        self._qualifying = tuple(sleeve.qualifying_exit_reasons)
        self._recovery_basis = sleeve.recovery_basis
        self._now = now

    # ── reads ──

    @property
    def loss_penalty_multiple(self) -> Decimal:
        return self._multiple

    def balance(self, *, as_of: date) -> Decimal:
        """The outstanding penalty after every event dated on or before ``as_of``."""
        return replay(options_repo.ledger_events(self._store, as_of=as_of))

    def committed_premium(self, *, as_of: date) -> Decimal:
        """Premium tied up on ``as_of`` by positions open at its end: each commitment dated by then
        whose position had not closed by then."""
        open_ids = {
            position.position_id
            for position in options_repo.open_positions(self._store, as_of=as_of)
        }
        return sum(
            (
                event.amount
                for event in options_repo.ledger_events(self._store, as_of=as_of)
                if event.event_type is LedgerEventType.PREMIUM_COMMITTED
                and event.position_id in open_ids
            ),
            ZERO,
        )

    def booked_loss_penalty(self, position_id: int) -> Decimal:
        """The penalty already booked against a position, at detection or at its close."""
        return sum(
            (
                event.penalty_added
                for event in options_repo.position_events(self._store, position_id, LOSS_EVENTS)
            ),
            ZERO,
        )

    def status(self, *, budget: Decimal, as_of: date) -> SleeveStatus:
        """The pool on ``as_of`` against the sleeve's gross ``budget`` (the share of investable
        capital ``capital.budgets.option_premium`` gives it on that day)."""
        balance = self.balance(as_of=as_of)
        committed = self.committed_premium(as_of=as_of)
        return SleeveStatus(
            as_of=as_of,
            budget=budget,
            penalty_balance=balance,
            committed_premium=committed,
            available=max(ZERO, budget - balance - committed),
            loss_penalty_multiple=self._multiple,
            qualifying_exit_reasons=self._qualifying,
            recovery_basis=self._recovery_basis,
        )

    def verify(self, *, as_of: date) -> list[str]:
        """The events whose stored running balance disagrees with the replay; empty when the
        ledger is consistent. Findings are reported, never quietly reconciled."""
        running = ZERO
        problems: list[str] = []
        for event in options_repo.ledger_events(self._store, as_of=as_of):
            running = max(ZERO, running + event.penalty_added)
            if event.penalty_balance_after != running:
                problems.append(
                    f"event {event.event_id} ({event.event_type.value} on {event.event_on}) "
                    f"stored {event.penalty_balance_after} but the replay gives {running}"
                )
        return problems

    # ── writes ──

    def book_premium_committed(
        self,
        *,
        position_id: int,
        premium: Decimal,
        on: date,
        conversion: Conversion | None = None,
        note: str | None = None,
    ) -> LedgerEvent:
        """Tie up an entry's premium until its position closes. It moves no penalty."""
        if premium <= ZERO:
            raise PenaltyError(f"a commitment is a positive premium, got {premium}")
        return self._write(
            LedgerEventType.PREMIUM_COMMITTED,
            on=on,
            amount=premium,
            penalty_added=ZERO,
            position_id=position_id,
            conversion=conversion,
            note=note
            or "premium committed by an open option position; subtracted from the pool until the "
            "position closes",
        )

    def book_loss_detected(
        self,
        *,
        position_id: int,
        expected_loss: Decimal,
        on: date,
        conversion: Conversion | None = None,
        note: str | None = None,
    ) -> LedgerEvent:
        """Book the penalty when the loss is detected (a confirmed zombie, an invalidation)."""
        if expected_loss < ZERO:
            raise PenaltyError(f"a loss is a magnitude, got {expected_loss}")
        return self._write(
            LedgerEventType.LOSS_DETECTED,
            on=on,
            amount=expected_loss,
            penalty_added=self._multiple * expected_loss,
            position_id=position_id,
            conversion=conversion,
            note=note or "loss detected; the penalty is booked at detection",
        )

    def reconcile_loss_closed(
        self,
        *,
        position_id: int,
        actual_loss: Decimal,
        on: date,
        conversion: Conversion | None = None,
        note: str | None = None,
    ) -> LedgerEvent:
        """Reconcile the realised loss against what detection booked: the difference only, so a
        close at the detected figure adds nothing and one loss is never counted twice."""
        if actual_loss < ZERO:
            raise PenaltyError(f"a loss is a magnitude, got {actual_loss}")
        already = self.booked_loss_penalty(position_id)
        return self._write(
            LedgerEventType.LOSS_CLOSED,
            on=on,
            amount=actual_loss,
            penalty_added=self._multiple * actual_loss - already,
            position_id=position_id,
            conversion=conversion,
            note=note
            or f"reconciled against {already} already booked; adjusted by the difference only",
        )

    def book_qualifying_gain(
        self,
        *,
        conid: int,
        realised_gain: Decimal,
        exit_reason: str,
        on: date,
        conversion: Conversion | None = None,
        note: str | None = None,
    ) -> LedgerEvent:
        """Reduce the penalty one for one with a non-option position's realised gain, on an exit
        in ``option_sleeve.qualifying_exit_reasons`` only."""
        if exit_reason not in self._qualifying:
            raise PenaltyError(
                f"exit reason {exit_reason!r} does not qualify: recovery counts gains from exits "
                f"for {list(self._qualifying)}; an override gain never reduces the penalty"
            )
        if realised_gain <= ZERO:
            raise PenaltyError(
                f"a qualifying gain is positive, got {realised_gain}: a losing exit reduces nothing"
            )
        return self._write(
            LedgerEventType.QUALIFYING_GAIN,
            on=on,
            amount=realised_gain,
            penalty_added=-realised_gain,
            conid=conid,
            exit_reason=exit_reason,
            conversion=conversion,
            note=note or f"qualifying realised gain ({exit_reason}, {self._recovery_basis})",
        )

    def record_option_gain(
        self,
        *,
        position_id: int,
        realised_gain: Decimal,
        on: date,
        conversion: Conversion | None = None,
        note: str | None = None,
    ) -> LedgerEvent:
        """Record a win inside the sleeve. It does not reduce the penalty: an option win is what
        is being earned back, not the earning."""
        if realised_gain < ZERO:
            raise PenaltyError(f"a gain is a magnitude, got {realised_gain}")
        return self._write(
            LedgerEventType.OPTION_GAIN,
            on=on,
            amount=realised_gain,
            penalty_added=ZERO,
            position_id=position_id,
            conversion=conversion,
            note=note
            or "option-sleeve gain; recovery is measured on non-option realised gains, so it does "
            "not reduce the penalty",
        )

    def _write(
        self,
        kind: LedgerEventType,
        *,
        on: date,
        amount: Decimal,
        penalty_added: Decimal,
        note: str,
        position_id: int | None = None,
        conid: int | None = None,
        exit_reason: str | None = None,
        conversion: Conversion | None = None,
    ) -> LedgerEvent:
        with self._store.transaction():
            if (
                position_id is not None
                and options_repo.get_position(self._store, position_id) is None
            ):
                raise PenaltyError(f"option position {position_id} does not exist")
            later = [
                event
                for event in options_repo.ledger_events(self._store, as_of=date.max)
                if event.event_on > on
            ]
            if later:
                raise PenaltyError(
                    f"the ledger already holds an event dated {later[0].event_on}, after {on}: "
                    "the balance is a replay in date order, so events are written in it"
                )
            balance = max(ZERO, self.balance(as_of=on) + penalty_added)
            event = LedgerEvent(
                event_id=None,
                event_on=on,
                event_type=kind,
                position_id=position_id,
                conid=conid,
                amount=amount,
                penalty_added=penalty_added,
                penalty_balance_after=balance,
                exit_reason=exit_reason,
                conversion=conversion,
                note=note,
                recorded_at=self._now(),
            )
            return options_repo.append_event(self._store, event)
