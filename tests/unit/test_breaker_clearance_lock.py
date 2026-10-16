"""Attacks on the clearance lock (I1, SESSION_PROMPTS Session 4 item 4).

The rule: no placement or modify reaches the broker without a ``PretradeClearance`` the gate
recorded, bound to the intent, the action, the broker order and the exact terms, spent once and
before it expires. These tests race it (two coroutines, two order paths on one database, two
store threads), probe its expiry at the store's one-second resolution, change one term at a time,
forge copies, crash it between the spend and the send and restart it, and check that a write
cannot outlive the checks it passed (a warning confirmed after the kill switch or a downgrade).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import uuid
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from order_rig import CONID, START, build_rig, quote, terms
from pydantic import ValidationError

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.orders import (
    AlgoLimitPrice,
    AlgoParameter,
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    ModeChange,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    RiskEffect,
    TimeInForce,
)
from obliskiq.errors import StoreError, WriteRefusedError
from obliskiq.execution.killswitch import set_file_flag
from obliskiq.ibkr.errors import AccountGuardError
from obliskiq.ibkr.order_decode import Question
from obliskiq.ibkr.orders import PlacementResult, PlacementStatus, ReplyDecider
from obliskiq.store import open_database
from obliskiq.store import orders as repo
from obliskiq.store.worker import StoreWorker

# ── helpers ──


async def _state(rig: Any, intent_id: uuid.UUID) -> IntentState:
    return (await rig.book.get(intent_id)).intent.state


async def _working(
    rig: Any,
    order_terms: OrderTerms,
    *,
    role: IntentRole = IntentRole.EXIT,
    reason: str = "target",
    effect: RiskEffect = RiskEffect.REDUCES,
) -> tuple[Any, str]:
    """A stored intent whose order is working at the fake broker."""
    stored = await rig.intent(order_terms, role=role, reason=reason)
    result = await rig.boundary.place(
        stored.intent.intent_id, await rig.clear(stored.intent, effect=effect)
    )
    assert result.status is PlacementStatus.PLACED
    assert result.order_id is not None
    return stored, result.order_id


def _placed(results: list[PlacementResult | BaseException]) -> list[PlacementResult]:
    return [
        r for r in results if isinstance(r, PlacementResult) and r.status is PlacementStatus.PLACED
    ]


def _confirmed(rig: Any) -> list[str]:
    return [reply_id for reply_id, confirmed in rig.broker.replies if confirmed]


def _wrap_decider(rig: Any, before: Callable[[], None]) -> None:
    """Run ``before`` when the first warning arrives, then let the real policy decide."""
    original = rig.replies.decider

    def decider(clearance: PretradeClearance, now: Any) -> ReplyDecider:
        decide = original(clearance, now)

        def wrapped(question: Question) -> Any:
            before()
            return decide(question)

        return wrapped

    rig.replies.decider = decider


# ── races: one clearance, one intent, never two orders ──


async def test_one_clearance_raced_by_two_coroutines_places_once(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    results = await asyncio.gather(
        order_rig.boundary.place(stored.intent.intent_id, clearance),
        order_rig.boundary.place(stored.intent.intent_id, clearance),
        return_exceptions=True,
    )
    assert len(_placed(results)) == 1
    assert len(order_rig.broker.placements) == 1
    assert len(order_rig.broker.orders) == 1
    assert await _state(order_rig, stored.intent.intent_id) is IntentState.WORKING


async def test_two_clearances_for_one_intent_raced_place_once(order_rig: Any) -> None:
    stored = await order_rig.intent()
    first = await order_rig.clear(stored.intent)
    second = await order_rig.clear(stored.intent)
    results = await asyncio.gather(
        order_rig.boundary.place(stored.intent.intent_id, first),
        order_rig.boundary.place(stored.intent.intent_id, second),
        return_exceptions=True,
    )
    assert len(_placed(results)) == 1
    assert len(order_rig.broker.orders) == 1


@pytest.mark.parametrize("state", [IntentState.STAGED, IntentState.SUBMITTED])
@pytest.mark.parametrize("shared_clearance", [True, False])
async def test_two_order_paths_on_one_database_place_an_intent_once(
    order_rig: Any,
    make_session: Callable[..., Any],
    tmp_path: Path,
    state: IntentState,
    shared_clearance: bool,
) -> None:
    """Two processes (each its own store connection, order client and placement lock) race one
    intent; a submitted one models a retry after an unknown outcome."""
    other = await build_rig(
        settings=order_rig.settings,
        session=make_session(),
        broker=order_rig.broker,
        tmp_path=tmp_path,
    )
    try:
        stored = await order_rig.intent(state=state)
        first = await order_rig.clear(stored.intent)
        second = first if shared_clearance else await order_rig.clear(stored.intent)
        results = await asyncio.gather(
            order_rig.boundary.place(stored.intent.intent_id, first),
            other.boundary.place(stored.intent.intent_id, second),
            return_exceptions=True,
        )
        assert len(order_rig.broker.orders) == 1
        assert len(_placed(results)) <= 1
        for result in results:
            if isinstance(result, PlacementResult):
                assert result.status in (
                    PlacementStatus.PLACED,
                    PlacementStatus.ALREADY_PLACED,
                    PlacementStatus.DUPLICATE,
                )
    finally:
        await other.aclose()


async def test_two_store_threads_never_spend_one_clearance_twice(order_rig: Any) -> None:
    store_config = order_rig.settings.store
    second = StoreWorker(
        lambda: open_database(
            order_rig.db_path,
            busy_timeout_ms=store_config.busy_timeout_ms,
            synchronous=store_config.synchronous,
        )
    )
    try:
        stored = await order_rig.intent()
        clearances = [await order_rig.clear(stored.intent) for _ in range(25)]
        digest = stored.intent.terms.digest()

        async def spend(worker: StoreWorker, clearance: PretradeClearance) -> str | None:
            return await worker.run(
                repo.spend_clearance,
                clearance.clearance_id,
                intent_id=stored.intent.intent_id,
                action=ClearanceAction.PLACE,
                order_id=None,
                terms_digest=digest,
                now=order_rig.clock(),
            )

        outcomes = await asyncio.gather(
            *(spend(w, c) for c in clearances for w in (order_rig.worker, second))
        )
        for index in range(len(clearances)):
            pair = outcomes[2 * index : 2 * index + 2]
            assert pair.count(None) == 1, pair
            assert "the clearance was already used" in pair
    finally:
        await second.aclose()


# ── expiry at the store's one-second resolution ──


async def test_a_clearance_is_refused_at_the_exact_second_it_expires(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent, ttl_seconds=60)
    order_rig.clock.now = clearance.expires_at
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert refused.value.code == "clearance_refused"
    assert order_rig.broker.placements == []


async def test_a_clearance_is_refused_within_its_last_second(order_rig: Any) -> None:
    """Expiry 14:01:00.9, now 14:01:00.5: both stamps read 14:01:00, so the store refuses with
    0.4 s left. Early, never late: the truncation can only fail closed."""
    stored = await order_rig.intent()
    order_rig.clock.now = START + timedelta(milliseconds=900)
    clearance = await order_rig.clear(stored.intent, ttl_seconds=60)
    order_rig.clock.now = clearance.expires_at - timedelta(milliseconds=400)
    assert order_rig.clock.now < clearance.expires_at
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert refused.value.code == "clearance_refused"
    assert order_rig.broker.placements == []


async def test_a_clearance_one_second_before_it_expires_is_spent(order_rig: Any) -> None:
    """The positive control: the expiry checks do not refuse a clearance that is still valid."""
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent, ttl_seconds=60)
    order_rig.clock.now = clearance.expires_at - timedelta(seconds=1)
    result = await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert result.status is PlacementStatus.PLACED


async def test_a_clearance_shorter_than_the_stores_resolution_cannot_be_recorded(
    order_rig: Any,
) -> None:
    stored = await order_rig.intent()
    order_rig.clock.now = START + timedelta(milliseconds=100)
    clearance = await order_rig.clear(stored.intent, record=False)
    short = clearance.model_copy(
        update={"expires_at": clearance.issued_at + timedelta(milliseconds=500)}
    )
    with pytest.raises(StoreError):
        await order_rig.store(repo.record_clearance, short)


# ── terms: any change of what is sent needs its own clearance ──

_PATIENT = AlgoParameter(name="adaptivePriority", value="Patient")
_BASE = OrderTerms(
    conid=CONID,
    side=OrderSide.BUY,
    quantity=Decimal(10),
    price=AlgoLimitPrice(strategy="Adaptive", limit=Decimal(100), parameters=(_PATIENT,)),
    tif=TimeInForce.DAY,
    venue="SMART",
)


def _variant(**changes: Any) -> OrderTerms:
    return OrderTerms.model_validate({**_BASE.model_dump(), **changes})


def _algo(**changes: Any) -> AlgoLimitPrice:
    return AlgoLimitPrice.model_validate({**_BASE.price.model_dump(), **changes})


_CHANGED_TERMS = {
    "venue": _variant(venue="ISLAND"),
    "time in force": _variant(tif=TimeInForce.GTC),
    "immediate or cancel": _variant(tif=TimeInForce.IOC),
    "algo strategy": _variant(price=_algo(strategy="Vwap")),
    "algo parameter value": _variant(
        price=_algo(parameters=(AlgoParameter(name="adaptivePriority", value="Urgent"),))
    ),
    "algo parameter added": _variant(
        price=_algo(parameters=(_PATIENT, AlgoParameter(name="maxPctVol", value="0.1")))
    ),
    "algo parameters dropped": _variant(price=_algo(parameters=())),
    "algo dropped for a plain limit": _variant(price=LimitPrice(limit=Decimal(100))),
    "limit one tick higher": _variant(price=_algo(limit=Decimal("100.01"))),
    "quantity one ten-thousandth larger": _variant(quantity=Decimal("10.0001")),
    "units become money": _variant(quantity=None, cash_quantity=Decimal(10)),
    "another instrument": _variant(conid=CONID + 1),
    "the other side": _variant(side=OrderSide.SELL),
}


@pytest.mark.parametrize("changed", _CHANGED_TERMS.values(), ids=_CHANGED_TERMS.keys())
async def test_a_clearance_for_terms_that_differ_in_one_field_is_refused(
    order_rig: Any, changed: OrderTerms
) -> None:
    assert changed.digest() != _BASE.digest()
    stored = await order_rig.intent(_BASE)
    evidence = quote(at=order_rig.clock(), conid=changed.conid)
    clearance = await order_rig.clear(stored.intent, order_terms=changed, evidence=evidence)
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert refused.value.code == "clearance_refused"
    assert order_rig.broker.placements == []
    assert await _state(order_rig, stored.intent.intent_id) is IntentState.REJECTED


async def test_terms_spelled_differently_with_the_same_values_are_one_order(
    order_rig: Any,
) -> None:
    """The positive control of the digest: ``100.000`` is ``100`` and ``1E+1`` is ``10``; the
    order sent carries the values that were cleared."""
    respelled = _variant(quantity=Decimal("1E+1"), price=_algo(limit=Decimal("100.000")))
    assert respelled.digest() == _BASE.digest()
    stored = await order_rig.intent(_BASE)
    clearance = await order_rig.clear(stored.intent, order_terms=respelled)
    result = await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert result.status is PlacementStatus.PLACED
    sent = order_rig.broker.placements[0]
    assert (sent["price"], sent["quantity"]) == (100, 10)
    assert sent["strategyParameters"] == {"adaptivePriority": "Patient"}


# ── forged copies and misused clearances ──


async def test_a_forged_reducing_copy_of_a_buy_clearance_sends_nothing_with_the_switch_on(
    order_rig: Any,
) -> None:
    """``model_copy`` skips validation, so a buy can be dressed as reducing risk and skip the
    kill switch check; the ledger must still refuse it before anything is sent."""
    stored = await order_rig.intent()
    recorded = await order_rig.clear(stored.intent)
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    forged = recorded.model_copy(update={"effect": RiskEffect.REDUCES})
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, forged)
    assert refused.value.code in ("clearance_altered", "kill_switch_on")
    assert order_rig.broker.placements == []


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(
            lambda c, _other: c.model_copy(
                update={"expires_at": c.expires_at + timedelta(hours=1)}
            ),
            id="expiry pushed out",
        ),
        pytest.param(
            lambda c, other: c.model_copy(update={"clearance_id": other.clearance_id}),
            id="another clearance's id",
        ),
        pytest.param(
            lambda c, other: c.model_copy(update={"terms": other.terms}),
            id="another clearance's terms",
        ),
        pytest.param(
            lambda c, _other: c.model_copy(update={"asset_class": AssetClass.ETF}),
            id="another asset class",
        ),
        pytest.param(lambda c, _other: c.model_copy(update={"sigma": 9.0}), id="a wider sigma"),
    ],
)
async def test_a_forged_copy_is_refused(order_rig: Any, forge: Any) -> None:
    stored = await order_rig.intent()
    recorded = await order_rig.clear(stored.intent, ttl_seconds=30)
    other = await order_rig.clear(stored.intent, order_terms=terms(quantity="11"))
    order_rig.clock.advance(31)  # the recorded clearance has expired; the forged one claims not
    with pytest.raises(WriteRefusedError):
        await order_rig.boundary.place(stored.intent.intent_id, forge(recorded, other))
    assert order_rig.broker.placements == []


async def test_a_clearance_copied_across_a_process_boundary_is_still_spendable(
    order_rig: Any,
) -> None:
    """A JSON round trip is not a forgery: the positive control of the field-for-field check."""
    stored = await order_rig.intent()
    recorded = await order_rig.clear(stored.intent)
    copied = PretradeClearance.model_validate_json(recorded.model_dump_json())
    result = await order_rig.boundary.place(stored.intent.intent_id, copied)
    assert result.status is PlacementStatus.PLACED


async def test_a_modify_clearance_presented_for_a_placement_reads_nothing_and_sends_nothing(
    order_rig: Any,
) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent, action=ClearanceAction.MODIFY, order_id="1")
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert refused.value.code == "clearance_mismatch"
    assert order_rig.broker.placements == []
    assert order_rig.broker.live_order_reads == 0
    used = await order_rig.store(repo.get_clearance, clearance.clearance_id)
    assert used.used_at is None


async def test_a_modify_clearance_spent_for_one_order_cannot_modify_it_again(
    order_rig: Any,
) -> None:
    stored, order_id = await _working(order_rig, terms(side=OrderSide.SELL))
    smaller = terms(side=OrderSide.SELL, quantity="5")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=smaller,
    )
    await order_rig.boundary.modify(
        stored.intent.intent_id, order_id=order_id, terms=smaller, clearance=clearance
    )
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=order_id, terms=smaller, clearance=clearance
        )
    assert refused.value.code == "clearance_refused"
    assert len(order_rig.broker.modifies) == 1


@pytest.mark.parametrize("target", ["another intent's order", "an unmanaged order"])
async def test_a_modify_of_an_order_that_is_not_the_intents_is_refused(
    order_rig: Any, target: str
) -> None:
    mine, _ = await _working(order_rig, terms(side=OrderSide.SELL, quantity="4"))
    if target == "an unmanaged order":
        order_id = order_rig.broker.add_order(side="SELL", quantity="6").order_id
    else:
        _, order_id = await _working(order_rig, terms(side=OrderSide.SELL, quantity="6"))
    moved = terms(side=OrderSide.SELL, quantity="4", limit="101")
    clearance = await order_rig.clear(
        mine.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=moved,
    )
    with pytest.raises(WriteRefusedError):
        await order_rig.boundary.modify(
            mine.intent.intent_id, order_id=order_id, terms=moved, clearance=clearance
        )
    assert order_rig.broker.modifies == []


async def test_a_modify_cannot_change_units_into_money(order_rig: Any) -> None:
    """A sell cannot be sized in money at all (I2 counts units); a buy's modify keeps its kind
    of sizing."""
    with pytest.raises(ValidationError, match="sized in units"):
        terms(side=OrderSide.SELL, quantity=None, cash="1000")
    stored, order_id = await _working(
        order_rig,
        terms(quantity="10"),
        role=IntentRole.ENTRY,
        reason="entry",
        effect=RiskEffect.ADDS,
    )
    in_money = terms(quantity=None, cash="1000")
    clearance = await order_rig.clear(
        stored.intent,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=in_money,
    )
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=order_id, terms=in_money, clearance=clearance
        )
    assert refused.value.code == "modify_changes_order"
    assert order_rig.broker.modifies == []


# ── a crash between the spend and the send, then a restart ──


async def test_a_crash_after_the_spend_never_doubles_the_order_after_a_restart(
    order_rig: Any, make_session: Callable[..., Any], tmp_path: Path
) -> None:
    stored = await order_rig.intent()
    spent = await order_rig.clear(stored.intent)

    async def die(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the process died between the spend and the send")

    order_rig.client._submit = die
    with pytest.raises(RuntimeError):
        await order_rig.boundary.place(stored.intent.intent_id, spent)
    assert order_rig.broker.placements == []
    assert await _state(order_rig, stored.intent.intent_id) is IntentState.SUBMITTED

    restarted = await build_rig(
        settings=order_rig.settings,
        session=make_session(),
        broker=order_rig.broker,
        tmp_path=tmp_path,
    )
    try:
        with pytest.raises(WriteRefusedError) as refused:
            await restarted.boundary.place(stored.intent.intent_id, spent)
        assert refused.value.code == "clearance_refused"
        result = await restarted.boundary.place(
            stored.intent.intent_id, await restarted.clear(stored.intent)
        )
        assert result.status is PlacementStatus.PLACED
        assert len(order_rig.broker.placements) == 1
    finally:
        await restarted.aclose()


async def test_an_answer_lost_then_a_restart_finds_the_order_and_sends_nothing(
    order_rig: Any, make_session: Callable[..., Any], tmp_path: Path
) -> None:
    order_rig.broker.lose_next_answer = True
    stored = await order_rig.intent()
    first = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert first.status is PlacementStatus.UNKNOWN
    restarted = await build_rig(
        settings=order_rig.settings,
        session=make_session(),
        broker=order_rig.broker,
        tmp_path=tmp_path,
    )
    try:
        retry = await restarted.boundary.place(
            stored.intent.intent_id, await restarted.clear(stored.intent)
        )
        assert retry.status is PlacementStatus.ALREADY_PLACED
        assert len(order_rig.broker.placements) == 1
        assert await _state(restarted, stored.intent.intent_id) is IntentState.WORKING
    finally:
        await restarted.aclose()


# ── a warning confirmed after the checks it passed have changed ──


async def test_a_warning_is_not_confirmed_for_a_buy_once_the_kill_switch_is_on(
    order_rig: Any,
) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    order_rig.broker.questions = deque([(["o10153"], "price capped")])
    _wrap_decider(
        order_rig,
        lambda: set_file_flag(order_rig.data_dir, actor="owner", reason="drill", at=START),
    )
    await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert _confirmed(order_rig) == []
    assert order_rig.broker.live() == []


async def test_a_warning_is_not_confirmed_after_a_downgrade_to_shadow(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    order_rig.broker.questions = deque([(["o10153"], "price capped")])
    store_config = order_rig.settings.store

    def downgrade_elsewhere() -> None:
        store = open_database(
            order_rig.db_path,
            busy_timeout_ms=store_config.busy_timeout_ms,
            synchronous=store_config.synchronous,
        )
        try:
            repo.record_mode_change(
                store,
                ModeChange(
                    at=START,
                    from_mode=ExecutionMode.PAPER,
                    to_mode=ExecutionMode.SHADOW,
                    direction="downgrade",
                    actor="system",
                    reason="breaker in another process",
                ),
            )
        finally:
            store.close()

    _wrap_decider(order_rig, downgrade_elsewhere)
    await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert _confirmed(order_rig) == []
    assert order_rig.broker.live() == []


async def test_a_reducing_sells_warning_is_still_confirmed_with_the_kill_switch_on(
    order_rig: Any,
) -> None:
    """The positive control for the two tests above: the kill switch keeps the sells that
    reduce risk, so a fix must not decline their warnings."""
    stored = await order_rig.intent(
        terms(side=OrderSide.SELL), role=IntentRole.EXIT, reason="target"
    )
    clearance = await order_rig.clear(stored.intent, effect=RiskEffect.REDUCES)
    order_rig.broker.questions = deque([(["o10153"], "price capped")])
    _wrap_decider(
        order_rig,
        lambda: set_file_flag(order_rig.data_dir, actor="owner", reason="drill", at=START),
    )
    result = await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert result.status is PlacementStatus.PLACED
    assert len(_confirmed(order_rig)) == 1


async def test_the_paper_guard_is_checked_again_before_a_warning_is_confirmed(
    order_rig: Any,
) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    order_rig.broker.questions = deque([(["o10153"], "price capped")])
    _wrap_decider(
        order_rig, lambda: object.__setattr__(order_rig.guard, "account_id", "U_LIVE_TEST")
    )
    with pytest.raises((AccountGuardError, WriteRefusedError)):
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert order_rig.broker.replies == []
    assert order_rig.broker.live() == []


async def test_a_decider_that_raises_confirms_nothing(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    order_rig.broker.questions = deque([(["o10153"], "price capped")])

    def broken() -> None:
        raise ZeroDivisionError("a bug in the policy")

    _wrap_decider(order_rig, broken)
    with pytest.raises(ZeroDivisionError):
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert _confirmed(order_rig) == []
    assert order_rig.broker.live() == []
