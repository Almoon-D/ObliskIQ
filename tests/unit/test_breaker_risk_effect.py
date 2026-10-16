"""Attacks on class by effect, never by role (I3) at the write boundary, with the kill switch ON.

The rule (SESSION_PROMPTS Session 4 item 4, DECISIONS D44): a buy or a conversion always adds
risk, whatever its role; only a sell the gate cleared as reducing risk passes the kill switch.
Every role, side and effect is tried for a placement and for a modify; the combinations the rule
forbids must not even be expressible as a clearance, in the model or in the store. The intents
are stored directly, not through ``new_intent``: the boundary must class by effect whatever the
intent's role, however the intent reached the store.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
import uuid
from datetime import timedelta
from typing import Any

import pytest
from order_rig import START, quote, terms
from pydantic import ValidationError

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.orders import (
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    RiskEffect,
    Sleeve,
)
from obliskiq.errors import StoreError, WriteRefusedError
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.util.timeutil import utc_stamp

_FX_CONID = 12087792
_REASONS = {
    IntentRole.ENTRY: "entry",
    IntentRole.PROTECTIVE: "stop",
    IntentRole.EXIT: "target",
    IntentRole.CONVERSION: "fund a trade",
}


def _terms_for(role: IntentRole, side: OrderSide, quantity: str = "10") -> OrderTerms:
    if role is IntentRole.CONVERSION:
        return terms(side=side, cash=str(int(quantity) * 500), conversion=True, conid=_FX_CONID)
    return terms(side=side, quantity=quantity)


async def _stored(rig: Any, role: IntentRole, side: OrderSide) -> Any:
    """A staged intent of ``role`` and ``side``, stored past the intent discipline."""
    intent = OrderIntent(
        intent_id=uuid.uuid7(),
        sleeve=Sleeve.CORE,
        role=role,
        terms=_terms_for(role, side),
        reason=_REASONS[role],
        mode=ExecutionMode.PAPER,
        approval_required=False,
        created_at=rig.clock(),
        expires_at=rig.clock() + timedelta(hours=6),
    )
    stored = await rig.book.add(intent)
    for step in (IntentState.APPROVED, IntentState.STAGED):
        await rig.book.advance(intent.intent_id, step, "test path")
    return stored


def _reduces_allowed(side: OrderSide, role: IntentRole) -> bool:
    return side is OrderSide.SELL and role is not IntentRole.CONVERSION


_TABLE = [
    pytest.param(role, side, effect, id=f"{role.value}-{side.value}-{effect.value}")
    for role in IntentRole
    for side in OrderSide
    for effect in RiskEffect
]
#: The combinations a clearance can express (the placement table proves the others cannot be).
_EXPRESSIBLE = [
    pytest.param(role, side, effect, id=f"{role.value}-{side.value}-{effect.value}")
    for role in IntentRole
    for side in OrderSide
    for effect in RiskEffect
    if effect is RiskEffect.ADDS or _reduces_allowed(side, role)
]


@pytest.mark.parametrize(("role", "side", "effect"), _TABLE)
async def test_with_the_switch_on_only_a_sell_cleared_as_reducing_risk_reaches_the_broker(
    order_rig: Any, role: IntentRole, side: OrderSide, effect: RiskEffect
) -> None:
    placed = await _stored(order_rig, role, side)
    passes = effect is RiskEffect.REDUCES
    if passes and not _reduces_allowed(side, role):
        with pytest.raises(ValidationError):
            await order_rig.clear(placed.intent, effect=effect, record=False)
        return
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    clearance = await order_rig.clear(placed.intent, effect=effect)
    if passes:
        result = await order_rig.boundary.place(placed.intent.intent_id, clearance)
        assert result.status is PlacementStatus.PLACED
    else:
        with pytest.raises(WriteRefusedError) as refused:
            await order_rig.boundary.place(placed.intent.intent_id, clearance)
        assert refused.value.code == "kill_switch_on"
        assert order_rig.broker.placements == []


@pytest.mark.parametrize(("role", "side", "effect"), _EXPRESSIBLE)
async def test_with_the_switch_on_only_a_modify_cleared_as_reducing_risk_reaches_the_broker(
    order_rig: Any, role: IntentRole, side: OrderSide, effect: RiskEffect
) -> None:
    stored = await _stored(order_rig, role, side)
    placed = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent, effect=effect)
    )
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    smaller = _terms_for(role, side, quantity="5")
    clearance = await order_rig.clear(
        stored.intent,
        effect=effect,
        action=ClearanceAction.MODIFY,
        order_id=placed.order_id,
        order_terms=smaller,
    )
    if effect is RiskEffect.REDUCES:
        result = await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=placed.order_id, terms=smaller, clearance=clearance
        )
        assert result.status is PlacementStatus.PLACED
        assert len(order_rig.broker.modifies) == 1
    else:
        with pytest.raises(WriteRefusedError) as refused:
            await order_rig.boundary.modify(
                stored.intent.intent_id,
                order_id=placed.order_id,
                terms=smaller,
                clearance=clearance,
            )
        assert refused.value.code == "kill_switch_on"
        assert order_rig.broker.modifies == []


@pytest.mark.parametrize(
    ("side", "conversion", "refused_by"),
    [
        (OrderSide.BUY, False, "the table"),
        (OrderSide.BUY, True, "the table"),
        (OrderSide.SELL, True, "the read-back"),
    ],
)
async def test_a_reducing_row_written_past_the_model_is_never_spent(
    order_rig: Any, side: OrderSide, conversion: bool, refused_by: str
) -> None:
    """A row written past the model (another writer, a hand edit): the table's CHECK refuses a
    reducing buy, and the read-back refuses a reducing conversion, so neither is ever spent."""
    role = IntentRole.CONVERSION if conversion else IntentRole.ENTRY
    stored = await order_rig.intent(_terms_for(role, side), role=role, reason=_REASONS[role])
    honest = await order_rig.clear(stored.intent, record=False)
    forged = honest.model_copy(update={"effect": RiskEffect.REDUCES})

    def write_past_the_model(store: Any) -> None:
        terms_ = forged.terms
        with store.transaction():
            store.execute(
                "INSERT INTO clearances (clearance_id, intent_id, action, order_id, conid, "
                "side, quantity, terms_digest, effect, clearance, issued_at, expires_at) "
                "VALUES (?, ?, 'place', NULL, ?, ?, ?, ?, 'reduces_risk', ?, ?, ?)",
                (
                    str(forged.clearance_id),
                    str(forged.intent_id),
                    terms_.conid,
                    terms_.side.value,
                    None if terms_.quantity is None else str(terms_.quantity),
                    terms_.digest(),
                    forged.model_dump_json(),
                    utc_stamp(forged.issued_at),
                    utc_stamp(forged.expires_at),
                ),
            )

    if refused_by == "the table":
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            await order_rig.store(write_past_the_model)
    else:
        await order_rig.store(write_past_the_model)
        await order_rig.kill.turn_on(actor="owner", reason="drill")
        with pytest.raises((WriteRefusedError, StoreError)):
            await order_rig.boundary.place(stored.intent.intent_id, forged)
    assert order_rig.broker.placements == []


def test_a_reducing_clearance_needs_a_plain_sell_in_the_model() -> None:
    for side, conversion in ((OrderSide.BUY, False), (OrderSide.BUY, True), (OrderSide.SELL, True)):
        order_terms = (
            terms(side=side, cash="5000", conversion=True, conid=_FX_CONID)
            if conversion
            else terms(side=side)
        )
        with pytest.raises(ValidationError):
            PretradeClearance(
                clearance_id=uuid.uuid4(),
                intent_id=uuid.uuid7(),
                action=ClearanceAction.PLACE,
                terms=order_terms,
                effect=RiskEffect.REDUCES,
                asset_class=AssetClass.STOCK,
                quote=quote(at=START, conid=order_terms.conid),
                issued_at=START,
                expires_at=START + timedelta(minutes=1),
            )
