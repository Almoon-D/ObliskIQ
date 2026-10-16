"""The intent book: moves only along the legal transitions, facts only while an order can be at
the broker, and nothing changes when a move is refused.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from typing import Any

import pytest
from order_rig import terms

from obliskiq.domain.orders import IntentState
from obliskiq.errors import ExecutionError, IllegalTransitionError


async def test_a_new_intent_must_start_proposed(order_rig: Any) -> None:
    stored = await order_rig.intent(state=IntentState.APPROVED)
    moved = (await order_rig.book.get(stored.intent.intent_id)).intent
    with pytest.raises(IllegalTransitionError):
        await order_rig.book.add(moved.model_copy(update={"intent_id": uuid.uuid7()}))


async def test_an_illegal_move_raises_and_changes_nothing(order_rig: Any) -> None:
    stored = await order_rig.intent(state=IntentState.APPROVED)
    intent_id = stored.intent.intent_id
    before = await order_rig.book.history(intent_id)
    with pytest.raises(IllegalTransitionError):
        await order_rig.book.advance(intent_id, IntentState.WORKING, "skip ahead")
    assert await order_rig.book.history(intent_id) == before
    assert (await order_rig.book.get(intent_id)).intent.state is IntentState.APPROVED


async def test_a_terminal_intent_never_moves(order_rig: Any) -> None:
    stored = await order_rig.intent(state=IntentState.PROPOSED)
    intent_id = stored.intent.intent_id
    await order_rig.book.advance(intent_id, IntentState.EXPIRED, "too late")
    for target in IntentState:
        with pytest.raises(IllegalTransitionError):
            await order_rig.book.advance(intent_id, target, "again")


async def test_facts_only_while_an_order_can_be_at_the_broker(order_rig: Any) -> None:
    stored = await order_rig.intent(state=IntentState.STAGED)
    intent_id = stored.intent.intent_id
    with pytest.raises(IllegalTransitionError):
        await order_rig.book.fact(intent_id, "too early")
    await order_rig.book.advance(intent_id, IntentState.SUBMITTED, "sent")
    event = await order_rig.book.fact(intent_id, "modified", terms=terms(limit="99"))
    assert event.from_state is event.to_state is IntentState.SUBMITTED
    assert (await order_rig.book.get(intent_id)).intent.terms == terms(limit="99")


async def test_an_unknown_intent_raises(order_rig: Any) -> None:
    with pytest.raises(ExecutionError):
        await order_rig.book.get(uuid.uuid7())
    assert await order_rig.book.by_coid("unknown") is None
