"""The basic rebalancer: targets against holdings in integer units, inside the inner band, above
the minimum trade and the commission floor, within what the book may spend; and why a role or
the whole book is left alone.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from capital_rig import make_rebalancer
from fake_broker import EURUSD, FakeBroker
from order_rig import build_rig
from service_rig import EMIM, IBGM, IWDA, SERVICE_VENUES, skeleton_market

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_runtime import CapitalConfig
from obliskiq.decision.rebalance_basic import (
    BasicRebalancer,
    RebalancePlan,
    confirm_targets,
    minimum_trade,
)
from obliskiq.domain.capital import Adjustment, AdjustmentKind, Deposit
from obliskiq.domain.orders import (
    ExecutionMode,
    Fill,
    HoldingAssignment,
    IntentRole,
    IntentState,
    OrderSide,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
    Sleeve,
)
from obliskiq.errors import TaxError
from obliskiq.execution.executor import RETRY_NEXT_WINDOW
from obliskiq.execution.instruments import UniverseDirectory
from obliskiq.store import capital as capital_repo
from obliskiq.store import orders as repo

#: Monday 26 October 2026, 10:00 in Amsterdam and London's morning: both venues trade.
OPEN = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def calendars(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=SERVICE_VENUES
    )


@pytest.fixture
async def rig(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    with respx.mock(assert_all_called=False) as router:
        broker = FakeBroker(router)
        built = await build_rig(
            settings=shipped_settings,
            session=make_session(),
            broker=broker,
            tmp_path=tmp_path,
            calendars=calendars,
        )
        skeleton_market(broker)
        built.clock.now = OPEN
        yield built
        await built.aclose()


def rebalancer(rig: Any, settings: ObliskConfig | None = None, **kw: Any) -> BasicRebalancer:
    return make_rebalancer(rig, settings, **kw)


def by_role(plan: RebalancePlan) -> dict[str, Any]:
    return {d.role: d for d in plan.decisions}


def with_capital(settings: ObliskConfig, **values: Any) -> ObliskConfig:
    return settings.model_copy(update={"capital": settings.capital.model_copy(update=values)})


def with_trading(settings: ObliskConfig, **values: Any) -> ObliskConfig:
    return settings.model_copy(update={"trading": settings.trading.model_copy(update=values)})


#: What one unit of each vehicle is worth in euros at the skeleton market's quotes.
UNIT_EUR = {IBGM: Decimal("177.50"), EMIM: Decimal("46.42"), IWDA: Decimal("126.033")}


async def hold(rig: Any, conid: int, units: str, currency: str) -> None:
    """The core holds ``units``, and the book paid for them: its cash falls by their value, so
    the book stays at its cap of 1,000 (a holding assigned without a fill would join the book as
    a contribution, DECISIONS D106)."""
    before = rig.broker.positions.get(conid)
    rig.broker.hold(conid, units, currency)
    await rig.store(
        repo.assign_holding,
        HoldingAssignment(
            conid=conid, owner=Sleeve.CORE, assigned_by="test", assigned_at=OPEN, reason="test"
        ),
    )
    added = Decimal(units) - (Decimal(0) if before is None else before[0])
    if conid in UNIT_EUR and added:
        await rig.store(
            capital_repo.record_flow,
            Adjustment(
                category=AdjustmentKind.CORRECTION,
                amount=-(added * UNIT_EUR[conid]),
                currency="EUR",
                on=OPEN.date(),
                recorded_at=OPEN,
                recorded_by="test",
            ),
        )


# ── the plan ──


async def test_an_empty_book_buys_every_role_largest_shortfall_first(rig: Any) -> None:
    plan = await rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.blocked is None
    assert plan.capital is not None
    assert plan.capital.managed == Decimal(1000)
    assert plan.spendable == Decimal(975)  # the cap, less the cash floor of 25
    # The core's budget is 950: its own 70 % with the inactive satellite's and options' shares;
    # the cash floor of 5 % stays cash.
    assert plan.core_capital == Decimal(950)
    roles = by_role(plan)
    assert [(roles[r].action, roles[r].quantity) for r in roles] == [
        ("buy", Decimal(1)),  # world_equity: 171 of target, one IWDA is 126.03 in euros
        ("buy", Decimal(2)),  # em_equity: 133, two EMIM are 92.84
        ("buy", Decimal(3)),  # euro_govt_intermediate: 646, three IBGM are 532.50
    ]
    assert roles["world_equity"].notional == Decimal("126.0330")
    assert "world_equity" in plan.render()
    assert "may spend 975.00" in plan.render()


async def test_buys_stop_at_what_the_book_may_spend(rig: Any) -> None:
    rig.broker.cash = {"EUR": (Decimal(200), None)}  # 175 to spend above the floor of 25
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.SHADOW))
    # The largest shortfall first: IBGM (680 short) cannot buy one unit of 177.50.
    assert roles["euro_govt_intermediate"].action == "skip"
    assert "no cash for one unit" in roles["euro_govt_intermediate"].reason
    # Then IWDA (180 short): one unit of 126.03 at the pair's rate.
    assert (roles["world_equity"].action, roles["world_equity"].quantity) == ("buy", 1)
    # 49 are left: one EMIM of 46.42 is below the minimum trade of 50.
    assert roles["em_equity"].action == "skip"
    assert "below the minimum trade 50.00" in roles["em_equity"].reason


async def test_a_book_inside_its_bands_holds_and_an_overweight_sells(rig: Any) -> None:
    await hold(rig, IBGM, "4", "EUR")  # 710: 30 above 680, the band is 20
    await hold(rig, EMIM, "3", "EUR")  # 139.26 against 140: inside
    await hold(rig, IWDA, "1", "USD")  # 126 against 180: 54 short, less than a unit
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.PAPER))
    assert roles["em_equity"].action == "hold"
    assert roles["world_equity"].action == "skip"
    assert "less than one whole unit" in roles["world_equity"].reason
    # 30 over is less than one IBGM: nothing to sell.
    assert roles["euro_govt_intermediate"].action == "skip"
    await hold(rig, IBGM, "5", "EUR")  # 887.50: 207.50 over, one unit to sell
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.PAPER))
    assert (roles["euro_govt_intermediate"].action, roles["euro_govt_intermediate"].quantity) == (
        "sell",
        1,
    )


async def test_a_holding_the_targets_no_longer_name_is_sold(rig: Any) -> None:
    rig.broker.quote(89005163, bid="20.00", ask="20.02", last="20.01")  # EXV1
    await hold(rig, 89005163, "10", "EUR")
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.PAPER))
    unlisted = roles["unlisted 89005163"]
    assert (unlisted.action, unlisted.quantity, unlisted.weight) == ("sell", 10, 0)


async def test_a_trade_below_the_minimum_or_without_a_commission_is_not_made(rig: Any) -> None:
    settings = with_trading(
        rig.settings,
        min_trade=rig.settings.trading.min_trade.model_copy(update={"absolute_base": Decimal(200)}),
    )
    roles = by_role(await rebalancer(rig, settings).survey(ExecutionMode.SHADOW))
    assert roles["em_equity"].action == "skip"
    assert "below the minimum trade 200.00" in roles["em_equity"].reason
    no_schedules = rig.settings.model_copy(
        update={"costs": rig.settings.costs.model_copy(update={"commissions": []})}
    )
    roles = by_role(await rebalancer(rig, no_schedules).survey(ExecutionMode.SHADOW))
    assert all("no commission schedule" in d.reason for d in roles.values())


def test_the_minimum_trade_combines_its_parts(shipped_settings: ObliskConfig) -> None:
    trading = shipped_settings.trading
    assert minimum_trade(trading, Decimal(1000)) == Decimal(50)  # max(50, 20)
    assert minimum_trade(trading, Decimal(10000)) == Decimal(200)  # max(50, 200)
    smaller = trading.model_copy(
        update={"min_trade": trading.min_trade.model_copy(update={"combine": "min"})}
    )
    assert minimum_trade(smaller, Decimal(10000)) == Decimal(50)
    only_fraction = trading.model_copy(
        update={"min_trade": trading.min_trade.model_copy(update={"absolute_base": None})}
    )
    assert minimum_trade(only_fraction, Decimal(1000)) == Decimal(20)
    only_absolute = trading.model_copy(
        update={"min_trade": trading.min_trade.model_copy(update={"fraction_of_managed": None})}
    )
    assert minimum_trade(only_absolute, Decimal(10000)) == Decimal(50)


# ── what leaves a role alone ──


async def test_a_role_whose_venue_is_closed_waits(rig: Any) -> None:
    rig.clock.now = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)  # Europe has closed
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.SHADOW))
    assert all("not tradable now" in d.reason for d in roles.values())


async def test_a_role_whose_listing_maps_to_no_venue_waits(rig: Any) -> None:
    directory = UniverseDirectory(
        i.model_copy(update={"listing_exchange": "NOWHERE"}) if i.conid == IBGM else i
        for i in UniverseDirectory.from_settings(rig.settings).instruments()
    )
    roles = by_role(await rebalancer(rig, directory=directory).survey(ExecutionMode.SHADOW))
    assert roles["euro_govt_intermediate"].action == "skip"
    assert "NOWHERE" in roles["euro_govt_intermediate"].reason
    assert roles["em_equity"].action == "buy"


async def test_a_role_without_a_price_has_no_commission(rig: Any) -> None:
    from obliskiq.decision.rebalance_basic import _Role

    unpriced = _Role("r", IBGM, Decimal("0.5"), None, Decimal(0), None, None)
    assert rebalancer(rig)._commission(unpriced, Decimal(1), "XAMS") is None


async def test_a_role_an_intent_works_on_waits(rig: Any) -> None:
    survey = rebalancer(rig)
    made = await survey.apply(await survey.survey(ExecutionMode.SHADOW))
    assert len(made) == 3
    roles = by_role(await survey.survey(ExecutionMode.SHADOW))
    assert all(d.reason == "an intent already works on it" for d in roles.values())
    # The open buys are committed against what the book may spend.
    plan = await survey.survey(ExecutionMode.SHADOW)
    assert plan.spendable is not None
    assert plan.spendable < Decimal(300)
    # A cancelled intent waiting for its retry still counts as worked on; another mode does not.
    first = made[0]
    for step in (IntentState.APPROVED, IntentState.STAGED, IntentState.SUBMITTED):
        await rig.book.advance(first.intent_id, step, "test")
    await rig.book.fact(first.intent_id, f"{RETRY_NEXT_WINDOW}: test")
    await rig.book.advance(first.intent_id, IntentState.CANCELLED, "test")
    roles = by_role(await survey.survey(ExecutionMode.SHADOW))
    assert all(d.reason == "an intent already works on it" for d in roles.values())
    roles = by_role(await survey.survey(ExecutionMode.PAPER))
    assert all(d.action == "buy" for d in roles.values())


async def test_a_role_without_a_price_or_an_instrument_is_skipped(rig: Any) -> None:
    del rig.broker.quotes[EMIM]
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.SHADOW))
    assert (roles["em_equity"].action, roles["em_equity"].reason) == ("skip", "no price")
    directory = UniverseDirectory(
        i for i in UniverseDirectory.from_settings(rig.settings).instruments() if i.conid != IBGM
    )
    roles = by_role(await rebalancer(rig, directory=directory).survey(ExecutionMode.SHADOW))
    assert roles["euro_govt_intermediate"].reason == "no instrument describes the conid"
    assert roles["euro_govt_intermediate"].symbol == "?"


async def test_a_role_not_confirmed_by_ibkr_is_skipped(rig: Any) -> None:
    survey = rebalancer(rig)
    survey.refuse({IWDA: "IBKR resolves IWDA on LSEETF to conid 1"})
    roles = by_role(await survey.survey(ExecutionMode.SHADOW))
    assert roles["world_equity"].reason == "not confirmed: IBKR resolves IWDA on LSEETF to conid 1"


async def test_a_dollar_line_is_valued_by_the_pairs_quote_without_a_ledger_rate(
    rig: Any,
) -> None:
    rig.broker.cash["USD"] = (Decimal(20000), None)
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.SHADOW))
    # 146.55 dollars at 1.165 to the euro.
    assert roles["world_equity"].action == "buy"
    assert roles["world_equity"].notional.quantize(Decimal("0.01")) == Decimal("125.79")
    del rig.broker.quotes[EURUSD]
    plan = await rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.blocked == "the USD cash cannot be valued in EUR"
    rig.broker.cash = {"EUR": (Decimal(10000), None)}
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.SHADOW))
    assert roles["world_equity"].reason == "no price"
    rig.broker.unavailable.add("snapshot")
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.SHADOW))
    assert all(d.reason == "no price" for d in roles.values())
    rig.broker.unavailable.clear()
    rig.broker.cash["GBP"] = (Decimal(100), None)  # no ledger rate and no configured pair
    plan = await rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.blocked == "the GBP cash cannot be valued in EUR"


# ── what leaves the book alone ──


async def test_the_kill_switch_a_freeze_or_an_unreadable_account_stops_the_plan(rig: Any) -> None:
    survey = rebalancer(rig)
    await rig.kill.turn_on(actor="test", reason="drill")
    killed = await survey.survey(ExecutionMode.SHADOW)
    assert killed.blocked == "the kill switch is on: no new decision"
    assert "managed capital" not in killed.render()
    await rig.kill.turn_off(actor="test", reason="drill over")
    await rig.store(
        repo.record_reconciliation,
        ReconcileRun(
            at=OPEN,
            status=ReconcileStatus.DRIFT,
            findings=(ReconcileFinding(kind="cash", detail="d", freezes=True),),
        ),
        anchor=False,
    )
    assert "reconciliation is drift" in str((await survey.survey(ExecutionMode.SHADOW)).blocked)
    await rig.store(
        repo.record_reconciliation,
        ReconcileRun(at=OPEN, status=ReconcileStatus.GREEN, findings=()),
        anchor=False,
    )
    rig.broker.unavailable.add("ledger")
    assert "the account cannot be read" in str((await survey.survey(ExecutionMode.SHADOW)).blocked)
    rig.broker.unavailable.clear()
    rig.broker.nav = None
    assert "nothing is managed" in str((await survey.survey(ExecutionMode.SHADOW)).blocked)


async def test_a_held_role_without_a_price_stops_the_plan(rig: Any) -> None:
    await hold(rig, IBGM, "3", "EUR")
    del rig.broker.quotes[IBGM]
    plan = await rebalancer(rig).survey(ExecutionMode.PAPER)
    assert plan.blocked == f"a held position has no price: conid {IBGM}"
    assert "nothing trades" in plan.render()


async def test_an_open_buy_that_cannot_be_valued_stops_the_plan(rig: Any) -> None:
    survey = rebalancer(rig)
    await survey.apply(await survey.survey(ExecutionMode.SHADOW))
    narrow = rebalancer(
        rig,
        directory=UniverseDirectory(
            i
            for i in UniverseDirectory.from_settings(rig.settings).instruments()
            if i.conid != IWDA
        ),
    )
    plan = await narrow.survey(ExecutionMode.SHADOW)
    assert plan.blocked == "an open buy cannot be valued in the base currency"


# ── shadow holdings and intents ──


async def test_shadow_holdings_are_the_sleeves_simulated_fills(rig: Any) -> None:
    survey = rebalancer(rig)
    (intent, *_) = await survey.apply(await survey.survey(ExecutionMode.SHADOW))
    stored = await rig.book.get(intent.intent_id)
    for step in (IntentState.APPROVED, IntentState.STAGED, IntentState.SUBMITTED):
        await rig.book.advance(intent.intent_id, step, "test")
    fill = Fill(
        execution_id="shadow-1",
        coid=stored.coid,
        conid=intent.conid,
        side=OrderSide.BUY,
        quantity=intent.terms.quantity or Decimal(1),
        price=Decimal("177.6"),
        commission=Decimal(3),
        executed_at=OPEN,
        simulated=True,
    )
    await rig.store(repo.upsert_fills, [fill])
    await rig.book.advance(intent.intent_id, IntentState.FILLED, "simulated")
    held = await survey.holdings(ExecutionMode.SHADOW)
    assert held == {intent.conid: intent.terms.quantity}
    assert await survey.holdings(ExecutionMode.PAPER) == {}  # nothing at the broker
    await survey.apply(await survey.survey(ExecutionMode.PAPER))  # paper intents: not shadow's
    assert await survey.holdings(ExecutionMode.SHADOW) == held


async def test_intents_are_priced_patiently_and_carry_no_stop(rig: Any) -> None:
    survey = rebalancer(rig)
    made = await survey.apply(await survey.survey(ExecutionMode.SHADOW))
    assert {i.conid for i in made} == {IWDA, EMIM, IBGM}
    for intent in made:
        assert intent.role is IntentRole.ENTRY
        assert intent.reason == "rebalance"
        assert intent.protective is None
        assert intent.sleeve is Sleeve.CORE
        assert intent.expires_at - intent.created_at == timedelta(minutes=480)
    ibgm = next(i for i in made if i.conid == IBGM)
    assert ibgm.terms.price.limit == Decimal("177.49")  # a tick inside the mid of 177.50


async def test_a_trade_that_cannot_be_priced_is_left_for_the_next_cycle(rig: Any) -> None:
    survey = rebalancer(rig)
    plan = await survey.survey(ExecutionMode.SHADOW)
    del rig.broker.quotes[EMIM]
    rig.broker.rules_by_conid[IBGM] = {"orderTypes": ["limit"], "incrementRules": []}
    made = await survey.apply(plan)
    assert [i.conid for i in made] == [IWDA]
    assert await rebalancer(rig, directory=UniverseDirectory(())).apply(plan) == []


async def test_a_trade_whose_rules_cannot_be_read_is_left(rig: Any) -> None:
    survey = rebalancer(rig)
    plan = await survey.survey(ExecutionMode.SHADOW)
    rig.broker.unavailable.add("rules")
    assert await survey.apply(plan) == []


# ── confirming the targets ──


async def test_each_target_is_confirmed_through_the_contract_search(rig: Any) -> None:
    directory = UniverseDirectory.from_settings(rig.settings)
    assert await confirm_targets(rig.settings, directory, rig.ibkr.secdef) == {}
    rig.broker.contracts[IWDA] = ("IWDA", "LSEETF", "USD")
    rig.broker.contracts[1] = ("EMIM", "AEB", "EUR")
    del rig.broker.contracts[EMIM]
    rig.broker.contracts.pop(IBGM)
    refused = await confirm_targets(rig.settings, directory, rig.ibkr.secdef)
    assert refused[EMIM] == "IBKR resolves EMIM on AEB to conid 1, not the pinned 153454120"
    assert refused[IBGM].startswith("its identity cannot be confirmed")
    empty = UniverseDirectory(())
    assert set((await confirm_targets(rig.settings, empty, rig.ibkr.secdef)).values()) == {
        "no instrument describes it"
    }


def test_a_capped_book_names_its_cap(shipped_settings: ObliskConfig) -> None:
    capital = shipped_settings.capital.model_dump()
    with pytest.raises(ValueError, match=r"needs capital\.cap_base"):
        CapitalConfig.model_validate(capital | {"cap_base": None})


# ── deposits and the tax lots ──


async def test_a_deposit_that_cannot_be_valued_stops_the_plan(rig: Any) -> None:
    deployment = rig.settings.capital.deployment.model_copy(update={"tranche_days": 5})
    whole = with_capital(rig.settings, mode="whole_account", cap_base=None, deployment=deployment)
    pounds = Deposit(
        amount=Decimal(50), currency="GBP", on=OPEN.date(), recorded_at=OPEN, recorded_by="o",
        matched_at=OPEN,
    )  # fmt: skip
    await rig.store(capital_repo.record_flow, pounds)  # the NAV holds it: the reader is fine
    plan = await rebalancer(rig, whole).survey(ExecutionMode.SHADOW)
    assert plan.blocked == "a deposit cannot be valued in EUR"


async def test_without_readable_lots_buys_proceed_unchecked_by_the_two_month_rule(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    survey = rebalancer(rig)

    async def unreadable() -> Any:
        raise TaxError("the lots are corrupt")

    monkeypatch.setattr(survey._tax, "hooks", unreadable)
    plan = await survey.survey(ExecutionMode.SHADOW)
    assert plan.blocked is None
    assert len(plan.trades) == 3
