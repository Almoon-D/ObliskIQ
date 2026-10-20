"""The service's jobs: a decision cycle, the heartbeat and the daily digest (MASTER_PLAN §5.14).

**A decision cycle** is one run record of three guarded steps: ``nav`` (today's NAV row,
``orchestration/nav.py``), ``rebalance`` (the skeleton's targets against the book, as intents,
``decision/rebalance_basic.py``) and ``execute`` (the executor's cycle: sync, settle exits, expire,
follow fills, reprice, place with funding, protect, repatriate, reconcile). A dry run reads and
plans, and writes nothing: no NAV row, no run record, no intent, no order. The owner hears a run
summary when a cycle did something or something failed.

**The heartbeat** (every ``runtime.heartbeat_seconds``): the executor's cycle over the intents
already made, so reprices keep their pace, a staged intent goes out as its venue opens, a fill
(one a restart's cOID lookup found included) is followed and its holding registered, the stops are
checked and the broker is reconciled; then stuck intents, venues blocked when they should trade,
the broker session, the health file and the ping. Every condition it finds is alerted once while
it holds (``reporting/alerts.py``).

**The digest** gathers and sends the day's report (``reporting/digest.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from .. import __version__
from ..config.schema import ObliskConfig
from ..decision.rebalance_basic import BasicRebalancer, RebalancePlan
from ..domain.orders import (
    TERMINAL_STATES,
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderIntent,
    ReconcileStatus,
)
from ..execution.executor import CycleReport
from ..execution.runtime import OrderPath
from ..logging_setup import get_logger
from ..reporting.alerts import Alert, Severity, condition_key
from ..reporting.digest import gather_digest, render_digest, venue_line
from ..reporting.reporter import Reporter
from ..reporting.summary import CycleSummary, format_cycle
from ..store import orders as repo
from .health import ping, write_heartbeat
from .nav import NavCollector, NavOutcome
from .runlog import DEGRADED, ERROR, OK, PARTIAL, SKIPPED, RunRecord, StepOutcome

log = get_logger("orchestration.jobs")

#: The run log's names for the steps of a cycle and of the digest.
CYCLE_STEPS = ("nav", "rebalance", "execute")
DIGEST_STEPS = ("digest",)
#: Alert sources: the job whose rounds raise and clear a condition.
HEARTBEAT = "heartbeat"
CYCLE = "cycle"


@dataclass
class CycleResult:
    """What one cycle did."""

    status: str
    plan: RebalancePlan | None = None
    made: list[OrderIntent] = field(default_factory=list)
    report: CycleReport | None = None
    nav: NavOutcome | None = None


class Jobs:
    """Runs a cycle, a heartbeat or a digest over an assembled order path."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        path: OrderPath,
        rebalancer: BasicRebalancer,
        nav: NavCollector,
        reporter: Reporter,
        data_dir: Path,
        venues: Sequence[str],
        session_state: Callable[[], str],
        now: Callable[[], datetime],
        ping_url: SecretStr | None = None,
    ) -> None:
        self._settings = settings
        self._path = path
        self._rebalancer = rebalancer
        self._nav = nav
        self._reporter = reporter
        self._data_dir = data_dir
        self._venues = tuple(venues)
        self._session_state = session_state
        self._now = now
        self._ping_url = ping_url
        self._heartbeat_file = data_dir / settings.paths.heartbeat_file
        self._stuck = timedelta(minutes=settings.runtime.stuck_after_minutes)
        self._reset_done = False
        self.last_heartbeat: datetime | None = None
        self.last_cycle: str = "none yet"

    @property
    def venues(self) -> tuple[str, ...]:
        return self._venues

    # ── the cycle ──

    async def _reset_suppressed(self, mode: ExecutionMode) -> None:
        """Once per process, before the first placement in a writing mode: IBKR re-enables every
        warning a user suppressed, so none is accepted without the reply policy (D49)."""
        if self._reset_done or mode is ExecutionMode.SHADOW:
            return
        await self._path.orders.reset_suppressed()
        self._reset_done = True

    async def cycle(self, name: str, session_date: date, *, dry_run: bool = False) -> CycleResult:
        """One decision cycle; ``dry_run`` plans and writes nothing."""
        path = self._path
        result = CycleResult(status=ERROR)
        record = RunRecord(
            path.worker,
            command=f"cycle:{name}",
            session_date=session_date,
            steps=CYCLE_STEPS,
            now=self._now,
            dry_run=dry_run,
        )
        await record.begin()
        try:
            mode = await path.modes.current()
            result.nav = await record.step(
                "nav", lambda: self._nav.collect(dry_run=dry_run), _judge_nav
            )

            async def rebalance() -> tuple[RebalancePlan, list[OrderIntent]]:
                plan = await self._rebalancer.survey(mode)
                return plan, [] if dry_run else await self._rebalancer.apply(plan)

            planned = await record.step(
                "rebalance", rebalance, lambda done: _judge_plan(done, dry_run=dry_run)
            )
            if planned is not None:
                result.plan, result.made = planned
            if dry_run:
                record.outcomes.append(StepOutcome("execute", SKIPPED, "dry run"))
            else:
                await self._reset_suppressed(mode)
                result.report = await record.step("execute", path.executor.cycle, _judge_cycle)
        except Exception as exc:  # the mode unreadable, the store failing: the run itself broke
            log.exception("a cycle failed", extra={"cycle": name})
            result.status = await record.finish(exc)
            await self._notify_cycle(name, mode=None, result=result, errors=(repr(exc),))
            return result
        result.status = await record.finish()
        self.last_cycle = f"{name} {result.status} at {self._now().isoformat()}"
        if not dry_run:
            errors = tuple(
                f"{o.name}: {o.reason}" for o in record.outcomes if o.status in (ERROR, DEGRADED)
            )
            await self._notify_cycle(name, mode=mode, result=result, errors=errors)
        return result

    async def _notify_cycle(
        self,
        name: str,
        *,
        mode: ExecutionMode | None,
        result: CycleResult,
        errors: tuple[str, ...],
    ) -> None:
        if result.status in (ERROR, PARTIAL):
            self._reporter.push(
                Alert(
                    "service.cycle_failed",
                    condition_key("service.cycle_failed", name),
                    f"cycle {name} ended {result.status}: {'; '.join(errors) or 'see the run log'}",
                    Severity.CRITICAL,
                    self._now(),
                ),
                CYCLE,
            )
        if mode is not None:
            summary = CycleSummary(
                name=name,
                mode=mode,
                status=result.status,
                plan=result.plan,
                made=tuple(result.made),
                report=result.report,
                errors=errors,
            )
            if summary.eventful:
                await self._reporter.report(*format_cycle(summary))
        await self._reporter.end_round(CYCLE)

    # ── the heartbeat ──

    async def heartbeat(self) -> None:
        """One heartbeat: the open work, the stops, the conditions, the health file and ping."""
        path = self._path
        at = self._now()
        problems: list[str] = []
        try:
            await path.executor.cycle()
        except Exception as exc:  # the heartbeat must outlive any one failure
            log.exception("the heartbeat's work failed")
            problems.append(f"{type(exc).__name__}: {exc}")
            self._reporter.push(
                Alert(
                    "service.heartbeat_failed",
                    "service.heartbeat_failed",
                    f"the heartbeat's work failed: {type(exc).__name__}: {exc}",
                    Severity.WARNING,
                    at,
                ),
                HEARTBEAT,
            )
        try:
            await self._stuck_intents(at)
        except Exception as exc:  # the store failing: the health file still says so
            log.exception("the open intents could not be read")
            problems.append(f"the open intents cannot be read: {type(exc).__name__}: {exc}")
        self._blocked_venues(at)
        session = self._session_state()
        if session.startswith("lost"):
            self._reporter.push(
                Alert(
                    "session.session_lost",
                    "session.session_lost",
                    f"the broker session is {session}",
                    Severity.CRITICAL,
                    at,
                ),
                HEARTBEAT,
            )
        await self._reporter.end_round(HEARTBEAT)
        await self._health(at, session, problems)
        self.last_heartbeat = at

    async def _stuck_intents(self, at: datetime) -> None:
        """An intent submitted with no answer, or open past its expiry, for ``stuck_after``."""
        open_states = [state for state in IntentState if state not in TERMINAL_STATES]
        for stored in await self._path.worker.run(repo.intents_in_states, open_states):
            intent = stored.intent
            if intent.role is IntentRole.PROTECTIVE:
                continue  # a resting stop's intent governs its modifies; it is never stuck
            history = await self._path.book.history(intent.intent_id)
            since = history[-1].at if history else intent.created_at
            unanswered = intent.state is IntentState.SUBMITTED and at - since > self._stuck
            overdue = at - intent.expires_at > self._stuck
            if unanswered or overdue:
                why = "submitted with no answer" if unanswered else "open past its expiry"
                self._reporter.push(
                    Alert(
                        "service.stuck_intent",
                        f"service.stuck_intent:{intent.intent_id}",
                        f"intent {intent.intent_id} ({intent.side.value} conid {intent.conid}) "
                        f"is {why} since {since.isoformat()}",
                        Severity.WARNING,
                        at,
                    ),
                    HEARTBEAT,
                )

    def _blocked_venues(self, at: datetime) -> None:
        for mic in self._venues:
            line = venue_line(self._path.calendars, mic, at)
            if line.blocked:
                self._reporter.push(
                    Alert(
                        "service.venue_blocked",
                        condition_key("service.venue_blocked", mic),
                        f"{line.badge} {mic} {line.text}",
                        Severity.WARNING,
                        at,
                    ),
                    HEARTBEAT,
                )

    async def _health(self, at: datetime, session: str, problems: list[str]) -> None:
        facts: dict[str, Any] = {
            "version": __version__,
            "status": "degraded" if problems else "ok",
            "session": session,
            "last_cycle": self.last_cycle,
            "problems": problems,
        }
        try:
            facts["mode"] = (await self._path.modes.current()).value
        except Exception as exc:
            facts["mode"] = f"unreadable ({type(exc).__name__})"
        facts["kill_switch"] = "on" if await self._path.kill.is_on() else "off"
        try:
            latest = await self._path.worker.run(repo.latest_reconciliation)
        except Exception as exc:
            facts["reconciliation"] = f"unreadable ({type(exc).__name__})"
        else:
            facts["reconciliation"] = "none yet" if latest is None else latest.status.value
        try:
            write_heartbeat(self._heartbeat_file, at, facts)
        except OSError:
            log.exception("the heartbeat file could not be written")
        if self._ping_url is not None:
            await ping(self._ping_url, timeout=self._settings.runtime.health.ping_timeout_seconds)

    def health_lines(self) -> list[str]:
        beat = "none yet" if self.last_heartbeat is None else self.last_heartbeat.isoformat()
        return [
            f"last heartbeat {beat}",
            f"broker session {self._session_state()}",
            f"last cycle {self.last_cycle}",
        ]

    # ── the digest ──

    async def digest(self, session_date: date) -> str:
        """Gather and send the daily digest, as a run record of one step."""
        path = self._path
        record = RunRecord(
            path.worker,
            command="digest",
            session_date=session_date,
            steps=DIGEST_STEPS,
            now=self._now,
        )
        await record.begin()

        async def send() -> bool:
            digest = await gather_digest(
                self._settings,
                worker=path.worker,
                modes=path.modes,
                kill=path.kill,
                rebalancer=self._rebalancer,
                calendars=path.calendars,
                venues=self._venues,
                health=self.health_lines(),
                alerts=self._reporter.book.active,
                now=self._now,
            )
            subject, body = render_digest(
                digest,
                rows=self._settings.reporting.digest_rows,
                base_currency=self._settings.account.base_currency,
            )
            return await self._reporter.report(subject, body)

        await record.step("digest", send, _judge_sent)
        return await record.finish()


