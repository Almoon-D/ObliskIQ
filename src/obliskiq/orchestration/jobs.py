"""The service's jobs: a decision cycle, the heartbeat and the daily digest (MASTER_PLAN §5.14).

**A decision cycle** is one run record of six guarded steps: ``nav`` (today's NAV row and the
day's exchange rates, ``orchestration/nav.py``), ``breakers`` (the managed book's reading, the
day's close at ``portfolio_circuit_breaker.close_cycle``, and the book's breakers,
``decision/breakers.py``: a tripped rolling-month breaker freezes new buys and alerts; a tripped
hard breaker stands the mode down to shadow, latches and alerts, and the rest of the cycle runs
in shadow), ``reserve`` (the owner questions whose deadline
passed take their default, then the reservation planner's survey, questions and today's sales,
``capital/planner.py``), ``rebalance`` (the core engine's target portfolio against the book, as
intents, ``decision/rebalance_core.py``), ``thesis`` (the thesis book's entries and exits, as
intents, ``decision/rebalance_thesis.py``) and ``execute`` (the executor's cycle: sync, settle
exits, expire, follow fills, reprice, place with funding, protect, repatriate, reconcile). A dry
run reads and plans, and writes nothing: no NAV row, no run record, no question, no intent, no
order. The owner hears a run summary when a cycle did something or something failed, and every
question the planner asks.

**The heartbeat** (every ``runtime.heartbeat_seconds``): the executor's cycle over the intents
already made, so reprices keep their pace, a staged intent goes out as its venue opens, a fill
(one a restart's cOID lookup found included) is followed and its holding registered, the stops are
checked and the broker is reconciled; then the tax book takes the new fills (``tax/book.py``);
then stuck intents, venues blocked when they should trade, the broker session, the health file
(with each source's health, ``data/runner.py``) and the ping. Every condition it finds is
alerted once while it holds (``reporting/alerts.py``).

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
from ..capital.planner import PlannerActions, ReservationPlan
from ..config.schema import ObliskConfig
from ..data.runner import source_health
from ..decision.breakers import BREAKER_ACTOR, UNREADABLE, BreakerReport
from ..decision.rebalance_core import CoreRebalancer, RebalancePlan
from ..decision.rebalance_thesis import ThesisPlan, ThesisRebalancer
from ..domain.capital import OwnerQuestion
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
from ..store import sources as source_repo
from .health import ping, write_heartbeat
from .nav import NavCollector, NavOutcome
from .parts import CapitalParts
from .runlog import DEGRADED, ERROR, OK, PARTIAL, SKIPPED, RunRecord, StepOutcome

log = get_logger("orchestration.jobs")

#: The run log's names for the steps of a cycle and of the digest.
CYCLE_STEPS = ("nav", "breakers", "reserve", "rebalance", "thesis", "execute")
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
    breakers: BreakerReport | None = None
    reservations: ReservationPlan | None = None
    planner: PlannerActions | None = None
    theses: ThesisPlan | None = None


class Jobs:
    """Runs a cycle, a heartbeat or a digest over an assembled order path."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        path: OrderPath,
        rebalancer: CoreRebalancer,
        thesis: ThesisRebalancer,
        capital: CapitalParts,
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
        self._thesis = thesis
        self._capital = capital
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
            result.nav = await record.step("nav", lambda: self._read_nav(dry_run), _judge_nav)
            watched = await record.step(
                "breakers", lambda: self._watch(mode, name, dry_run=dry_run), _judge_breakers
            )
            if watched is not None:
                result.breakers, mode = watched

            async def reserve() -> tuple[ReservationPlan, PlannerActions]:
                if not dry_run:
                    for answered in await self._capital.questions.expire_due():
                        self._question_expired(answered.question, answered.effect)
                plan = await self._capital.planner.survey(mode)
                if dry_run:
                    return plan, PlannerActions()
                return plan, await self._capital.planner.apply(plan)

            reserved = await record.step(
                "reserve", reserve, lambda done: _judge_reserve(done, dry_run=dry_run)
            )
            if reserved is not None:
                result.reservations, result.planner = reserved
                self._reservation_alerts(*reserved)

            async def rebalance() -> tuple[RebalancePlan, list[OrderIntent]]:
                plan = await self._rebalancer.survey(mode)
                return plan, [] if dry_run else await self._rebalancer.apply(plan)

            planned = await record.step(
                "rebalance", rebalance, lambda done: _judge_plan(done, dry_run=dry_run)
            )
            if planned is not None:
                result.plan, result.made = planned

            async def theses() -> tuple[ThesisPlan, list[OrderIntent]]:
                plan = await self._thesis.survey(mode)
                return plan, [] if dry_run else await self._thesis.apply(plan)

            booked = await record.step(
                "thesis", theses, lambda done: _judge_theses(done, dry_run=dry_run)
            )
            if booked is not None:
                result.theses = booked[0]
                result.made = [*result.made, *booked[1]]
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

    async def _read_nav(self, dry_run: bool) -> NavOutcome:
        """Today's NAV row and, from the same ledger read, the day's exchange rates (the rates a
        fill of the day is valued at for tax)."""
        outcome = await self._nav.collect(dry_run=dry_run)
        if not dry_run and outcome.cash is not None:
            await self._capital.tax.note_rates(outcome.cash)
        return outcome

    async def _watch(
        self, mode: ExecutionMode, cycle: str, *, dry_run: bool
    ) -> tuple[BreakerReport, ExecutionMode]:
        """The book's reading and its breakers, and the mode the rest of the cycle runs in: a
        hard breaker that trips stands the mode down to shadow, latches and alerts; a frozen or
        unreadable book alerts while it holds. A dry run reads and judges only."""
        breakers = self._capital.breakers
        report = await breakers.observe(mode, close=breakers.closes(cycle), dry_run=dry_run)
        if dry_run:
            return report, mode
        at = self._now()
        if report.newly_tripped:
            reason = f"the hard breaker tripped: {report.hard.reason}"
            # The stand-down first: it must hold even if the latch cannot be written.
            stood = await self._path.modes.stand_down(actor=BREAKER_ACTOR, reason=reason)
            self._reporter.push(
                Alert(
                    "breaker.hard_tripped",
                    f"breaker.hard_tripped:{report.book}",
                    (
                        "the shadow book is stood down"
                        if mode is ExecutionMode.SHADOW
                        else f"mode {mode.value} -> shadow"
                    )
                    + (
                        " (held by the FORCED_SHADOW file: the store could not record it)"
                        if stood.by_file
                        else ""
                    )
                    + f": {reason}. Nothing is sold. Look at the book, then reset the breaker "
                    "(obliskiq breakers reset) before upgrading again (obliskiq mode set).",
                    Severity.CRITICAL,
                    at,
                    condition=False,
                ),
                CYCLE,
            )
            await self._reporter.flush()
            await breakers.latch(report)
            return report, await self._path.modes.current()
        refused = report.buys_refused
        if refused is not None:
            unreadable = UNREADABLE in (report.rolling.status, report.hard.status)
            kind = "breaker.unreadable" if unreadable else "breaker.frozen"
            self._reporter.push(
                Alert(
                    kind,
                    f"{kind}:{report.book}",
                    f"new buys are refused: {refused}. Sales stay allowed and nothing is sold.",
                    Severity.WARNING if unreadable else Severity.CRITICAL,
                    at,
                ),
                CYCLE,
            )
        return report, mode

    def _question_expired(self, question: OwnerQuestion, effect: str) -> None:
        self._reporter.push(
            Alert(
                "capital.question_expired",
                f"capital.question_expired:{question.question_id}",
                f"Q{question.question_id} had no answer by its deadline; its default "
                f"{question.default!r} applies: {effect}",
                Severity.WARNING,
                self._now(),
                condition=False,
            ),
            CYCLE,
        )

    def _reservation_alerts(self, plan: ReservationPlan, actions: PlannerActions) -> None:
        at = self._now()
        for question in actions.asked:
            options = "; ".join(
                f"{o.key}: {o.label}" + ("" if o.key != question.default else " (the default)")
                for o in question.options
            )
            self._reporter.push(
                Alert(
                    "capital.question",
                    f"capital.question:{question.question_id}",
                    f"Q{question.question_id}: {question.context}. Options — {options}. Answer "
                    f"with `obliskiq questions answer {question.question_id} <option> --confirm` "
                    f"by {question.deadline.isoformat()}, or the default applies",
                    Severity.CRITICAL,
                    at,
                    condition=False,
                ),
                CYCLE,
            )
        for problem in plan.problems:
            self._reporter.push(
                Alert(
                    "capital.reservation_short",
                    condition_key("capital.reservation_short", problem.split(":")[0]),
                    problem,
                    Severity.CRITICAL,
                    at,
                ),
                CYCLE,
            )

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
        await self._take_fills(at, problems)
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

    async def _take_fills(self, at: datetime, problems: list[str]) -> None:
        """The tax book takes the fills it has not taken yet, at the day's recorded rates."""
        tax = self._capital.tax
        try:
            await tax.note_rates(await self._capital.capital.cash())
            outcome = await tax.catch_up()
        except Exception as exc:  # the lots are not the heartbeat's to lose
            log.exception("the tax book could not take the fills")
            problems.append(f"the tax book failed: {type(exc).__name__}: {exc}")
            return
        for what in outcome.incomplete:
            self._reporter.push(
                Alert(
                    "tax.incomplete",
                    f"tax.incomplete:{what.split(':')[0]}",
                    f"a fill the tax lots cannot account for: {what}; that security's tax is "
                    "not estimated until its lots are complete",
                    Severity.WARNING,
                    at,
                    condition=False,
                ),
                HEARTBEAT,
            )
        if outcome.waiting is not None:
            self._reporter.push(
                Alert(
                    "tax.waiting",
                    condition_key("tax.waiting", outcome.waiting),
                    f"the tax lots wait: {outcome.waiting}",
                    Severity.INFO,
                    at,
                ),
                HEARTBEAT,
            )

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
            runs = await self._path.worker.run(source_repo.source_runs)
        except Exception as exc:
            facts["sources"] = f"unreadable ({type(exc).__name__})"
        else:
            facts["sources"] = {
                h.source: h.state.value for h in source_health(self._settings, runs, now=at)
            }
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
                thesis=self._thesis,
                planner=self._capital.planner,
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


