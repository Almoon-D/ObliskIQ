"""The approval queue: off by default, per sleeve; the owner's decisions are recorded and move
the intent.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Any

import pytest

from obliskiq.domain.orders import IntentState, Sleeve
from obliskiq.errors import ExecutionError
from obliskiq.execution.approvals import ApprovalQueue
from obliskiq.store import orders as repo


def test_the_queue_is_off_by_default(shipped_settings: Any) -> None:
    assert shipped_settings.execution.approval.sleeves == []


async def test_the_queue_holds_what_needs_a_decision(order_rig: Any) -> None:
    held = await order_rig.staged(approval_required=True, state=IntentState.PROPOSED)
    await order_rig.staged(state=IntentState.PROPOSED)  # needs none
    await order_rig.staged(approval_required=True)  # staged: past the queue
    queue = order_rig.approvals
    assert [s.intent.intent_id for s in await queue.pending()] == [held.intent.intent_id]
    decision = await queue.approve(held.intent.intent_id, actor="owner", reason="looks right")
    assert decision.approved
    assert (await order_rig.book.get(held.intent.intent_id)).intent.state is IntentState.APPROVED
    assert await order_rig.store(repo.approval_for, held.intent.intent_id) == decision
    assert await queue.pending() == []


async def test_a_rejection_ends_the_intent(order_rig: Any) -> None:
    held = await order_rig.staged(approval_required=True, state=IntentState.PROPOSED)
    decision = await order_rig.approvals.reject(held.intent.intent_id, actor="owner", reason="no")
    assert not decision.approved
    assert (await order_rig.book.get(held.intent.intent_id)).intent.state is IntentState.REJECTED


async def test_only_a_proposed_intent_is_decided(order_rig: Any) -> None:
    staged = await order_rig.staged()
    with pytest.raises(ExecutionError, match="only a proposed intent"):
        await order_rig.approvals.approve(staged.intent.intent_id, actor="owner", reason="x")


async def test_a_listed_sleeve_holds_its_intents(order_rig: Any) -> None:
    execution = order_rig.settings.execution
    settings = order_rig.settings.model_copy(
        update={
            "execution": execution.model_copy(
                update={
                    "approval": execution.approval.model_copy(update={"sleeves": [Sleeve.OPTIONS]})
                }
            )
        }
    )
    queue = ApprovalQueue(settings, order_rig.worker, order_rig.book, now=order_rig.clock)
    assert queue.required_for(Sleeve.OPTIONS)
    assert not queue.required_for(Sleeve.CORE)
    options = await order_rig.staged(sleeve=Sleeve.OPTIONS, state=IntentState.PROPOSED)
    await order_rig.staged(state=IntentState.PROPOSED)
    assert [s.intent.intent_id for s in await queue.pending()] == [options.intent.intent_id]


async def test_an_intent_with_a_recorded_decision_no_longer_waits(order_rig: Any) -> None:
    from obliskiq.domain.orders import ApprovalDecision

    held = await order_rig.staged(approval_required=True, state=IntentState.PROPOSED)
    decision = ApprovalDecision(
        intent_id=held.intent.intent_id,
        approved=True,
        actor="owner",
        reason="recorded elsewhere",
        at=order_rig.clock(),
    )
    await order_rig.store(repo.record_approval, decision)
    assert await order_rig.approvals.pending() == []