def _judge_nav(outcome: NavOutcome) -> StepOutcome:
    if outcome.reading is None:
        return StepOutcome("nav", ERROR, outcome.reason)
    return StepOutcome("nav", OK, outcome.reason)


def _judge_plan(planned: tuple[RebalancePlan, list[OrderIntent]], *, dry_run: bool) -> StepOutcome:
    plan, made = planned
    detail = {"trades": len(plan.trades), "intents": len(made)}
    if plan.blocked is None:
        if not dry_run and len(made) < len(plan.trades):
            unpriced = len(plan.trades) - len(made)
            return StepOutcome(
                "rebalance", DEGRADED, f"{unpriced} planned trade(s) could not be priced", detail
            )
        return StepOutcome("rebalance", OK, f"{len(plan.trades)} trade(s) planned", detail)
    # A deliberate stop (the kill switch, a freeze) skips the step; a read that failed is an error.
    failed = plan.blocked.startswith(("the account cannot be read", "the account's net"))
    return StepOutcome("rebalance", ERROR if failed else SKIPPED, plan.blocked, detail)


def _judge_cycle(report: CycleReport) -> StepOutcome:
    detail = {"steps": len(report.steps)}
    run = report.reconcile
    if run is not None and run.status is not ReconcileStatus.GREEN:
        findings = "; ".join(f"{f.kind}: {f.detail}" for f in run.findings if f.freezes)
        return StepOutcome(
            "execute", DEGRADED, f"reconciliation {run.status.value}: {findings}", detail
        )
    return StepOutcome("execute", OK, f"{len(report.steps)} action(s)", detail)


def _judge_sent(sent: bool) -> StepOutcome:
    if sent:
        return StepOutcome("digest", OK, "sent")
    return StepOutcome("digest", DEGRADED, "the digest could not be delivered")
