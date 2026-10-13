"""The intent state machine and the book that applies it.

    proposed → approved → staged → submitted → working → partially_filled
        → filled | cancelled | rejected | expired

Every state before the broker can end in cancelled, rejected or expired; a submitted order can
also be filled at once; a working one can fill, part-fill, be cancelled or expire. Terminal
states are never left. Any other move raises :class:`~obliskiq.errors.IllegalTransitionError`
before the store is touched, and every move the store accepts appends an
:class:`~obliskiq.domain.orders.IntentEvent` in the same transaction.

Facts within a state (a modify the broker accepted, a partial fill, a warning answered) are
events with ``from_state == to_state``, recorded only while an order can be at the broker.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final
from uuid import UUID

from ..domain.orders import (
    TERMINAL_STATES,
    IntentEvent,
    IntentState,
    OrderIntent,
    OrderTerms,
)
from ..errors import ExecutionError, IllegalTransitionError
from ..ibkr.orders import coid_for
from ..store import orders as repo
from ..store.orders import StoredIntent
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

S = IntentState

#: Where each state may go next. Terminal states go nowhere.
LEGAL_TRANSITIONS: Final[Mapping[IntentState, frozenset[IntentState]]] = MappingProxyType(
    {
        S.PROPOSED: frozenset({S.APPROVED, S.CANCELLED, S.REJECTED, S.EXPIRED}),
        S.APPROVED: frozenset({S.STAGED, S.CANCELLED, S.REJECTED, S.EXPIRED}),
        S.STAGED: frozenset({S.SUBMITTED, S.CANCELLED, S.REJECTED, S.EXPIRED}),
        S.SUBMITTED: frozenset(
            {S.WORKING, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED}
        ),
        S.WORKING: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED}),
        S.PARTIALLY_FILLED: frozenset({S.FILLED, S.CANCELLED, S.EXPIRED}),
        **{state: frozenset() for state in TERMINAL_STATES},
    }
)

#: States in which an order can be at the broker: facts may be recorded in them.
BROKER_STATES: Final = frozenset({S.SUBMITTED, S.WORKING, S.PARTIALLY_FILLED})


def can_transition(from_state: IntentState, to_state: IntentState) -> bool:
    return to_state in LEGAL_TRANSITIONS[from_state]


def check_transition(from_state: IntentState, to_state: IntentState) -> None:
    if not can_transition(from_state, to_state):
        raise IllegalTransitionError(
            f"an intent cannot move from {from_state.value} to {to_state.value}"
        )


class IntentBook:
    """Intents in the store, moved only along the legal transitions."""

    def __init__(
        self,
        worker: StoreWorker,
        *,
        coid_max_length: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._worker = worker
        self._coid_max_length = coid_max_length
        self._now = now

    async def add(self, intent: OrderIntent) -> StoredIntent:
        """Store a new intent under its cOID; a repeated intent id or cOID is refused."""
        if intent.state is not IntentState.PROPOSED:
            raise IllegalTransitionError("a new intent starts as proposed")
        coid = coid_for(intent.intent_id, self._coid_max_length)
        await self._worker.run(repo.insert_intent, intent, coid=coid, at=self._now())
        return StoredIntent(intent=intent, coid=coid)

    async def get(self, intent_id: UUID) -> StoredIntent:
        stored = await self._worker.run(repo.get_intent, intent_id)
        if stored is None:
            raise ExecutionError(f"no intent {intent_id} in the store")
        return stored

    async def by_coid(self, coid: str) -> StoredIntent | None:
        return await self._worker.run(repo.intent_by_coid, coid)

    async def advance(self, intent_id: UUID, to_state: IntentState, detail: str) -> IntentEvent:
        """Move the intent to ``to_state``; an illegal move raises and changes nothing."""
        current = (await self.get(intent_id)).intent.state
        check_transition(current, to_state)
        return await self._worker.run(
            repo.transition_intent,
            intent_id,
            from_state=current,
            to_state=to_state,
            at=self._now(),
            detail=detail,
        )

    async def fact(
        self, intent_id: UUID, detail: str, *, terms: OrderTerms | None = None
    ) -> IntentEvent:
        """Record a fact within the current state (``terms``: a modify the broker accepted)."""
        current = (await self.get(intent_id)).intent.state
        if current not in BROKER_STATES:
            raise IllegalTransitionError(
                f"a fact is recorded only while an order can be at the broker, not in "
                f"{current.value}"
            )
        return await self._worker.run(
            repo.record_intent_fact,
            intent_id,
            state=current,
            at=self._now(),
            detail=detail,
            terms=terms,
        )

    async def history(self, intent_id: UUID) -> list[IntentEvent]:
        return await self._worker.run(repo.intent_history, intent_id)
