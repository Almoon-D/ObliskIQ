"""Attacks on the modes and the paper guard at the write boundary.

The rule (MASTER_PLAN §5.19, DECISIONS D51, AGENTS.md): shadow never writes; paper writes only
under profile paper to an account with a paper prefix, re-checked before every write; the mode is
read again immediately before the send, so a downgrade by another process stops the next write;
cancel is allowed in every mode, the paper guard alone applying. Accounts that only look like a
paper account are tried on every kind of write, and the mode is changed between the boundary's
first check and its last.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Any

import pytest
from order_rig import terms

from obliskiq.domain.orders import (
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    OrderSide,
    RiskEffect,
)
from obliskiq.errors import WriteRefusedError
from obliskiq.ibkr.orders import PlacementStatus

_NOT_PAPER = ["U1234567", "du1234567", " DU1234567", "XDU1234567", "D-U1234567", "F1234567", "UDU1"]


async def _working_sell(rig: Any) -> tuple[Any, str]:
    stored = await rig.intent(terms(side=OrderSide.SELL), role=IntentRole.EXIT, reason="target")
    result = await rig.boundary.place(
        stored.intent.intent_id, await rig.clear(stored.intent, effect=RiskEffect.REDUCES)
    )
    assert result.status is PlacementStatus.PLACED
    return stored, result.order_id


@pytest.mark.parametrize("account", _NOT_PAPER)
async def test_an_account_without_a_paper_prefix_gets_no_write_of_any_kind(
    order_rig: Any, account: str
) -> None:
    stored, order_id = await _working_sell(order_rig)
    fresh = await order_rig.intent()
    place_clearance = await order_rig.clear(fresh.intent)
    smaller = terms(side=OrderSide.SELL, quantity="5")
    modify_clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=smaller,
    )
    placements = len(order_rig.broker.placements)
    object.__setattr__(order_rig.guard, "account_id", account)
    with pytest.raises(WriteRefusedError) as placing:
        await order_rig.boundary.place(fresh.intent.intent_id, place_clearance)
    with pytest.raises(WriteRefusedError) as modifying:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=order_id, terms=smaller, clearance=modify_clearance
        )
    with pytest.raises(WriteRefusedError) as cancelling:
        await order_rig.boundary.cancel(order_id)
    assert {placing.value.code, modifying.value.code, cancelling.value.code} == {"paper_guard"}
    assert len(order_rig.broker.placements) == placements
    assert order_rig.broker.modifies == []
    assert order_rig.broker.cancels == []


async def test_a_paper_account_gets_every_write(order_rig: Any) -> None:
    """The positive control for the refusals above."""
    stored, order_id = await _working_sell(order_rig)
    smaller = terms(side=OrderSide.SELL, quantity="5")
    modified = await order_rig.boundary.modify(
        stored.intent.intent_id,
        order_id=order_id,
        terms=smaller,
        clearance=await order_rig.clear(
            stored.intent,
            effect=RiskEffect.REDUCES,
            action=ClearanceAction.MODIFY,
            order_id=order_id,
            order_terms=smaller,
        ),
    )
    assert modified.status is PlacementStatus.PLACED
    assert (await order_rig.boundary.cancel(order_id)).error is None


async def test_shadow_refuses_a_modify_even_one_that_reduces_risk(order_rig: Any) -> None:
    """Shadow never writes, so a protective stop cannot be resized or restored there; only a
    cancel still reaches the broker (Session 4a's exits must plan around it)."""
    stored, order_id = await _working_sell(order_rig)
    await order_rig.modes.downgrade(ExecutionMode.SHADOW, actor="system", reason="breaker")
    smaller = terms(side=OrderSide.SELL, quantity="5")
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id,
            order_id=order_id,
            terms=smaller,
            clearance=await order_rig.clear(
                stored.intent,
                effect=RiskEffect.REDUCES,
                action=ClearanceAction.MODIFY,
                order_id=order_id,
                order_terms=smaller,
            ),
        )
    assert refused.value.code == "mode_forbids_write"
    assert order_rig.broker.modifies == []
    assert (await order_rig.boundary.cancel(order_id)).error is None


async def test_a_modify_reads_the_mode_again_immediately_before_the_send(order_rig: Any) -> None:
    stored, order_id = await _working_sell(order_rig)
    smaller = terms(side=OrderSide.SELL, quantity="5")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=smaller,
    )
    reads: list[ExecutionMode] = []

    async def downgraded_after_the_first_read() -> ExecutionMode:
        reads.append(ExecutionMode.PAPER if not reads else ExecutionMode.SHADOW)
        return reads[-1]

    order_rig.modes.current = downgraded_after_the_first_read
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=order_id, terms=smaller, clearance=clearance
        )
    assert refused.value.code == "mode_forbids_write"
    assert len(reads) == 2
    assert order_rig.broker.modifies == []


@pytest.mark.parametrize("mode", [ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE])
async def test_a_live_level_under_the_paper_profile_writes_nothing(
    order_rig: Any, mode: ExecutionMode
) -> None:
    """A live mode that reached the store some other way still writes nothing under paper."""
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)

    async def live() -> ExecutionMode:
        return mode

    order_rig.modes.current = live
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert refused.value.code == "mode_forbids_write"
    assert order_rig.broker.placements == []
