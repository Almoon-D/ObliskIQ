"""``obliskiq serve``: one asyncio service that runs the bot unattended (MASTER_PLAN §5.14).

**Start.** Under the service lock (one process runs the executor, DECISIONS D47) the service:
closes the run records a killed process left ``running`` as ``interrupted`` (the next start is the
only thing that can record a death); looks every intent that may be at the broker up by its cOID
(``Executor.resume``: a crash between a placement and its fill never sends a second order);
confirms every core vehicle's conid through IBKR's contract search; refreshes IBKR's trading
schedule for the calendars' cross-check; starts the broker session's keepalive and the kill
switch's sweep; and catches up from the run log: a cycle or digest of the last day whose moment
passed with no finished run runs now if it is at most ``runtime.catch_up_minutes`` late, and is
recorded as missed otherwise.

**Then** it sleeps until the next heartbeat or job (``orchestration/schedule.py``: the cycles,
the digest, the market collections and each source's runs) and runs it, refreshing IBKR's
schedule once a day, re-reading the owner's manual incident file each heartbeat and the
single-stock halt feed every ``halts.nasdaq_trader.refresh_minutes`` (``data/halts.py``; its last
state is restored at the start), until ``stop`` is set (SIGTERM or SIGINT from the command
line): the job in hand finishes, the keepalive and the sweep stop, the health file says so, and
the queued alerts go out.

**Alerts.** The order path, the broker session and the calendars report through synchronous
hooks; :class:`AlertHooks` tags each with the job in hand (or ``session``, ``calendar``, ``sweep``
outside one) and the reporter sends it, once while its condition holds. Session alerts are sent at
once, so a failed re-login reaches the owner within one heartbeat (DECISIONS D68).

**Downgrade.** ``runtime.downgrade_after_failed_cycles`` cycles in a row that end ``error`` or
``partial`` move a writing mode down to shadow, audited and alerted (MASTER_PLAN §5.12: downgrades
are automatic, upgrades an owner command).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from ..calendars.status import Disagreement
from ..config.schema import ObliskConfig
from ..decision.rebalance_core import CoreRebalancer, confirm_vehicles
from ..domain.orders import ExecutionMode
from ..errors import CalendarError
from ..execution.boundary import OrderAlert
from ..execution.killswitch import SweepReport
from ..execution.runtime import OrderPath, schedule_references
from ..ibkr.session import SessionAlert
from ..logging_setup import get_logger
from ..reporting.alerts import (
    Alert,
    Severity,
    from_disagreement,
    from_order,
    from_session,
    from_sweep,
)
from ..reporting.reporter import Reporter
from ..store import runs as run_repo
from .clock import Clock
from .health import write_heartbeat
from .jobs import Jobs
from .runlog import DEGRADED, ERROR, OK, PARTIAL, SKIPPED, RunRecord, StepOutcome
from .schedule import (
    COLLECT,
    CYCLE,
    SOURCE,
    CollectPlan,
    Job,
    SourcePlan,
    jobs_between,
    next_job,
)

if TYPE_CHECKING:
    from ..data.halts import HaltFeed
    from ..data.market.daily import MarketCollector
    from ..data.runner import SourceRunner
    from ..llm.extractor import EventExtractor, ExtractorRun

log = get_logger("orchestration.service")

#: What a reaped run record says about itself.
INTERRUPTED_DETAIL = (
    "interrupted: the process was killed before it recorded a verdict; what it wrote before then "
    "is whatever its own tables hold, and this row makes no claim about it"
)
#: How far back the start looks in the run log for jobs it missed.
LOOKBACK = timedelta(days=1)
#: Statuses that mean a job ran to a verdict (an interrupted one did not).
_FINISHED = frozenset({"ok", "degraded", "partial", "error", "skipped"})
#: The alert source of the halt feed's rounds.
HALTS = "halts"
#: The run log's name for an EventExtractor run.
EXTRACT = "extract"
#: Digits an extractor run's cost is recorded with (US dollars).
COST_DIGITS = 6


def _judge_extraction(result: ExtractorRun) -> StepOutcome:
    """An extractor run's verdict: skipped without a key, degraded when the budget stopped it
    or a call failed, ok otherwise."""
    detail = {
        "ok": result.ok,
        "invalid": result.invalid,
        "failed": result.failed,
        "cost_usd": round(result.cost_usd, COST_DIGITS),
    }
    if result.state == "skipped":
        return StepOutcome(EXTRACT, SKIPPED, result.reason, detail)
    status = DEGRADED if result.state == "budget_spent" or result.failed else OK
    return StepOutcome(EXTRACT, status, result.reason, detail)


class AlertHooks:
    """The synchronous hooks the order path, the session and the calendars report through."""

    def __init__(self, reporter: Reporter, *, now: Callable[[], datetime]) -> None:
        self._reporter = reporter
        self._now = now
        self.source: str | None = None
        self._tasks: set[asyncio.Task[int]] = set()

    def _flush_soon(self) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._reporter.flush())
        except RuntimeError:  # no loop (a hook fired outside the service): the next flush sends
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def order(self, alert: OrderAlert) -> None:
        self._reporter.push(from_order(alert), self.source or "order")

    def session(self, alert: SessionAlert) -> None:
        self._reporter.push(from_session(alert), "session")
        self._flush_soon()

    def calendar(self, found: Disagreement) -> None:
        self._reporter.push(from_disagreement(found, self._now()), "calendar")
        self._flush_soon()

    def sweep(self, report: SweepReport) -> None:
        alert = from_sweep(report, self._now())
        if alert is not None:
            self._reporter.push(alert, "sweep")
            self._flush_soon()

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class Service:
    """The heartbeat, the decision cycles and the digest, until stopped."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        path: OrderPath,
        jobs: Jobs,
        rebalancer: CoreRebalancer,
        reporter: Reporter,
        hooks: AlertHooks,
        clock: Clock,
        collector: MarketCollector | None = None,
        collect: CollectPlan | None = None,
        sources: SourceRunner | None = None,
        source_plan: SourcePlan | None = None,
        halts: HaltFeed | None = None,
        extractor: EventExtractor | None = None,
    ) -> None:
        self._settings = settings
        self._path = path
        self._jobs = jobs
        self._collector = collector
        self._collect = collect if collector is not None else None
        self._sources = sources
        self._source_plan = source_plan if sources is not None else None
        self._halts = halts
        self._extractor = extractor
        self._rebalancer = rebalancer
        self._reporter = reporter
        self._hooks = hooks
        self._clock = clock
        self._runtime = settings.runtime
        self._heartbeat = timedelta(seconds=settings.runtime.heartbeat_seconds)
        self._failures = 0
        self._refreshed: date | None = None
        self._sweep_stop = asyncio.Event()
        self._sweep: asyncio.Task[None] | None = None
        self.ran: list[Job] = []

    # ── start ──

    @property
    def jobs(self) -> Jobs:
        return self._jobs

    async def mode(self) -> ExecutionMode:
        return await self._path.modes.current()

    async def prepare(self, *, dry_run: bool = False) -> None:
        """What every start needs before a job: the dead process's runs closed, every intent
        that may be at the broker looked up by its cOID, the core's vehicles confirmed and the
        trading schedule refreshed. ``dry_run`` only confirms and refreshes (it writes nothing)."""
        path = self._path
        self._hooks.source = "startup"
        if not dry_run:
            now = self._clock.now()
            reaped = await path.worker.run(run_repo.reap_running, at=now, detail=INTERRUPTED_DETAIL)
            if reaped:
                log.warning("closed the runs a killed process left open", extra={"runs": reaped})
            for step in await path.executor.resume():
                log.info("resumed", extra={"intent": str(step.intent_id), "action": step.action})
        await self._confirm_targets()
        await self._refresh_schedule()
        if self._halts is not None:
            restored = await self._halts.restore()
            log.info("restored the halt feed's last state", extra={"incidents": restored})
        await self._reporter.end_round("startup")
        self._hooks.source = None

    async def start(self) -> None:
        path = self._path
        now = self._clock.now()
        await self.prepare()
        if path.session is not None:
            await path.session.start_keepalive()
        self._sweep = asyncio.create_task(
            path.kill.run(path.orders, self._sweep_stop, on_report=self._hooks.sweep)
        )
        await self._catch_up(now)

    async def _confirm_targets(self) -> None:
        conids = self._rebalancer.vehicle_conids(self._clock.now().date())
        try:
            refused = await confirm_vehicles(self._path.directory, self._path.client.secdef, conids)
        except Exception as exc:  # nothing confirmed: every vehicle waits
            refused = {conid: f"the confirmation failed: {type(exc).__name__}" for conid in conids}
        self._rebalancer.refuse(refused)
        for conid, why in sorted(refused.items()):
            self._reporter.push(
                Alert(
                    "service.target_unconfirmed",
                    f"service.target_unconfirmed:{conid}",
                    f"core vehicle conid {conid} is not traded: {why}",
                    Severity.CRITICAL,
                    self._clock.now(),
                ),
                "startup",
            )

    async def _refresh_schedule(self) -> None:
        """IBKR's trading schedule for one contract per venue; it covers a few days, so the
        heartbeat refreshes it once a day."""
        self._refreshed = self._clock.now().date()
        schedule = self._path.schedule
        if schedule is None:
            return
        references = schedule_references(self._path.calendars, self._path.directory)
        try:
            failed = await schedule.refresh(self._path.client.contracts, references)
        except Exception:  # the cross-check is advisory: the calendars still decide
            log.exception("the trading schedule could not be refreshed")
            return
        if failed:
            log.warning("trading schedules not refreshed", extra={"venues": failed})

    async def _finished(self, job: Job) -> bool:
        runs = await self._path.worker.run(
            run_repo.runs_for, command=job.command, session_date=job.session_date
        )
        return any(run.status in _FINISHED for run in runs)

    async def _catch_up(self, now: datetime) -> None:
        """Run the jobs the last day missed, if they are still fresh; record the stale ones."""
        late = timedelta(minutes=self._runtime.catch_up_minutes)
        for job in jobs_between(
            self._path.calendars,
            self._runtime,
            now - LOOKBACK,
            now,
            self._collect,
            self._source_plan,
        ):
            if await self._finished(job):
                continue
            if now - job.at <= late:
                log.warning("catching up a missed job", extra={"job": job.command})
                await self._run_job(job)
                continue
            record = RunRecord(
                self._path.worker,
                command=job.command,
                session_date=job.session_date,
                steps=(),
                now=self._clock.now,
            )
            await record.begin()
            record.outcomes.append(
                StepOutcome(
                    "missed",
                    SKIPPED,
                    f"the service was not running at {job.at.isoformat()} and the job is more "
                    f"than {self._runtime.catch_up_minutes} minutes late",
                )
            )
            await record.finish()

    # ── the loop ──

    async def run(self, stop: asyncio.Event, *, until: datetime | None = None) -> None:
        """Start, then run heartbeats and jobs until ``stop`` (or the clock passes ``until``)."""
        await self.start()
        try:
            await self._loop(stop, until)
        finally:
            await self.close()

    async def _loop(self, stop: asyncio.Event, until: datetime | None) -> None:
        last = self._clock.now()
        beat = last
        while not stop.is_set():
            job = next_job(
                self._path.calendars, self._runtime, last, self._collect, self._source_plan
            )
            wake = beat if job is None else min(beat, job.at)
            if until is not None and wake > until:
                return
            await self._clock.sleep_until(wake, stop)
            if stop.is_set():
                return
            now = self._clock.now()
            # Every job due since the last pass, two at the same moment included.
            for due in jobs_between(
                self._path.calendars, self._runtime, last, now, self._collect, self._source_plan
            ):
                if not await self._finished(due):
                    await self._run_job(due)
            last = max(last, now)
            if beat <= now:
                await self._beat()
                beat = self._clock.now() + self._heartbeat

    async def _beat(self) -> None:
        """A heartbeat, after the day's first refresh of IBKR's schedule and a re-read of the
        owner's manual incident file (an edit takes effect within one heartbeat)."""
        if self._refreshed != self._clock.now().date():
            await self._refresh_schedule()
        try:
            self._path.calendars.reload_manual_incidents()
        except CalendarError as exc:  # built without a data directory: nothing to re-read
            log.debug("no manual incident file to re-read", extra={"error": str(exc)})
        try:
            await self._read_halts()
        except Exception:  # the halt feed never stops the heartbeat; its last state stays
            log.exception("the halt feed could not be read")
        self._hooks.source = "heartbeat"
        try:
            await self._jobs.heartbeat()
        except Exception:  # a heartbeat that breaks never stops the service; the next one runs
            log.exception("a heartbeat failed")
        finally:
            self._hooks.source = None

    async def _read_halts(self) -> None:
        """The halt feed, when its refresh is due; repeated failures alert until a good read."""
        halts = self._halts
        if halts is None or not halts.due(self._clock.now()):
            return
        result = await halts.refresh()
        if result.alert:
            self._reporter.push(
                Alert(
                    "halts.feed_failing",
                    "halts.feed_failing",
                    f"the single-stock halt feed failed {result.failures} times in a row "
                    f"({result.error}); its last state, {result.incidents} halts, stays in force",
                    Severity.WARNING,
                    self._clock.now(),
                ),
                HALTS,
            )
        await self._reporter.end_round(HALTS)

    async def _run_job(self, job: Job) -> None:
        self.ran.append(job)
        self._hooks.source = job.kind
        try:
            if job.kind == CYCLE:
                result = await self._jobs.cycle(job.name, job.session_date)
                await self._after_cycle(result.status)
            elif job.kind == COLLECT:
                await self._collect_job(job)
            elif job.kind == SOURCE:
                await self._source_job(job)
            else:
                await self._jobs.digest(job.session_date)
        except Exception:  # a job that breaks never stops the service
            log.exception("a job failed", extra={"job": job.command})
        finally:
            self._hooks.source = None

    async def _collect_job(self, job: Job) -> None:
        """One venue's collection after its close (``data/market/daily.py``)."""
        if self._collector is None or self._collect is None:  # pragma: no cover - never scheduled
            return
        venues = self._collect.venues_of(self._path.calendars, job)
        await self._collector.run(job.command, job.session_date, venues=venues)

    async def _source_job(self, job: Job) -> None:
        """One scheduled run of a source (``data/runner.py``); after a news source, the
        EventExtractor reads what is queued (``llm/extractor.py``), as a run of its own."""
        if self._sources is None or self._source_plan is None:  # pragma: no cover - never planned
            return
        source = self._source_plan.source_of(job)
        await self._sources.run_job(job.command, job.session_date, source)
        if self._extractor is None or self._settings.sources.provider(source).area != "news":
            return
        record = RunRecord(
            self._path.worker,
            command=EXTRACT,
            session_date=job.session_date,
            steps=(EXTRACT,),
            now=self._clock.now,
        )
        await record.begin()
        await record.step(EXTRACT, self._extractor.run, _judge_extraction)
        await record.finish()

    async def _after_cycle(self, status: str) -> None:
        self._failures = self._failures + 1 if status in (ERROR, PARTIAL) else 0
        if self._failures < self._runtime.downgrade_after_failed_cycles:
            return
        self._failures = 0
        reason = (
            f"{self._runtime.downgrade_after_failed_cycles} decision cycles in a row ended in error"
        )
        change = await self._path.modes.downgrade(
            ExecutionMode.SHADOW, actor="service", reason=reason
        )
        if change is None:
            return
        self._reporter.push(
            Alert(
                "service.downgraded",
                "service.downgraded",
                f"mode {change.from_mode.value} -> shadow: {reason}. Upgrading again is an owner "
                "command (obliskiq mode set).",
                Severity.CRITICAL,
                self._clock.now(),
                condition=False,
            ),
            "cycle",
        )
        await self._reporter.flush()

    # ── stop ──

    async def close(self) -> None:
        self._sweep_stop.set()
        if self._sweep is not None:
            with contextlib.suppress(Exception):
                await self._sweep
            self._sweep = None
        if self._path.session is not None:
            await self._path.session.stop_keepalive()
        try:
            write_heartbeat(
                self._path.data_dir / self._settings.paths.heartbeat_file,
                self._clock.now(),
                {"status": "stopped", "last_cycle": self._jobs.last_cycle},
            )
        except OSError:
            log.exception("the heartbeat file could not be written at the stop")
        await self._hooks.drain()
        await self._reporter.flush()
