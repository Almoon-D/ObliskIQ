"""The order client on the stateful fake broker: placement, warnings, refusals, the cOID lookup,
modify, cancel, whatif and the order body.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from collections import deque
from decimal import Decimal
from typing import Any

import httpx
import pytest
from order_rig import CONID, quote, terms

from obliskiq.domain.enums import MarketDataStatus
from obliskiq.domain.orders import (
    AlgoLimitPrice,
    AlgoParameter,
    ClearanceAction,
    IntentRole,
    OrderSide,
    RiskEffect,
)
from obliskiq.errors import WriteRefusedError
from obliskiq.ibkr.errors import AccountGuardError, LookupIncompleteError
from obliskiq.ibkr.order_decode import Question
from obliskiq.ibkr.orders import (
    LIMIT_CAPPED_ORDER_TYPES,
    PlacementStatus,
    ReplyVerdict,
    coid_for,
    order_body,
)


async def _ok() -> None:
    return None


def _confirm_all(question: Question) -> ReplyVerdict:
    return ReplyVerdict(True, "test confirms")


def _decline_all(question: Question) -> ReplyVerdict:
    return ReplyVerdict(False, "test declines")


async def _place(rig: Any, stored: Any, clearance: Any, decide: Any = _confirm_all) -> Any:
    return await rig.client.place_idempotent(
        coid=stored.coid,
        terms=stored.intent.terms,
        clearance=clearance,
        decide=decide,
        last_check=_ok,
    )


# ── the order body ──


def test_every_price_policy_builds_a_limit_capped_order_type() -> None:
    algo = terms().model_copy(
        update={
            "price": AlgoLimitPrice(
                strategy="Adaptive",
                limit=Decimal("101"),
                parameters=(AlgoParameter(name="adaptivePriority", value="Patient"),),
            )
        }
    )
    for order in (terms(), terms(side=OrderSide.SELL, stop="95", limit="94"), algo):
        body = order_body(order, coid="c", account_id="DU1")
        assert body["orderType"] in LIMIT_CAPPED_ORDER_TYPES
        assert "price" in body
    stop = order_body(terms(side=OrderSide.SELL, stop="95", limit="94"), coid="c", account_id="DU1")
    assert (stop["orderType"], stop["price"], stop["auxPrice"]) == ("STOP_LIMIT", 94, 95)
    capped = order_body(algo, coid="c", account_id="DU1")
    assert capped["strategy"] == "Adaptive"
    assert capped["strategyParameters"] == {"adaptivePriority": "Patient"}


def test_the_body_carries_the_coid_account_and_routing() -> None:
    smart = order_body(terms(quantity="10.5", limit="100.25"), coid="abc", account_id="DU1")
    assert smart["cOID"] == "abc"
    assert smart["acctId"] == "DU1"
    assert smart["conid"] == CONID
    assert "conidex" not in smart
    assert smart["quantity"] == 10.5
    assert smart["price"] == 100.25
    assert smart["outsideRTH"] is False
    routed = order_body(terms(venue="IBIS"), coid="abc", account_id="DU1")
    assert routed["conidex"] == f"{CONID}@IBIS"
    assert "conid" not in routed


def test_a_cash_quantity_and_a_conversion_use_their_own_fields() -> None:
    cash = order_body(terms(cash="2000"), coid="c", account_id="DU1")
    assert cash["cashQty"] == 2000
    assert "quantity" not in cash
    conversion = order_body(
        terms(cash="5000", conversion=True, conid=12087792), coid="c", account_id="DU1"
    )
    assert conversion["isCcyConv"] is True
    assert conversion["fxQty"] == 5000
    assert "cashQty" not in conversion
    assert "quantity" not in conversion


def test_the_coid_is_the_intent_id_or_a_deterministic_short_form() -> None:
    intent_id = uuid.uuid7()
    assert coid_for(intent_id, 36) == str(intent_id)
    short = coid_for(intent_id, 26)
    assert len(short) == 26
    assert short == coid_for(intent_id, 26)
    assert coid_for(intent_id, 16) == short[:16]
    assert coid_for(uuid.uuid7(), 26) != short


# ── placement ──


async def test_a_placement_is_looked_up_then_sent_once(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    result = await _place(order_rig, stored, clearance)
    assert result.status is PlacementStatus.PLACED
    assert result.order_id is not None
    assert len(order_rig.broker.placements) == 1
    assert order_rig.broker.placements[0]["cOID"] == stored.coid
    assert order_rig.broker.live_order_reads == 1
    assert order_rig.broker.trade_reads == 1


async def test_a_found_coid_is_not_placed_again_and_keeps_its_clearance(order_rig: Any) -> None:
    stored = await order_rig.intent()
    order_rig.broker.add_order(side="BUY", coid=stored.coid)
    clearance = await order_rig.clear(stored.intent)
    result = await _place(order_rig, stored, clearance)
    assert result.status is PlacementStatus.ALREADY_PLACED
    assert order_rig.broker.placements == []
    again = await _place(order_rig, stored, clearance)  # unspent: the lookup came first
    assert again.status is PlacementStatus.ALREADY_PLACED


async def test_a_coid_found_only_among_trades_is_already_placed(order_rig: Any) -> None:
    stored = await order_rig.intent()
    order = order_rig.broker.add_order(side="BUY", coid=stored.coid)
    order_rig.broker.fill(order.order_id)
    del order_rig.broker.orders[order.order_id]  # gone from live orders, a day later
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert result.status is PlacementStatus.ALREADY_PLACED
    assert result.lookup is not None
    assert len(result.lookup.fills) == 1
    assert order_rig.broker.placements == []


async def test_an_incomplete_live_orders_list_refuses_before_sending(order_rig: Any) -> None:
    order_rig.broker.snapshot = False
    stored = await order_rig.intent()
    with pytest.raises(LookupIncompleteError):
        await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert order_rig.broker.placements == []


async def test_the_last_check_runs_after_the_lookup_and_can_stop_the_send(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)

    async def refuse() -> None:
        assert order_rig.broker.live_order_reads == 1  # after the lookup
        raise WriteRefusedError("kill_switch_on", "test")

    with pytest.raises(WriteRefusedError):
        await order_rig.client.place_idempotent(
            coid=stored.coid,
            terms=stored.intent.terms,
            clearance=clearance,
            decide=_confirm_all,
            last_check=refuse,
        )
    assert order_rig.broker.placements == []


async def test_a_clearance_for_another_intent_or_action_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    other = await order_rig.intent()
    for clearance in (
        await order_rig.clear(other.intent),
        await order_rig.clear(stored.intent, action=ClearanceAction.MODIFY, order_id="1"),
    ):
        with pytest.raises(WriteRefusedError) as refused:
            await _place(order_rig, stored, clearance)
        assert refused.value.code == "clearance_mismatch"
    assert order_rig.broker.live_order_reads == 0


async def test_the_paper_guard_refuses_a_live_account(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    object.__setattr__(order_rig.guard, "account_id", "U1234567")
    with pytest.raises(AccountGuardError):
        await _place(order_rig, stored, clearance)
    with pytest.raises(AccountGuardError):
        await order_rig.client.cancel("1")
    assert order_rig.broker.placements == []
    assert order_rig.broker.cancels == []


async def test_a_refusal_is_rejected(order_rig: Any) -> None:
    order_rig.broker.reject_next = "We cannot accept an order at the limit price you selected."
    stored = await order_rig.intent()
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert result.status is PlacementStatus.REJECTED
    assert "limit price" in result.detail


async def test_a_reused_coid_is_reported_as_a_duplicate(order_rig: Any) -> None:
    stored = await order_rig.intent()
    order_rig.broker._used_coids.add(stored.coid)  # used earlier today, not listed
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert result.status is PlacementStatus.DUPLICATE


async def test_an_answer_lost_after_the_order_was_taken_is_unknown(order_rig: Any) -> None:
    order_rig.broker.lose_next_answer = True
    stored = await order_rig.intent()
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert result.status is PlacementStatus.UNKNOWN
    assert len(order_rig.broker.placements) == 1  # sent once, never repeated by the session


async def test_crash_between_place_and_status_never_places_twice(order_rig: Any) -> None:
    order_rig.broker.lose_next_answer = True
    stored = await order_rig.intent()
    first = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert first.status is PlacementStatus.UNKNOWN
    # The process restarts: a fresh clearance, and the lookup finds the order.
    second = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert second.status is PlacementStatus.ALREADY_PLACED
    assert len(order_rig.broker.placements) == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, PlacementStatus.NOT_SENT),
        (429, PlacementStatus.NOT_SENT),
        (400, PlacementStatus.REJECTED),
        (403, PlacementStatus.REJECTED),
        (500, PlacementStatus.UNKNOWN),
        (503, PlacementStatus.UNKNOWN),
    ],
)
async def test_http_failures_of_a_placement(order_rig: Any, status: int, expected: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    order_rig.broker.respond_next = httpx.Response(status, json={"error": "nope"})
    result = await _place(order_rig, stored, clearance)
    assert result.status is expected
    assert len(order_rig.broker.placements) == 1  # never repeated by the session


async def test_a_body_that_is_not_json_is_unknown(order_rig: Any) -> None:
    stored = await order_rig.intent()
    order_rig.broker.respond_next = httpx.Response(200, text="<html>")
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert result.status is PlacementStatus.UNKNOWN


@pytest.mark.parametrize(
    "body",
    [{"surprise": 1}, [], [{"a": 1}, {"b": 2}], [{"neither": "shape"}], [{"error": "item error"}]],
)
async def test_answers_of_no_known_shape(order_rig: Any, body: Any) -> None:
    stored = await order_rig.intent()
    order_rig.broker.respond_next = httpx.Response(200, json=body)
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    expected = (
        PlacementStatus.REJECTED if body == [{"error": "item error"}] else PlacementStatus.UNKNOWN
    )
    assert result.status is expected


# ── warnings ──


async def test_confirmed_warnings_are_answered_until_the_order_is_taken(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o10153"], "cap"), (["o10331"], "stop")])
    stored = await order_rig.intent()
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert result.status is PlacementStatus.PLACED
    assert [r.message_ids for r in result.replies] == [("o10153",), ("o10331",)]
    assert [confirmed for _, confirmed in order_rig.broker.replies] == [True, True]


async def test_a_declined_warning_declines_and_cancels_what_is_listed(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o999"], "unknown")])
    order_rig.broker.keep_declined = True  # the broker still lists it: it is cancelled
    stored = await order_rig.intent()
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent), _decline_all)
    assert result.status is PlacementStatus.DECLINED
    assert order_rig.broker.replies[-1][1] is False
    assert len(order_rig.broker.cancels) == 1
    assert "cancelled 1 listed order" in result.detail


async def test_a_declined_warning_with_nothing_listed(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o999"], "unknown")])
    stored = await order_rig.intent()
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent), _decline_all)
    assert result.status is PlacementStatus.DECLINED
    assert "nothing is listed" in result.detail
    assert order_rig.broker.cancels == []


async def test_more_warning_rounds_than_allowed_are_declined(order_rig: Any) -> None:
    rounds = order_rig.settings.execution.orders.max_reply_rounds
    order_rig.broker.questions = deque([(["o10153"], "again")] * (rounds + 1))
    stored = await order_rig.intent()
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent))
    assert result.status is PlacementStatus.DECLINED
    assert len(result.replies) == rounds + 1
    assert result.replies[-1].confirmed is False


async def test_a_lapsed_reply_is_unknown(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o10153"], "cap")])
    stored = await order_rig.intent()

    def lapse(question: Question) -> ReplyVerdict:
        order_rig.broker._pending.clear()  # another request invalidated it
        return ReplyVerdict(True, "confirm")

    result = await _place(order_rig, stored, await order_rig.clear(stored.intent), lapse)
    assert result.status is PlacementStatus.UNKNOWN


# ── modify, cancel, reads ──


async def test_modify_spends_a_modify_clearance_for_the_new_terms(order_rig: Any) -> None:
    stored = await order_rig.intent(
        terms(side=OrderSide.SELL, stop="95", limit="94"), role=IntentRole.PROTECTIVE, reason="stop"
    )
    placed = await _place(
        order_rig, stored, await order_rig.clear(stored.intent, effect=RiskEffect.REDUCES)
    )
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=placed.order_id,
        order_terms=moved,
    )
    result = await order_rig.client.modify(
        order_id=placed.order_id,
        coid=stored.coid,
        terms=moved,
        clearance=clearance,
        decide=_confirm_all,
        last_check=_ok,
    )
    assert result.status is PlacementStatus.PLACED
    order = order_rig.broker.orders[placed.order_id]
    assert (order.aux_price, order.price) == (Decimal(97), Decimal(96))


async def test_a_declined_modify_leaves_the_order_standing(order_rig: Any) -> None:
    stored = await order_rig.intent(
        terms(side=OrderSide.SELL, stop="95", limit="94"), role=IntentRole.PROTECTIVE, reason="stop"
    )
    placed = await _place(
        order_rig, stored, await order_rig.clear(stored.intent, effect=RiskEffect.REDUCES)
    )
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    order_rig.broker.questions = deque([(["o0"], "stop order risk")])
    result = await order_rig.client.modify(
        order_id=placed.order_id,
        coid=stored.coid,
        terms=moved,
        clearance=await order_rig.clear(
            stored.intent,
            effect=RiskEffect.REDUCES,
            action=ClearanceAction.MODIFY,
            order_id=placed.order_id,
            order_terms=moved,
        ),
        decide=_decline_all,
        last_check=_ok,
    )
    assert result.status is PlacementStatus.DECLINED
    assert order_rig.broker.cancels == []
    assert order_rig.broker.orders[placed.order_id].status == "Submitted"


async def test_cancel_and_status(order_rig: Any) -> None:
    order = order_rig.broker.add_order(side="BUY")
    answer = await order_rig.client.cancel(order.order_id)
    assert answer.error is None
    assert (await order_rig.client.status(order.order_id)).status == "Cancelled"
    missing = await order_rig.client.cancel("999999")
    assert missing.error is not None


async def test_whatif_keeps_its_warning_and_code(order_rig: Any) -> None:
    order_rig.broker.whatif_answer = dict(
        order_rig.broker.whatif_answer,
        warn="21/You are trying to submit an order without having market data for this instrument.",
    )
    preview = await order_rig.client.whatif(terms(), coid="preview")
    assert preview.warning_code == "21"
    assert preview.warning is not None
    assert preview.warning.startswith("You are trying")
    assert preview.commission == Decimal(1)
    assert preview.total == Decimal("1978.60")
    assert preview.error is None
    assert order_rig.broker.placements == []


async def test_reset_suppressed_messages(order_rig: Any) -> None:
    await order_rig.client.reset_suppressed()
    assert order_rig.broker.suppress_resets == 1


async def test_the_reply_policy_answers_a_real_no_market_data_warning(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o354"], "no market data")])
    stored = await order_rig.intent()
    delayed = quote(status=MarketDataStatus.DELAYED, delay=1200, at=order_rig.clock())
    clearance = await order_rig.clear(stored.intent, evidence=delayed)
    decide = order_rig.replies.decider(clearance, order_rig.clock)
    result = await _place(order_rig, stored, clearance, decide)
    assert result.status is PlacementStatus.PLACED
    assert result.replies[0].confirmed is True


async def test_a_decline_that_cannot_be_sent_still_looks_the_coid_up(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o999"], "unknown")])
    stored = await order_rig.intent()

    def lapse_and_decline(question: Question) -> ReplyVerdict:
        order_rig.broker._pending.clear()  # the decline will answer 503
        return ReplyVerdict(False, "unknown warning")

    result = await _place(
        order_rig, stored, await order_rig.clear(stored.intent), lapse_and_decline
    )
    assert result.status is PlacementStatus.DECLINED
    assert "nothing is listed" in result.detail


async def test_a_decline_whose_lookup_fails_says_so(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o999"], "unknown")])
    stored = await order_rig.intent()

    def decline_then_break(question: Question) -> ReplyVerdict:
        order_rig.broker.snapshot = False
        return ReplyVerdict(False, "unknown warning")

    result = await _place(
        order_rig, stored, await order_rig.clear(stored.intent), decline_then_break
    )
    assert result.status is PlacementStatus.DECLINED
    assert "could not be looked up" in result.detail


@pytest.mark.parametrize("failure", ["503", "OrderID is locked"])
async def test_a_decline_whose_cancel_fails_says_so(order_rig: Any, failure: str) -> None:
    order_rig.broker.questions = deque([(["o999"], "unknown")])
    order_rig.broker.keep_declined = True
    order_rig.broker.cancel_failure = failure
    stored = await order_rig.intent()
    result = await _place(order_rig, stored, await order_rig.clear(stored.intent), _decline_all)
    assert result.status is PlacementStatus.DECLINED
    assert "cancelling the listed order failed" in result.detail
