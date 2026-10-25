"""The core rebalancer: the engine's target against holdings in integer units, only new cash inside
the outer bands and every vehicle back to its target beyond them, above the minimum trade and the
commission floor, within what the book may spend; and why a vehicle or the whole book is left
alone.

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
from core_rig import RIG_PINS, PinnedVehicles
from fake_broker import EURUSD, FakeBroker
from order_rig import build_rig
from service_rig import EMIM, IBGM, IWDA, SERVICE_VENUES, skeleton_market

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_runtime import CapitalConfig
from obliskiq.decision.core_rebalancer import (
    CoreRebalancer,
    RebalancePlan,
    confirm_vehicles,
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
from obliskiq.domain.portfolio import TargetStatus
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


def rebalancer(rig: Any, settings: ObliskConfig | None = None, **kw: Any) -> CoreRebalancer:
    return make_rebalancer(rig, settings, **kw)


def by_role(plan: RebalancePlan) -> dict[str, Any]:
    return {d.role: d for d in plan.decisions}


def with_capital(settings: ObliskConfig, **values: Any) -> ObliskConfig:
    return settings.model_copy(update={"capital": settings.capital.model_copy(update=values)})


def with_trading(settings: ObliskConfig, **values: Any) -> ObliskConfig:
    return settings.model_copy(update={"trading": settings.trading.model_copy(update=values)})


def with_bands(settings: ObliskConfig, absolute: str, relative: str) -> ObliskConfig:
    bands = settings.core.bands.model_copy(
        update={"absolute": Decimal(absolute), "relative": Decimal(relative)}
    )
    return settings.model_copy(update={"core": settings.core.model_copy(update={"bands": bands})})


#: A contract the rig quotes besides the three funds: it fills the cash role in the tests that
#: need one (iShares Core MSCI Europe, EXV1, in euros).
CASH_LINE = 89005163
WITH_CASH = {**RIG_PINS, "cash": (CASH_LINE, None)}


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


async def test_inside_the_outer_bands_only_new_cash_is_spent_and_nothing_is_sold(rig: Any) -> None:
    wide = with_bands(rig.settings, "0.5", "0.9")
    await hold(rig, IBGM, "5", "EUR")  # 887.50 against 646: far over, inside a wide band
    await hold(rig, EMIM, "3", "EUR")  # 139.26 against 133
    await hold(rig, IWDA, "1", "USD")  # 126 against 171: 45 short, less than a unit
    plan = await rebalancer(rig, wide).survey(ExecutionMode.PAPER)
    assert plan.portfolio is not None
    assert not plan.portfolio.rebalance
    roles = by_role(plan)
    assert roles["euro_govt_intermediate"].action == "hold"
    assert "a sale waits for the outer band" in roles["euro_govt_intermediate"].reason
    assert roles["em_equity"].action == "hold"
    assert roles["world_equity"].action == "skip"
    assert "less than one whole unit" in roles["world_equity"].reason
    assert "only new cash is spent" in plan.render()


async def test_new_cash_buys_the_vehicle_furthest_under_its_target(rig: Any) -> None:
    wide = with_bands(rig.settings, "0.5", "1")
    await hold(rig, IBGM, "3", "EUR")  # 532.50 against 646
    plan = await rebalancer(rig, wide).survey(ExecutionMode.PAPER)
    assert plan.portfolio is not None
    assert not plan.portfolio.rebalance
    roles = by_role(plan)
    assert (roles["world_equity"].action, roles["world_equity"].quantity) == ("buy", 1)
    assert (roles["em_equity"].action, roles["em_equity"].quantity) == ("buy", 2)
    # 113.50 short of 646 is inside the inner band's reach but less than one unit.
    assert roles["euro_govt_intermediate"].action == "skip"


async def test_beyond_an_outer_band_every_vehicle_goes_back_to_its_target(rig: Any) -> None:
    await hold(rig, IBGM, "5", "EUR")  # 887.50: 93 % of the book against a target of 68 %
    plan = await rebalancer(rig).survey(ExecutionMode.PAPER)
    assert plan.portfolio is not None
    assert plan.portfolio.rebalance
    roles = by_role(plan)
    # The target is 646, three units: the simulator's delta execution sells two.
    assert (roles["euro_govt_intermediate"].action, roles["euro_govt_intermediate"].quantity) == (
        "sell",
        2,
    )
    # The sale's proceeds are not spent in the cycle that makes it: what is left of the book's
    # cash (112.50 less the floor) cannot buy a unit of IWDA or two of EMIM.
    assert roles["world_equity"].action == "skip"
    assert "no cash for one unit" in roles["world_equity"].reason
    assert roles["em_equity"].action == "skip"
    assert "every vehicle goes to its target" in plan.render()


async def test_a_holding_the_targets_no_longer_name_is_sold(rig: Any) -> None:
    rig.broker.quote(89005163, bid="20.00", ask="20.02", last="20.01")  # EXV1
    await hold(rig, 89005163, "10", "EUR")
    plan = await rebalancer(rig).survey(ExecutionMode.PAPER)
    assert plan.portfolio is not None
    assert plan.portfolio.rebalance
    assert any("no role names it" in reason for reason in plan.portfolio.reasons)
    unlisted = by_role(plan)["unlisted 89005163"]
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
    from obliskiq.decision.core_rebalancer import _Role

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


async def test_a_role_without_a_price_is_held_in_cash(rig: Any) -> None:
    rig.broker.quote(CASH_LINE, bid="20.00", ask="20.02", last="20.01")
    cash = PinnedVehicles(WITH_CASH)
    del rig.broker.quotes[EMIM]
    plan = await rebalancer(rig, vehicles=cash).survey(ExecutionMode.SHADOW)
    assert plan.unfilled == {"em_equity": "its vehicle has no price"}
    assert plan.portfolio is not None
    em = next(r for r in plan.portfolio.roles if r.role == "em_equity")
    assert em.weight == 0
    assert "held in the cash role" in em.reason
    roles = by_role(plan)
    assert roles["cash"].weight > Decimal("0.13")  # the role's 14 % went to cash
    assert "unfilled em_equity" in plan.render()


async def test_a_book_the_variant_cannot_fill_is_not_evaluable(rig: Any) -> None:
    del rig.broker.quotes[EMIM]
    plan = await rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.portfolio is not None
    assert plan.portfolio.status is TargetStatus.NOT_EVALUABLE
    assert plan.blocked is not None
    assert plan.blocked.startswith("the core's target is not evaluable")
    assert "the cash role cash has no vehicle" in plan.blocked
    assert plan.decisions == []


async def test_a_vehicle_without_an_instrument_is_not_priced(rig: Any) -> None:
    directory = UniverseDirectory(
        i for i in UniverseDirectory.from_settings(rig.settings).instruments() if i.conid != IBGM
    )
    plan = await rebalancer(rig, directory=directory).survey(ExecutionMode.SHADOW)
    assert plan.unfilled["euro_govt_intermediate"] == "its vehicle has no price"


async def test_a_role_not_confirmed_by_ibkr_is_skipped(rig: Any) -> None:
    survey = rebalancer(rig)
    survey.refuse({IWDA: "IBKR resolves IWDA on LSEETF to conid 1"})
    plan = await survey.survey(ExecutionMode.SHADOW)
    roles = by_role(plan)
    assert roles["world_equity"].reason == "not confirmed: IBKR resolves IWDA on LSEETF to conid 1"
    assert plan.refused == {IWDA: "IBKR resolves IWDA on LSEETF to conid 1"}


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
    plan = await rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert plan.unfilled["world_equity"] == "its vehicle has no price"
    rig.broker.unavailable.add("snapshot")
    plan = await rebalancer(rig).survey(ExecutionMode.SHADOW)
    assert set(plan.unfilled) == {"cash", "world_equity", "em_equity", "euro_govt_intermediate"}
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


# ── confirming the vehicles ──


async def test_each_vehicle_is_confirmed_through_the_contract_search(rig: Any) -> None:
    directory = UniverseDirectory.from_settings(rig.settings)
    conids = [IWDA, EMIM, IBGM]
    assert await confirm_vehicles(directory, rig.ibkr.secdef, conids) == {}
    rig.broker.contracts[IWDA] = ("IWDA", "LSEETF", "USD")
    rig.broker.contracts[1] = ("EMIM", "AEB", "EUR")
    del rig.broker.contracts[EMIM]
    rig.broker.contracts.pop(IBGM)
    refused = await confirm_vehicles(directory, rig.ibkr.secdef, conids)
    assert refused[EMIM] == "IBKR resolves EMIM on AEB to conid 1, not the pinned 153454120"
    assert refused[IBGM].startswith("its identity cannot be confirmed")
    empty = UniverseDirectory(())
    assert set((await confirm_vehicles(empty, rig.ibkr.secdef, conids)).values()) == {
        "no instrument describes it"
    }


async def test_a_survey_confirms_each_chosen_contract_once_and_refuses_what_is_not(
    rig: Any,
) -> None:
    survey = rebalancer(rig, secdef=rig.ibkr.secdef)
    assert (await survey.survey(ExecutionMode.SHADOW)).refused == {}
    rig.broker.contracts[1] = ("EMIM", "AEB", "EUR")
    del rig.broker.contracts[EMIM]
    # Confirmed once: a contract the search stopped finding is not asked about again.
    assert (await survey.survey(ExecutionMode.SHADOW)).refused == {}
    fresh = rebalancer(rig, secdef=rig.ibkr.secdef)
    plan = await fresh.survey(ExecutionMode.SHADOW)
    assert set(plan.refused) == {EMIM}
    assert by_role(plan)["em_equity"].reason.startswith("not confirmed: IBKR resolves EMIM")


async def test_a_search_the_broker_refuses_leaves_the_vehicles_unconfirmed(rig: Any) -> None:
    rig.broker.contracts.clear()
    survey = rebalancer(rig, secdef=rig.ibkr.secdef)
    plan = await survey.survey(ExecutionMode.SHADOW)
    assert set(plan.refused) == {IWDA, EMIM, IBGM}
    assert all(why.startswith("its identity cannot be confirmed") for why in plan.refused.values())
    assert all(d.action == "skip" for d in plan.decisions)


async def test_a_search_that_breaks_confirms_nothing_this_survey_and_is_tried_again(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.decision import core_rebalancer

    async def broken(*args: Any) -> Any:
        raise RuntimeError("the search broke")

    survey = rebalancer(rig, secdef=rig.ibkr.secdef)
    monkeypatch.setattr(core_rebalancer, "confirm_vehicles", broken)
    plan = await survey.survey(ExecutionMode.SHADOW)
    assert set(plan.refused) == {IWDA, EMIM, IBGM}
    assert all(why == "the confirmation failed: RuntimeError" for why in plan.refused.values())
    monkeypatch.undo()
    assert (await survey.survey(ExecutionMode.SHADOW)).refused == {}


async def test_a_tilting_variant_without_stored_closes_is_not_evaluable(rig: Any) -> None:
    b = next(v for v in rig.settings.core.variants if v.name == "B")
    assert b.kind == "risk_balance"
    core = rig.settings.core.model_copy(update={"variant": "B"})
    plan = await rebalancer(rig, rig.settings.model_copy(update={"core": core})).survey(
        ExecutionMode.SHADOW
    )
    assert plan.portfolio is not None
    assert plan.portfolio.status is TargetStatus.NOT_EVALUABLE


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


# ── the last branches ──


async def test_a_vehicle_inside_the_no_trade_band_of_its_target_is_held(rig: Any) -> None:
    await hold(rig, IBGM, "5", "EUR")  # beyond the outer band: every vehicle goes to target
    await hold(rig, IWDA, "1", "USD")  # 126 against 171: one unit is the floor of 1.36
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.PAPER))
    assert roles["euro_govt_intermediate"].action == "sell"
    assert (roles["world_equity"].action, roles["world_equity"].reason) == (
        "hold",
        "inside its band",
    )


async def test_new_cash_leaves_a_vehicle_inside_the_inner_band_alone(rig: Any) -> None:
    wide = with_bands(rig.settings, "0.5", "1")
    inner = rig.settings.trading.inner_band.model_copy(
        update={"absolute": Decimal("0.5"), "relative": Decimal("1")}
    )
    await hold(rig, EMIM, "2", "EUR")  # 92.84 against 133: 40 short, inside a wide inner band
    plan = await rebalancer(rig, with_trading(wide, inner_band=inner)).survey(ExecutionMode.PAPER)
    assert by_role(plan)["em_equity"].action == "hold"


async def test_a_sale_waits_while_an_open_reservation_is_not_covered(rig: Any) -> None:
    from obliskiq.domain.capital import WithdrawalReservation

    await hold(rig, IBGM, "5", "EUR")
    await rig.store(
        capital_repo.record_reservation,
        WithdrawalReservation(
            amount=Decimal(900),
            currency="EUR",
            due_date=OPEN.date() + timedelta(days=30),
            reason="a test withdrawal",
            created_at=OPEN,
            created_by="owner",
            updated_at=OPEN,
        ),
    )
    roles = by_role(await rebalancer(rig).survey(ExecutionMode.PAPER))
    assert roles["euro_govt_intermediate"].action == "skip"
    assert "the reservation planner chooses those sales" in roles["euro_govt_intermediate"].reason


async def test_a_vehicle_without_an_instrument_or_a_price_is_skipped_with_why(rig: Any) -> None:
    from obliskiq.capital.valuation import UnitValue
    from obliskiq.decision.core_rebalancer import _Role
    from obliskiq.domain.portfolio import TargetPortfolio

    survey = rebalancer(rig)
    unknown = _Role.valued("a", Decimal("0.5"), Decimal(0), UnitValue(1, None, None, None))
    portfolio = TargetPortfolio(
        variant="rig", as_of=OPEN.date(), trend_as_of=None, status=TargetStatus.READY,
        rebalance=True, roles=(), vehicles=(), reasons=(),
    )  # fmt: skip
    (row,) = survey._decide(
        portfolio, [unknown], set(), Decimal(950), Decimal(1000), Decimal(900), OPEN,
        refused={}, hooks=None, sells_paused=False,
    )  # fmt: skip
    assert (row.action, row.reason) == ("skip", "no instrument describes the conid")


def test_a_partner_without_a_price_is_not_used_and_a_primary_without_one_unfills_its_role() -> None:
    from core_rig import vehicle

    from obliskiq.capital.valuation import UnitValue
    from obliskiq.decision.core_vehicles import RoleChoice, VehicleChoice

    priced = UnitValue(1, None, Decimal(10), Decimal(1))
    unpriced = UnitValue(2, None, None, None)
    choice = VehicleChoice(
        {
            "a": RoleChoice("a", vehicle(1), vehicle(2), ""),
            "b": RoleChoice("b", vehicle(2), None, ""),
        }
    )
    plan = RebalancePlan(mode=ExecutionMode.SHADOW, at=OPEN)
    vehicles = CoreRebalancer._priced(choice, {1: priced, 2: unpriced}, plan)
    assert {role: (v.primary, v.partner) for role, v in vehicles.items()} == {"a": ("1", None)}
    assert plan.unfilled == {"b": "its vehicle has no price"}
