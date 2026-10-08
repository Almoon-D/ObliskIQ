"""What a collector run did.

The collectors run unattended. Nobody watches the terminal, so ``collector_runs``
plus the JSON log is the entire account of what happened — and the distinction
between the statuses is the point of the table:

``ok``        everything collected, on the inputs intended.
``degraded``  everything collected, but on a weaker input than intended — a
              strike band set from a fallback sigma, say. Not a failure, but the
              one thing worth alerting on, because it means an upstream step is
              not keeping up.
``partial``   some targets failed and the rest were written.
``error``     the run could not proceed at all.
``interrupted`` the process was killed before it reported anything. Written
              by the NEXT run rather than by this one, because a process
              that has been killed cannot record its own death.

Collapsing ``degraded`` into ``ok`` hides a silently worsening data set;
collapsing it into ``partial`` cries wolf on a run that lost nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle; only the annotation needs it
    from .sequence import SequenceRun

from ..config import Config
from ..errors import NoMatchingTargets, StepDeadlineExceeded
from ..logging_setup import get_logger
from ..store import Store
from ..util.timeutil import age_seconds, utc_stamp

log = get_logger("collect.run")

#: How much of a failure message to keep per target. The detail column is a
#: record, not a log sink.
DETAIL_EXCERPT_CHARS = 500


class RunStatus:
    """The legal values of ``collector_runs.status``."""

    RUNNING = "running"
    OK = "ok"
    DEGRADED = "degraded"
    PARTIAL = "partial"
    ERROR = "error"
    #: The run had no targets to collect because a --only filter named none of
    #: them. Distinct from ERROR on purpose: an operator reading the run log for
    #: "did the curve collector fail last night" must not see a scoped manual run
    #: reported as a failure. Distinct from OK for the same reason in reverse —
    #: nothing was collected, and a run that reports success over nothing is the
    #: failure this package exists to prevent.
    SKIPPED = "skipped"
    #: The process was killed before it could write a verdict. Never set by the
    #: run itself — a killed process records nothing — but by the next run's
    #: reaper, which is the only thing in a position to know. Distinct from ERROR
    #: because an error names what went wrong and an interruption names that
    #: nothing got the chance to; distinct from OK because the amount of work
    #: actually done is unknown, and a run reporting success over an unknown is
    #: the failure this package exists to prevent.
    INTERRUPTED = "interrupted"

    #: Every value ``collector_runs.status`` and ``collector_sequences.status``
    #: admit. The authority is the CHECK constraint in the migrations, not this
    #: tuple; ``tests/test_collect_run_continuity.py`` asserts the two agree in
    #: both directions, so a migration that widens the CHECK without adding the
    #: value here — or a value added here that the store would reject at write
    #: time — fails the build rather than being found at 22:30 on a VM.
    ALL = (RUNNING, OK, DEGRADED, SKIPPED, PARTIAL, ERROR, INTERRUPTED)


@dataclass
class TargetOutcome:
    """One target's result inside a run."""

    target: str
    ok: bool
    rows: int = 0
    degraded: bool = False
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"target": self.target, "ok": self.ok, "rows": self.rows}
        if self.degraded:
            payload["degraded"] = True
        if self.reason:
            payload["reason"] = self.reason[:DETAIL_EXCERPT_CHARS]
        payload.update(self.detail)
        return payload


