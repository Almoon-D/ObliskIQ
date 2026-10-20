"""The daily digest: mode, NAV, positions, targets, intents, fills, errors, venue status and health.

The service gathers it after the day's last cycle (``runtime.digest``) and sends it through the
notifier; ``obliskiq status`` prints the same gathering on demand. Everything it shows is read
from the store, the calendars and one survey of the book; it places nothing. A part that cannot be
read says so instead of being left out: an empty digest must never read as a healthy one.

Venue badges follow market-pulse-bot's language (legacy/pulse text_formatter.py ``PHASE_EMOJI``):
🟢 regular, 🔵 auction, 🟣 extended hours, 🔘 lunch, ⚫️ closed, ⚪️ no session today, and for a
venue blocked when it should trade 🚨 an incident, ♦️ an inconsistent schedule, 🔶 a disagreement
with IBKR's schedule, ❔ a date the calendar does not cover.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from ..calendars.service import Calendars
from ..calendars.status import BlockReason
from ..config.schema import ObliskConfig
from ..decision.rebalance_basic import BasicRebalancer, RebalancePlan
from ..domain.calendar import PhaseKind
from ..domain.orders import (
    ExecutionMode,
    Fill,
    IntentState,
    ReconcileRun,
)
from ..errors import CalendarError, ObliskError
from ..execution.killswitch import KillSwitch
from ..execution.modes import Modes
from ..store import nav as nav_repo
from ..store import orders as repo
from ..store import runs as run_repo
from ..store.nav import NavReading
from ..store.orders import StoredIntent
from ..store.runs import RunRow
from ..store.worker import StoreWorker

#: A phase's badge (market-pulse-bot's PHASE_EMOJI).
PHASE_BADGE = {
    PhaseKind.REGULAR: "🟢",
    PhaseKind.OPENING_AUCTION: "🔵",
    PhaseKind.CLOSING_AUCTION: "🔵",
    PhaseKind.PRE_MARKET: "🟣",
    PhaseKind.POST_MARKET: "🟣",
    PhaseKind.LUNCH_BREAK: "🔘",
    PhaseKind.CLOSED: "⚫️",
}
NO_SESSION_BADGE = "⚪️"
#: Why a venue that should trade cannot, as a badge.
BLOCK_BADGE = {
    BlockReason.INCIDENT_OVERLAP: "🚨",
    BlockReason.INCIDENT_FILE_INVALID: "🚨",
    BlockReason.SCHEDULE_INCONSISTENT: "♦️",
    BlockReason.SOURCE_DISAGREEMENT: "🔶",
    BlockReason.CALENDAR_NOT_COVERED: "❔",
    BlockReason.UNKNOWN_VENUE: "❔",
    BlockReason.NO_TRADABLE_WINDOW: "♦️",
}
#: Run statuses the digest lists as errors.
FAILED_RUNS = frozenset({"error", "partial", "interrupted"})


@dataclass(frozen=True, slots=True)
class VenueLine:
    """A venue now: its badge, what it is doing, and whether it is blocked when it should trade."""

    venue: str
    badge: str
    text: str
    blocked: bool


def venue_line(calendars: Calendars, mic: str, at: datetime) -> VenueLine:
    try:
        day = calendars.session_day(mic, at)
        status = calendars.venue_status(mic, day)
    except CalendarError as exc:
        return VenueLine(mic, BLOCK_BADGE[BlockReason.CALENDAR_NOT_COVERED], str(exc), True)
    if status.reason is BlockReason.NOT_A_SESSION:
        return VenueLine(mic, NO_SESSION_BADGE, f"no session on {day.isoformat()}", False)
    if status.reason is not None:
        badge = BLOCK_BADGE.get(status.reason, "❔")
        return VenueLine(mic, badge, f"blocked: {status.reason.value} {status.detail}", True)
    phase = calendars.phase_at(mic, at).phase
    return VenueLine(mic, PHASE_BADGE.get(phase, "❔"), phase.value, False)


@dataclass
class Digest:
    """What the digest shows; a part that could not be read holds why."""

    day: date
    at: datetime
    mode: ExecutionMode | None = None
    kill_on: bool = True
    reconcile: ReconcileRun | None = None
    nav: NavReading | None = None
    plan: RebalancePlan | None = None
    intents: list[StoredIntent] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    runs: list[RunRow] = field(default_factory=list)
    venues: list[VenueLine] = field(default_factory=list)
    health: list[str] = field(default_factory=list)
    alerts: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


async def gather_digest(
    settings: ObliskConfig,
    *,
    worker: StoreWorker,
    modes: Modes,
    kill: KillSwitch,
    rebalancer: BasicRebalancer,
    calendars: Calendars,
    venues: Sequence[str],
    health: Sequence[str],
    alerts: dict[str, str],
    now: Callable[[], datetime],
) -> Digest:
    """Read everything the digest shows, as of ``now``."""
    at = now()
    day = at.date()
    start = datetime.combine(day, time.min, tzinfo=UTC)
    digest = Digest(day=day, at=at, health=list(health), alerts=dict(alerts))
    try:
        digest.mode = await modes.current()
    except Exception as exc:  # a mode that cannot be read is shown as such
        digest.problems.append(f"the mode cannot be read: {exc}")
    digest.kill_on = await kill.is_on()
    digest.reconcile = await worker.run(repo.latest_reconciliation)
    window = timedelta(days=settings.runtime.status_report_days)
    series = await worker.run(nav_repo.nav_series, since=day - window, as_of=day)
    digest.nav = series[-1] if series else None
    if digest.mode is not None:
        try:
            digest.plan = await rebalancer.survey(digest.mode)
        except ObliskError as exc:
            digest.problems.append(f"the book cannot be surveyed: {exc}")
    every = await worker.run(repo.intents_in_states, list(IntentState))
    digest.intents = sorted(
        (s for s in every if s.intent.created_at >= start), key=lambda s: s.intent.created_at
    )
    digest.fills = await worker.run(repo.fills_between, since=start, as_of=at)
    digest.runs = [
        r for r in await worker.run(run_repo.runs_since, since=day) if r.started_at >= start
    ]
    digest.venues = [venue_line(calendars, mic, at) for mic in venues]
    return digest


def _more(lines: list[str], items: Sequence[str], rows: int) -> None:
    lines.extend(items[:rows])
    if len(items) > rows:
        lines.append(f"  … and {len(items) - rows} more")


def render_digest(digest: Digest, *, rows: int, base_currency: str) -> tuple[str, str]:
    """``(subject, body)`` of the digest."""
    mode = "unreadable" if digest.mode is None else digest.mode.value
    failed = [run for run in digest.runs if run.status in FAILED_RUNS]
    blocked = [line for line in digest.venues if line.blocked]
    attention = len(failed) + len(blocked) + len(digest.alerts) + len(digest.problems)
    flag = f"{attention} need attention" if attention else "nothing needs attention"
    subject = f"ObliskIQ daily digest {digest.day.isoformat()} ({mode}): {flag}"
    lines = [f"Mode {mode} · kill switch {'ON' if digest.kill_on else 'off'}"]
    run = digest.reconcile
    lines.append(
        "Reconciliation: none yet"
        if run is None
        else f"Reconciliation: {run.status.value} at {run.at.isoformat()}"
    )
    nav = digest.nav
    lines.append(
        "NAV: no reading recorded"
        if nav is None
        else f"NAV: {nav.nav_base:,.2f} {nav.currency} (read {nav.as_of.isoformat()})"
    )
    plan = digest.plan
    lines.append("")
    lines.append("Book (targets, positions):")
    if plan is None:
        lines.append("  not surveyed")
    else:
        if plan.capital is not None:
            lines.append(
                f"  managed {plan.capital.managed:,.2f} {base_currency} ({plan.capital.detail})"
            )
        if plan.blocked is not None:
            lines.append(f"  no rebalance now: {plan.blocked}")
        for decision in plan.decisions:
            value = "unvalued" if decision.value is None else f"{decision.value:,.2f}"
            weight = (
                ""
                if decision.value is None or plan.capital is None or plan.capital.managed == 0
                else f" ({decision.value / plan.capital.managed:.1%})"
            )
            lines.append(
                f"  {decision.role} {decision.symbol}: held {decision.held} = {value}{weight}; "
                f"target {decision.weight:.0%} = {decision.target:,.2f}; {decision.action}"
            )
    lines.append("")
    lines.append(f"Intents today ({len(digest.intents)}):")
    _more(
        lines,
        [
            f"  {s.intent.created_at.strftime('%H:%M')} {s.intent.side.value} "
            f"{s.intent.terms.quantity or s.intent.terms.cash_quantity} conid {s.intent.conid} "
            f"{s.intent.role.value}: {s.intent.state.value}"
            for s in digest.intents
        ],
        rows,
    )
    lines.append(f"Fills today ({len(digest.fills)}):")
    _more(
        lines,
        [
            f"  {f.executed_at.strftime('%H:%M')} {f.side.value} {f.quantity} conid {f.conid} at "
            f"{f.price}{' (simulated)' if f.simulated else ''}"
            for f in digest.fills
        ],
        rows,
    )
    lines.append(f"Runs today ({len(digest.runs)}), errors ({len(failed)}):")
    _more(lines, [_run_line(r) for r in failed], rows)
    lines.append("")
    lines.append("Venues:")
    lines += [f"  {v.badge} {v.venue} {v.text}" for v in digest.venues]
    lines.append("")
    lines.append("Health:")
    lines += [f"  {item}" for item in digest.health]
    lines += [f"  cannot read: {problem}" for problem in digest.problems]
    if digest.alerts:
        lines.append(f"Open alerts ({len(digest.alerts)}):")
        _more(lines, [f"  {kind}: {key}" for key, kind in digest.alerts.items()], rows)
    return subject, "\n".join(lines)


def _run_line(run: RunRow) -> str:
    error = run.detail.get("error") or run.detail.get("interrupted") or ""
    failing = [
        f"{step.get('step')}: {step.get('status')} {step.get('reason', '')}".strip()
        for step in run.detail.get("steps", [])
        if isinstance(step, dict) and step.get("status") in FAILED_RUNS
    ]
    tail = "; ".join([*failing, str(error)] if error else failing)
    return f"  {run.started_at.strftime('%H:%M')} {run.command} {run.status}: {tail}"
