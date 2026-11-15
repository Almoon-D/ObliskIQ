"""A thesis held as a long option, over the fake broker: one bearish owner idea from ``idea add``
to a shadow long put, its exit and its grade beside the underlying twin; and the failure branches
of the path (DECISIONS D157, D167).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from eligibility_double import AllowlistEligibility
from options_rig import EXPIRIES, SPY, seed_options
from service_rig import CURRENCY, ServiceRig
from thesis_rig import draft

from obliskiq.cli import idea as idea_cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.decision.rebalance_thesis import ThesisPlan, ThesisRebalancer
from obliskiq.decision.thesis_intake import ThesisIntake
from obliskiq.domain.enums import OptionRight
from obliskiq.domain.market import ChainRow
from obliskiq.domain.options import LedgerEventType
from obliskiq.domain.orders import ExecutionMode, IntentRole, IntentState, OrderSide, Sleeve
from obliskiq.domain.thesis import Expression, Thesis, ThesisState
from obliskiq.engines.options_sleeve.penalty import PenaltyLedger
from obliskiq.orchestration.assemble import build_thesis_book
from obliskiq.orchestration.parts import build_capital
from obliskiq.store import open_database
from obliskiq.store import options as options_repo
from obliskiq.store import orders as repo
from obliskiq.store import theses as thesis_repo
from obliskiq.store import thesis_options as link_repo

D = Decimal
#: The contracts of the rig's chain are numbered from here, in the order it writes them.
FIRST = 900001
#: Monday 26 October 2026, 14:30 UTC: the NYSE and the US option exchanges trade.
NEW_YORK = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
MONDAY = NEW_YORK.date()
#: Before the far expiry's sell-by session, after the near one's.
HORIZON = "2027-03-15"
FAR = EXPIRIES[1]
#: SPY falls to 420 or the idea is wrong above 530: at 500 the gain is 2.67 times the loss.
FALL = {"symbol": "SPY", "target": D(420), "invalidation": D(530), "view": "bearish"}


def sized(settings: ObliskConfig, cap_base: int = 100000, **sleeve: Any) -> ObliskConfig:
    """A book of 100,000: the thesis book works on 10,000, the option budget is 5,000 and one
    block 1,000."""
    capital = settings.capital.model_copy(update={"cap_base": D(cap_base)})
    options = settings.option_sleeve.model_copy(update=sleeve)
    return settings.model_copy(update={"capital": capital, "option_sleeve": options})


def quote_spy(s: ServiceRig, last: str) -> None:
    price = D(last)
    s.broker.quote(
        SPY, bid=str(price - D("0.02")), ask=str(price + D("0.02")), last=last, sigma="15%"
    )


def quote_option(s: ServiceRig, conid: int, mid: str, **kw: Any) -> None:
    price = D(mid)
    s.broker.quote(
        conid,
        bid=str(price - D("0.01")),
        ask=str(price + D("0.01")),
        last=mid,
        sigma="20%",
        option=True,
        **kw,
    )


async def started(make: Any, shipped_settings: ObliskConfig, **kw: Any) -> ServiceRig:
    """The service on Monday with SPY at 500, its chain of the Thursday before (every contract
    numbered and quoted), a year of its closes, the dollar rate and a cheap implied vol."""
    seed = {
        key: kw.pop(key)
        for key in ("iv_percentile", "open_interest", "history", "risk_free", "chain")
        if key in kw
    }
    chain = seed.pop("chain", True)
    book_share = kw.pop("capital_share", None)
    settings = sized(shipped_settings, **kw)
    if book_share is not None:
        thesis = settings.thesis.model_copy(update={"capital_share": D(book_share)})
        settings = settings.model_copy(update={"thesis": thesis})
    s: ServiceRig = await make(settings=settings)
    s.clock.wall.now = NEW_YORK
    s.broker.cash = {"EUR": (D(200000), None), "USD": (D(20000), D("0.86"))}
    s.broker.nav = D(250000)
    CURRENCY[SPY] = "USD"
    quote_spy(s, "500")
    s.rows = ()  # type: ignore[attr-defined]
    if not chain:
        return s
    settings = s.path.settings
    seeded = await s.rig.store(
        lambda store: seed_options(
            store,
            settings,
            first_conid=FIRST,
            **{"history": 260, "risk_free": 4.0, **seed},
        )
    )
    for row in seeded.rows:
        CURRENCY[row.option_conid] = "USD"
        quote_option(s, row.option_conid, f"{row.mid:.2f}")
    s.rows = seeded.rows  # type: ignore[attr-defined]
    return s


def put(s: ServiceRig, strike: float, expiry: date = FAR) -> ChainRow:
    return next(
        row
        for row in s.rows  # type: ignore[attr-defined]
        if row.strike == strike and row.expiry == expiry and row.right is OptionRight.PUT
    )


def book(s: ServiceRig) -> ThesisRebalancer:
    settings = s.path.settings
    parts = build_capital(settings, s.path, now=s.clock.now)
    return build_thesis_book(settings, s.path, parts, now=s.clock.now)


async def idea(s: ServiceRig, **changes: Any) -> Thesis:
    intake = ThesisIntake(s.path.settings, worker=s.rig.worker, now=s.clock.now)
    fields = {**FALL, "expression": Expression.OPTION, "horizon": date.fromisoformat(HORIZON)}
    stored = await intake.submit(draft(**{**fields, **changes}), actor="test")
    assert not isinstance(stored, list), stored
    return stored


async def state(s: ServiceRig, thesis_id: int = 1) -> Thesis:
    found = await s.rig.store(thesis_repo.get_thesis, thesis_id)
    assert found is not None
    return found


async def log(s: ServiceRig, thesis_id: int = 1) -> list[str]:
    return [
        f"{e.kind.value}: {e.detail}" for e in await s.rig.store(thesis_repo.log_entries, thesis_id)
    ]


async def thesis_intents(s: ServiceRig) -> list[Any]:
    every = await s.rig.store(repo.intents_in_states, list(IntentState))
    return [i.intent for i in every if i.intent.thesis_id is not None]


async def work(s: ServiceRig, minutes: int = 30) -> None:
    """Heartbeats for ``minutes``: reprices reach the touch and simulated orders fill."""
    for _ in range(minutes // 5):
        s.clock.wall.now += timedelta(minutes=5, seconds=1)
        await s.service.jobs.heartbeat()


async def cycle(s: ServiceRig) -> ThesisPlan:
    result = await s.service.jobs.cycle("test", s.clock.now().date())
    assert result.theses is not None
    return result.theses


async def surveyed(s: ServiceRig) -> ThesisPlan:
    return await book(s).survey(ExecutionMode.SHADOW)


def sppw_of(plan: ThesisPlan) -> Any:
    return next(d for d in plan.decisions if d.thesis.instrument.symbol == "SPPW")


def only(plan: ThesisPlan) -> Any:
    (decision,) = plan.decisions
    return decision


def codes(plan: ThesisPlan) -> list[str]:
    return [finding.code.value for finding in only(plan).findings]


async def pool(s: ServiceRig, day: date = MONDAY) -> Any:
    settings = s.path.settings
    return await s.rig.store(
        lambda store: PenaltyLedger(store, settings).status(budget=D(5000), as_of=day)
    )


# ── acceptance ──


async def test_a_bearish_owner_idea_goes_from_idea_add_to_a_shadow_put_its_exit_and_its_grade(
    make: Any, shipped_settings: ObliskConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = await started(make, shipped_settings)
    config = shipped_settings.store
    monkeypatch.setenv("OBLISK_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(idea_cli, "utc_now", s.clock.now)
    monkeypatch.setattr(
        idea_cli,
        "open_store",
        lambda _settings, _dir: open_database(
            s.rig.db_path, busy_timeout_ms=config.busy_timeout_ms, synchronous=config.synchronous
        ),
    )

    def command(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        status = run(["idea", *argv], env_file=None, stdout=out, stderr=out)
        return status, out.getvalue()

    fall = ("add", "SPY", "--expect", "US equities fall as earnings disappoint", "--target",
            "420", "--invalidation", "530", "--horizon", HORIZON, "--bearish")  # fmt: skip
    # Held as shares a fall would be a short: refused, with the way to say it.
    status, said = await asyncio.to_thread(command, *fall)
    assert (status, "short: its target 420 is below its invalidation 530" in said) == (
        EXIT_INVALID,
        True,
    )
    status, said = await asyncio.to_thread(command, *fall, "--as", "option")
    assert status == EXIT_OK, said
    assert "stored: T1 SPY candidate (owner, normal, bearish, as option)" in said

    # Cycle one: the sleeve's gates pass on a put of the far expiry; one block, one contract.
    plan = await cycle(s)
    entered = only(plan)
    assert entered.action == "enter", plan.render()
    chosen = entered.option
    assert (chosen.right, chosen.expiry, chosen.contracts) == (OptionRight.PUT, FAR, 1)
    assert chosen.close_by >= date.fromisoformat(HORIZON)
    row = put(s, chosen.strike)
    assert chosen.option_conid == row.option_conid
    premium = D(repr(row.mid)) * 100 / D("1.1683")  # the chain's mid, in euros
    assert abs(entered.size_base - premium) < D("0.01")
    held = await state(s)
    assert (held.state, held.mode, held.held_as) == (
        ThesisState.APPROVED,
        ExecutionMode.SHADOW,
        Expression.OPTION,
    )
    (entry,) = await thesis_intents(s)
    assert (entry.conid, entry.side, entry.sleeve, entry.role) == (
        row.option_conid,
        OrderSide.BUY,
        Sleeve.SATELLITE,
        IntentRole.ENTRY,
    )
    assert (entry.terms.quantity, entry.protective) == (D(1), None)  # the loss is the premium
    assert entry.terms.price.kind != "market"
    await work(s)
    assert s.broker.placements == []  # shadow: nothing reached the broker

    # Cycle two: the fill opens the sleeve's position and commits its premium; it is held.
    plan = await cycle(s)
    assert (only(plan).action, only(plan).held) == ("hold", D(1)), plan.render()
    assert (await state(s)).state is ThesisState.ACTIVE
    (position,) = await s.rig.store(options_repo.open_positions, as_of=MONDAY)
    assert (position.contracts, position.target, position.close_by) == (1, 420.0, chosen.close_by)
    status_now = await pool(s)
    assert status_now.committed_premium == position.premium_base > 0
    assert status_now.available == D(5000) - position.premium_base

    # SPY falls through the target: the thesis's own exit sells the put, far above its premium.
    quote_spy(s, "415")
    quote_option(s, row.option_conid, "62")
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason, only(plan).quantity) == ("exit", "target", D(1))
    (twin,) = plan.twins
    assert (twin.thesis_id, twin.price, twin.reason) == (1, D(415), "target")
    await work(s)
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason) == ("close", "target"), plan.render()
    closed = await state(s)
    assert (closed.state, closed.exit_reason) == (ThesisState.CLOSED, "target")
    assert await s.rig.store(options_repo.open_positions, as_of=MONDAY) == ()
    (graded,) = await s.rig.store(thesis_repo.grades, as_of=MONDAY)
    assert (graded.held_as, graded.reached, graded.quantity) == (Expression.OPTION, True, D(1))
    assert graded.payoff > 2  # the put made several times its premium

    # The pair: the option's result from the ledger, the twin at the base size of 1,250.
    (pair,) = await s.rig.store(link_repo.pairs, as_of=MONDAY)
    assert (pair.right, pair.exit_reason, pair.twin_size_base) == (OptionRight.PUT, "target", 1250)
    assert pair.option_pnl > 3000
    assert pair.twin_pnl == D(1250) * (1 - D(415) / D(500))  # a fall is the put's twin's gain
    events = await s.rig.store(options_repo.ledger_events, as_of=MONDAY)
    assert [event.event_type for event in events] == [
        LedgerEventType.PREMIUM_COMMITTED,
        LedgerEventType.OPTION_GAIN,
    ]
    after = await pool(s)
    assert (after.penalty_balance, after.committed_premium) == (0, 0)
    assert (await cycle(s)).decisions == []  # the slot is free and holds nothing

    await s.service.jobs.digest(MONDAY)
    digest = s.notifier.digest()
    assert "options: premium at risk 0.00 in 0 position(s), penalty balance 0.00" in digest
    assert "option record, puts: 1 closed on " in digest
    assert "1 twin(s) in the underlying at the base size made +212.50" in digest
    assert "record, held as option: 1 closed, 1 reached their target" in digest
    status, said = await asyncio.to_thread(command, "show", "T1")
    assert f"held as 1 long put(s): SPY {FAR.isoformat()} {chosen.strike:g} PUT" in said
    assert "its twin in the underlying, 1250.00 entered at 500.0: left at 415.00 (target):" in said
    assert "17.00%, +212.50" in said
    assert "grade: reached its target; payoff on the premium " in said
    assert [line.split(":")[0] for line in await log(s)] == [
        "intake",
        "state",
        "order",
        "state",
        "order",
        "state",
    ]


# ── failure branches ──


async def entered(s: ServiceRig, **changes: Any) -> Any:
    """One idea entered and filled as a put: the decision that chose its contract."""
    await idea(s, **changes)
    decision = only(await cycle(s))
    assert decision.action == "enter", decision.reason
    await work(s)
    assert only(await cycle(s)).action == "hold"
    return decision


async def test_an_option_without_a_real_time_quote_is_not_bought(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    for row in s.rows:  # type: ignore[attr-defined]
        quote_option(s, row.option_conid, f"{row.mid:.2f}", availability="DP")  # delayed
    await idea(s)
    assert only(await cycle(s)).action == "enter"  # the sleeve chose on its stored chain
    await work(s)
    every = await s.rig.store(repo.intent_fills)
    assert [e for e in every if e.sleeve is Sleeve.SATELLITE] == []  # the gate cleared no order
    assert await s.rig.store(options_repo.open_positions, as_of=MONDAY) == ()
    (intent,) = await thesis_intents(s)
    assert intent.state is not IntentState.FILLED
    # The same with the underlying delayed, and with no quote of the option at all.
    for row in s.rows:  # type: ignore[attr-defined]
        quote_option(s, row.option_conid, f"{row.mid:.2f}")
    s.broker.quote(SPY, bid="499.98", ask="500.02", last="500", sigma="15%", availability="DP")
    await work(s)
    assert await s.rig.store(options_repo.open_positions, as_of=MONDAY) == ()


async def test_an_option_with_no_quote_at_all_waits(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    for row in s.rows:  # type: ignore[attr-defined]
        del s.broker.quotes[row.option_conid]
    await idea(s)
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["no_quote"])


async def test_an_illiquid_chain_is_not_bought(make: Any, shipped_settings: ObliskConfig) -> None:
    s = await started(make, shipped_settings, open_interest=100)  # below liquidity's 500
    await idea(s)
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["option_gates"])
    assert "none of the 8 puts of SPY cleared the sleeve's gates" in only(plan).reason
    assert "open_interest 8" in only(plan).reason
    assert await book(s).apply(plan) == []
    assert (await log(s))[-1].startswith("withheld: option_gates: none of the 8 puts of SPY")


async def test_a_premium_above_the_option_budget_is_not_bought(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, cap_base=20000)
    await idea(s)
    plan = await surveyed(s)  # a budget of 1,000 against puts of 1,178 and more that pay
    assert (only(plan).action, codes(plan)) == ("wait", ["option_gates"])
    assert "price_ceiling" in only(plan).reason


async def test_a_premium_above_the_books_capital_is_not_bought(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, capital_share="0.005")  # a book of 500
    await idea(s)
    plan = await surveyed(s)
    assert plan.capital == D(500)
    assert (only(plan).action, codes(plan)) == ("wait", ["option_gates"])
    assert "price_ceiling" in only(plan).reason


async def test_a_horizon_no_tenor_covers_is_not_bought(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s, horizon=date(2027, 6, 30))  # after the far expiry itself
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["option_gates"])
    assert "horizon_cover 8" in only(plan).reason


async def test_a_put_on_an_instrument_without_options_is_nothing_with_its_reason(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, chain=False)
    intake = ThesisIntake(s.path.settings, worker=s.rig.worker, now=s.clock.now)
    fall = {"target": D(30), "invalidation": D(50), "view": "bearish"}
    refused = await intake.submit(
        draft(symbol="SPPW", expression=Expression.AUTO, **fall), actor="test"
    )
    assert isinstance(refused, list)
    assert [(f.code.value, f.permanent) for f in refused] == [("no_options", True)]
    assert "SPPW cannot be held as an option, so nothing is held" in refused[0].reason
    outside = await intake.submit(
        draft(symbol=None, candidate="x", isin="US0378331005", expression="option"), actor="test"
    )
    assert isinstance(outside, list)
    assert [f.code.value for f in outside] == ["no_options"]
    # An instrument whose chain is collected, with no chain in the store yet: it waits.
    await idea(s)
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["no_options"])
    assert only(plan).reason == f"no_options: no put of SPY is in a chain collected by {MONDAY}"
    assert await thesis_intents(s) == []


async def test_auto_takes_the_call_when_the_gates_pass_and_the_instrument_when_they_do_not(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    rise = {"target": D(600), "invalidation": D(470), "view": "bullish",
            "horizon": date(2027, 1, 15)}  # fmt: skip
    await idea(s, expression=Expression.AUTO, **rise)
    plan = await surveyed(s)
    assert only(plan).action == "enter", plan.render()
    assert (only(plan).option.right, only(plan).option.expiry) == (OptionRight.CALL, FAR)
    assert f"T1 SPY as SPY {FAR.isoformat()}" in plan.render()


async def test_auto_on_an_underlying_the_account_may_not_buy_is_the_option_or_nothing(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    # A dear implied vol: no option. SPY itself is not for this account, so nothing is held.
    s = await started(make, shipped_settings, iv_percentile=0.9)
    rise = {"target": D(600), "invalidation": D(470), "view": "bullish",
            "horizon": date(2027, 1, 15)}  # fmt: skip
    await idea(s, expression=Expression.AUTO, **rise)
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["option_gates", "not_tradable"])
    assert "refused by iv_percentile 10" in only(plan).reason


async def test_auto_on_an_instrument_without_options_is_held_as_the_instrument(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, chain=False)
    s.clock.wall.now = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # Xetra trades
    sppw = 355720826
    CURRENCY[sppw] = "EUR"
    s.broker.quote(sppw, bid="44.98", ask="45.02", last="45", sigma="15%")
    await s.rig.store(_bars, sppw)
    await idea(s, symbol="SPPW", target=D(60), invalidation=D(40), view="bullish",
               expression=Expression.AUTO)  # fmt: skip
    plan = await surveyed(s)
    assert (only(plan).action, only(plan).option) == ("enter", None), plan.render()
    (intent,) = await book(s).apply(plan)
    assert (intent.conid, intent.protective is not None) == (sppw, True)
    held = await state(s)
    assert (held.expression, held.held_as) == (Expression.AUTO, Expression.SHARES)
    assert "withheld: not held as an option: no_options: no option chain is collected on SPPW" in (
        " | ".join(await log(s))
    )


def _bars(store: Any, conid: int) -> None:
    from obliskiq.domain.enums import AssetClass, InstrumentRole
    from obliskiq.domain.instrument import Instrument
    from obliskiq.domain.market import Bar
    from obliskiq.store import repos

    repos.upsert_instrument(
        store,
        Instrument(
            conid=conid, symbol="SPPW", listing_exchange="IBIS2", currency="EUR",
            asset_class=AssetClass.ETF, domicile=None, ucits=None, kid_status=None,
            share_class=None, lot_size=None, option_exchange=None, history_exchange=None,
            roles=frozenset({InstrumentRole.DATA}), superseded_by=None,
            created_at=NEW_YORK - timedelta(days=90), superseded_at=None,
        ),
    )  # fmt: skip
    days = [MONDAY - timedelta(days=n) for n in range(1, 40)]
    repos.upsert_bars(
        store,
        [
            Bar(conid=conid, session_date=d, open=None, high=None, low=None, close=45.0,
                volume=100000, source="t")
            for d in days
            if d.weekday() < 5
        ],
    )  # fmt: skip


async def test_the_thesiss_invalidation_sells_the_put_and_books_its_loss_at_detection(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    chosen = (await entered(s)).option
    quote_spy(s, "531")  # above the invalidation: the fall is wrong
    quote_option(s, chosen.option_conid, "4")
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason) == ("exit", "invalidation")
    assert only(plan).detected_loss > 0
    events = await s.rig.store(options_repo.ledger_events, as_of=MONDAY)
    assert [e.event_type for e in events][-1] is LedgerEventType.LOSS_DETECTED
    booked = (await pool(s)).penalty_balance
    assert booked == 2 * only(plan).detected_loss  # the multiple, at detection
    await work(s)
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason) == ("close", "invalidation")
    events = await s.rig.store(options_repo.ledger_events, as_of=MONDAY)
    assert [e.event_type for e in events][-1] is LedgerEventType.LOSS_CLOSED
    after = await pool(s)
    (pair,) = await s.rig.store(link_repo.pairs, as_of=MONDAY)
    assert after.penalty_balance == -2 * pair.option_pnl > 0  # reconciled to the realised loss
    assert after.committed_premium == 0
    assert pair.twin_pnl == D(1250) * -(D(531) / D(500) - 1)  # the twin lost with it
    (graded,) = await s.rig.store(thesis_repo.grades, as_of=MONDAY)
    assert (graded.reached, graded.payoff < 0) == (False, True)


async def test_the_penalty_balance_blocks_a_new_block_until_gains_repay_it(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    chosen = (await entered(s)).option
    quote_spy(s, "531")
    quote_option(s, chosen.option_conid, "4")  # most of the premium is lost
    await cycle(s)
    await work(s)
    assert only(await cycle(s)).action == "close"
    locked = await pool(s)
    assert locked.penalty_balance > D(1500)  # twice the loss
    # A second fall, with a budget the penalty now exceeds.
    quote_spy(s, "500")
    tight = sized(s.path.settings, cap_base=30000)  # a budget of 1,500
    small = await make(settings=tight)
    await idea(small)
    plan = await book(small).survey(ExecutionMode.SHADOW)
    assert codes(plan) == ["option_budget"]
    assert "locked by penalty" in only(plan).reason
    assert only(plan).action == "wait"
    # A share idea that reaches its target repays the balance one for one with its gain.
    sppw = 355720826
    CURRENCY[sppw] = "EUR"
    s.broker.quote(sppw, bid="44.98", ask="45.02", last="45", sigma="15%")
    await s.rig.store(_bars, sppw)
    await idea(s, symbol="SPPW", target=D(60), invalidation=D(40), view="bullish",
               expression=Expression.SHARES)  # fmt: skip
    assert sppw_of(await cycle(s)).action == "enter"
    await work(s, 20)  # Xetra closes at 16:30 UTC
    assert sppw_of(await cycle(s)).action == "hold"
    s.broker.quote(sppw, bid="60.98", ask="61.02", last="61", sigma="15%")
    assert sppw_of(await cycle(s)).reason == "target"
    await work(s, 20)
    assert sppw_of(await cycle(s)).action == "close"
    events = await s.rig.store(options_repo.ledger_events, as_of=MONDAY)
    repaid = events[-1]
    assert (repaid.event_type, repaid.exit_reason) == (LedgerEventType.QUALIFYING_GAIN, "target")
    assert (await pool(s)).penalty_balance == max(D(0), locked.penalty_balance - repaid.amount)
    assert repaid.amount > 300  # 27 shares, some 16 a share


async def test_a_confirmed_zombie_is_sold_and_its_penalty_booked_once(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    chosen = (await entered(s)).option
    settings = s.path.settings
    key = (chosen.expiry, chosen.strike)
    # Forty days before the expiry SPY is well above the strike, and a bid is still there.
    for day in (date(2027, 3, 9), date(2027, 3, 10)):
        s.clock.wall.now = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
        quote_spy(s, "525")
        quote_option(s, chosen.option_conid, "2.40")
        await s.rig.store(
            lambda store, day=day: seed_options(
                store, settings, as_of=day, first_conid=FIRST + 9, spot=525.0, risk_free=4.0,
                bids={key: 2.4}, expiries=(FAR,),
            )
        )  # fmt: skip
        thesis_book = book(s)
        plan = await thesis_book.survey(ExecutionMode.SHADOW)
        if day.day == 9:
            assert only(plan).action == "hold", plan.render()  # one session never triggers
            assert only(plan).review is not None
            await thesis_book.apply(plan)
    assert (only(plan).action, only(plan).reason) == ("exit", "zombie"), plan.render()
    await thesis_book.apply(plan)
    events = await s.rig.store(options_repo.ledger_events, as_of=date(2027, 3, 10))
    assert [e.event_type for e in events] == [
        LedgerEventType.PREMIUM_COMMITTED,
        LedgerEventType.LOSS_DETECTED,
    ]
    await work(s, 120)
    plan = await book(s).survey(ExecutionMode.SHADOW)
    assert (only(plan).action, only(plan).reason) == ("close", "zombie")
    await book(s).apply(plan)
    assert (await state(s)).exit_reason == "zombie"
    # The twin outlives the option: the thesis's own exits have not been reached.
    link = await s.rig.store(link_repo.get, 1)
    assert link.twin_open
    (pair,) = await s.rig.store(link_repo.pairs, as_of=date(2027, 3, 10))
    assert (pair.exit_reason, pair.twin_pnl) == ("zombie", None)
    # After the horizon the twin leaves on its own, at the underlying's price.
    s.clock.wall.now = datetime(2027, 3, 16, 14, 30, tzinfo=UTC)
    plan = await book(s).survey(ExecutionMode.SHADOW)
    assert plan.decisions == []
    assert [(t.price, t.reason) for t in plan.twins] == [(D(525), "time_window")]
    await book(s).apply(plan)
    (pair,) = await s.rig.store(link_repo.pairs, as_of=date(2027, 3, 16))
    assert pair.twin_pnl == D(1250) * -(D(525) / D(500) - 1)


async def test_an_option_is_sold_on_its_mandatory_close_session(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    tenor = shipped_settings.tenor.model_copy(update={"require_theta_budget_check": False})
    s = await started(make, shipped_settings.model_copy(update={"tenor": tenor}))
    # A horizon on the far expiry's own sell-by session: the tenor just covers it.
    # (The theta budget would refuse so late a horizon; it is switched off to reach this exit.)
    chosen = (await entered(s, horizon=date(2027, 4, 2))).option
    assert chosen.close_by == date(2027, 4, 2)
    s.clock.wall.now = datetime(2027, 4, 1, 14, 30, tzinfo=UTC)
    assert only(await surveyed(s)).action == "hold"
    s.clock.wall.now = datetime(2027, 4, 2, 14, 30, tzinfo=UTC)
    plan = await surveyed(s)
    assert (only(plan).action, only(plan).reason, only(plan).quantity) == (
        "exit",
        "mandatory_close",
        D(1),
    )


async def test_the_scaled_exit_sells_half_a_block_once_and_one_contract_runs_whole(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, block_share=D("0.6"))  # a block of 3,000
    decision = await entered(s)
    chosen = decision.option
    assert chosen.contracts == 2
    double = D(repr(put(s, chosen.strike).mid)) * 2 + 1
    quote_option(s, chosen.option_conid, f"{double:.2f}")  # twice the premium paid
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason, only(plan).quantity) == (
        "exit",
        "scaled_exit",
        D(1),
    )
    await work(s)
    plan = await cycle(s)
    assert (only(plan).action, only(plan).held) == ("hold", D(1))  # once: the rest runs
    assert (await state(s)).state is ThesisState.ACTIVE
    assert (await pool(s)).committed_premium > 0  # released by the close alone


async def test_no_path_writes_an_option(make: Any, shipped_settings: ObliskConfig) -> None:
    s = await started(make, shipped_settings)
    chosen = (await entered(s)).option
    parts = build_capital(s.path.settings, s.path, now=s.clock.now)
    # A sale of two contracts against the one held would open a written option.
    naked = await parts.maker.make(
        conid=chosen.option_conid, side=OrderSide.SELL, quantity=D(2), sleeve=Sleeve.SATELLITE,
        reason="override", mode=ExecutionMode.SHADOW, thesis_id="T1",
    )  # fmt: skip
    assert not isinstance(naked, str)
    await work(s)
    fills = [
        e.fill for e in await s.rig.store(repo.intent_fills) if e.fill.conid == chosen.option_conid
    ]
    assert [(f.side, f.quantity) for f in fills] == [(OrderSide.BUY, D(1))]
    # Every order the book itself makes on an option is a buy to open or a sell of what it holds.
    plan = await surveyed(s)
    assert only(plan).held == D(1)


async def test_an_entry_that_did_not_fill_is_made_again_while_its_rules_hold(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    for row in s.rows:  # type: ignore[attr-defined]
        quote_option(s, row.option_conid, f"{row.mid:.2f}", availability="DP")  # delayed
    await idea(s)
    chosen = only(await cycle(s)).option
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["working"])
    # The next day the order has ended unfilled: the same contract and block again.
    s.clock.wall.now = NEW_YORK + timedelta(days=1)
    await work(s, 10)  # the executor ends the expired intent
    mid = f"{put(s, chosen.strike).mid:.2f}"
    quote_option(s, chosen.option_conid, mid)
    plan = await surveyed(s)
    assert (only(plan).action, only(plan).quantity) == ("enter", D(1)), plan.render()
    assert only(plan).reason == f"its entry again: 1 x {chosen.symbol}"
    assert only(plan).option == chosen
    # Not while the kill switch is on, without a quote, or above what the pool allows.
    await s.path.kill.turn_on(actor="test", reason="drill")
    assert codes(await surveyed(s)) == ["entries_blocked"]
    await s.path.kill.turn_off(actor="test", reason="over")
    del s.broker.quotes[chosen.option_conid]
    assert codes(await surveyed(s)) == ["no_quote"]
    quote_option(s, chosen.option_conid, "60")
    plan = await surveyed(s)
    assert (codes(plan), "above what the pool" in only(plan).reason) == (["option_budget"], True)
    quote_option(s, chosen.option_conid, mid)
    # Never once its levels are passed, or its entry window has ended.
    quote_spy(s, "531")
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("expire", ["invalidated"])
    quote_spy(s, "500")
    s.clock.wall.now = NEW_YORK + timedelta(days=28)
    plan = await surveyed(s)
    assert (only(plan).action, only(plan).reason) == (
        "expire",
        "its entry window ended without a fill",
    )
    await book(s).apply(plan)
    assert (await state(s)).state is ThesisState.EXPIRED
    assert await s.rig.store(options_repo.open_positions, as_of=s.clock.now().date()) == ()


async def test_a_new_option_waits_for_the_kill_switch_and_a_full_book(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    await s.path.kill.turn_on(actor="test", reason="drill")
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["entries_blocked"])
    await s.path.kill.turn_off(actor="test", reason="over")
    # Its live quote above what the pool allows: the stored chain was cheaper.
    for row in s.rows:  # type: ignore[attr-defined]
        quote_option(s, row.option_conid, "60")
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["option_budget"])
    assert "at its live quote the block costs" in only(plan).reason
    # Its exchange closed: it waits for the session.
    for row in s.rows:  # type: ignore[attr-defined]
        quote_option(s, row.option_conid, f"{row.mid:.2f}")
    s.clock.wall.now = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["venue_not_tradable"])
    s.clock.wall.now = NEW_YORK
    # An option the jurisdiction's policy refuses is never bought.
    strict = book(s)
    strict._options._eligibility = AllowlistEligibility([])  # type: ignore[attr-defined]
    plan = await strict.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, codes(plan)) == ("refuse", ["ineligible"])
    # Past its target before it was bought: refused for good.
    quote_spy(s, "415")
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("refuse", ["target_passed"])
