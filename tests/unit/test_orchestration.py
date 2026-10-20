"""The service's parts: its schedule, run records, lock, health file and ping, the NAV step and
the run log in the store.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.errors import StoreError
from obliskiq.execution.account import CashLine, CashRead
from obliskiq.orchestration.clock import SystemClock
from obliskiq.orchestration.health import ping, read_heartbeat, write_heartbeat
from obliskiq.orchestration.lock import ServiceLock, ServiceLockedError
from obliskiq.orchestration.nav import NavCollector
from obliskiq.orchestration.runlog import (
    DEGRADED,
    ERROR,
    OK,
    PARTIAL,
    SKIPPED,
    RunRecord,
    StepOutcome,
    exit_code,
    verdict,
)
from obliskiq.orchestration.schedule import Job, jobs_between, jobs_on, next_job
from obliskiq.store import Store
from obliskiq.store import nav as nav_repo
from obliskiq.store import runs as run_repo
from obliskiq.store.nav import NavReading
from obliskiq.store.worker import StoreWorker

AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def anchored(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=("XETR", "XNYS", "XTKS")
    )


# ── the schedule ──


def test_a_day_has_four_cycles_and_a_digest_in_time_order(
    anchored: Calendars, shipped_settings: ObliskConfig
) -> None:
    jobs = jobs_on(anchored, shipped_settings.runtime, date(2026, 10, 26))
    assert [(j.command, j.at.strftime("%H:%M")) for j in jobs] == [
        ("cycle:asia", "01:00"),
        ("cycle:europe", "09:00"),
        ("cycle:us", "14:30"),
        ("cycle:us_late", "19:00"),
        ("digest", "20:30"),
    ]
    assert jobs_on(anchored, shipped_settings.runtime, date(2026, 10, 25)) == []  # a Sunday


def test_the_next_job_and_a_window_of_jobs(
    anchored: Calendars, shipped_settings: ObliskConfig
) -> None:
    runtime = shipped_settings.runtime
    saturday = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    job = next_job(anchored, runtime, saturday)
    assert job is not None
    assert job.command == "cycle:asia"
    assert job.session_date == date(2026, 10, 26)
    window = jobs_between(anchored, runtime, AT, AT + timedelta(hours=6))
    assert [j.name for j in window] == ["us"]  # (09:00, 15:00]: not the europe job at 09:00
    far = datetime(2040, 1, 1, tzinfo=UTC)  # beyond what the calendars cover
    assert next_job(anchored, runtime, far) is None
    assert Job(AT, "digest", "digest", "XNYS", AT.date()).command == "digest"


async def test_the_system_clock_waits_or_returns_at_once() -> None:
    clock = SystemClock()
    stop = asyncio.Event()
    before = clock.now()
    await clock.sleep_until(before - timedelta(seconds=1), stop)  # already past
    stop.set()
    await clock.sleep_until(before + timedelta(hours=1), stop)  # stopped: returns at once
    assert (clock.now() - before).total_seconds() < 5


# ── run records ──


def test_the_verdict_is_the_worst_steps() -> None:
    def outcomes(*statuses: str) -> list[StepOutcome]:
        return [StepOutcome(f"s{i}", status) for i, status in enumerate(statuses)]

    assert verdict(outcomes(OK, OK)) == OK
    assert verdict(outcomes(OK, DEGRADED)) == DEGRADED
    assert verdict(outcomes(OK, ERROR)) == PARTIAL
    assert verdict(outcomes(ERROR, ERROR, SKIPPED)) == ERROR
    assert verdict(outcomes(SKIPPED)) == SKIPPED
    assert verdict([]) == ERROR  # success over nothing is never reported
    assert [exit_code(s) for s in (OK, DEGRADED, PARTIAL, ERROR, "interrupted")] == [0, 2, 1, 1, 1]
    assert StepOutcome("x", OK, "r" * 600, {"n": 1}).as_dict()["reason"] == "r" * 500


@pytest.fixture
async def worker(tmp_path: Path, shipped_settings: ObliskConfig) -> Any:
    from obliskiq.store import open_database

    pragmas = shipped_settings.store
    opened = StoreWorker(
        lambda: open_database(
            tmp_path / "worker.db",
            busy_timeout_ms=pragmas.busy_timeout_ms,
            synchronous=pragmas.synchronous,
        )
    )
    yield opened
    await opened.aclose()


async def test_a_run_is_recorded_before_and_after_each_step(worker: StoreWorker) -> None:
    record = RunRecord(
        worker, command="cycle:europe", session_date=AT.date(), steps=("a", "b"), now=lambda: AT
    )
    await record.begin()
    (running,) = await worker.run(run_repo.runs_since, since=AT.date())
    assert (running.status, running.steps_planned, running.finished_at) == ("running", 2, None)

    async def works() -> int:
        return 3

    async def breaks() -> int:
        raise RuntimeError("broke")

    assert await record.step("a", works, lambda n: StepOutcome("a", OK, f"{n} rows")) == 3
    assert await record.step("b", breaks, lambda _n: StepOutcome("b", OK)) is None
    assert await record.finish() == PARTIAL
    (done,) = await worker.run(run_repo.runs_for, command="cycle:europe", session_date=AT.date())
    assert (done.status, done.exit_code, done.steps_completed) == (PARTIAL, 1, 2)
    steps = await worker.run(run_repo.steps_of, done.run_id)
    assert [(s.name, s.status) for s in steps] == [("a", OK), ("b", ERROR)]
    assert "RuntimeError: broke" in steps[1].detail["reason"]


async def test_a_run_that_breaks_is_an_error_and_a_dry_run_writes_nothing(
    worker: StoreWorker,
) -> None:
    record = RunRecord(
        worker, command="digest", session_date=AT.date(), steps=("digest",), now=lambda: AT
    )
    await record.begin()
    assert await record.finish(RuntimeError("the store went away")) == ERROR
    (done,) = await worker.run(run_repo.runs_since, since=AT.date())
    assert "the store went away" in done.detail["error"]
    dry = RunRecord(
        worker, command="x", session_date=AT.date(), steps=("a",), now=lambda: AT, dry_run=True
    )
    await dry.begin()

    async def works() -> int:
        return 1

    await dry.step("a", works, lambda _n: StepOutcome("a", OK))
    assert await dry.finish() == OK
    assert len(await worker.run(run_repo.runs_since, since=AT.date())) == 1


def test_a_dead_processs_runs_are_reaped_as_interrupted(store: Store) -> None:
    run_repo.begin_run(
        store, run_id="r", command="cycle:us", session_date=AT.date(), started_at=AT, steps=("a",)
    )
    run_repo.begin_step(store, run_id="r", step_index=0, name="a", started_at=AT)
    assert run_repo.reap_running(store, at=AT + timedelta(hours=1), detail="killed") == 1
    (run,) = run_repo.runs_since(store, since=AT.date())
    assert (run.status, run.detail["interrupted"]) == ("interrupted", "killed")
    (step,) = run_repo.steps_of(store, "r")
    assert step.status == "interrupted"
    assert run_repo.reap_running(store, at=AT, detail="again") == 0
    store.execute("UPDATE runs SET detail = 'not json' WHERE run_id = 'r'")
    assert run_repo.runs_since(store, since=AT.date())[0].detail == {}
    store.execute("UPDATE runs SET detail = '[1]' WHERE run_id = 'r'")
    assert run_repo.runs_since(store, since=AT.date())[0].detail == {}
    store.execute("UPDATE runs SET detail = '' WHERE run_id = 'r'")
    assert run_repo.runs_since(store, since=AT.date())[0].detail == {}


# ── the NAV series ──


def test_nav_rows_are_one_a_day_corrected_by_a_later_reading(store: Store) -> None:
    day = AT.date()
    nav_repo.record_nav(store, NavReading(day, Decimal(100), "EUR", "cp", AT))
    nav_repo.record_nav(store, NavReading(day, Decimal(101), "EUR", "cp", AT))
    (only,) = nav_repo.nav_series(store, since=day, as_of=day)
    assert only.nav_base == Decimal(101)
    assert nav_repo.nav_series(store, since=day, as_of=day - timedelta(days=1)) == []
    with pytest.raises(StoreError, match="not a net asset value"):
        nav_repo.record_nav(store, NavReading(day, Decimal(0), "EUR", "cp", AT))


class _Reader:
    def __init__(self, nav: Decimal | None, refusal: str | None = None) -> None:
        self._read = CashRead(
            lines=(CashLine("EUR", Decimal(1), None),),
            nav=nav,
            nav_refusal=refusal,
            observed_at=AT,
        )

    async def cash(self) -> CashRead:
        return self._read


async def test_the_nav_step_records_what_the_ledger_says_and_refuses_nothing_read(
    worker: StoreWorker, shipped_settings: ObliskConfig
) -> None:
    collector = NavCollector(
        shipped_settings, reader=_Reader(Decimal(5000)), worker=worker, now=lambda: AT
    )  # type: ignore[arg-type]
    outcome = await collector.collect()
    assert outcome.reading is not None
    assert outcome.reason == "5000 EUR"
    assert (await worker.run(nav_repo.nav_series, since=AT.date(), as_of=AT.date()))[
        0
    ].source == "cp"
    missing = NavCollector(
        shipped_settings, reader=_Reader(None, "the BASE row is in USD"), worker=worker
    )  # type: ignore[arg-type]
    refused = await missing.collect()
    assert refused.reading is None
    assert "(the BASE row is in USD)" in refused.reason
    assert "not a NAV of zero" in refused.reason
    dry = NavCollector(shipped_settings, reader=_Reader(Decimal(1)), worker=worker, now=lambda: AT)  # type: ignore[arg-type]
    assert (await dry.collect(dry_run=True)).reading is not None
    assert (await worker.run(nav_repo.nav_series, since=AT.date(), as_of=AT.date()))[
        0
    ].nav_base == 5000


# ── the lock and the health file ──


def test_one_process_holds_the_service_lock(tmp_path: Path) -> None:
    path = tmp_path / "data" / "service.lock"
    with ServiceLock(path, command="serve") as held:
        assert held.path == path
        assert '"command": "serve"' in path.read_text(encoding="utf-8")
        with pytest.raises(ServiceLockedError, match="serve"):
            ServiceLock(path, command="run-cycle").acquire()
    assert path.read_text(encoding="utf-8") == ""
    again = ServiceLock(path, command="run-cycle").acquire()
    again.release()
    again.release()  # releasing twice is harmless


def test_the_heartbeat_file_is_written_whole_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "data" / "heartbeat.json"
    assert read_heartbeat(path) == (None, {})
    write_heartbeat(path, AT, {"status": "ok"})
    at, facts = read_heartbeat(path)
    assert at == AT
    assert facts["status"] == "ok"
    path.write_text("[1]", encoding="utf-8")
    assert read_heartbeat(path) == (None, {})


async def test_the_ping_answers_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    url = SecretStr("https://hc.test/ping/secret-uuid")
    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://hc.test/ping/secret-uuid")
        route.mock(return_value=httpx.Response(200))
        assert await ping(url, timeout=1)
        route.mock(return_value=httpx.Response(404))
        assert not await ping(url, timeout=1)
        route.mock(side_effect=httpx.ConnectError("down"))
        assert not await ping(url, timeout=1)
        route.mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            assert await ping(url, timeout=1, client=client)
    assert "secret-uuid" not in caplog.text