class CollectorRun:
    """Context manager writing one ``collector_runs`` row per run.

    The row is written on entry with ``status='running'`` so that a run killed by
    the machine leaves evidence rather than nothing at all — the crash case is
    exactly the one an unattended collector has to be legible in.
    """

    def __init__(
        self,
        store: Store,
        collector: str,
        *,
        dry_run: bool = False,
        sequence: SequenceRun | None = None,
        step_index: int | None = None,
    ) -> None:
        self._store = store
        self._collector = collector
        self._dry_run = dry_run
        self._run_id: int | None = None
        self._started_at = utc_stamp()
        # NULL when the collector was invoked on its own. `obliskiq-collect
        # chain` is not half of anything, and a NULL says so rather than
        # inventing a sequence of one.
        self._sequence_id = sequence.sequence_id if sequence is not None else None
        self._step_index = step_index
        self.outcomes: list[TargetOutcome] = []
        self.notes: dict[str, Any] = {}

    # ── recording ──

    def record(self, outcome: TargetOutcome) -> TargetOutcome:
        self.outcomes.append(outcome)
        if outcome.ok:
            log.info(
                "target collected",
                extra={
                    "collector": self._collector,
                    "target": outcome.target,
                    "rows": outcome.rows,
                    "degraded": outcome.degraded,
                },
            )
        else:
            log.warning(
                "target failed",
                extra={
                    "collector": self._collector,
                    "target": outcome.target,
                    "reason": outcome.reason,
                },
            )
        return outcome

    def succeeded(
        self,
        target: str,
        rows: int,
        *,
        degraded: bool = False,
        reason: str | None = None,
        **detail: Any,
    ) -> TargetOutcome:
        return self.record(
            TargetOutcome(
                target=target,
                ok=True,
                rows=rows,
                degraded=degraded,
                reason=reason,
                detail=detail,
            )
        )

    def failed(self, target: str, reason: str, **detail: Any) -> TargetOutcome:
        return self.record(TargetOutcome(target=target, ok=False, reason=reason, detail=detail))

    # ── derived state ──

    @property
    def run_id(self) -> int | None:
        return self._run_id

    @property
    def collector(self) -> str:
        return self._collector

    @property
    def rows_written(self) -> int:
        return sum(outcome.rows for outcome in self.outcomes)

    @property
    def targets_ok(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ok)

    @property
    def targets_failed(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.ok)

    @property
    def degraded_targets(self) -> list[str]:
        return [o.target for o in self.outcomes if o.ok and o.degraded]

    def status(self) -> str:
        if self.targets_failed:
            # Every target failing is not a partial run, it is a failed one.
            return RunStatus.ERROR if not self.targets_ok else RunStatus.PARTIAL
        if self.degraded_targets:
            return RunStatus.DEGRADED
        return RunStatus.OK

    # ── lifecycle ──

    def __enter__(self) -> CollectorRun:
        if self._dry_run:
            return self
        with self._store.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO collector_runs (collector, started_at, status, sequence_id, "
                "step_index) VALUES (?, ?, ?, ?, ?)",
                (
                    self._collector,
                    self._started_at,
                    RunStatus.RUNNING,
                    self._sequence_id,
                    self._step_index,
                ),
            )
            self._run_id = int(cursor.lastrowid or 0)
        log.info("collector started", extra={"collector": self._collector, "run_id": self._run_id})
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_type is None:
            status = self.status()
        elif issubclass(exc_type, NoMatchingTargets):
            status = RunStatus.SKIPPED
        elif issubclass(exc_type, StepDeadlineExceeded):
            # INTERRUPTED and not ERROR, on the distinction this module already
            # draws for a reaped row: an error names what went wrong, an
            # interruption names that nothing got the chance to reach a verdict.
            # A step abandoned at its budget is the second of those, and it
            # arrives early enough to be written down properly rather than
            # inferred from a row still saying 'running'.
            status = RunStatus.INTERRUPTED
        else:
            status = RunStatus.ERROR
        detail: dict[str, Any] = {"targets": [o.as_dict() for o in self.outcomes]}
        if self.notes:
            detail["notes"] = self.notes
        if exc is not None:
            detail["error"] = repr(exc)[:DETAIL_EXCERPT_CHARS]

        if self.degraded_targets:
            # High priority on purpose: a degraded run means an upstream step is
            # not keeping up, and it will keep not keeping up until someone looks.
            log.warning(
                "collector run DEGRADED: some targets were collected on a weaker "
                "input than intended",
                extra={
                    "collector": self._collector,
                    "run_id": self._run_id,
                    "degraded_targets": self.degraded_targets,
                },
            )

        log.info(
            "collector finished",
            extra={
                "collector": self._collector,
                "run_id": self._run_id,
                "status": status,
                "rows_written": self.rows_written,
                "targets_ok": self.targets_ok,
                "targets_failed": self.targets_failed,
            },
        )

        if not self._dry_run and self._run_id is not None:
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE collector_runs SET finished_at = ?, status = ?, rows_written = ?, "
                    "targets_ok = ?, targets_failed = ?, detail = ? WHERE run_id = ?",
                    (
                        utc_stamp(),
                        status,
                        self.rows_written,
                        self.targets_ok,
                        self.targets_failed,
                        json.dumps(detail, default=str),
                        self._run_id,
                    ),
                )
        return False  # never swallow the exception


#: What a reaped row records instead of a verdict. It is a statement about the
#: process, not about the data: nothing here claims to know what was written.
INTERRUPTED_DETAIL = (
    "interrupted: the process was killed before it recorded a verdict. What it "
    "wrote before that point is whatever the collector's own tables hold; this "
    "row makes no claim about it."
)


def reap_interrupted_runs(store: Store, config: Config, *, now: datetime | None = None) -> int:
    """Close out runs and sequences a killed process left open. Returns the count.

    Called at the top of every entrypoint, because the next run is the only thing
    in a position to do it: a process that has been killed cannot record its own
    death, and there is no supervisor here that would.

    **The test is age, not liveness.** A PID is not enough — it is reused, and it
    means nothing across the reboot that is the most likely reason a run died in
    the first place. A lock file has the same problem one layer up. So a row is
    reaped once it is older than ``collectors.run_stale_after_seconds``, which is
    set above the longest run this package can perform and below the cadence it
    performs it at. The cost of that choice is stated plainly: a run that
    genuinely takes longer than the window would be mislabelled while it is still
    working, which is why the window is generous rather than tight.

    An unparseable ``started_at`` is left alone. Reaping on the strength of a
    stamp nobody can read would be guessing, and the safe direction here is to
    leave the row saying it is running.
    """
    cutoff = config.require_int("collectors.run_stale_after_seconds")
    stamp = utc_stamp(now) if now is not None else utc_stamp()
    detail = json.dumps({"error": INTERRUPTED_DETAIL})
    reaped = 0

    for table, key in (("collector_runs", "run_id"), ("collector_sequences", "sequence_id")):
        rows = store.execute(
            f"SELECT {key} AS identifier, started_at FROM {table} WHERE status = ?",
            (RunStatus.RUNNING,),
        ).fetchall()
        stale = [
            row["identifier"]
            for row in rows
            if (age := age_seconds(row["started_at"], now=now)) is not None and age > cutoff
        ]
        if not stale:
            continue
        with store.transaction() as connection:
            connection.executemany(
                f"UPDATE {table} SET status = ?, finished_at = ?, detail = ? WHERE {key} = ?",
                [(RunStatus.INTERRUPTED, stamp, detail, identifier) for identifier in stale],
            )
        reaped += len(stale)
        log.warning(
            "reaped runs a killed process left open",
            extra={"table": table, "count": len(stale), "stale_after_seconds": cutoff},
        )

    return reaped
