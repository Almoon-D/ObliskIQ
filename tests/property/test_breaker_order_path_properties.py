"""Properties that attack the order path's money rules over generated inputs.

* I1, the digest: two terms hash alike exactly when they are the same order, whatever the
  spelling of their amounts, and one digest never covers two different order bodies.
* I1, the spend: a clearance is spent at most once, only for its own intent, action, broker
  order and terms, never at or after its expiry (instants with microseconds against the store's
  second-resolution stamps), and a refusal changes nothing.
* I3: only a sell that is not a conversion can be cleared as reducing risk, and everything else
  adds risk.
* I4: the sweep's verdict on any live order, for any store knowledge or a store that is down,
  never keeps a buy, a conversion or an unreadable side, nor what the store knows to add risk.
* State machine: every accepted move or fact appends exactly one event, every refused one none;
  moves only go forward and terminal states are never left.
* Reply policy: ``o354`` is confirmed only when every condition of D49 holds.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st
from order_rig import CONID, terms
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_execution import RealtimePreferred
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import (
    TERMINAL_STATES,
    AlgoLimitPrice,
    AlgoParameter,
    BrokerOrder,
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderIntent,
    OrderSide,
    OrderTerms,
    PretradeClearance,
    QuoteEvidence,
    RiskEffect,
    Sleeve,
    StopLimitPrice,
    TimeInForce,
)
from obliskiq.errors import IllegalTransitionError, StoreError
from obliskiq.execution.boundary import adds_risk
from obliskiq.execution.killswitch import KillSwitch
from obliskiq.execution.quotes import QuotePolicy
from obliskiq.execution.replies import ReplyPolicy
from obliskiq.execution.state import BROKER_STATES, IntentBook, can_transition
from obliskiq.ibkr.order_decode import Question
from obliskiq.ibkr.orders import order_body
from obliskiq.store import open_database
from obliskiq.store import orders as repo
from obliskiq.store.orders import StoredIntent
from obliskiq.store.worker import StoreWorker
from obliskiq.util.timeutil import utc_stamp

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
FX_CONID = 12087792

#: The order of the states along the plan's chain; terminal states share the last rank.
_RANK = {
    IntentState.PROPOSED: 0,
    IntentState.APPROVED: 1,
    IntentState.STAGED: 2,
    IntentState.SUBMITTED: 3,
    IntentState.WORKING: 4,
    IntentState.PARTIALLY_FILLED: 5,
    **dict.fromkeys(TERMINAL_STATES, 6),
}


def _intent(order_terms: OrderTerms | None = None, role: IntentRole = IntentRole.ENTRY) -> Any:
    return OrderIntent(
        intent_id=uuid.uuid7(),
        sleeve=Sleeve.CORE,
        role=role,
        terms=order_terms if order_terms is not None else terms(),
        reason="entry",
        mode=ExecutionMode.PAPER,
        approval_required=False,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=6),
    )


def _memory_store() -> Any:
    return open_database(":memory:", busy_timeout_ms=1000, synchronous="NORMAL")


# ── I1: the digest ──

_AMOUNT = st.sampled_from(["1", "1.0", "1.00", "2", "2.5", "2.50", "100", "1E+2"]).map(Decimal)
_PARAMETER = st.builds(
    AlgoParameter, name=st.sampled_from(["a", "b"]), value=st.sampled_from(["1", "2"])
)


@st.composite
def _order_terms(draw: st.DrawFn) -> OrderTerms:
    kind = draw(st.sampled_from(["limit", "stop_limit", "algo"]))
    limit = draw(_AMOUNT)
    price: Any
    if kind == "limit":
        price = LimitPrice(limit=limit)
    elif kind == "stop_limit":
        price = StopLimitPrice(stop=draw(_AMOUNT), limit=limit)
    else:
        price = AlgoLimitPrice(
            strategy=draw(st.sampled_from(["Adaptive", "Vwap"])),
            limit=limit,
            parameters=tuple(draw(st.lists(_PARAMETER, max_size=2))),
        )
    conversion = kind == "limit" and draw(st.booleans())
    side = draw(st.sampled_from(OrderSide))
    # A sell is sized in units unless it is a conversion (the domain refuses the rest).
    in_cash = conversion or (side is OrderSide.BUY and draw(st.booleans()))
    size = draw(_AMOUNT)
    return OrderTerms(
        conid=draw(st.sampled_from([CONID, CONID + 1])),
        side=side,
        quantity=None if in_cash else size,
        cash_quantity=size if in_cash else None,
        price=price,
        tif=draw(st.sampled_from(TimeInForce)),
        venue=draw(st.sampled_from(["SMART", "IBIS"])),
        conversion=conversion,
    )


def _body(order_terms: OrderTerms) -> dict[str, Any]:
    return order_body(order_terms, coid="c", account_id="DU1")


@given(_order_terms(), _order_terms())
def test_one_digest_is_one_order(first: OrderTerms, second: OrderTerms) -> None:
    same_digest = first.digest() == second.digest()
    event("one order" if same_digest else "two orders")
    assert same_digest == (first == second)
    if same_digest:
        assert _body(first) == _body(second)
    if _body(first) != _body(second):
        assert not same_digest


def _respelled(value: Any, padded: bool) -> Any:
    if isinstance(value, Decimal):
        return value + Decimal("0.000") if padded else value.normalize()
    if isinstance(value, dict):
        return {key: _respelled(item, padded) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_respelled(item, padded) for item in value]
    return value


@given(_order_terms(), st.booleans())
def test_respelling_an_amount_changes_neither_the_digest_nor_the_order_sent(
    order_terms: OrderTerms, padded: bool
) -> None:
    respelled = OrderTerms.model_validate(_respelled(order_terms.model_dump(), padded))
    assert respelled.digest() == order_terms.digest()
    assert _body(respelled) == _body(order_terms)


# ── I1: the spend ──

_BINDINGS = ["right", "another intent", "another action", "a broker order", "other terms"]


@settings(max_examples=150)
@given(
    issued_us=st.integers(0, 999_999),
    ttl_seconds=st.integers(1, 120),
    attempts=st.lists(
        st.tuples(st.integers(-5_000_000, 130_000_000), st.sampled_from(_BINDINGS)),
        min_size=1,
        max_size=8,
    ),
)
def test_a_clearance_is_spent_once_for_its_own_write_and_never_late(
    issued_us: int, ttl_seconds: int, attempts: list[tuple[int, str]]
) -> None:
    store = _memory_store()
    try:
        mine, other = _intent(), _intent()
        repo.insert_intent(store, mine, coid="mine", at=NOW)
        repo.insert_intent(store, other, coid="other", at=NOW)
        issued = NOW + timedelta(microseconds=issued_us)
        clearance = PretradeClearance(
            clearance_id=uuid.uuid4(),
            intent_id=mine.intent_id,
            action=ClearanceAction.PLACE,
            terms=mine.terms,
            effect=RiskEffect.ADDS,
            asset_class=AssetClass.STOCK,
            quote=None,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=ttl_seconds),
        )
        repo.record_clearance(store, clearance)
        spent = 0
        for offset_us, binding in attempts:
            now = issued + timedelta(microseconds=offset_us)
            binds: dict[str, Any] = {
                "intent_id": mine.intent_id,
                "action": ClearanceAction.PLACE,
                "order_id": None,
                "terms_digest": clearance.terms_digest,
            }
            if binding == "another intent":
                binds["intent_id"] = other.intent_id
            elif binding == "another action":
                binds |= {"action": ClearanceAction.MODIFY, "order_id": "1"}
            elif binding == "a broker order":
                binds["order_id"] = "1"
            elif binding == "other terms":
                binds["terms_digest"] = terms(quantity="11").digest()
            before = repo.get_clearance(store, clearance.clearance_id)
            refusal = repo.spend_clearance(store, clearance.clearance_id, now=now, **binds)
            after = repo.get_clearance(store, clearance.clearance_id)
            assert before is not None
            assert after is not None
            # The store keeps whole seconds: a clearance is live while now's stamp is before
            # the expiry's, which can only end it early, never late.
            live = utc_stamp(now) < utc_stamp(clearance.expires_at)
            if refusal is None:
                event("spent")
                spent += 1
                assert binding == "right"
                assert now < clearance.expires_at
                assert after.used_at is not None
            else:
                assert after.used_at == before.used_at
                assert not (binding == "right" and before.used_at is None and live), refusal
        assert spent <= 1
    finally:
        store.close()


# ── I3: class by effect ──


@given(_order_terms(), st.sampled_from(RiskEffect))
def test_only_a_plain_sell_can_be_cleared_as_reducing_risk(
    order_terms: OrderTerms, effect: RiskEffect
) -> None:
    plain_sell = order_terms.side is OrderSide.SELL and not order_terms.conversion
    try:
        clearance = PretradeClearance(
            clearance_id=uuid.uuid4(),
            intent_id=uuid.uuid7(),
            action=ClearanceAction.PLACE,
            terms=order_terms,
            effect=effect,
            asset_class=AssetClass.STOCK,
            quote=None,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
        )
    except ValidationError:
        assert effect is RiskEffect.REDUCES
        assert not plain_sell
        return
    assert adds_risk(clearance) is (effect is RiskEffect.ADDS)
    if not adds_risk(clearance):
        assert plain_sell


# ── I4: the sweep's verdict ──


class _Store:
    """What the sweep's store reads return: an intent under the cOID and its spent effect."""

    def __init__(
        self, intent: OrderIntent | None, effect: RiskEffect | None, *, down: bool
    ) -> None:
        self._intent = intent
        self._effect = effect
        self._down = down

    async def run(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        if self._down:
            raise StoreError("the store is down")
        if function is repo.intent_by_coid:
            return None if self._intent is None else StoredIntent(intent=self._intent, coid="c")
        if function is repo.spent_effect:
            return self._effect
        raise AssertionError(f"the sweep read {function.__name__}")


_STORED = st.one_of(st.none(), st.tuples(st.sampled_from(IntentRole), st.sampled_from(OrderSide)))


@given(
    broker_side=st.sampled_from([OrderSide.BUY, OrderSide.SELL, None]),
    sec_type=st.sampled_from(["STK", "OPT", "CASH", "cash", None]),
    has_coid=st.booleans(),
    stored=_STORED,
    effect=st.sampled_from([None, RiskEffect.ADDS, RiskEffect.REDUCES]),
    down=st.booleans(),
)
def test_the_sweep_never_keeps_what_adds_risk_and_keeps_the_sells_that_reduce_it(
    shipped_settings: ObliskConfig,
    broker_side: OrderSide | None,
    sec_type: str | None,
    has_coid: bool,
    stored: tuple[IntentRole, OrderSide] | None,
    effect: RiskEffect | None,
    down: bool,
) -> None:
    intent = None
    if stored is not None:
        role, side = stored
        order_terms = (
            terms(side=side, cash="5000", conversion=True, conid=FX_CONID)
            if role is IntentRole.CONVERSION
            else terms(side=side)
        )
        intent = _intent(order_terms, role)
    kill = KillSwitch(Path("unused"), _Store(intent, effect, down=down), shipped_settings)  # type: ignore[arg-type]
    order = BrokerOrder(
        order_id="1",
        coid="c" if has_coid else None,
        conid=CONID,
        side=broker_side,
        sec_type=sec_type,
        order_type="LMT",
        status="Submitted",
        total_quantity=Decimal(10),
        filled_quantity=Decimal(0),
        remaining_quantity=Decimal(10),
        average_price=None,
        tif="DAY",
        observed_at=NOW,
    )
    cancel, by_broker = asyncio.run(kill._cancel_verdict(order))

    risky_at_the_broker = broker_side is not OrderSide.SELL or (sec_type or "").upper() == "CASH"
    store_knows = has_coid and not down and intent is not None
    known_risky = intent is not None and (
        intent.role is IntentRole.CONVERSION
        or intent.side is OrderSide.BUY
        or effect is RiskEffect.ADDS
    )
    if risky_at_the_broker:
        assert cancel
        assert by_broker
    elif store_knows and known_risky:
        assert cancel
        assert not by_broker
    else:
        assert not cancel  # a sell that reduces risk, or one nobody can class: kept
        assert by_broker == (not store_knows or effect is None)


# ── the state machine through the intent book ──

#: The next state along the plan's chain, so a generated life reaches the broker states.
_FORWARD = {
    IntentState.PROPOSED: IntentState.APPROVED,
    IntentState.APPROVED: IntentState.STAGED,
    IntentState.STAGED: IntentState.SUBMITTED,
    IntentState.SUBMITTED: IntentState.WORKING,
    IntentState.WORKING: IntentState.PARTIALLY_FILLED,
    IntentState.PARTIALLY_FILLED: IntentState.FILLED,
}

_FORWARD_OP = st.tuples(st.just("forward"), st.none())
_FACT_OP = st.tuples(st.just("fact"), st.none())
_OPS = st.lists(
    st.one_of(
        _FORWARD_OP,
        _FORWARD_OP,
        _FACT_OP,
        st.tuples(st.just("advance"), st.sampled_from(IntentState)),
        st.tuples(st.just("fact moving the order"), st.none()),
    ),
    max_size=20,
)


_STARTS = st.sampled_from(
    [IntentState.SUBMITTED, IntentState.WORKING, IntentState.PARTIALLY_FILLED, IntentState.PROPOSED]
)


@settings(max_examples=80)
@given(_STARTS, _OPS)
def test_the_book_appends_one_event_per_accepted_change_and_none_per_refusal(
    start: IntentState, ops: list[tuple[str, IntentState | None]]
) -> None:
    async def scenario() -> None:
        worker = StoreWorker(_memory_store)
        try:
            book = IntentBook(worker, coid_max_length=36, now=lambda: NOW)
            intent = _intent()
            intent_id = intent.intent_id
            await book.add(intent)
            state, events = IntentState.PROPOSED, 1
            while state is not start:
                state = _FORWARD[state]
                await book.advance(intent_id, state, "walk to the start")
                events += 1
            for op, chosen in ops:
                target = chosen
                if op == "forward":
                    op, target = "advance", _FORWARD.get(state, IntentState.APPROVED)
                if op == "advance" and target is not None and can_transition(state, target):
                    await book.advance(intent_id, target, "move")
                    assert _RANK[target] > _RANK[state]
                    assert state not in TERMINAL_STATES
                    state, events = target, events + 1
                elif op == "advance" and target is not None:
                    with pytest.raises(IllegalTransitionError):
                        await book.advance(intent_id, target, "move")
                elif op == "fact" and state in BROKER_STATES:
                    await book.fact(intent_id, "a fact")
                    events += 1
                    event("a fact recorded")
                elif op == "fact moving the order" and state in BROKER_STATES:
                    with pytest.raises(StoreError):
                        await book.fact(intent_id, "moved", terms=terms(conid=CONID + 1))
                else:
                    with pytest.raises(IllegalTransitionError):
                        await book.fact(intent_id, "a fact")
                history = await book.history(intent_id)
                assert [entry.seq for entry in history] == list(range(1, events + 1))
                assert (await book.get(intent_id)).intent.state is state
            event(f"reached {state.value}")
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ── the reply policy: o354 ──


@settings(max_examples=300)
@given(
    asset_class=st.one_of(st.just(AssetClass.STOCK), st.sampled_from(AssetClass)),
    status=st.one_of(st.just(MarketDataStatus.DELAYED), st.sampled_from([None, *MarketDataStatus])),
    delay=st.one_of(st.integers(1, 1200), st.none(), st.integers(0, 3000)),
    age_ms=st.one_of(st.integers(0, 60_000), st.integers(-2_000, 120_000)),
    side=st.sampled_from(OrderSide),
    limit=st.decimals(min_value=Decimal(90), max_value=Decimal(110), places=2),
    stop=st.one_of(st.none(), st.decimals(min_value=Decimal(85), max_value=Decimal(105), places=2)),
    sigma=st.one_of(st.none(), st.floats(min_value=0.01, max_value=2.0)),
)
def test_no_market_data_is_confirmed_only_when_every_condition_holds(
    shipped_settings: ObliskConfig,
    asset_class: AssetClass,
    status: MarketDataStatus | None,
    delay: int | None,
    age_ms: int,
    side: OrderSide,
    limit: Decimal,
    stop: Decimal | None,
    sigma: float | None,
) -> None:
    price: Any = LimitPrice(limit=limit) if stop is None else StopLimitPrice(stop=stop, limit=limit)
    order_terms = OrderTerms(
        conid=CONID,
        side=side,
        quantity=Decimal(10),
        price=price,
        tif=TimeInForce.DAY,
        venue="SMART",
    )
    evidence = QuoteEvidence(
        conid=CONID,
        status=status,
        delay_seconds=delay,
        observed_at=NOW,
        bid=Decimal("99.9"),
        ask=Decimal("100.1"),
    )
    clearance = PretradeClearance(
        clearance_id=uuid.uuid4(),
        intent_id=uuid.uuid7(),
        action=ClearanceAction.PLACE,
        terms=order_terms,
        effect=RiskEffect.ADDS,
        asset_class=asset_class,
        quote=evidence,
        sigma=sigma,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    now = NOW + timedelta(milliseconds=age_ms)
    verdict = ReplyPolicy(shipped_settings).decide(
        Question(reply_id="r", message_ids=("o354",), messages=("no market data",)),
        clearance,
        now=now,
    )
    rule = QuotePolicy(shipped_settings).rule_for(asset_class)
    collar = shipped_settings.execution.collar
    fraction: float | None = None
    # A delayed quote needs a sigma, even one claiming no delay (D49).
    if delay == 0 and sigma is not None:
        fraction = collar.base_fraction
    elif delay is not None and sigma is not None:
        year = collar.sessions_per_year * collar.session_hours * 3600
        fraction = collar.base_fraction + collar.sigma_multiplier * sigma * math.sqrt(delay / year)
    ceiling_edge: float | None = None
    if fraction is not None:
        ceiling_edge = collar.max_fraction - fraction
        if ceiling_edge < 0:  # past the ceiling the sigma is implausible: refused
            fraction = None
    edge: float | None = None
    if stop is not None:
        inside = side is OrderSide.SELL and fraction is not None
        if inside and fraction is not None:
            edge = 100 * (1 - fraction) - float(stop)
    else:
        inside = fraction is not None
        if fraction is not None:
            reach = 100 * (1 + fraction) if side is OrderSide.BUY else 100 * (1 - fraction)
            edge = reach - float(limit) if side is OrderSide.BUY else float(limit) - reach
    if edge is not None:
        inside = edge >= 0
    others = (
        isinstance(rule, RealtimePreferred)
        and status is MarketDataStatus.DELAYED
        and delay is not None
        and delay <= rule.max_delay_seconds
        and 0 <= age_ms <= rule.max_quote_age_seconds * 1000
    )
    event("confirmed" if verdict.confirm else "declined")
    if verdict.confirm:
        assert others, verdict.reason
    near_ceiling = ceiling_edge is not None and abs(ceiling_edge) < 1e-9
    # Float and Decimal may part within 1e-9 of an edge.
    if (edge is None or abs(edge) >= 1e-9) and not near_ceiling:
        assert verdict.confirm is (others and inside), verdict.reason
