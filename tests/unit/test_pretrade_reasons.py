"""The pre-trade gate on the fake broker: every reason code it refuses with, each beside its
positive control (the same order cleared once the one condition is put right).

A test named ``test_reason_<code>`` exists for every :class:`Reason`; the last test holds the
module to that.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fake_broker import AVAILABILITY, EURUSD
from order_rig import (
    EUR_ETF,
    FUTURE,
    FUTURE_OPTION,
    LEVERAGED,
    OPEN,
    OPTION,
    STOCK,
    US_ETF,
    spec,
    terms,
    whatif_answer,
    with_caps,
)

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import AssetClass
from obliskiq.domain.instrument import TradedInstrument
from obliskiq.domain.orders import (
    ApprovalDecision,
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderSide,
    PretradeClearance,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
    RiskEffect,
    Sleeve,
)
from obliskiq.errors import StoreError
from obliskiq.execution.intents import new_intent
from obliskiq.execution.modes import Modes
from obliskiq.execution.pretrade import PretradeRefusal, Reason
from obliskiq.ibkr.guard import AccountGuard
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.store import orders as repo


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    return order_rig


async def refused(rig: Any, stored: Any, code: Reason, **kwargs: Any) -> PretradeRefusal:
    answer = await rig.gate.clear(stored.intent, **kwargs)
    assert isinstance(answer, PretradeRefusal), "cleared; a refusal was expected"
    assert code.value in answer.codes, answer.reason
    assert answer.intent_id == stored.intent.intent_id
    return answer


async def cleared(rig: Any, stored: Any, **kwargs: Any) -> PretradeClearance:
    answer = await rig.gate.clear(stored.intent, **kwargs)
    assert isinstance(answer, PretradeClearance), answer.reason
    return answer


def describe(rig: Any, conid: int, **changes: Any) -> None:
    """Describe a test instrument otherwise in the gate's directory."""
    rig.directory.instruments[conid] = rig.directory.instruments[conid].model_copy(update=changes)


def redescribe(rig: Any, conid: int, **changes: Any) -> None:
    """Describe a test instrument otherwise (the gate reads the directory)."""
    describe(rig, conid, **changes)
    rig.make_gate()


async def working(rig: Any, **kwargs: Any) -> tuple[Any, str]:
    """A cleared, placed, working intent and its broker order id."""
    stored = await rig.staged(**kwargs)
    result = await rig.boundary.place(stored.intent.intent_id, await cleared(rig, stored))
    assert result.status is PlacementStatus.PLACED
    assert result.order_id is not None
    return await rig.book.get(stored.intent.intent_id), result.order_id


# ── the positive control of everything below ──


async def test_a_buy_inside_every_rule_is_cleared_and_bound_to_its_quote(rig: Any) -> None:
    stored = await rig.staged()
    clearance = await cleared(rig, stored)
    assert clearance.effect is RiskEffect.ADDS
    assert clearance.terms == stored.intent.terms
    assert clearance.quote is not None
    assert clearance.quote.conid == STOCK
    assert clearance.sigma == pytest.approx(0.3)
    assert clearance.expires_at == clearance.quote.observed_at + timedelta(seconds=60)
    assert (clearance.currency, clearance.notional) == ("USD", Decimal(1000))
    assert clearance.notional_base == Decimal("860")
    recorded = await rig.store(repo.get_clearance, clearance.clearance_id)
    assert recorded is not None
    assert recorded.clearance == clearance
    placed = await rig.boundary.place(stored.intent.intent_id, clearance)
    assert placed.status is PlacementStatus.PLACED


# ── the intent and the write asked for ──


