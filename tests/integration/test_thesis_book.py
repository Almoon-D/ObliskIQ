"""The thesis book over the fake broker: one owner idea from ``idea add`` to a shadow fill, its
exit at the invalidation and its grade in the digest; and the failure branches of its money path.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from eligibility_double import AllowlistEligibility
from service_rig import CURRENCY, EMIM, ServiceRig
from thesis_rig import draft

from obliskiq.capital.allocation import THESIS_BOOK
from obliskiq.cli import idea as idea_cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.data.candidates import parse_ishares
from obliskiq.decision import rebalance_thesis
from obliskiq.decision.rebalance_thesis import ThesisPlan, ThesisRebalancer
from obliskiq.decision.thesis_intake import ThesisIntake
from obliskiq.domain.enums import AssetClass, InstrumentRole
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import Bar
from obliskiq.domain.orders import (
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderSide,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
    Sleeve,
)
from obliskiq.domain.thesis import Thesis, ThesisState
from obliskiq.errors import CalendarError, ObliskError
from obliskiq.execution.instruments import UniverseDirectory, describe_pinned
from obliskiq.orchestration import jobs
from obliskiq.orchestration.assemble import build_thesis_book
from obliskiq.orchestration.parts import build_capital
from obliskiq.store import breakers as breaker_repo
from obliskiq.store import open_database, repos
from obliskiq.store import orders as repo
from obliskiq.store import theses as thesis_repo
from obliskiq.store import universe as universe_repo
from obliskiq.store.breakers import ManagedReading
from obliskiq.store.h13 import H13Look, book_capital, record_h13_look
from obliskiq.store.scoreboard import AllocationEvent, record_allocation
from obliskiq.tax.book import identity_of

D = Decimal
#: SPPW on Xetra's ETF segment and FCX on the NYSE (config/universe.yaml).
SPPW, FCX = 355720826, 7089
CURRENCY.update({SPPW: "EUR", FCX: "USD"})
#: Monday 26 October 2026: 09:00 UTC is 10:00 on Xetra; the NYSE opens at 13:30 UTC.
EUROPE = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
NEW_YORK = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
MONDAY = EUROPE.date()
HORIZON = "2027-04-30"


def sized(settings: ObliskConfig, **thesis: Any) -> ObliskConfig:
    """A book of 100,000 (the thesis book works on 10,000, 1,250 a normal idea)."""
    capital = settings.capital.model_copy(update={"cap_base": D(100000)})
    book = settings.thesis.model_copy(update=thesis)
    return settings.model_copy(update={"capital": capital, "thesis": book})


async def seed_bars(s: ServiceRig, conid: int, close: float, volume: float | None = 100000) -> None:
    declared = s.path.settings.universe.pinned(conid)
    await s.rig.store(
        repos.upsert_instrument,
        Instrument(
            conid=conid,
            symbol=f"C{conid}" if declared is None else declared.symbol,
            listing_exchange="TSEJ" if declared is None else declared.listing_exchange,
            currency="JPY" if declared is None else declared.currency,
            asset_class=AssetClass.STOCK,
            domicile=None,
            ucits=None,
            kid_status=None,
            share_class=None,
            lot_size=None,
            option_exchange=None,
            history_exchange=None,
            roles=frozenset({InstrumentRole.DATA}),
            superseded_by=None,
            created_at=EUROPE - timedelta(days=90),
            superseded_at=None,
        ),
    )
    days = [MONDAY - timedelta(days=n) for n in range(1, 40)]
    bars = [
        Bar(conid=conid, session_date=d, open=None, high=None, low=None, close=close,
            volume=volume, source="t")
        for d in days
        if d.weekday() < 5
    ]  # fmt: skip
    await s.rig.store(repos.upsert_bars, bars)


def quote(s: ServiceRig, conid: int, last: str) -> None:
    price = D(last)
    s.broker.quote(
        conid, bid=str(price - D("0.02")), ask=str(price + D("0.02")), last=last, sigma="15%"
    )


async def started(make: Any, shipped_settings: ObliskConfig, **kw: Any) -> ServiceRig:
    paper = kw.pop("paper", False)
    s: ServiceRig = await make(settings=sized(shipped_settings, **kw), paper=paper)
    s.clock.wall.now = EUROPE
    s.broker.cash = {"EUR": (D(200000), None), "USD": (D(20000), D("0.86"))}
    s.broker.nav = D(250000)
    quote(s, SPPW, "45")
    quote(s, FCX, "45")
    await seed_bars(s, SPPW, 45.0)
    await seed_bars(s, FCX, 45.0)
    return s


def book(s: ServiceRig, **kw: Any) -> ThesisRebalancer:
    settings = s.path.settings
    parts = build_capital(settings, s.path, now=s.clock.now)
    built = build_thesis_book(settings, s.path, parts, now=s.clock.now)
    for name, value in kw.items():
        setattr(built, f"_{name}", value)
    return built


async def idea(s: ServiceRig, **changes: Any) -> Thesis:
    intake = ThesisIntake(s.path.settings, worker=s.rig.worker, now=s.clock.now)
    fields = {"symbol": "SPPW", "target": D(60), "invalidation": D(40)}
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
    result = await s.service.jobs.cycle("test", MONDAY)
    assert result.theses is not None
    return result.theses


def only(plan: ThesisPlan) -> Any:
    (decision,) = plan.decisions
    return decision


def codes(plan: ThesisPlan) -> list[str]:
    return [finding.code.value for finding in only(plan).findings]


# ── acceptance ──


async def test_no_idea_a_cycle_places_nothing_for_the_book(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    plan = await cycle(s)
    assert (plan.decisions, plan.trades, plan.blocked) == ([], [], None)
    assert "no open thesis: the book holds nothing" in plan.render()
    await work(s)
    assert await thesis_intents(s) == []
    every = await s.rig.store(repo.intents_in_states, list(IntentState))
    assert {i.intent.sleeve for i in every} == {Sleeve.CORE}  # the reserve alone traded


async def test_an_owner_idea_goes_from_idea_add_to_a_shadow_fill_an_exit_and_its_grade(
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

    status, said = await asyncio.to_thread(
        command, "add", "SPPW", "--expect", "world equities re-rate", "--target", "60",
        "--invalidation", "40", "--horizon", HORIZON, "--conviction", "high",
    )  # fmt: skip
    assert status == EXIT_OK, said
    assert "stored: T1 SPPW candidate (owner, high, bullish)" in said

    # Cycle one: the gate passes it, it takes a slot at 1.5 times the base size and is entered.
    plan = await cycle(s)
    entered = only(plan)
    assert plan.capital == D(10000), plan.render()
    assert entered.action == "enter", plan.render()
    assert (entered.action, entered.size_base, entered.quantity) == ("enter", D(1875), D(41))
    held = await state(s)
    assert (held.state, held.mode, held.size_base) == (
        ThesisState.APPROVED,
        ExecutionMode.SHADOW,
        D(1875),
    )
    (entry,) = await thesis_intents(s)
    assert (entry.thesis_id, entry.sleeve, entry.role) == ("T1", Sleeve.SATELLITE, IntentRole.ENTRY)
    assert entry.protective is not None
    assert entry.protective.stop == D(40)  # its stop rests at the invalidation
    status, said = await asyncio.to_thread(command, "withdraw", "T1")
    assert (status, "entry order is working" in said) == (EXIT_INVALID, True)
    await work(s)
    assert s.broker.placements == []  # shadow: nothing reached the broker
    status, said = await asyncio.to_thread(command, "withdraw", "T1")
    assert (status, "entry has filled" in said) == (EXIT_INVALID, True)

    # Cycle two: the fill makes it active; inside its levels it is held.
    plan = await cycle(s)
    assert (only(plan).action, only(plan).held) == ("hold", D(41))
    assert (await state(s)).state is ThesisState.ACTIVE
    assert "T1 SPPW (owner, high, approved) held 41: hold (at 45" in plan.render()

    # The price falls through the invalidation: the thesis's own exit, then its close and grade.
    quote(s, SPPW, "39.5")
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason, only(plan).quantity) == (
        "exit",
        "invalidation",
        D(41),
    )
    await work(s)
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason) == ("close", "invalidation")
    closed = await state(s)
    assert (closed.state, closed.exit_reason) == (ThesisState.CLOSED, "invalidation")
    (graded,) = await s.rig.store(thesis_repo.grades, as_of=MONDAY)
    assert (graded.reached, graded.exit_reason, graded.quantity) == (False, "invalidation", D(41))
    assert graded.payoff < 0 < graded.stated_payoff
    assert (await cycle(s)).decisions == []  # the slot is free and holds nothing

    await s.service.jobs.digest(MONDAY)
    digest = s.notifier.digest()
    assert "Thesis book:" in digest
    assert "T1 SPPW closed today: closed by invalidation: payoff -" in digest
    assert "record, book: 1 closed, 0 reached their target, mean payoff -" in digest
    assert "record, source owner: 1 closed" in digest
    assert "record, conviction high: 1 closed" in digest
    status, said = await asyncio.to_thread(command, "show", "T1")
    assert "grade: did not reach its target; payoff -" in said
    assert "closed by invalidation" in said
    assert [line.split(":")[0] for line in await log(s)] == [
        "intake",
        "state",
        "order",
        "state",
        "order",
        "state",
    ]


# ── the gate's failure branches, in shadow ──

XEON = 46041702
CURRENCY[XEON] = "EUR"


async def surveyed(s: ServiceRig, **kw: Any) -> ThesisPlan:
    return await book(s, **kw).survey(ExecutionMode.SHADOW)


async def test_an_ineligible_instrument_is_refused(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    strict = book(s, eligibility=AllowlistEligibility([]))
    plan = await strict.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, codes(plan)) == ("refuse", ["ineligible"])
    assert await strict.apply(plan) == []
    assert (await state(s)).state is ThesisState.REFUSED
    assert (await log(s))[-1] == f"state: ineligible: conid {SPPW} is not in the allowlist"
    assert (await surveyed(s)).decisions == []


async def test_an_illiquid_instrument_is_refused_and_an_unmeasured_one_waits(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    quote(s, XEON, "45")
    await seed_bars(s, XEON, 45.0, volume=None)  # bars without a volume measure nothing
    await idea(s, symbol="XEON")
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["liquidity_unmeasured"])
    await seed_bars(s, XEON, 45.0, volume=10)  # 450 a day: 1,250 is far above 1 % of it
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("refuse", ["illiquid"])


async def test_a_full_book_withholds_the_next_idea_in_order_of_arrival(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, max_positions=1)
    quote(s, XEON, "45")
    await seed_bars(s, XEON, 45.0)
    await idea(s)
    await idea(s, symbol="XEON")
    thesis_book = book(s)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    first, second = plan.decisions
    # One slot: with one position the base size is the whole book, held to the position cap.
    assert (first.action, first.size_base) == ("enter", D(2500))
    assert (second.action, [f.code.value for f in second.findings]) == ("wait", ["book_full"])
    (made,) = await thesis_book.apply(plan)
    assert made.thesis_id == "T1"
    assert (await log(s, 2))[-1].startswith("withheld: book_full: the book holds its 1 positions")
    await thesis_book.apply(await thesis_book.survey(ExecutionMode.SHADOW))
    assert len(await log(s, 2)) == 2  # the same reason is not logged again every cycle


async def test_a_conid_another_book_or_thesis_holds_is_refused(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await cycle(s)
    await work(s)  # the reserve's shadow fills: it holds EMIM now
    await seed_bars(s, EMIM, 46.42)
    await idea(s, symbol="EMIM")
    await idea(s)
    await idea(s)
    await idea(s, symbol="XEON")
    quote(s, XEON, "45")
    await seed_bars(s, XEON, 45.0)
    await s.rig.assign(XEON, Sleeve.CORE)
    plan = await surveyed(s)
    core, first, twin, assigned = plan.decisions
    assert (core.action, core.reason) == ("refuse", "held_elsewhere: the core book holds EMIM")
    assert first.action == "enter"
    assert (twin.action, twin.reason) == (
        "refuse",
        "held_elsewhere: another thesis already holds a slot on SPPW",
    )
    assert assigned.reason == (
        "held_elsewhere: XEON is assigned to core, not to the thesis book's sleeve"
    )


async def test_a_stale_quote_at_entry_withholds_the_idea_and_no_order_is_made(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    del s.broker.quotes[SPPW]
    plan = await surveyed(s)
    assert (only(plan).action, codes(plan)) == ("wait", ["no_quote"])
    # The quote is lost between the plan and the order: approved, nothing made, tried again.
    quote(s, SPPW, "45")
    thesis_book = book(s)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert only(plan).action == "enter"
    del s.broker.quotes[SPPW]
    assert await thesis_book.apply(plan) == []
    assert (await state(s)).state is ThesisState.APPROVED
    assert (await log(s))[-1] == "withheld: not_priced: entry not made: it has no quote"
    assert await thesis_intents(s) == []
    quote(s, SPPW, "45")
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, only(plan).size_base) == ("enter", D(1250))
    (made,) = await thesis_book.apply(plan)
    assert made.terms.quantity == D(27)
    # Its entry order works: nothing more is decided on it.
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, codes(plan)) == ("wait", ["working"])


async def test_entries_wait_for_the_kill_switch_a_freeze_the_venue_and_cash(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    await idea(s, symbol="FCX")
    await s.path.kill.turn_on(actor="test", reason="drill")
    plan = await surveyed(s)
    europe, new_york = plan.decisions
    assert plan.entries_blocked == "the kill switch is on"
    assert [f.code.value for f in europe.findings] == ["entries_blocked"]
    assert [f.code.value for f in new_york.findings] == ["entries_blocked", "venue_not_tradable"]
    assert "no entry now: the kill switch is on" in plan.render()
    await s.path.kill.turn_off(actor="test", reason="over")
    await s.rig.store(
        repo.record_reconciliation,
        ReconcileRun(
            at=s.clock.now(),
            status=ReconcileStatus.DRIFT,
            findings=(ReconcileFinding(kind="position", detail="x", freezes=True),),
        ),
    )
    plan = await surveyed(s)
    assert plan.entries_blocked == "reconciliation is drift: new risk is frozen"
    # A frozen book enters nothing either (the breakers, decision/breakers.py).
    for days in (2, 1):
        day = MONDAY - timedelta(days=days)
        await s.rig.store(
            breaker_repo.record_reading,
            ManagedReading(
                book="shadow",
                as_of=day,
                managed=D(130000),
                reference_conid=None,
                reference_unit=None,
                is_close=True,
                recorded_at=EUROPE - timedelta(days=days),
            ),
        )
    plan = await surveyed(s)
    assert "the rolling-month breaker is tripped" in str(plan.entries_blocked)
    s.broker.cash = {"EUR": (D(1000), None)}
    assert "no_cash" in [f.code.value for f in (await surveyed(s)).decisions[0].findings]


async def test_an_idea_expires_when_its_window_ends_or_its_levels_pass_first(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s, enter_by=MONDAY)
    await idea(s, symbol="XEON")
    quote(s, XEON, "61")
    await seed_bars(s, XEON, 45.0)
    thesis_book = book(s)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert [d.action for d in plan.decisions] == ["enter", "refuse"]
    assert plan.decisions[1].findings[0].code.value == "target_passed"
    del s.broker.quotes[SPPW]
    await thesis_book.apply(plan)  # approved, its entry not priced
    quote(s, SPPW, "39")
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, codes(plan)) == ("expire", ["invalidated"])
    await thesis_book.apply(plan)
    assert (await state(s)).state is ThesisState.EXPIRED
    await idea(s, enter_by=MONDAY)
    s.clock.wall.now = EUROPE + timedelta(days=1)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, only(plan).reason) == (
        "expire",
        "its entry window ended without a fill",
    )


async def test_a_book_without_capital_or_an_unreadable_account_enters_nothing(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, capital_share=D(0))
    await idea(s)
    plan = await surveyed(s)
    assert (plan.capital, codes(plan)) == (D(0), ["no_capital"])
    s.broker.nav = None
    blocked = await surveyed(s)
    assert blocked.blocked is not None
    assert "net liquidation value cannot be read" in blocked.blocked
    assert "nothing is decided" in blocked.render()
    s.broker.unavailable.add("ledger")
    assert (await surveyed(s)).blocked.startswith("the account cannot be read")  # type: ignore[union-attr]
    result = await s.service.jobs.cycle("test", MONDAY)
    assert result.theses is not None
    assert result.theses.blocked is not None


async def test_a_universe_instrument_nobody_describes_waits(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    bare = book(s, directory=UniverseDirectory([]))
    bare._capital = s.service.jobs._capital.capital  # the book is still valued
    plan = await bare.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, codes(plan)) == ("wait", ["not_described"])


# ── a share outside the universe file ──

TOYOTA, TOYOTA_CONID, TOYOTA_ISIN = "jp:toyota_motor", 13976, "JP3633400001"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ishares"


async def toyota(s: ServiceRig, **changes: Any) -> Thesis:
    parsed = parse_ishares(
        (FIXTURES / "ewj_us.csv").read_bytes(),
        fund="EWJ",
        url="u",
        downloaded_at=s.clock.now() - timedelta(hours=1),
        config=s.path.settings.collectors.candidates,
    )
    await s.rig.store(universe_repo.save_holdings, parsed.file, parsed.lines)
    return await idea(
        s, symbol=None, candidate=TOYOTA, isin=TOYOTA_ISIN, target=D(4000),
        invalidation=D(2500), **changes,
    )  # fmt: skip


async def test_a_stock_candidate_is_resolved_pinned_and_described_then_judged(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await toyota(s)
    s.broker.contracts[TOYOTA_CONID] = ("7203", "TSEJ", "JPY")
    thesis_book = book(s)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, only(plan).reason) == (
        "resolve",
        "its stock candidate is not pinned yet",
    )
    assert s.path.directory.describe(TOYOTA_CONID) is None
    await thesis_book.apply(plan)
    pinned = (await state(s)).instrument
    assert (pinned.conid, pinned.listing_exchange, pinned.currency) == (TOYOTA_CONID, "TSEJ", "JPY")
    resolution = (await s.rig.store(universe_repo.candidate_resolutions))[TOYOTA]
    assert (resolution.pinned, resolution.pinned_for) == (True, "T1")
    described = s.path.directory.describe(TOYOTA_CONID)
    assert described is not None
    assert (described.asset_class, described.description) == (AssetClass.STOCK, "TOYOTA MOTOR")
    assert (await log(s))[-1] == "resolved: pinned to conid 13976 (7203 on TSEJ, JPY)"
    # The tax book knows the share by the ISIN its thesis carries.
    parts = build_capital(s.path.settings, s.path, now=s.clock.now)
    await parts.tax.engine()
    assert identity_of(TOYOTA_CONID, s.path.directory, parts.tax._facts) is not None
    # Judged like any other from the next cycle: a share is eligible; it waits for what is
    # not measured yet (a quote in yen, its liquidity) and for Tokyo to open.
    quote(s, TOYOTA_CONID, "3000")
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert only(plan).action == "wait"
    assert "ineligible" not in codes(plan)
    assert {"no_quote", "venue_not_tradable"} <= set(codes(plan))
    # A path opened afresh describes it from the store.
    fresh = UniverseDirectory.from_settings(s.path.settings)
    await describe_pinned(fresh, s.rig.worker)
    assert fresh.describe(TOYOTA_CONID) == described


async def test_a_candidate_that_does_not_resolve_is_refused(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await toyota(s)
    thesis_book = book(s)
    await thesis_book.apply(await thesis_book.survey(ExecutionMode.SHADOW))
    refused = await state(s)  # the search answers nothing for 7203
    assert refused.state is ThesisState.REFUSED
    assert (await log(s))[-1].startswith("state: unresolved: 7203 on TSEJ")
    assert s.path.directory.describe(TOYOTA_CONID) is None


async def test_a_candidate_that_left_the_holdings_or_ran_out_of_time_ends(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await toyota(s, enter_by=MONDAY)
    thesis_book = book(s)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(rebalance_thesis, "find_candidate", lambda *_a, **_k: None)
    await thesis_book.apply(plan)
    monkey.undo()
    assert (await log(s))[-1] == "state: unresolved: no stock candidate 'jp:toyota_motor' any more"
    await toyota(s, enter_by=MONDAY)
    s.clock.wall.now = EUROPE + timedelta(days=1)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, only(plan).reason) == (
        "expire",
        "its entry window ended before it resolved",
    )


# ── on paper ──


def stock_orders(s: ServiceRig, conid: int) -> list[dict[str, Any]]:
    return [body for body in s.placed() if body["conid"] == conid]


async def test_a_failed_conversion_places_no_entry(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, paper=True)
    s.clock.wall.now = NEW_YORK
    s.broker.cash["USD"] = (D(0), D("0.86"))  # the dollars must be bought first
    s.broker.reject_next = "the conversion is refused"
    await idea(s, symbol="FCX")
    plan = await cycle(s)
    fcx = next(d for d in plan.decisions if d.thesis.instrument.symbol == "FCX")
    assert fcx.action == "enter"
    assert stock_orders(s, FCX) == []  # no buy without its funding
    (entry,) = await thesis_intents(s)
    assert entry.state is IntentState.STAGED
    assert (await state(s)).state is ThesisState.APPROVED


async def test_an_entry_on_paper_is_funded_placed_and_protected_and_leaves_without_its_stop(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, paper=True)
    s.broker.settle_cash = True
    await idea(s)
    await cycle(s)
    (buy,) = stock_orders(s, SPPW)
    assert (buy["side"], buy["quantity"]) == ("BUY", 27)
    s.fill_working()
    await s.service.jobs.heartbeat()  # the fill is followed: the holding and its stop
    assignment = await s.rig.store(repo.holding_assignment, SPPW)
    assert assignment is not None
    assert assignment.owner is Sleeve.SATELLITE
    (stop,) = [o for o in s.broker.live() if o.conid == SPPW and o.side == "SELL"]
    assert D(str(stop.aux_price)) == D(40)  # the stop rests at the invalidation
    plan = await cycle(s)
    held = next(d for d in plan.decisions if d.thesis.thesis_id == 1)
    assert (held.action, held.held) == ("hold", D(27))
    assert (await state(s)).state is ThesisState.ACTIVE
    # The stop is missing when the target is reached: the exit goes all the same.
    stop.status = "Cancelled"
    quote(s, SPPW, "60.5")
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason) == ("exit", "target")
    sells = [body for body in stock_orders(s, SPPW) if body["side"] == "SELL"]
    assert [(b["orderType"], b["quantity"]) for b in sells][-1] == ("LMT", 27), sells


async def test_a_position_its_stop_sold_is_closed_for_the_stop(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings, paper=True)
    s.broker.settle_cash = True
    await idea(s)
    await cycle(s)
    s.fill_working()
    await s.service.jobs.heartbeat()
    await cycle(s)
    (stop,) = [o for o in s.broker.live() if o.conid == SPPW and o.side == "SELL"]
    quote(s, SPPW, "39.9")
    s.broker.fill(stop.order_id, price="39.9")
    await s.service.jobs.heartbeat()
    plan = await cycle(s)
    assert (only(plan).action, only(plan).reason) == ("close", "stop")
    (graded,) = await s.rig.store(thesis_repo.grades, as_of=MONDAY)
    assert (graded.exit_reason, graded.mode, graded.reached) == ("stop", ExecutionMode.PAPER, False)


async def test_a_thesis_of_another_mode_s_record_is_left_alone(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    thesis_book = book(s)
    await thesis_book.apply(await thesis_book.survey(ExecutionMode.SHADOW))
    plan = await thesis_book.survey(ExecutionMode.PAPER)
    assert (only(plan).action, only(plan).reason) == (
        "hold",
        "it belongs to the shadow record, not this mode's",
    )
    live = await thesis_book.survey(ExecutionMode.LIVE)
    assert live.capital == D(0)  # the live share is the capital allocation rule's


# ── what the cycle and the digest say ──


async def test_a_venue_the_calendars_cannot_judge_withholds_the_entry(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)

    class NoVenue:
        def venue_for_ibkr(self, code: str) -> str:
            raise CalendarError(f"IBKR exchange code {code!r} maps to no venue")

    plan = await book(s, calendars=NoVenue()).survey(ExecutionMode.SHADOW)
    assert codes(plan) == ["venue_not_tradable"]
    assert "its venue cannot be judged" in only(plan).reason


async def test_the_cycle_s_thesis_step_says_what_it_did(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    plan = await surveyed(s)
    assert jobs._judge_theses((plan, []), dry_run=True).status == "ok"
    unpriced = jobs._judge_theses((plan, []), dry_run=False)
    assert (unpriced.status, unpriced.reason) == (
        "degraded",
        "1 planned trade(s) could not be priced",
    )
    plan.blocked = "the kill switch drill"
    assert jobs._judge_theses((plan, []), dry_run=False).status == "skipped"
    plan.blocked = "the account cannot be read: down"
    assert jobs._judge_theses((plan, []), dry_run=False).status == "error"
    dry = await s.service.jobs.cycle("test", MONDAY, dry_run=True)
    assert dry.theses is not None
    assert dry.made == []
    assert (await state(s)).state is ThesisState.CANDIDATE  # a dry run writes nothing


async def test_the_digest_says_when_the_book_cannot_be_surveyed(
    make: Any, shipped_settings: ObliskConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await started(make, shipped_settings)

    async def broken(_mode: ExecutionMode) -> ThesisPlan:
        raise ObliskError("the store is locked")

    monkeypatch.setattr(s.service.jobs._thesis, "survey", broken)
    await s.service.jobs.digest(MONDAY)
    body = s.notifier.digest()
    assert "Thesis book:\n  not surveyed" in body
    assert "record: no thesis has closed" in body
    assert "cannot read: the thesis book cannot be surveyed: the store is locked" in body


async def test_a_position_is_held_while_an_order_works_or_no_quote_judges_it(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    await cycle(s)
    await work(s)
    thesis_book = book(s)
    # No quote: its levels are not judged, and it is held.
    del s.broker.quotes[SPPW]
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert plan.blocked == f"a held position has no price: conid {SPPW}"
    quote(s, SPPW, "39")
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert only(plan).action == "exit"
    await thesis_book.apply(plan)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)  # its exit works
    assert (only(plan).action, only(plan).reason) == ("hold", "an order still works on it")
    await work(s)
    # Sold out, but another order still works on the instrument: closed once it has ended.
    parts = build_capital(s.path.settings, s.path, now=s.clock.now)
    extra = await parts.maker.make(
        conid=SPPW, side=OrderSide.BUY, quantity=D(1), sleeve=Sleeve.SATELLITE,
        reason="thesis", mode=ExecutionMode.SHADOW, thesis_id="T1",
    )  # fmt: skip
    assert not isinstance(extra, str)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (only(plan).action, only(plan).reason) == ("hold", "an order still works on it")
    assert only(plan).held == D(0)


async def test_cash_that_cannot_be_valued_blocks_the_book(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    await idea(s)
    s.broker.cash["GBP"] = (D(5), None)
    plan = await surveyed(s)
    assert plan.blocked == "the GBP cash cannot be valued in EUR"
    assert plan.decisions == []


# ── H13's holdbacks on the live account ──


async def _hold_back(s: ServiceRig, group: str) -> None:
    look = H13Look(
        record="live",
        group=group,
        month="2027-06",
        look=1,
        months=6,
        closed=4,
        mean=-0.05,
        sd=0.01,
        t_up=None,
        t_down=-12.0,
        c_up=None,
        c_down=3.365,
        without_best=None,
        step=False,
        back=True,
        evaluable=True,
        series="[]",
        detail="below its boundary",
        at=EUROPE,
    )
    await s.rig.store(record_h13_look, look)


async def test_a_held_back_source_is_refused_and_a_held_back_conviction_sized_as_normal(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    share = AllocationEvent(EUROPE, THESIS_BOOK, D("0.10"), "allocation rule", "exploration")
    await s.rig.store(record_allocation, share)
    await idea(s, conviction="max")
    thesis_book = book(s)
    before = only(await thesis_book.survey(ExecutionMode.LIVE))
    await _hold_back(s, "conviction:max")
    normal = only(await thesis_book.survey(ExecutionMode.LIVE))
    assert before.size_base is not None
    assert normal.size_base is not None
    assert normal.size_base == before.size_base / shipped_settings.thesis.conviction.max
    # In shadow nothing is held back: the holdbacks are the live account's.
    shadow = only(await thesis_book.survey(ExecutionMode.SHADOW))
    assert shadow.action == "enter"
    await _hold_back(s, "source:owner")
    refused = await thesis_book.survey(ExecutionMode.LIVE)
    assert only(refused).action == "refuse"
    assert codes(refused) == ["held_back"]
    assert "no longer traded on the account" in only(refused).reason


async def test_each_cycle_records_the_book_s_capital_for_h13_even_with_no_idea(
    make: Any, shipped_settings: ObliskConfig
) -> None:
    s = await started(make, shipped_settings)
    thesis_book = book(s)
    plan = await thesis_book.survey(ExecutionMode.SHADOW)
    assert plan.decisions == []
    assert plan.capital is not None
    assert plan.capital > D(0)
    await thesis_book.apply(plan)
    await thesis_book.apply(await thesis_book.survey(ExecutionMode.SHADOW))
    (kept,) = await s.rig.store(book_capital)
    assert (kept.mode, kept.on, kept.capital) == (ExecutionMode.SHADOW, MONDAY, plan.capital)
    # An account that cannot be read records nothing and blocks nothing while the book is idle.
    s.broker.unavailable.add("ledger")
    idle = await thesis_book.survey(ExecutionMode.SHADOW)
    assert (idle.capital, idle.blocked) == (None, None)