def _judge_breakers(watched: tuple[BreakerReport, ExecutionMode]) -> StepOutcome:
    """Measured and clear is ok; a trip, a freeze or a breaker that cannot be read is degraded,
    so the owner hears of it and the cycle does not count as failed."""
    report, mode = watched
    detail = {
        "book": report.book,
        "rolling": report.rolling.status,
        "hard": report.hard.status,
        "mode": mode.value,
    }
    if report.newly_tripped:
        return StepOutcome(
            "breakers",
            DEGRADED,
            f"forced shadow: the hard breaker tripped ({report.hard.reason})",
            detail,
        )
    refused = report.buys_refused
    if refused is not None:
        return StepOutcome("breakers", DEGRADED, f"new buys frozen: {refused}", detail)
    return StepOutcome("breakers", OK, "; ".join(report.lines()), detail)


def _judge_reserve(done: tuple[ReservationPlan, PlannerActions], *, dry_run: bool) -> StepOutcome:
    plan, actions = done
    detail = {
        "reservations": len(plan.lines),
        "sales_today": len(plan.sales_today),
        "intents": len(actions.intents),
        "questions": len(plan.to_ask),
    }
    if plan.blocked is not None:
        failed = plan.blocked.startswith(("the account cannot be read", "the account's net"))
        return StepOutcome("reserve", ERROR if failed else SKIPPED, plan.blocked, detail)
    if plan.problems:
        return StepOutcome("reserve", DEGRADED, "; ".join(plan.problems), detail)
    if not plan.lines:
        return StepOutcome("reserve", OK, "no open reservation", detail)
    made = "planned" if dry_run else f"{len(actions.intents)} sale(s) made"
    return StepOutcome("reserve", OK, f"{len(plan.lines)} reservation(s); {made}", detail)


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


def _judge_theses(booked: tuple[ThesisPlan, list[OrderIntent]], *, dry_run: bool) -> StepOutcome:
    plan, made = booked
    detail = {"theses": len(plan.decisions), "trades": len(plan.trades), "intents": len(made)}
    if plan.blocked is not None:
        failed = plan.blocked.startswith(("the account cannot be read", "the account's net"))
        return StepOutcome("thesis", ERROR if failed else SKIPPED, plan.blocked, detail)
    if not dry_run and len(made) < len(plan.trades):
        unpriced = len(plan.trades) - len(made)
        return StepOutcome(
            "thesis", DEGRADED, f"{unpriced} planned trade(s) could not be priced", detail
        )
    return StepOutcome("thesis", OK, f"{len(plan.trades)} trade(s) planned", detail)


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