async def test_reason_intent_unknown(rig: Any) -> None:
    now = rig.clock()
    intent = new_intent(
        rig.settings,
        sleeve=Sleeve.CORE,
        role=IntentRole.ENTRY,
        terms=terms(),
        reason="entry",
        mode=ExecutionMode.PAPER,
        approval_required=False,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    answer = await rig.gate.clear(intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.code == "intent_unknown"
    await rig.book.add(intent)
    for step in (IntentState.APPROVED, IntentState.STAGED):
        await rig.book.advance(intent.intent_id, step, "test")
    assert isinstance(await rig.gate.clear(intent), PretradeClearance)


async def test_reason_intent_state(rig: Any) -> None:
    stored = await rig.staged(state=IntentState.APPROVED)
    await refused(rig, stored, Reason.INTENT_STATE)
    await refused(rig, stored, Reason.INTENT_STATE, action=ClearanceAction.MODIFY, order_id="1")
    await rig.book.advance(stored.intent.intent_id, IntentState.STAGED, "test")
    await cleared(rig, stored)


async def test_reason_intent_expired(rig: Any) -> None:
    stored = await rig.staged()
    rig.clock.advance(7 * 3600)
    answer = await refused(rig, stored, Reason.INTENT_EXPIRED)
    assert answer.codes == ("intent_expired",)  # refused at once, before the broker is read
    rig.clock.now = OPEN
    await cleared(rig, stored)


async def test_reason_terms_mismatch(rig: Any) -> None:
    stored = await rig.staged()
    await refused(rig, stored, Reason.TERMS_MISMATCH, terms=terms(quantity="11"))
    await cleared(rig, stored)


async def test_reason_modify_changes_order(rig: Any) -> None:
    stored, order_id = await working(rig)
    other_side = terms(side=OrderSide.SELL)
    await refused(
        rig,
        stored,
        Reason.MODIFY_CHANGES_ORDER,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        terms=other_side,
    )
    clearance = await cleared(
        rig, stored, action=ClearanceAction.MODIFY, order_id=order_id, terms=terms(limit="99.5")
    )
    assert clearance.order_id == order_id


async def test_reason_order_not_intents(rig: Any) -> None:
    stored, order_id = await working(rig)
    await refused(
        rig,
        stored,
        Reason.ORDER_NOT_INTENTS,
        action=ClearanceAction.MODIFY,
        order_id="999",
        terms=terms(limit="99.5"),
    )
    await cleared(
        rig, stored, action=ClearanceAction.MODIFY, order_id=order_id, terms=terms(limit="99.5")
    )


# ── currency conversions ──


def pair_quote(rig: Any, *, bid: str = "1.1649", ask: str = "1.1651", sigma: str = "8%") -> None:
    """The EUR.USD quote: a reference of 1.165 dollars a euro."""
    rig.broker.quote(EURUSD, bid=bid, ask=ask, last="1.165", sigma=sigma)


async def conversion(
    rig: Any,
    *,
    side: OrderSide = OrderSide.SELL,
    cash: str = "1000",
    limit: str = "1.162",
    conid: int = EURUSD,
) -> Any:
    """A conversion intent: by default 1,000 euros sold for dollars at no less than 1.162."""
    return await rig.intent(
        terms(side=side, cash=cash, limit=limit, conversion=True, conid=conid),
        role=IntentRole.CONVERSION,
        reason="funding",
    )


async def test_a_conversion_inside_every_rule_is_cleared_against_what_it_converts_from(
    rig: Any,
) -> None:
    pair_quote(rig)
    clearance = await cleared(rig, await conversion(rig))
    assert clearance.asset_class is AssetClass.FX
    assert clearance.effect is RiskEffect.ADDS
    assert (clearance.currency, clearance.notional) == ("EUR", Decimal(1000))
    assert clearance.notional_base == Decimal(1000)
    assert clearance.quote is not None
    assert clearance.quote.conid == EURUSD
    # A buy of the pair sells dollars for euros: it is cleared against the dollars.
    back = await cleared(rig, await conversion(rig, side=OrderSide.BUY, limit="1.168"))
    assert (back.currency, back.notional) == ("USD", Decimal(1000))
    assert back.notional_base == Decimal(860)


async def test_reason_conversion_pair(rig: Any) -> None:
    pair_quote(rig)
    unknown = await conversion(rig, conid=STOCK)
    answer = await refused(rig, unknown, Reason.CONVERSION_PAIR)
    assert "short" not in answer.codes  # a conversion is bounded by cash, not by I2
    assert rig.broker.whatifs == []  # refused before the broker is touched
    await cleared(rig, await conversion(rig))


async def test_a_conversion_is_held_to_the_pairs_quote_and_collar(rig: Any) -> None:
    stored = await conversion(rig)
    await refused(rig, stored, Reason.QUOTE_POLICY)  # no quote for the pair yet
    pair_quote(rig)
    greedy = await conversion(rig, limit="1.10")  # selling euros at 1.10 is through the collar
    assert "below the collar" in (await refused(rig, greedy, Reason.COLLAR)).reason
    await cleared(rig, stored)


async def test_a_conversion_cannot_borrow_or_spend_the_cash_others_need(rig: Any) -> None:
    pair_quote(rig)
    too_much = await conversion(rig, cash="12000")
    answer = await refused(rig, too_much, Reason.MARGIN_USE)
    assert "would borrow" in answer.reason
    first = await cleared(rig, await conversion(rig, cash="6000"))
    placed = await rig.boundary.place(first.intent_id, first)
    assert placed.status is PlacementStatus.PLACED  # working: 6,000 euros committed
    euros = await rig.staged(EUR_ETF, limit="100")  # 1,000 euros of a euro ETF
    rig.broker.whatif_answer = whatif_answer("1001.00", "EUR")
    await cleared(rig, euros)
    second = await conversion(rig, cash="4000")
    answer = await refused(rig, second, Reason.INSUFFICIENT_CASH)
    assert "7000.00 committed" in answer.reason  # the conversion and the ETF buy


async def test_reason_currency_cap(rig: Any) -> None:
    pair_quote(rig)
    rig.broker.nav = Decimal(20000)  # the cap is 16,000 euros of one foreign currency
    stored = await conversion(rig)
    answer = await refused(rig, stored, Reason.CURRENCY_CAP)
    assert "single-foreign-currency cap" in answer.reason
    rig.broker.nav = Decimal(100000)
    await cleared(rig, stored)


async def test_the_currency_cap_counts_conversions_still_working_into_the_currency(
    rig: Any,
) -> None:
    pair_quote(rig)
    rig.broker.cash = {"EUR": (Decimal(40000), None), "USD": (Decimal(0), Decimal("0.86"))}
    rig.broker.nav = Decimal(20000)  # the cap is 16,000 euros of dollars
    first = await cleared(rig, await conversion(rig, cash="10000"))
    placed = await rig.boundary.place(first.intent_id, first)
    assert placed.status is PlacementStatus.PLACED  # about 10,000 euros of dollars on the way
    second = await conversion(rig, cash="10000")
    answer = await refused(rig, second, Reason.CURRENCY_CAP)
    assert "above the single-foreign-currency cap" in answer.reason
    await cleared(rig, await conversion(rig, cash="5000"))


async def test_reason_exposure_unreadable(rig: Any) -> None:
    pair_quote(rig)
    await rig.hold(STOCK, "10")  # dollars in a position IBKR reports no value for
    stored = await conversion(rig)
    answer = await refused(rig, stored, Reason.EXPOSURE_UNREADABLE)
    assert str(STOCK) in answer.reason
    rig.broker.marks[STOCK] = Decimal(100)
    await cleared(rig, stored)


async def test_a_conversion_out_of_a_currency_not_held_is_refused(rig: Any) -> None:
    pair_quote(rig)
    rig.broker.cash = {"EUR": (Decimal(10000), None)}
    stored = await conversion(rig, side=OrderSide.BUY, limit="1.168")
    assert "to convert from" in (await refused(rig, stored, Reason.CURRENCY_NOT_HELD)).reason
    rig.broker.cash["USD"] = (Decimal(5000), None)
    await refused(rig, stored, Reason.FX_RATE_UNAVAILABLE)


async def test_a_conversion_the_whatif_refuses_is_refused(rig: Any) -> None:
    pair_quote(rig)
    rig.broker.whatif_answer = whatif_answer("1000", "EUR", error="no trading permission")
    answer = await refused(rig, await conversion(rig), Reason.WHATIF_REFUSED)
    assert "no trading permission" in answer.reason


async def test_a_conversion_is_previewed_as_the_plain_order_on_its_pair(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IBKR's whatif answers 503 to isCcyConv/fxQty (T1, 2026-10-20): the gate previews the
    equivalent limit order in units and still clears the conversion itself."""
    pair_quote(rig)
    seen: list[Any] = []
    whatif = rig.reader.whatif

    async def spy(order_terms: Any) -> Any:
        seen.append(order_terms)
        return await whatif(order_terms)

    monkeypatch.setattr(rig.reader, "whatif", spy)
    stored = await conversion(rig, cash="1000")
    clearance = await cleared(rig, stored)
    assert [(t.conversion, t.cash_quantity, t.quantity) for t in seen] == [
        (False, None, Decimal(1000))
    ]
    assert clearance.terms.conversion is True
    assert clearance.terms.cash_quantity == Decimal(1000)


async def test_a_conversion_too_small_to_preview_is_refused(rig: Any) -> None:
    pair_quote(rig)
    stored = await conversion(rig, side=OrderSide.BUY, cash="0.4", limit="1.168")
    answer = await refused(rig, stored, Reason.WHATIF_REFUSED)
    assert "less than one" in answer.reason


async def test_a_conversion_whose_whatif_fails_is_refused_and_a_check_clears_it(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.ibkr.errors import IbkrUpstreamError

    pair_quote(rig)
    stored = await conversion(rig)
    assert await rig.gate.check(stored.intent, assume={}) is None  # a check issues nothing

    async def broken(order_terms: Any) -> Any:
        raise IbkrUpstreamError("whatif unavailable")

    monkeypatch.setattr(rig.reader, "whatif", broken)
    answer = await refused(rig, stored, Reason.WHATIF_REFUSED)
    assert "the whatif failed" in answer.reason


async def test_a_conversion_beside_a_working_buy_the_bot_did_not_place_is_refused(
    rig: Any,
) -> None:
    pair_quote(rig)
    rig.broker.add_order(side="BUY", conid=EUR_ETF)
    await refused(rig, await conversion(rig), Reason.CASH_UNACCOUNTED)


async def test_a_conversion_without_a_readable_nav_is_refused(rig: Any) -> None:
    pair_quote(rig)
    rig.broker.nav = None
    await refused(rig, await conversion(rig), Reason.NAV_UNAVAILABLE)


async def test_a_conversion_out_of_the_base_currency_skips_the_cap_on_the_way_home(
    rig: Any,
) -> None:
    pair_quote(rig)
    rig.broker.nav = Decimal(1000)  # far below any foreign exposure
    home = await conversion(rig, side=OrderSide.BUY, limit="1.168")
    await cleared(rig, home)  # dollars into euros: the cap is on foreign currencies


async def test_a_conversion_is_not_held_to_the_notional_caps(rig: Any) -> None:
    pair_quote(rig)
    rig.broker.cash["EUR"] = (Decimal(40000), None)
    big = await conversion(rig, cash="30000")  # above the per-order cap of 25,000 euros
    clearance = await cleared(rig, big)
    await rig.boundary.place(clearance.intent_id, clearance)
    stock = await rig.staged()  # its notional still counts alone toward the daily cap
    await cleared(rig, stock)


async def test_an_unreadable_account_refuses_a_conversion(rig: Any) -> None:
    pair_quote(rig)
    rig.broker.unavailable.add("ledger")
    await refused(rig, await conversion(rig), Reason.ACCOUNT_UNREADABLE)
    rig.broker.unavailable.clear()
    rig.broker.snapshot = False
    await refused(rig, await conversion(rig), Reason.ACCOUNT_UNREADABLE)


# ── the paper guard, the mode and the kill switch ──


async def test_reason_paper_guard(rig: Any) -> None:
    stored = await rig.staged()
    paper = rig.guard
    rig.guard = AccountGuard(account_id="U1234567", profile="paper", paper_prefixes=("DU",))
    rig.make_gate()
    await refused(rig, stored, Reason.PAPER_GUARD)
    assert rig.broker.whatifs == []  # refused before the broker is touched
    rig.guard = paper
    rig.make_gate()
    await cleared(rig, stored)


async def test_reason_mode_unreadable(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stored = await rig.staged()

    async def unreadable() -> ExecutionMode:
        raise StoreError("the mode audit cannot be read")

    monkeypatch.setattr(rig.modes, "current", unreadable)
    await refused(rig, stored, Reason.MODE_UNREADABLE)
    monkeypatch.undo()
    await cleared(rig, stored)


async def test_reason_mode_forbids_write(rig: Any) -> None:
    stored = await rig.staged()
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="downgrade")
    await refused(rig, stored, Reason.MODE_FORBIDS_WRITE)
    await rig.to_paper()
    await cleared(rig, stored)


async def test_reason_intent_mode(rig: Any) -> None:
    now = rig.clock()
    shadow = new_intent(
        rig.settings,
        sleeve=Sleeve.CORE,
        role=IntentRole.ENTRY,
        terms=terms(),
        reason="entry",
        mode=ExecutionMode.SHADOW,
        approval_required=False,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    stored = await rig.book.add(shadow)
    for step in (IntentState.APPROVED, IntentState.STAGED):
        await rig.book.advance(shadow.intent_id, step, "test")
    answer = await refused(rig, stored, Reason.INTENT_MODE)
    assert answer.codes == ("intent_mode",)
    await cleared(rig, await rig.staged())


async def test_reason_profile_mismatch(rig: Any) -> None:
    stored = await rig.staged()
    paper_modes = rig.modes
    rig.modes = Modes(rig.worker, rig.settings, profile="live", now=rig.clock)
    rig.make_gate()
    await refused(rig, stored, Reason.PROFILE_MISMATCH)
    rig.modes = paper_modes
    rig.make_gate()
    await cleared(rig, stored)


async def test_reason_reconciliation_frozen(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    stop = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    buy = await rig.staged()
    for status in (ReconcileStatus.DRIFT, ReconcileStatus.INCOMPLETE):
        finding = ReconcileFinding(kind="test", detail="a test finding", freezes=True)
        run = ReconcileRun(at=rig.clock(), status=status, findings=(finding,))
        await rig.store(repo.record_reconciliation, run)
        answer = await refused(rig, buy, Reason.RECONCILIATION_FROZEN)
        assert status.value in answer.reason
        await cleared(rig, stop)  # an order that reduces risk is never frozen
    green = ReconcileRun(at=rig.clock(), status=ReconcileStatus.GREEN, findings=())
    await rig.store(repo.record_reconciliation, green)
    await cleared(rig, buy)


async def test_reason_kill_switch_on(rig: Any) -> None:
    stored = await rig.staged()
    await rig.kill.turn_on(actor="test", reason="drill")
    await refused(rig, stored, Reason.KILL_SWITCH_ON)
    await rig.kill.turn_off(actor="test", reason="drill over")
    await cleared(rig, stored)


# ── the instrument: described, not prohibited, eligible, approved ──


async def test_reason_instrument_unknown(rig: Any) -> None:
    await refused(rig, await rig.staged(999999), Reason.INSTRUMENT_UNKNOWN)
    option = await rig.staged(OPTION, quantity="1", limit="5")
    rig.broker.whatif_answer = whatif_answer("501.00", "USD")
    stock = rig.directory.instruments.pop(STOCK)
    answer = await refused(rig, option, Reason.INSTRUMENT_UNKNOWN)
    assert "underlying" in answer.reason
    rig.directory.instruments[STOCK] = stock
    await cleared(rig, option)


@pytest.mark.parametrize(
    ("conid", "quantity", "limit", "marker"),
    [
        (LEVERAGED, "10", "100", "leveraged or inverse"),
        (FUTURE, "1", "100", "futures contract"),
        (FUTURE_OPTION, "1", "5", "option on a futures contract"),
    ],
)
async def test_reason_prohibited_product(
    rig: Any, conid: int, quantity: str, limit: str, marker: str
) -> None:
    answer = await refused(
        rig, await rig.staged(conid, quantity=quantity, limit=limit), Reason.PROHIBITED_PRODUCT
    )
    assert marker in answer.reason
    await cleared(rig, await rig.staged())


async def test_a_prohibited_product_is_refused_on_every_side_and_through_an_option(
    rig: Any,
) -> None:
    await rig.hold(LEVERAGED, "10")
    sell = await rig.staged(LEVERAGED, side=OrderSide.SELL)
    await refused(rig, sell, Reason.PROHIBITED_PRODUCT)  # never traded, by any side or role
    rig.directory.instruments[700001] = TradedInstrument(
        conid=700001,
        symbol="TQQQ C50",
        listing_exchange="CBOE",
        currency="USD",
        asset_class=AssetClass.OPTION,
        description="TQQQ call",
        multiplier=Decimal(100),
        underlying_conid=LEVERAGED,
        expiry=date(2027, 1, 15),
    )
    rig.broker.quote(700001, bid="4.9", ask="5.1", last="5", option=True)
    answer = await refused(
        rig, await rig.staged(700001, quantity="1", limit="5"), Reason.PROHIBITED_PRODUCT
    )
    assert "ProShares UltraPro QQQ" in answer.reason


async def test_reason_ineligible(rig: Any) -> None:
    await refused(rig, await rig.staged(US_ETF), Reason.INELIGIBLE)
    stored = await rig.staged()
    rig.eligibility.allowed.discard(STOCK)
    answer = await refused(rig, stored, Reason.INELIGIBLE)
    assert "not in the allowlist" in answer.reason  # the policy's own reason reaches the owner
    rig.eligibility.allowed.add(STOCK)
    await cleared(rig, stored)


async def test_reason_mandatory_close(rig: Any) -> None:
    rig.broker.whatif_answer = whatif_answer("501.00", "USD")
    far = await rig.staged(OPTION + 1, quantity="1", limit="5")  # ETP_OPTION, far expiry
    await cleared(rig, far)  # an option on an ineligible ETP, outside its window
    redescribe(rig, OPTION + 1, expiry=date(2026, 10, 23))
    answer = await refused(rig, far, Reason.MANDATORY_CLOSE)
    assert "2026-10-09" in answer.reason  # ten sessions before the last tradable session
    await rig.hold(OPTION + 1, "1")
    close = await rig.staged(OPTION + 1, side=OrderSide.SELL, quantity="1", limit="4.98")
    await cleared(rig, close)  # closing is what the rule asks for


async def test_reason_approval_missing(rig: Any) -> None:
    stored = await rig.staged(approval_required=True)
    answer = await refused(rig, stored, Reason.APPROVAL_MISSING)
    assert "waits for the owner's approval" in answer.reason
    decision = ApprovalDecision(
        intent_id=stored.intent.intent_id,
        approved=False,
        actor="owner",
        reason="not now",
        at=rig.clock(),
    )
    await rig.store(repo.record_approval, decision)
    answer = await refused(rig, stored, Reason.APPROVAL_MISSING)
    assert "rejected" in answer.reason
    await rig.store(repo.record_approval, decision.model_copy(update={"approved": True}))
    await cleared(rig, stored)


async def test_a_sleeve_held_for_approval_holds_every_intent_that_adds_risk(rig: Any) -> None:
    execution = rig.settings.execution
    held = rig.settings.model_copy(
        update={
            "execution": execution.model_copy(
                update={
                    "approval": execution.approval.model_copy(update={"sleeves": [Sleeve.CORE]})
                }
            )
        }
    )
    rig.make_gate(settings=held)
    await refused(rig, await rig.staged(), Reason.APPROVAL_MISSING)
    await rig.hold(STOCK, "10")
    await cleared(rig, await rig.staged(side=OrderSide.SELL))  # a sell reduces risk: waived
    rig.make_gate()
    await cleared(rig, await rig.staged())


# ── the calendars ──


async def test_reason_venue_not_tradable(rig: Any) -> None:
    stored = await rig.staged()
    rig.clock.now = datetime(2026, 10, 13, 3, 0, tzinfo=OPEN.tzinfo)
    answer = await refused(rig, stored, Reason.VENUE_NOT_TRADABLE)
    assert "outside_window" in answer.reason
    rig.clock.now = OPEN
    await cleared(rig, stored)


async def test_reason_option_expired(rig: Any) -> None:
    redescribe(rig, OPTION, expiry=date(2026, 10, 9))
    await rig.hold(OPTION, "1")
    close = await rig.staged(OPTION, side=OrderSide.SELL, quantity="1", limit="4.98")
    answer = await refused(rig, close, Reason.OPTION_EXPIRED)
    assert "2026-10-09" in answer.reason
    redescribe(rig, OPTION, expiry=date(2027, 1, 15))
    await cleared(rig, close)


# ── the broker reads, the quote policy and the collar ──


@pytest.mark.parametrize("what", ["positions", "ledger"])
async def test_reason_account_unreadable(rig: Any, what: str) -> None:
    stored = await rig.staged()
    rig.broker.unavailable.add(what)
    await refused(rig, stored, Reason.ACCOUNT_UNREADABLE)
    rig.broker.unavailable.clear()
    await cleared(rig, stored)


async def test_a_live_orders_list_that_is_not_a_snapshot_refuses(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.snapshot = False
    answer = await refused(rig, stored, Reason.ACCOUNT_UNREADABLE)
    assert "complete snapshot" in answer.reason
    rig.broker.snapshot = True
    await cleared(rig, stored)


async def test_reason_quote_policy(rig: Any) -> None:
    stored = await rig.staged()
    del rig.broker.quotes[STOCK]
    answer = await refused(rig, stored, Reason.QUOTE_POLICY)
    assert "no quote" in answer.reason
    rig.broker.unavailable.add("snapshot")
    answer = await refused(rig, stored, Reason.QUOTE_POLICY)
    assert "cannot be read" in answer.reason
    rig.broker.unavailable.clear()
    rig.broker.quote(STOCK)
    await cleared(rig, stored)


async def test_reason_instrument_halted(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.quote(STOCK, last="H100")
    await refused(rig, stored, Reason.INSTRUMENT_HALTED)
    rig.broker.quote(STOCK)
    await cleared(rig, stored)


async def test_reason_collar(rig: Any) -> None:
    breach = await rig.staged(limit="100.6")  # the real-time collar is 0.5 % above 100
    answer = await refused(rig, breach, Reason.COLLAR)
    assert "above the collar" in answer.reason
    await cleared(rig, await rig.staged(limit="100.4"))
    protective_buy = await rig.staged(stop="99", limit="100", role=IntentRole.PROTECTIVE)
    await refused(rig, protective_buy, Reason.COLLAR)  # a buy stop-limit has no collar rule


async def test_reason_protective_spec(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    for stop, limit, words in (
        ("99.8", "99", "inside the collar's band"),
        ("95", "96", "above the stop"),
        ("95", "80", "below the stop"),  # the spec allows 5 % below 95: 90.25
    ):
        stored = await rig.staged(
            side=OrderSide.SELL, stop=stop, limit=limit, role=IntentRole.PROTECTIVE
        )
        answer = await refused(rig, stored, Reason.PROTECTIVE_SPEC)
        assert words in answer.reason
        assert "collar" not in answer.codes  # checked against its spec instead
    good = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    assert (await cleared(rig, good)).effect is RiskEffect.REDUCES


async def test_a_stop_rests_by_its_own_spec_and_never_without_one(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    narrow = await rig.intent(
        terms(side=OrderSide.SELL, stop="95", limit="93"),
        role=IntentRole.PROTECTIVE,
        reason="stop",
        protective=spec("95", Decimal("0.01")),
    )
    answer = await refused(rig, narrow, Reason.PROTECTIVE_SPEC)  # 93 is 2 % below 95
    assert "more than 0.01 below the stop" in answer.reason
    wide = await rig.intent(
        terms(side=OrderSide.SELL, stop="95", limit="94"),
        role=IntentRole.PROTECTIVE,
        reason="stop",
        protective=spec("95", Decimal("0.2")),  # wider than execution.protective's 10 %
    )
    assert "acts as a market order" in (await refused(rig, wide, Reason.PROTECTIVE_SPEC)).reason
    bare = await rig.intent(
        terms(side=OrderSide.SELL, stop="95", limit="94.5"), role=IntentRole.EXIT, reason="stop"
    )
    assert "carries none" in (await refused(rig, bare, Reason.PROTECTIVE_SPEC)).reason


async def test_a_quote_that_goes_stale_before_the_clearance_is_recorded_refuses(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = await rig.staged()
    whatif = rig.reader.whatif

    async def slow(order_terms: Any) -> Any:
        rig.clock.advance(61)
        return await whatif(order_terms)

    monkeypatch.setattr(rig.reader, "whatif", slow)
    answer = await refused(rig, stored, Reason.QUOTE_POLICY)
    assert "went stale" in answer.reason


# ── holdings ──


async def test_reason_holding_unmanaged(rig: Any) -> None:
    await rig.hold(STOCK, "10", owner="unmanaged")
    await refused(rig, await rig.staged(), Reason.HOLDING_UNMANAGED)
    sell = await rig.staged(side=OrderSide.SELL)
    await refused(rig, sell, Reason.HOLDING_UNMANAGED)
    await rig.assign(STOCK, Sleeve.CORE)
    await cleared(rig, sell)


async def test_reason_holding_unassigned(rig: Any) -> None:
    await rig.hold(STOCK, "10", owner=None)
    stored = await rig.staged()
    answer = await refused(rig, stored, Reason.HOLDING_UNASSIGNED)
    assert "unmanaged until the owner assigns it" in answer.reason
    await rig.assign(STOCK, Sleeve.CORE)
    await cleared(rig, stored)


async def test_reason_holding_other_sleeve(rig: Any) -> None:
    await rig.hold(STOCK, "10", owner=Sleeve.SATELLITE)
    await refused(rig, await rig.staged(), Reason.HOLDING_OTHER_SLEEVE)
    await cleared(rig, await rig.staged(sleeve=Sleeve.SATELLITE))


# ── no short (I2; the rest of its cases are in test_pretrade_no_short.py) ──


async def test_reason_short(rig: Any) -> None:
    await refused(rig, await rig.staged(side=OrderSide.SELL), Reason.SHORT)  # nothing held
    await rig.hold(STOCK, "10")
    await refused(rig, await rig.staged(side=OrderSide.SELL, quantity="11"), Reason.SHORT)
    await cleared(rig, await rig.staged(side=OrderSide.SELL, quantity="10"))


async def test_reason_written_option(rig: Any) -> None:
    sell_to_open = await rig.staged(OPTION, side=OrderSide.SELL, quantity="1", limit="4.98")
    answer = await refused(rig, sell_to_open, Reason.WRITTEN_OPTION)
    assert "short" not in answer.codes
    await rig.hold(OPTION, "1")
    await cleared(rig, sell_to_open)  # now a sell to close


async def test_reason_fills_unreconciled(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    other = rig.broker.add_order(side="SELL", conid=STOCK, quantity="5", coid="elsewhere")
    other.filled = Decimal(2)  # the order says it filled; no trade says so yet
    stored = await rig.staged(side=OrderSide.SELL, quantity="3")
    await refused(rig, stored, Reason.FILLS_UNRECONCILED)
    rig.broker.trades.append(
        {
            "execution_id": "exec-elsewhere",
            "side": "S",
            "size": 2,
            "price": "100",
            "order_ref": "elsewhere",
            "conid": STOCK,
            "trade_time_r": int(rig.clock().timestamp() * 1000),
        }
    )
    await cleared(rig, stored)


# ── cash, no leverage and the caps ──


async def test_reason_currency_not_held(rig: Any) -> None:
    stored = await rig.staged()
    usd = rig.broker.cash.pop("USD")
    answer = await refused(rig, stored, Reason.CURRENCY_NOT_HELD)
    assert "Session 5" in answer.reason
    rig.broker.cash["USD"] = usd
    await cleared(rig, stored)


async def test_reason_fx_rate_unavailable(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.cash["USD"] = (Decimal(20000), None)
    await refused(rig, stored, Reason.FX_RATE_UNAVAILABLE)
    rig.broker.cash["USD"] = (Decimal(20000), Decimal("0.86"))
    await cleared(rig, stored)


@pytest.mark.parametrize(
    "answer",
    [
        whatif_answer("1001.00", "USD", error="no trading permission"),
        whatif_answer("1001.00", "EUR"),
        {"amount": {}, "error": None},
    ],
    ids=["error", "other_currency", "no_cost"],
)
async def test_reason_whatif_refused(rig: Any, answer: dict[str, Any]) -> None:
    stored = await rig.staged()
    rig.broker.whatif_answer = answer
    await refused(rig, stored, Reason.WHATIF_REFUSED)
    rig.broker.whatif_answer = whatif_answer("1001.00", "USD")
    await cleared(rig, stored)


async def test_a_whatif_that_fails_refuses(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from obliskiq.ibkr.errors import IbkrUpstreamError

    async def broken(order_terms: Any) -> Any:
        raise IbkrUpstreamError("whatif unavailable")

    monkeypatch.setattr(rig.reader, "whatif", broken)
    await refused(rig, await rig.staged(), Reason.WHATIF_REFUSED)


async def test_reason_margin_use(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.cash["USD"] = (Decimal(500), Decimal("0.86"))
    answer = await refused(rig, stored, Reason.MARGIN_USE)
    assert "would borrow" in answer.reason
    rig.broker.cash["USD"] = (Decimal(20000), Decimal("0.86"))
    await cleared(rig, stored)


async def test_reason_insufficient_cash(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.cash["USD"] = (Decimal(1020), Decimal("0.86"))  # covers the cost, not the floor
    answer = await refused(rig, stored, Reason.INSUFFICIENT_CASH)
    assert "margin_use" not in answer.codes
    rig.broker.cash["USD"] = (Decimal(1500), Decimal("0.86"))
    await cleared(rig, stored)
    # A second buy competes with the first's open clearance for the same dollars.
    answer = await refused(rig, await rig.staged(), Reason.INSUFFICIENT_CASH)
    assert "1000.00 committed" in answer.reason


async def test_reason_cash_unaccounted(rig: Any) -> None:
    stored = await rig.staged()
    foreign = rig.broker.add_order(side="BUY", conid=STOCK, coid=None)
    await refused(rig, stored, Reason.CASH_UNACCOUNTED)
    foreign.status = "Cancelled"
    await cleared(rig, stored)


async def test_reason_nav_unavailable(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.nav = None
    await refused(rig, stored, Reason.NAV_UNAVAILABLE)
    rig.broker.nav = Decimal(100000)
    await cleared(rig, stored)


async def test_reason_order_notional_cap(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.nav = Decimal(3000)  # 25 % is 750 EUR; the order is 860 EUR
    answer = await refused(rig, stored, Reason.ORDER_NOTIONAL_CAP)
    assert "860.00" in answer.reason
    rig.broker.nav = Decimal(4000)
    await cleared(rig, stored)


async def test_reason_daily_notional_cap(rig: Any) -> None:
    rig.broker.nav = Decimal(4000)  # 1,000 EUR an order, 2,000 EUR a day
    for _ in range(2):
        await cleared(rig, await rig.staged())
    third = await rig.staged()
    answer = await refused(rig, third, Reason.DAILY_NOTIONAL_CAP)
    assert "2580.00" in answer.reason
    rig.clock.advance(24 * 3600)  # the window rolls on; those intents have expired since
    await cleared(rig, await rig.staged())


async def test_reason_daily_order_cap(rig: Any) -> None:
    rig.make_gate(settings=with_caps(rig.settings, max_orders_per_day=2))
    first = await rig.staged()
    for _ in range(2):
        await cleared(rig, await rig.staged())
    await refused(rig, first, Reason.DAILY_ORDER_CAP)
    rig.make_gate(settings=with_caps(rig.settings, max_orders_per_day=3))
    await cleared(rig, first)


# ── the module's own completeness ──


def test_every_reason_code_has_its_test() -> None:
    missing = [code.value for code in Reason if f"test_reason_{code.value}" not in globals()]
    assert missing == []


def test_the_availability_field_is_the_one_the_quote_decoder_reads(
    shipped_settings: ObliskConfig,
) -> None:
    fields = {field.name: field.id for field in shipped_settings.ibkr.marketdata.snapshot_fields}
    assert fields["market_data_status"] == AVAILABILITY


async def test_a_ledger_with_an_unreadable_currency_refuses_a_buy(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.unreadable_cash.add("GBP")
    answer = await refused(rig, stored, Reason.ACCOUNT_UNREADABLE)
    assert "GBP" in answer.reason
    rig.broker.unreadable_cash.clear()
    await cleared(rig, stored)
