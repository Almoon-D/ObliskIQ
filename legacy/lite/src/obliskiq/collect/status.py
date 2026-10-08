"""What the machine did while nobody was watching.

The failure channel for an unattended collector, and the reason it is this and
not a webhook: a collector that phones home is a new outbound path, a new
credential and a new thing to get wrong on a host whose whole job is to need no
attention. Two channels that already exist do the work instead — the exit code
systemd sees, and a row in the store — and this is the reader for the second.

It answers the question somebody actually asks the morning after: *did last night
happen, and if not, which step and why.* Everything it prints comes from
``collector_sequences`` and ``collector_runs``. It opens no market-data provider,
spends no credit and touches no network.

**The reading and the rendering are separate**, and that split is what lets the
MCP server serve this without parsing a wall of text. :func:`collector_status`
returns the facts; :meth:`CollectorStatus.render` turns them into the report
``obliskiq-collect status`` has always printed, unchanged to the byte.
:func:`status_report` is that composition and keeps its signature, so the CLI and
every test written against the text are untouched. A server tool that scraped
the string would be a second parser of a format nothing declares.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ..config import Config
from ..store import Store
from ..util.timeutil import iso_date, utc_today
from .runlog import RunStatus

#: Statuses worth a second look. ``ok`` is not among them, and ``skipped`` is
#: not either — a step a filter did not name is not a fault.
ATTENTION = (RunStatus.DEGRADED, RunStatus.PARTIAL, RunStatus.ERROR, RunStatus.INTERRUPTED)


def _detail(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class StepRecord:
    """One step of a sequence, as the sequence itself recorded it."""

    step: str
    status: str
    reason: str

    @property
    def needs_attention(self) -> bool:
        return self.status in ATTENTION


@dataclass(frozen=True)
class SequenceRecord:
    """One invocation of a multi-step collector command."""

    session_date: str
    command: str
    status: str
    steps_planned: int
    steps_completed: int
    exit_code: int | None
    #: Every step the sequence recorded, in the order it recorded them.
    steps: tuple[StepRecord, ...] = ()
    #: ``False`` where the run noted its daily bars were stale, ``None`` where it
    #: noted nothing. Never conflated: a run that did not check and a run that
    #: checked and was happy are different facts.
    daily_bars_fresh: bool | None = None

    @property
    def steps_reported(self) -> int:
        """How many steps reported at all.

        INCOMPLETE means a step never reported, which is what a killed process
        leaves. A step omitted by ``--skip`` DID report — as skipped — and
        calling that incomplete would cry wolf on every deliberately scoped run.
        """
        return len(self.steps)

    @property
    def incomplete(self) -> bool:
        return self.steps_reported < self.steps_planned

    @property
    def steps_never_reported(self) -> int:
        return max(self.steps_planned - self.steps_reported, 0)

    @property
    def needs_attention(self) -> bool:
        return self.status in ATTENTION

    @property
    def failing_steps(self) -> tuple[StepRecord, ...]:
        return tuple(step for step in self.steps if step.needs_attention)


@dataclass(frozen=True)
class OrphanedRun:
    """A run row no process ever came back to close."""

    collector: str
    started_at: str
    status: str


@dataclass(frozen=True)
class CollectorRun:
    """The most recent run of one collector."""

    collector: str
    status: str
    started_at: str
    rows_written: int
    targets_ok: int
    targets_failed: int

    @property
    def needs_attention(self) -> bool:
        return self.status in ATTENTION


@dataclass(frozen=True)
class CollectorStatus:
    """What ``collector_sequences`` and ``collector_runs`` say, as facts."""

    since: str
    window_days: int
    sequences: tuple[SequenceRecord, ...] = ()
    orphaned: tuple[OrphanedRun, ...] = ()
    latest: tuple[CollectorRun, ...] = ()
    #: Findings the reader should not have to derive, in the report's own words.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """Nothing has run at all — which is neither an error nor success.

        Saying so plainly is the whole job: an empty report that read as "all
        clear" would be the failure this package exists to prevent.
        """
        return not (self.sequences or self.orphaned or self.latest)

    @property
    def needs_attention(self) -> bool:
        return (
            any(s.needs_attention or s.incomplete for s in self.sequences)
            or bool(self.orphaned)
            or any(run.needs_attention for run in self.latest)
        )

    def render(self) -> str:
        """The report ``obliskiq-collect status`` prints."""
        lines: list[str] = [f"Sequences since {self.since}, most recent first:"]
        if not self.sequences:
            lines.append(
                "  none. No 'obliskiq-collect daily' run recorded a sequence in this window."
            )
        for sequence in self.sequences:
            marker = "!" if sequence.needs_attention else " "
            lines.append(
                f" {marker} {sequence.session_date}  {sequence.command:<8} "
                f"{sequence.status:<11} {sequence.steps_completed}/{sequence.steps_planned} steps"
                + (f"  exit={sequence.exit_code}" if sequence.exit_code is not None else "  exit=?")
                + (
                    f"  INCOMPLETE: {sequence.steps_never_reported} step(s) never reported"
                    if sequence.incomplete
                    else ""
                )
            )
            if sequence.daily_bars_fresh is False:
                lines.append(
                    "      daily bars were STALE: every strike band that night was sized "
                    "from a weaker sigma"
                )
            for step in sequence.failing_steps:
                lines.append(f"      {step.step}: {step.status} — {step.reason}")

        lines.append("")
        lines.append("Runs a killed process left behind:")
        if not self.orphaned:
            lines.append("  none.")
        for run in self.orphaned:
            lines.append(f"  {run.started_at}  {run.collector:<13} {run.status}")

        lines.append("")
        lines.append("Most recent run per collector:")
        if not self.latest:
            lines.append("  none.")
        for run in self.latest:
            marker = "!" if run.needs_attention else " "
            lines.append(
                f" {marker} {run.collector:<15} {run.status:<11} {run.started_at}  "
                f"rows={run.rows_written} ok={run.targets_ok} failed={run.targets_failed}"
            )

        return "\n".join(lines)


def collector_status(store: Store, config: Config, *, days: int | None = None) -> CollectorStatus:
    """Read the two run tables and return what they say.

    The store read, once, with no formatting in it. Both the CLI report and the
    ``get_system_health`` tool are built from this, so they cannot disagree about
    whether last night happened.
    """
    window = days if days is not None else config.require_int("collectors.status_report_days")
    window = max(window, 1)
    since = iso_date(utc_today() - timedelta(days=window))

    sequence_rows = store.execute(
        "SELECT sequence_id, command, session_date, started_at, finished_at, status, "
        "steps_planned, steps_completed, exit_code, detail FROM collector_sequences "
        "WHERE session_date >= ? ORDER BY started_at DESC",
        (since,),
    ).fetchall()

    sequences: list[SequenceRecord] = []
    for row in sequence_rows:
        detail = _detail(row["detail"])
        notes = detail.get("notes") or {}
        fresh = notes.get("daily_bars_fresh")
        sequences.append(
            SequenceRecord(
                session_date=str(row["session_date"]),
                command=str(row["command"]),
                status=str(row["status"]),
                steps_planned=int(row["steps_planned"]),
                steps_completed=int(row["steps_completed"]),
                exit_code=row["exit_code"],
                steps=tuple(
                    StepRecord(
                        step=str(entry.get("step") or "?"),
                        status=str(entry.get("status") or "?"),
                        reason=str(entry.get("reason") or "no reason recorded"),
                    )
                    for entry in (detail.get("steps") or [])
                ),
                daily_bars_fresh=fresh if isinstance(fresh, bool) else None,
            )
        )

    orphaned = tuple(
        OrphanedRun(
            collector=str(row["collector"]),
            started_at=str(row["started_at"]),
            status=str(row["status"]),
        )
        for row in store.execute(
            "SELECT collector, started_at, status FROM collector_runs "
            "WHERE status IN (?, ?) AND started_at >= ? ORDER BY run_id DESC",
            (RunStatus.RUNNING, RunStatus.INTERRUPTED, since),
        ).fetchall()
    )

    latest = tuple(
        CollectorRun(
            collector=str(row["collector"]),
            status=str(row["status"]),
            started_at=str(row["started_at"]),
            rows_written=int(row["rows_written"] or 0),
            targets_ok=int(row["targets_ok"] or 0),
            targets_failed=int(row["targets_failed"] or 0),
        )
        for row in store.execute(
            "SELECT collector, status, started_at, rows_written, targets_ok, targets_failed "
            "FROM collector_runs WHERE run_id IN "
            "(SELECT MAX(run_id) FROM collector_runs GROUP BY collector) "
            "ORDER BY collector"
        ).fetchall()
    )

    return CollectorStatus(
        since=since,
        window_days=window,
        sequences=tuple(sequences),
        orphaned=orphaned,
        latest=latest,
        notes=_notes(sequences, orphaned, latest, since=since),
    )


def _notes(
    sequences: list[SequenceRecord],
    orphaned: tuple[OrphanedRun, ...],
    latest: tuple[CollectorRun, ...],
    *,
    since: str,
) -> tuple[str, ...]:
    """The findings in words, for a caller with no room to re-derive them."""
    if not (sequences or orphaned or latest):
        return (
            f"no collector has recorded a run or a sequence since {since}. This is an "
            "EMPTY store rather than a healthy one: nothing has run, so nothing is known "
            "about whether it would work.",
        )
    out: list[str] = []
    for sequence in sequences:
        if sequence.incomplete:
            out.append(
                f"the {sequence.session_date} {sequence.command} sequence planned "
                f"{sequence.steps_planned} step(s) and {sequence.steps_never_reported} "
                "never reported at all, which is what a killed process leaves behind."
            )
        if sequence.daily_bars_fresh is False:
            out.append(
                f"the {sequence.session_date} run found its daily bars STALE, so every "
                "strike band that night was sized from a weaker sigma."
            )
    if orphaned:
        out.append(
            f"{len(orphaned)} run row(s) were left running or interrupted; the next run "
            "reaps a row older than collectors.run_stale_after_seconds."
        )
    for run in latest:
        if run.needs_attention:
            out.append(
                f"the newest {run.collector} run is {run.status} "
                f"({run.targets_failed} target(s) failed)."
            )
    return tuple(out)


def status_report(store: Store, config: Config, *, days: int | None = None) -> str:
    """The report, as text. Returned rather than printed so a test can read it."""
    return collector_status(store, config, days=days).render()
