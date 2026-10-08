"""The option sleeve budget and the penalty balance.

The rule the whole sleeve turns on: a loss costs the premium **multiplied by**
``option_sleeve.loss_penalty_multiple``, and the multiple is held as a continuous
balance rather than a binary lock.

::

    penalty_balance += loss_penalty_multiple x realised_option_loss
    penalty_balance -= qualifying_realised_gains        (1:1, floored at 0)
    available_option_budget = max(
        0, budget_pct x NAV - penalty_balance - committed_premium)

Four details carry most of the design, and each exists to close a specific
incentive.

**The budget is a POOL, not a per-position allowance.** ``committed_premium`` is
the premium tied up by option positions still open, summed from
``premium_committed`` rows joined to ``position_ledger``. Without that
subtraction ``option_sleeve.budget_pct`` is not the "hard cap, % of current NAV"
it is specified as: ``max_concurrent_positions`` entries could each be sized
against the whole sleeve, committing five times the cap. Budget left unspent
stays unspent — it is never redistributed across the remaining slots, because a
rule that hands the leftovers to the next position fills the sleeve by
construction. A commitment carries ``penalty_added = 0`` and so leaves the
penalty replay and its audit trail exactly as they were.

**The penalty is booked at DETECTION, not at close** (``zombie_test.book_penalty_at``).
Booking at close would reward holding a dead position to expiry in order to
postpone the penalty. The later ``loss_closed`` row reconciles the actual figure
against the detected estimate and adjusts by **the delta only**, so a close never
double-counts what detection already booked.

**Recovery is measured on realised gains, never on NAV.** A NAV rule measures
market beta rather than decisions: a general drawdown would extend the penalty
indefinitely for reasons unrelated to the option, and a beta rally would clear it
without anything having been demonstrated.

**Only NON-OPTION positions exited for a reason in
``option_sleeve.qualifying_exit_reasons`` count.** A gain on an ``override`` exit
never counts — otherwise the sleeve unlocks by cutting winners early to bank
profit — and neither does a win inside the option sleeve itself, which is the
thing being earned back rather than the earning.

The balance is **replayed from the events** on every read rather than held as a
mutable field. ``penalty_balance_after`` is written on each row as the audit
trail, and :meth:`PenaltyLedger.verify` checks the two against each other.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..errors import ObliskError
from ..logging_setup import get_logger
from ..store.db import Store
from ..store.ledger import (
    EVENT_GAIN_REALISED,
    EVENT_LOSS_CLOSED,
    EVENT_LOSS_DETECTED,
    EVENT_PREMIUM_COMMITTED,
    EVENT_QUALIFYING_EQUITY_GAIN,
)
from ..util.timeutil import utc_stamp

log = get_logger("compute.penalty")

#: The floor. A penalty balance never goes negative: banking gains beyond the
#: outstanding penalty does not buy credit against a future loss.
PENALTY_FLOOR = 0.0


class PenaltyError(ObliskError):
    """A penalty write was refused."""


@dataclass(frozen=True)
class Conversion:
    """How a figure stated in another currency became the base-currency amount.

    **Every amount this module reads and writes is in ``account.base_currency``.**
    The budget is a fraction of NAV, which is a base-currency quantity, so a
    premium or a realised loss stated in the contract's own currency has to be
    converted before it can be added to or subtracted from it. That conversion
    happens at the server boundary, where the rate lives; this records what it
    was, so a booked figure can be checked against the rate that produced it
    rather than reconstructed years later from a note.

    ``None`` where nothing was converted: the figure was already in base.
    """

    currency: str
    quote_amount: float
    fx_to_base: float

    def as_row(self) -> tuple[str, float, float]:
        return self.currency, self.quote_amount, self.fx_to_base


@dataclass(frozen=True)
class SleeveStatus:
    """What the session is told at the top of every report."""

    nav: float
    budget_pct: float
    gross_budget: float
    penalty_balance: float
    #: Premium tied up by option positions that are still open. Subtracted from
    #: the gross budget, which is what makes budget_pct a cap rather than a
    #: per-position allowance.
    committed_premium: float
    available_budget: float
    #: Realised qualifying gains still required to clear the penalty entirely.
    gains_required: float
    loss_penalty_multiple: float
    qualifying_exit_reasons: tuple[str, ...]

    @property
    def is_locked(self) -> bool:
        """Nothing further can be opened. Say WHY with :attr:`locked_by`."""
        return self.available_budget <= PENALTY_FLOOR

    @property
    def locked_by(self) -> str | None:
        """What closed the sleeve, when something did.

        Since the budget became a pool, "no budget left" has two causes that
        call for opposite responses, and reporting them as one word would be
        the sort of true-but-useless statement a session then repeats. A sleeve
        shut by the PENALTY reopens on realised qualifying gains and on nothing
        else; a sleeve shut by COMMITMENTS reopens the moment a position closes
        and has no penalty to work off at all.
        """
        if not self.is_locked:
            return None
        penalty = self.penalty_balance > PENALTY_FLOOR
        committed = self.committed_premium > PENALTY_FLOOR
        if penalty and committed:
            return "penalty_and_commitments"
        if penalty:
            return "penalty"
        if committed:
            return "commitments"
        # Neither: the gross budget itself is zero or negative, which means NAV
        # is. Named rather than left as a fourth silent case.
        return "no_nav"


@dataclass(frozen=True)
class PenaltyEvent:
    event_id: int
    event_at: str
    event_type: str
    position_id: int | None
    contract_id: int | None
    realised_pnl: float | None
    penalty_added: float
    penalty_balance_after: float
    note: str | None
    #: What a commitment tied up, in base currency. Carried on the event because
    #: a caller told "premium committed" and not how much has been told nothing.
    premium_paid: float | None = None
    #: What the amounts above were stated in before conversion, or ``None`` when
    #: nothing was converted. The amounts themselves are always base currency.
    amount_currency: str | None = None
    amount_quote: float | None = None
    fx_to_base: float | None = None


class PenaltyLedger:
    """Reads and writes ``option_ledger``, and derives the sleeve budget."""

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config
        sleeve = config.section("option_sleeve")
        self._multiple = sleeve.require_float("loss_penalty_multiple")
        self._budget_pct = sleeve.require_float("budget_pct")
        self._recovery_basis = sleeve.require_str("recovery_basis")
        self._qualifying = tuple(
            str(reason) for reason in sleeve.require_list("qualifying_exit_reasons")
        )
        self._book_at = config.require_str("zombie_test.book_penalty_at")

    # ── reads ──

    @property
    def loss_penalty_multiple(self) -> float:
        return self._multiple

    @property
    def qualifying_exit_reasons(self) -> tuple[str, ...]:
        return self._qualifying

    @property
    def books_at_detection(self) -> bool:
        return self._book_at == "detection"

    def events(self) -> list[PenaltyEvent]:
        rows = self._store.execute(
            "SELECT event_id, event_at, event_type, position_id, contract_id, realised_pnl, "
            "penalty_added, penalty_balance_after, note, premium_paid, amount_currency, "
            "amount_quote, "
            "fx_to_base FROM option_ledger ORDER BY event_at, event_id"
        ).fetchall()
        return [
            PenaltyEvent(
                event_id=int(row["event_id"]),
                event_at=str(row["event_at"]),
                event_type=str(row["event_type"]),
                position_id=row["position_id"],
                contract_id=row["contract_id"],
                realised_pnl=(None if row["realised_pnl"] is None else float(row["realised_pnl"])),
                penalty_added=float(row["penalty_added"]),
                penalty_balance_after=float(row["penalty_balance_after"]),
                note=row["note"],
                premium_paid=(None if row["premium_paid"] is None else float(row["premium_paid"])),
                amount_currency=row["amount_currency"],
                amount_quote=(None if row["amount_quote"] is None else float(row["amount_quote"])),
                fx_to_base=(None if row["fx_to_base"] is None else float(row["fx_to_base"])),
            )
            for row in rows
        ]

    def balance(self) -> float:
        """The outstanding penalty, replayed from the events."""
        running = PENALTY_FLOOR
        for event in self.events():
            running = max(PENALTY_FLOOR, running + event.penalty_added)
        return running

    def committed_premium(self) -> float:
        """Premium tied up by option positions that are still OPEN.

        The join to ``position_ledger`` is what releases a commitment: there is
        deliberately no reversal event. A second row would have to be written
        every time a position closed, and the failure mode of that design is a
        release nobody wrote — which silently frees budget that is still spent,
        the exact error this method exists to fix.
        """
        row = self._store.execute(
            "SELECT COALESCE(SUM(committed.premium_paid), 0.0) FROM option_ledger AS committed "
            "JOIN position_ledger AS held ON held.position_id = committed.position_id "
            "WHERE committed.event_type = ? AND held.closed_at IS NULL",
            (EVENT_PREMIUM_COMMITTED,),
        ).fetchone()
        return float(row[0]) if row else PENALTY_FLOOR

    def detected_penalty_for(self, position_id: int) -> float:
        """The penalty already booked against a position, at detection or since."""
        row = self._store.execute(
            "SELECT COALESCE(SUM(penalty_added), 0.0) FROM option_ledger "
            "WHERE position_id = ? AND event_type IN (?, ?)",
            (position_id, EVENT_LOSS_DETECTED, EVENT_LOSS_CLOSED),
        ).fetchone()
        return float(row[0]) if row else PENALTY_FLOOR

    def status(self, nav: float) -> SleeveStatus:
        """Everything the top of a session report needs, in one object."""
        balance = self.balance()
        committed = self.committed_premium()
        gross = self._budget_pct * nav
        return SleeveStatus(
            nav=nav,
            budget_pct=self._budget_pct,
            gross_budget=gross,
            penalty_balance=balance,
            committed_premium=committed,
            available_budget=max(PENALTY_FLOOR, gross - balance - committed),
            gains_required=balance,
            loss_penalty_multiple=self._multiple,
            qualifying_exit_reasons=self._qualifying,
        )

    def verify(self) -> list[str]:
        """Replay the events and compare against the stored running balance.

        Returns the discrepancies, empty when the ledger is consistent. The
        self-audit reports findings rather than quietly reconciling them.
        """
        running = PENALTY_FLOOR
        problems: list[str] = []
        for event in self.events():
            running = max(PENALTY_FLOOR, running + event.penalty_added)
            if abs(running - event.penalty_balance_after) > _tolerance(running):
                problems.append(
                    f"event {event.event_id} ({event.event_type} at {event.event_at}) stored "
                    f"{event.penalty_balance_after:.2f} but the replay gives {running:.2f}"
                )
        return problems

    # ── writes ──

    def book_premium_committed(
        self,
        *,
        premium_paid: float,
        position_id: int,
        event_at: str | None = None,
        contract_id: int | None = None,
        note: str | None = None,
        conversion: Conversion | None = None,
    ) -> PenaltyEvent:
        """Record premium tied up by an entry, so the sleeve budget is a pool.

        ``premium_paid`` is in ``account.base_currency``, like every amount this
        module reads or writes; ``conversion`` records what it was before that,
        when the contract is quoted in another currency.

        ``position_id`` is required rather than optional: the commitment is
        released by the position closing, so one written without a position
        would never be released and would drain the sleeve permanently.
        """
        if premium_paid <= 0:
            raise PenaltyError(
                f"premium_paid must be a positive amount, got {premium_paid!r}. A "
                "commitment of nothing is not a commitment."
            )
        return self._write(
            event_type=EVENT_PREMIUM_COMMITTED,
            penalty_added=PENALTY_FLOOR,
            event_at=event_at,
            position_id=position_id,
            contract_id=contract_id,
            premium_paid=premium_paid,
            conversion=conversion,
            note=note
            or (
                "premium committed by an open option position; subtracted from available "
                "budget until the position closes. Does not touch the penalty balance."
            ),
        )

    def book_loss_detected(
        self,
        *,
        expected_loss: float,
        event_at: str | None = None,
        position_id: int | None = None,
        contract_id: int | None = None,
        premium_paid: float | None = None,
        note: str | None = None,
        conversion: Conversion | None = None,
    ) -> PenaltyEvent:
        """Book the penalty at DETECTION — zombie confirmation or invalidation.

        This is the moment the penalty applies. Deferring it to the close would
        reward holding a dead position to expiry to postpone the cost.
        """
        if expected_loss < 0:
            raise PenaltyError(
                f"expected_loss must be a positive magnitude, got {expected_loss!r}. A "
                "gain is not a loss with a sign."
            )
        if not self.books_at_detection:
            log.warning(
                "zombie_test.book_penalty_at is not 'detection'; booking anyway because "
                "the caller asked for a detection event",
                extra={"book_penalty_at": self._book_at},
            )
        return self._write(
            event_type=EVENT_LOSS_DETECTED,
            penalty_added=self._multiple * expected_loss,
            event_at=event_at,
            position_id=position_id,
            contract_id=contract_id,
            premium_paid=premium_paid,
            realised_pnl=-expected_loss,
            conversion=conversion,
            note=note,
        )

    def reconcile_loss_closed(
        self,
        *,
        actual_loss: float,
        event_at: str | None = None,
        position_id: int | None = None,
        contract_id: int | None = None,
        proceeds: float | None = None,
        note: str | None = None,
        conversion: Conversion | None = None,
    ) -> PenaltyEvent:
        """Reconcile the realised figure against what detection already booked.

        Adjusts by **the delta only**. A close after a detection adds nothing when
        the estimate was right, which is what stops the penalty being counted
        twice for one loss.
        """
        if actual_loss < 0:
            raise PenaltyError(f"actual_loss must be a positive magnitude, got {actual_loss!r}")
        already = (
            self.detected_penalty_for(position_id) if position_id is not None else PENALTY_FLOOR
        )
        target_penalty = self._multiple * actual_loss
        return self._write(
            event_type=EVENT_LOSS_CLOSED,
            penalty_added=target_penalty - already,
            event_at=event_at,
            position_id=position_id,
            contract_id=contract_id,
            proceeds=proceeds,
            realised_pnl=-actual_loss,
            resolved_at=event_at or utc_stamp(),
            conversion=conversion,
            note=note
            or (
                f"reconciled against {already:.2f} already booked at detection; "
                f"adjusted by the delta only"
            ),
        )

    def book_qualifying_gain(
        self,
        *,
        realised_pnl: float,
        exit_reason: str,
        event_at: str | None = None,
        position_id: int | None = None,
        contract_id: int | None = None,
        note: str | None = None,
        conversion: Conversion | None = None,
    ) -> PenaltyEvent:
        """Reduce the penalty 1:1 with a realised gain on a NON-OPTION position.

        Only exits whose reason is in ``option_sleeve.qualifying_exit_reasons``
        count. An ``override`` gain never does: otherwise the sleeve unlocks by
        cutting winners early to bank profit, which is the behaviour the penalty
        exists to discourage.
        """
        if exit_reason not in self._qualifying:
            raise PenaltyError(
                f"exit_reason {exit_reason!r} does not qualify. Recovery counts only "
                f"gains from positions exited for {list(self._qualifying)}; an "
                "'override' gain never reduces the penalty."
            )
        if realised_pnl <= 0:
            raise PenaltyError(
                f"a qualifying gain must be positive, got {realised_pnl!r}. A losing "
                "equity exit does not reduce the option penalty either way."
            )
        if self._recovery_basis != "realised_gains":
            raise PenaltyError(
                f"option_sleeve.recovery_basis is {self._recovery_basis!r}. Recovery is "
                "measured on realised gains, never on NAV: NAV measures market beta "
                "rather than decisions."
            )
        return self._write(
            event_type=EVENT_QUALIFYING_EQUITY_GAIN,
            penalty_added=-realised_pnl,
            event_at=event_at,
            position_id=position_id,
            contract_id=contract_id,
            realised_pnl=realised_pnl,
            conversion=conversion,
            note=note or f"qualifying equity gain, exit_reason={exit_reason}",
        )

    def record_option_gain(
        self,
        *,
        realised_pnl: float,
        event_at: str | None = None,
        position_id: int | None = None,
        contract_id: int | None = None,
        proceeds: float | None = None,
        note: str | None = None,
        conversion: Conversion | None = None,
    ) -> PenaltyEvent:
        """Record a WIN inside the option sleeve. It does not reduce the penalty.

        Recovery is measured on non-option realised gains. An option win is the
        thing being earned back, not the earning, so this row is written for the
        record with ``penalty_added`` of zero.
        """
        return self._write(
            event_type=EVENT_GAIN_REALISED,
            penalty_added=PENALTY_FLOOR,
            event_at=event_at,
            position_id=position_id,
            contract_id=contract_id,
            proceeds=proceeds,
            realised_pnl=realised_pnl,
            conversion=conversion,
            note=note
            or (
                "option-sleeve gain; recovery is measured on non-option realised gains, "
                "so this does not reduce the penalty balance"
            ),
        )

    # ── internals ──

    def _write(
        self,
        *,
        event_type: str,
        penalty_added: float,
        event_at: str | None,
        position_id: int | None,
        contract_id: int | None,
        realised_pnl: float | None = None,
        premium_paid: float | None = None,
        proceeds: float | None = None,
        resolved_at: str | None = None,
        note: str | None = None,
        conversion: Conversion | None = None,
    ) -> PenaltyEvent:
        stamp = event_at or utc_stamp()
        balance_after = max(PENALTY_FLOOR, self.balance() + penalty_added)
        currency, quote, rate = (None, None, None) if conversion is None else conversion.as_row()
        with self._store.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO option_ledger (
                    event_at, event_type, contract_id, position_id, premium_paid, proceeds,
                    realised_pnl, penalty_added, penalty_balance_after, resolved_at, note,
                    amount_currency, amount_quote, fx_to_base)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stamp,
                    event_type,
                    contract_id,
                    position_id,
                    premium_paid,
                    proceeds,
                    realised_pnl,
                    penalty_added,
                    balance_after,
                    resolved_at,
                    note,
                    currency,
                    quote,
                    rate,
                ),
            )
        event = PenaltyEvent(
            event_id=int(cursor.lastrowid or 0),
            event_at=stamp,
            event_type=event_type,
            position_id=position_id,
            contract_id=contract_id,
            realised_pnl=realised_pnl,
            penalty_added=penalty_added,
            penalty_balance_after=balance_after,
            note=note,
            premium_paid=premium_paid,
            amount_currency=currency,
            amount_quote=quote,
            fx_to_base=rate,
        )
        log.info(
            "option ledger event",
            extra={
                "event_type": event_type,
                "penalty_added": penalty_added,
                "penalty_balance_after": balance_after,
                "position_id": position_id,
            },
        )
        return event


def _tolerance(value: float) -> float:
    """Float comparison tolerance, scaled to the magnitude being compared."""
    return max(_ABSOLUTE_TOLERANCE, abs(value) * _RELATIVE_TOLERANCE)


#: Currency amounts, so a hundredth of a unit is the natural absolute floor.
_ABSOLUTE_TOLERANCE = 0.01
#: And a relative term so the check does not tighten as the balance grows.
_RELATIVE_TOLERANCE = 1e-9
