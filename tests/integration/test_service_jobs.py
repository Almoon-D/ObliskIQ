"""The service's jobs beyond the cycle: the digest, what the heartbeat finds and alerts, the start's
confirmations, the schedule's cross-check and the hooks the order path reports through.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from service_rig import EMIM, ServiceRig

from obliskiq.calendars.status import Disagreement, DisagreementKind
from obliskiq.domain.orders import IntentState
from obliskiq.execution.killswitch import SweepReport
from obliskiq.ibkr.schedule_source import IbkrTradingSchedule
from obliskiq.ibkr.session import SessionAlert, SessionAlertKind
from obliskiq.orchestration import jobs as jobs_module
from obliskiq.orchestration.health import read_heartbeat
from obliskiq.orchestration.service import AlertHooks
from obliskiq.reporting.digest import VenueLine
from obliskiq.store import orders as repo
from obliskiq.store import runs as run_repo

MONDAY = date(2026, 10, 26)
BEFORE_EUROPE = datetime(2026, 10, 26, 8, 55, tzinfo=UTC)
DIGEST = datetime(2026, 10, 26, 20, 30, tzinfo=UTC)


async def run_until(s: ServiceRig, start: datetime, until: datetime) -> None:
    s.clock.wall.now = start
    await s.service.run(asyncio.Event(), until=until)


async def test_the_daily_digest_is_sent_after_the_last_cycle(make: Any) -> None:
    s = await make()
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=20))
    await run_until(s, DIGEST - timedelta(minutes=1), DIGEST + timedelta(minutes=1))
    (digest,) = [body for subject, body in s.notifier.sent if "daily digest" in subject]
    assert "Mode shadow" in digest
    assert "NAV: 100,000.00 EUR" in digest
    assert "euro_govt_intermediate IBGM: held 3" in digest
    assert "Fills today (3)" in digest
    assert "XAMS" in digest
    assert "last heartbeat" in digest
    (record,) = await s.rig.store(run_repo.runs_for, command="digest", session_date=MONDAY)
    assert record.status == "ok"


async def test_a_digest_that_cannot_be_delivered_is_degraded(make: Any) -> None:
    s = await make()
    s.notifier.fail = True
    await run_until(s, DIGEST - timedelta(minutes=1), DIGEST + timedelta(minutes=1))
    (record,) = await s.rig.store(run_repo.runs_for, command="digest", session_date=MONDAY)
    assert record.status == "degraded"


async def test_a_heartbeat_whose_work_fails_alerts_and_says_so(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()

    async def broken() -> Any:
        raise RuntimeError("the executor broke")

    monkeypatch.setattr(s.path.executor, "cycle", broken)
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=1))
    (alert,) = s.notifier.alerts("service.heartbeat_failed")
    assert "RuntimeError: the executor broke" in alert
    _, facts = read_heartbeat(s.rig.data_dir / "heartbeat.json")
    assert facts["status"] == "stopped"  # the run ended; the beat before it said degraded


async def test_the_heartbeat_alerts_stuck_intents_blocked_venues_and_a_lost_session(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()
    await run_until(s, BEFORE_EUROPE, datetime(2026, 10, 26, 9, 1, tzinfo=UTC))
    working = await s.rig.store(
        repo.intents_in_states,
        [IntentState.SUBMITTED],
    )
    assert working  # simulated orders still resting at their patient limits

    def blocked(calendars: Any, mic: str, at: datetime) -> VenueLine:
        return VenueLine(mic, "🚨", "blocked: incident_overlap", mic == "XAMS")

    monkeypatch.setattr(jobs_module, "venue_line", blocked)
    monkeypatch.setattr(s.service.jobs, "_session_state", lambda: "lost (not authenticated)")
    monkeypatch.setattr(s.path.executor, "cycle", lambda: asyncio.sleep(0))  # nothing fills
    await run_until(
        s, datetime(2026, 10, 26, 10, 0, tzinfo=UTC), datetime(2026, 10, 26, 10, 1, tzinfo=UTC)
    )
    assert len(s.notifier.alerts("service.stuck_intent")) == len(working)
    (venue,) = s.notifier.alerts("service.venue_blocked")
    assert "XAMS" in venue
    assert s.notifier.alerts("session.session_lost") == [
        "🚨 session.session_lost: the broker session is lost (not authenticated)"
    ]
    # The next heartbeat finds the same conditions: nothing is said twice.
    sent = len(s.notifier.sent)
    await run_until(
        s, datetime(2026, 10, 26, 10, 10, tzinfo=UTC), datetime(2026, 10, 26, 10, 11, tzinfo=UTC)
    )
    assert len(s.notifier.sent) == sent


async def test_a_target_ibkr_does_not_confirm_is_alerted_and_not_traded(make: Any) -> None:
    s = await make()
    del s.broker.contracts[EMIM]
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=10))
    (alert,) = s.notifier.alerts("service.target_unconfirmed")
    assert f"skeleton conid {EMIM} is not traded" in alert
    conids = {i.intent.conid for i in await s.rig.store(repo.intents_in_states, list(IntentState))}
    assert EMIM not in conids
    assert len(conids) == 2


async def test_a_confirmation_that_breaks_leaves_every_role_waiting(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.orchestration import service as service_module

    s = await make()

    async def broken(*args: Any) -> dict[int, str]:
        raise RuntimeError("search broke")

    monkeypatch.setattr(service_module, "confirm_targets", broken)
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=10))
    assert len(s.notifier.alerts("service.target_unconfirmed")) == 3
    assert await s.rig.store(repo.intents_in_states, list(IntentState)) == []


async def test_the_trading_schedule_is_refreshed_from_one_contract_per_venue(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()
    schedule = IbkrTradingSchedule(
        s.rig.settings.ibkr, is_trading_weekday=lambda _venue, day: day.weekday() < 5
    )
    asked: list[dict[str, Any]] = []

    async def refresh(contracts: Any, references: dict[str, Any]) -> list[str]:
        asked.append(references)
        return ["XLON"]

    monkeypatch.setattr(schedule, "refresh", refresh)
    monkeypatch.setattr(s.service, "_path", dataclasses.replace(s.path, schedule=schedule))
    await s.service._refresh_schedule()
    (references,) = asked
    assert references["XAMS"] == (43614383, "AEB")  # the first instrument listed there
    assert references["XLON"] == (78999785, "LSEETF")
    assert references["XNYS"] == (4347, "NYSE")  # ALB, the lowest conid listed there

    async def broken(contracts: Any, references: dict[str, Any]) -> list[str]:
        raise RuntimeError("the schedule endpoint broke")

    monkeypatch.setattr(schedule, "refresh", broken)
    await s.service._refresh_schedule()  # logged: the calendars still decide


async def test_the_hooks_report_calendar_disagreements_sweeps_and_sessions(
    make: Any,
) -> None:
    s = await make()
    hooks: AlertHooks = s.hooks
    hooks.calendar(
        Disagreement(
            venue="XAMS",
            day=MONDAY,
            kind=DisagreementKind.CLOSE,
            source="ibkr_trading_schedule",
            calendar="16:30",
            observed="17:30",
        )
    )
    hooks.sweep(SweepReport(listed=1, kept=("1",)))  # a quiet pass says nothing
    hooks.sweep(SweepReport(listed=1, cancelled=("1",)))
    await hooks.drain()
    assert s.notifier.alerts("calendar.disagreement")
    assert s.notifier.alerts("kill_switch.sweep")


def test_a_hook_outside_the_event_loop_queues_for_the_next_flush(make: Any) -> None:
    from service_rig import RecordingNotifier

    from obliskiq.reporting.alerts import AlertBook
    from obliskiq.reporting.reporter import Reporter

    reporter = Reporter(RecordingNotifier(), AlertBook(None))
    hooks = AlertHooks(reporter, now=lambda: BEFORE_EUROPE)
    hooks.session(SessionAlert(SessionAlertKind.COMPETING, "another login", BEFORE_EUROPE))
    assert asyncio.run(reporter.flush()) == 1


async def test_a_cycle_whose_mode_cannot_be_read_is_an_error(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()

    async def unreadable() -> Any:
        raise RuntimeError("the store is locked")

    monkeypatch.setattr(s.path.modes, "current", unreadable)
    result = await s.service.jobs.cycle("europe", MONDAY)
    assert result.status == "error"
    (alert,) = s.notifier.alerts("service.cycle_failed")
    assert "the store is locked" in alert
    assert s.service.jobs.venues


async def test_the_health_file_and_the_digest_say_what_they_cannot_read(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from obliskiq.errors import ObliskError
    from obliskiq.orchestration import jobs as jobs_module

    s = await make()
    jobs = s.service.jobs

    async def unreadable() -> Any:
        raise RuntimeError("the store is locked")

    pinged: list[str] = []

    async def ping(url: SecretStr, *, timeout: float) -> bool:
        pinged.append(url.get_secret_value())
        return True

    def unwritable(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(jobs, "_ping_url", SecretStr("https://hc.test/ping"))
    monkeypatch.setattr(jobs_module, "ping", ping)
    monkeypatch.setattr(jobs_module, "write_heartbeat", unwritable)
    monkeypatch.setattr(s.path.modes, "current", unreadable)
    s.clock.wall.now = BEFORE_EUROPE
    await jobs.heartbeat()  # the file cannot be written; the mode cannot be read; it pings
    assert pinged == ["https://hc.test/ping"]
    await jobs.digest(MONDAY)
    (subject, body) = next(x for x in s.notifier.sent if "daily digest" in x[0])
    assert "(unreadable)" in subject
    assert "cannot read: the mode cannot be read: the store is locked" in body

    async def no_survey(mode: Any) -> Any:
        raise ObliskError("the ledger is gone")

    monkeypatch.undo()
    monkeypatch.setattr(s.service._rebalancer, "survey", no_survey)
    monkeypatch.setattr(jobs._capital.planner, "survey", no_survey)
    await jobs.digest(MONDAY)
    (_, body) = [x for x in s.notifier.sent if "daily digest" in x[0]][-1]
    assert "cannot read: the book cannot be surveyed: the ledger is gone" in body
    assert "the reservations cannot be planned: the ledger is gone" in body


def test_a_venue_line_names_what_the_calendars_cannot_answer(
    service_calendars: Any,
) -> None:
    from obliskiq.reporting.digest import venue_line

    unknown = venue_line(service_calendars, "XXXX", BEFORE_EUROPE)
    assert unknown.blocked
    assert unknown.badge == "❔"
    late = venue_line(service_calendars, "XAMS", datetime(2040, 1, 2, 12, 0, tzinfo=UTC))
    assert late.blocked
    assert "calendar_not_covered" in late.text or late.badge == "❔"
    weekend = venue_line(service_calendars, "XAMS", datetime(2026, 10, 24, 12, 0, tzinfo=UTC))
    assert not weekend.blocked
    assert weekend.badge == "⚪️"
    trading = venue_line(service_calendars, "XAMS", datetime(2026, 10, 26, 12, 0, tzinfo=UTC))
    assert (trading.badge, trading.text) == ("🟢", "regular")


async def test_a_heartbeat_whose_store_reads_fail_still_writes_the_health_file(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the store is locked")

    monkeypatch.setattr(repo, "intents_in_states", broken)
    monkeypatch.setattr(repo, "latest_reconciliation", broken)
    s.clock.wall.now = BEFORE_EUROPE
    await s.service.jobs.heartbeat()
    _, facts = read_heartbeat(s.rig.data_dir / "heartbeat.json")
    assert facts["status"] == "degraded"
    assert facts["reconciliation"] == "unreadable (RuntimeError)"
    assert any("the open intents cannot be read" in p for p in facts["problems"])


async def test_a_heartbeat_that_breaks_never_stops_the_service(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make()
    beats: list[datetime] = []

    async def broken() -> None:
        beats.append(s.clock.now())
        raise RuntimeError("the alert file is gone")

    monkeypatch.setattr(s.service.jobs, "heartbeat", broken)
    await run_until(s, BEFORE_EUROPE, BEFORE_EUROPE + timedelta(minutes=11))
    assert len(beats) == 3  # 08:55, 09:00 and 09:05: each failed and the next one ran


async def test_the_service_collects_each_venue_after_its_close_and_survives_a_failing_job(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make(collect=True)
    seen: list[tuple[str, tuple[str, ...]]] = []
    collector = s.service._collector
    assert collector is not None

    async def recorded(command: str, session_date: date, *, venues: Any, **kw: Any) -> Any:
        seen.append((command, tuple(venues)))
        if command == "collect:XAMS":
            raise RuntimeError("the collector broke")
        return None

    monkeypatch.setattr(collector, "run", recorded)
    await run_until(
        s, datetime(2026, 10, 26, 16, 59, tzinfo=UTC), datetime(2026, 10, 26, 21, 1, tzinfo=UTC)
    )
    commands = [command for command, _ in seen]
    assert commands.index("collect:XAMS") < commands.index("collect:XNYS")
    venues = dict(seen)
    assert venues["collect:XAMS"] == ("XAMS",)
    # The service's calendars cover seven venues: every other venue of the plan, uncovered, is
    # collected in New York's job.
    assert {"XNYS", "XHKG", "XKRX"} <= set(venues["collect:XNYS"])
    assert any(job.command == "digest" for job in s.service.ran)  # the broken job stopped nothing
