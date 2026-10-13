"""Properties of the intent state machine: only the legal moves happen, terminal states are
never left, and every accepted move leaves exactly one event in the store.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import itertools
import uuid
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st
from order_rig import terms

from obliskiq.domain.orders import (
    TERMINAL_STATES,
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderIntent,
    Sleeve,
)
from obliskiq.errors import IllegalTransitionError
from obliskiq.execution.state import LEGAL_TRANSITIONS, can_transition, check_transition
from obliskiq.store import open_database
from obliskiq.store import orders as repo

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
TARGETS = st.lists(st.sampled_from(list(IntentState)), max_size=25)


def test_every_state_is_reachable_and_every_terminal_state_is_a_dead_end() -> None:
    reached = {IntentState.PROPOSED}
    frontier = [IntentState.PROPOSED]
    while frontier:
        for nxt in LEGAL_TRANSITIONS[frontier.pop()]:
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    assert reached == set(IntentState)
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()
    for state, targets in LEGAL_TRANSITIONS.items():
        assert state not in targets  # a fact is not a transition


@given(TARGETS)
def test_only_legal_moves_are_accepted(targets: list[IntentState]) -> None:
    state = IntentState.PROPOSED
    for target in targets:
        if can_transition(state, target):
            check_transition(state, target)
            assert state not in TERMINAL_STATES
            state = target
        else:
            try:
                check_transition(state, target)
            except IllegalTransitionError:
                pass
            else:  # pragma: no cover - the property failing
                raise AssertionError(f"{state} -> {target} was accepted")


@given(TARGETS)
def test_every_accepted_move_appends_one_event(targets: list[IntentState]) -> None:
    store = open_database(":memory:", busy_timeout_ms=1000, synchronous="NORMAL")
    try:
        intent = OrderIntent(
            intent_id=uuid.uuid7(),
            sleeve=Sleeve.CORE,
            role=IntentRole.ENTRY,
            terms=terms(),
            reason="entry",
            mode=ExecutionMode.PAPER,
            approval_required=False,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        repo.insert_intent(store, intent, coid="c", at=NOW)
        state, accepted = IntentState.PROPOSED, 0
        for target in targets:
            if not can_transition(state, target):
                continue
            repo.transition_intent(
                store, intent.intent_id, from_state=state, to_state=target, at=NOW, detail="move"
            )
            state, accepted = target, accepted + 1
        history = repo.intent_history(store, intent.intent_id)
        assert len(history) == accepted + 1
        for before, after in itertools.pairwise(history):
            assert after.from_state is before.to_state
            assert can_transition(before.to_state, after.to_state)
        stored = repo.get_intent(store, intent.intent_id)
        assert stored is not None
        assert stored.intent.state is state
    finally:
        store.close()
