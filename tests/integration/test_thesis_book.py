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
from service_rig import CURRENCY, EMIM, ServiceRig
from thesis_rig import draft

from obliskiq.cli import idea as idea_cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.decision.rebalance_thesis import ThesisPlan, ThesisRebalancer
from obliskiq.decision.thesis_intake import ThesisIntake
from obliskiq.domain.enums import AssetClass, InstrumentRole
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import Bar
from obliskiq.domain.orders import ExecutionMode, IntentRole, IntentState, Sleeve
from obliskiq.domain.thesis import Thesis, ThesisState
from obliskiq.orchestration.assemble import build_thesis_book
from obliskiq.orchestration.parts import build_capital
from obliskiq.store import open_database, repos
from obliskiq.store import orders as repo
from obliskiq.store import theses as thesis_repo

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
    digest = s.notifier.sent[-1][1]
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
