"""The walking skeleton end to end: schedule → targets → intents → execution → reconcile → digest.

The service runs over the stateful fake broker on a simulated clock (``tests/support``): days of
heartbeats and decision cycles in milliseconds. These are Session 6's cases: the first cycle
creates and fills intents in shadow and on fake paper, the second does nothing; a crash between a
placement and its fill does not order twice; the kill switch stops the third; a three-day run
across the EU's daylight-saving change fires every cycle at its venue's local time; a holiday on
one venue skips that venue's cycles only; a session lost at the daily reset is logged in again and
a failed re-login is alerted within one heartbeat.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from service_rig import EMIM, IBGM, IWDA, ServiceRig

from obliskiq.domain.orders import ExecutionMode, IntentRole, IntentState, Sleeve
from obliskiq.orchestration.health import read_heartbeat
from obliskiq.store import nav as nav_repo
from obliskiq.store import orders as repo
from obliskiq.store import runs as run_repo

#: The Monday after the EU's daylight-saving change (25 October 2026); New York changes on 1 Nov.
MONDAY = date(2026, 10, 26)
BEFORE_EUROPE = datetime(2026, 10, 26, 8, 55, tzinfo=UTC)
#: Europe's cycle: Xetra's open (09:00 Berlin, 08:00 UTC after the change) plus an hour.
EUROPE_CYCLE = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
#: New York's cycle: the NYSE's open (09:30 New York, 13:30 UTC) plus an hour.
US_CYCLE = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)

BERLIN, NEW_YORK, TOKYO = (
    ZoneInfo("Europe/Berlin"),
    ZoneInfo("America/New_York"),
    ZoneInfo("Asia/Tokyo"),
)


async def run_until(service_rig: ServiceRig, start: datetime, until: datetime) -> None:
    service_rig.clock.wall.now = start
    await service_rig.service.run(asyncio.Event(), until=until)


async def intents(service_rig: ServiceRig, *states: IntentState) -> list[Any]:
    chosen = list(states) if states else list(IntentState)
    return await service_rig.rig.store(repo.intents_in_states, chosen)


async def runs_of(service_rig: ServiceRig, command: str, day: date = MONDAY) -> list[Any]:
    return await service_rig.rig.store(run_repo.runs_for, command=command, session_date=day)


# ── shadow ──


async def test_cycle_one_fills_in_shadow_and_cycle_two_does_nothing(make: Any) -> None:
    s = await make()
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(minutes=30))
    made = await intents(s)
    assert {i.intent.conid for i in made} == {IWDA, EMIM, IBGM}
    assert all(i.intent.mode is ExecutionMode.SHADOW for i in made)
    assert all(i.intent.role is IntentRole.ENTRY and i.intent.protective is None for i in made)
    # Patient limits inside the mid rest; the heartbeats reprice them to the touch, where they fill.
    filled = await intents(s, IntentState.FILLED)
    assert len(filled) == 3, [(i.intent.conid, i.intent.state) for i in made]
    quantities = {i.intent.conid: i.intent.terms.quantity for i in made}
    assert quantities == {IBGM: Decimal(3), IWDA: Decimal(1), EMIM: Decimal(3)}
    assert s.broker.placements == []  # nothing reached the broker
    # The NAV row of the day was written by the cycle's first step.
    readings = await s.rig.store(nav_repo.nav_series, since=MONDAY, as_of=MONDAY)
    assert [r.nav_base for r in readings] == [Decimal(100000)]
    (europe,) = await runs_of(s, "cycle:europe")
    assert europe.status == "ok"
    assert [step["step"] for step in europe.detail["steps"]] == ["nav", "rebalance", "execute"]

    # Cycle two, at New York's open: the book is inside its bands; nothing new.
    s2 = await make()
    await run_until(s2, US_CYCLE - timedelta(minutes=5), US_CYCLE + timedelta(minutes=10))
    assert len(await intents(s2)) == 3
    (us,) = await runs_of(s2, "cycle:us")
    rebalance = us.detail["steps"][1]
    assert rebalance["status"] == "ok"
    assert rebalance["trades"] == 0


async def test_a_summary_reaches_the_owner_after_an_eventful_cycle(make: Any) -> None:
    s = await make()
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(minutes=1))
    (subject,) = [x for x in s.notifier.subjects() if "europe cycle" in x]
    assert subject == "ObliskIQ europe cycle (shadow): 3 BUY / 0 SELL"


# ── fake paper ──


async def test_cycle_one_places_converts_and_fills_on_paper_and_cycle_two_does_nothing(
    make: Any,
) -> None:
    s = await make(paper=True)
    s.broker.cash["USD"] = (Decimal(0), Decimal("0.86"))  # dollars must be bought first
    s.broker.settle_cash = True
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(seconds=1))
    placed = {body["conid"] for body in s.placed()}
    assert placed == {IWDA, EMIM, IBGM}
    (conversion,) = s.conversions()  # one conversion funds the dollar buy
    assert conversion["side"] == "SELL"
    assert conversion["conid"] == 12087792
    assert s.broker.suppress_resets == 1  # reset once before the first placement

    s.fill_working()
    await run_until(s, s.clock.now(), EUROPE_CYCLE + timedelta(minutes=6))
    filled = await intents(s, IntentState.FILLED)
    assert {i.intent.conid for i in filled if i.intent.role is IntentRole.ENTRY} == {
        IWDA,
        EMIM,
        IBGM,
    }
    for conid in (IWDA, EMIM, IBGM):
        assignment = await s.rig.store(repo.holding_assignment, conid)
        assert assignment is not None
        assert assignment.owner is Sleeve.CORE
    before = len(s.broker.placements)
    await run_until(s, US_CYCLE - timedelta(minutes=1), US_CYCLE + timedelta(minutes=10))
    assert len(s.broker.placements) == before  # inertia: cycle two does nothing
    (us,) = await runs_of(s, "cycle:us")
    assert us.detail["steps"][1]["trades"] == 0


async def test_a_crash_between_place_and_fill_does_not_order_twice(make: Any) -> None:
    s = await make(paper=True)
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(seconds=1))
    first = list(s.placed())
    assert len(first) == 3
    # The process dies here: its run records stay as they are. The broker fills meanwhile.
    s.fill_working()
    restarted = await make(paper=True)
    await run_until(
        restarted, EUROPE_CYCLE + timedelta(minutes=20), US_CYCLE + timedelta(minutes=5)
    )
    assert restarted.placed() == first  # nothing placed again
    entries = [i for i in await intents(restarted) if i.intent.role is IntentRole.ENTRY]
    assert len(entries) == 3
    assert all(i.intent.state is IntentState.FILLED for i in entries)


async def test_a_run_a_killed_process_left_open_is_closed_as_interrupted(make: Any) -> None:
    s = await make()
    await s.rig.store(
        run_repo.begin_run,
        run_id="dead",
        command="cycle:europe",
        session_date=MONDAY,
        started_at=EUROPE_CYCLE,
        steps=("nav", "rebalance", "execute"),
    )
    # Restarted inside the catch-up window: the interrupted cycle runs again.
    await run_until(s, EUROPE_CYCLE + timedelta(minutes=30), EUROPE_CYCLE + timedelta(minutes=31))
    runs = await runs_of(s, "cycle:europe")
    assert [r.status for r in runs] == ["interrupted", "ok"]
    assert len(await intents(s)) == 3


async def test_a_job_missed_long_ago_is_recorded_not_run(make: Any) -> None:
    s = await make()
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=1))
    (asia,) = await runs_of(s, "cycle:asia")  # 01:00 UTC, far beyond the catch-up window
    assert asia.status == "skipped"
    assert "not running" in asia.detail["steps"][0]["reason"]
    assert await intents(s) == []


# ── the kill switch ──


async def test_the_kill_switch_stops_cycle_three(make: Any) -> None:
    s = await make(paper=True)
    await run_until(s, BEFORE_EUROPE, EUROPE_CYCLE + timedelta(seconds=1))
    s.fill_working()
    await run_until(s, s.clock.now(), US_CYCLE + timedelta(minutes=1))
    placed = len(s.broker.placements)
    # The owner sells half the world equity elsewhere: cycle three would buy it back.
    held, currency = s.broker.positions[IWDA]
    s.broker.positions[IWDA] = (held - 1, currency)
    await s.path.kill.turn_on(actor="test", reason="drill")
    late = datetime(2026, 10, 26, 19, 0, tzinfo=UTC)
    await run_until(s, late - timedelta(minutes=1), late + timedelta(minutes=5))
    assert len(s.broker.placements) == placed
    (us_late,) = await runs_of(s, "cycle:us_late")
    assert us_late.detail["steps"][1]["status"] == "skipped"
    assert "kill switch" in us_late.detail["steps"][1]["reason"]


# ── the schedule ──


async def test_a_three_day_run_across_the_eu_dst_change_fires_at_local_times(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()
    fired: list[tuple[str, datetime]] = []

    async def cycle(name: str, session_date: date, *, dry_run: bool = False) -> Any:
        fired.append((name, s.clock.now()))
        return type("Result", (), {"status": "ok"})()

    async def digest(session_date: date) -> str:
        fired.append(("digest", s.clock.now()))
        return "ok"

    async def heartbeat() -> None:
        return None

    monkeypatch.setattr(s.service.jobs, "cycle", cycle)
    monkeypatch.setattr(s.service.jobs, "digest", digest)
    monkeypatch.setattr(s.service.jobs, "heartbeat", heartbeat)
    # Friday 23 October (summer time in Berlin) to Tuesday 27 October (winter time).
    await run_until(
        s,
        datetime(2026, 10, 22, 23, 0, tzinfo=UTC),
        datetime(2026, 10, 27, 23, 0, tzinfo=UTC),
    )
    europe = [at for name, at in fired if name == "europe"]
    assert [at.astimezone(BERLIN).strftime("%a %H:%M") for at in europe] == [
        "Fri 10:00",
        "Mon 10:00",
        "Tue 10:00",
    ]
    assert [at.strftime("%H:%M") for at in europe] == ["08:00", "09:00", "09:00"]
    us = [at.astimezone(NEW_YORK).strftime("%a %H:%M") for name, at in fired if name == "us"]
    assert us == ["Fri 10:30", "Mon 10:30", "Tue 10:30"]
    late = [at.astimezone(NEW_YORK).strftime("%H:%M") for name, at in fired if name == "us_late"]
    assert late == ["15:00", "15:00", "15:00"]
    asia = [at.astimezone(TOKYO).strftime("%a %H:%M") for name, at in fired if name == "asia"]
    assert asia == ["Fri 10:00", "Mon 10:00", "Tue 10:00"]
    digests = [at.astimezone(NEW_YORK).strftime("%H:%M") for name, at in fired if name == "digest"]
    assert digests == ["16:30", "16:30", "16:30"]


async def test_a_holiday_on_one_venue_skips_that_venues_cycles_only(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()
    fired: list[str] = []

    async def cycle(name: str, session_date: date, *, dry_run: bool = False) -> Any:
        fired.append(name)
        return type("Result", (), {"status": "ok"})()

    async def nothing(*args: Any) -> None:
        return None

    monkeypatch.setattr(s.service.jobs, "cycle", cycle)
    monkeypatch.setattr(s.service.jobs, "digest", nothing)
    monkeypatch.setattr(s.service.jobs, "heartbeat", nothing)
    # Thanksgiving, 26 November 2026: the NYSE is closed; Xetra and Tokyo trade.
    await run_until(
        s,
        datetime(2026, 11, 25, 23, 0, tzinfo=UTC),
        datetime(2026, 11, 26, 23, 0, tzinfo=UTC),
    )
    assert fired == ["asia", "europe"]


# ── health, downgrade and stop ──


async def test_the_heartbeat_writes_the_health_file_and_a_stop_says_so(make: Any) -> None:
    s = await make()
    stop = asyncio.Event()
    s.clock.wall.now = BEFORE_EUROPE

    async def stop_soon() -> None:
        while s.service.jobs.last_heartbeat is None:
            await asyncio.sleep(0)
        at, facts = read_heartbeat(s.rig.data_dir / "heartbeat.json")
        assert at == BEFORE_EUROPE
        assert facts["status"] == "ok"
        assert facts["mode"] == "shadow"
        stop.set()

    await asyncio.gather(s.service.run(stop), stop_soon())
    _, facts = read_heartbeat(s.rig.data_dir / "heartbeat.json")
    assert facts["status"] == "stopped"


async def test_repeated_failed_cycles_downgrade_paper_to_shadow(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make(paper=True)

    async def broken() -> Any:
        raise RuntimeError("the cycle's work broke")

    monkeypatch.setattr(s.path.executor, "cycle", broken)
    monkeypatch.setattr(s.service.jobs, "heartbeat", lambda: asyncio.sleep(0))
    s.broker.nav = None  # the NAV step fails too: every step but the plan fails
    await run_until(
        s,
        datetime(2026, 10, 26, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 26, 19, 30, tzinfo=UTC),
    )
    assert await s.path.modes.current() is ExecutionMode.SHADOW
    (downgrade,) = s.notifier.alerts("service.downgraded")
    assert "paper -> shadow" in downgrade
    # Asia, Europe and New York failed: the downgrade; New York late fails in shadow too.
    assert len(s.notifier.alerts("service.cycle_failed")) == 4  # once per cycle: names differ
