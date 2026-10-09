"""One row per multi-step invocation, so a day half collected says so.

``obliskiq-collect daily`` runs the steps in ``DAILY_SEQUENCE`` and writes a
``collector_runs`` row for each. Nothing tied those rows together and nothing
recorded how many steps were *meant* to run, so a sequence killed partway
through left a store indistinguishable from one that collected those steps and
legitimately skipped the rest. Reading the exit code answered it — for whoever
was watching the terminal, which on an unattended VM is nobody.

``collector_sequences`` is the answer to "was yesterday fully collected?" asked
of the store alone:

* ``steps_planned`` is written **on entry**, before any step runs, for the same
  reason the run row goes in as ``running``: the count of intended work has to
  survive the process that intended it;
* ``steps_completed`` counts steps that reached a verdict of their own — a step
  omitted by ``--skip``, or whose targets a ``--only`` filter did not name, has
  collected nothing and does not count;
* ``exit_code`` is stored beside the status so the thing systemd saw and the
  thing a human reads the morning after are one row and can be checked against
  each other, rather than two accounts that merely coexist.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from ..logging_setup import get_logger
from ..store import Store
from ..util.timeutil import utc_stamp
from .runlog import DETAIL_EXCERPT_CHARS, RunStatus

log = get_logger("collect.sequence")

#: Exit codes, restated here rather than imported from ``collect.cli`` because
#: the CLI imports this module and the cycle would be the wrong way round. The
#: single definition lives in ``collect.cli``; the test
#: ``test_the_exit_codes_are_one_definition`` in
#: ``tests/test_collect_sequence_isolation.py`` pins the two against each
#: other so they cannot drift.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 2

#: Which statuses a step contributes as "it reported a verdict about data".
#: SKIPPED is deliberately absent: a filtered-out step collected nothing. So is
#: INTERRUPTED — a step abandoned at its wall-clock budget wrote an unknown
#: amount and claims nothing about it — but unlike SKIPPED it still moves the
#: sequence's own verdict; see ``SequenceRun.status``.
_COMPLETING = frozenset({RunStatus.OK, RunStatus.DEGRADED, RunStatus.PARTIAL, RunStatus.ERROR})


@dataclass
class StepOutcome:
    """One step's verdict inside a sequence."""

    step: str
    status: str
    reason: str | None = None
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"step": self.step, "status": self.status}
        if self.reason:
            payload["reason"] = self.reason[:DETAIL_EXCERPT_CHARS]
        if self.detail:
            payload.update(self.detail)
        return payload


class SequenceRun:
    """Context manager writing one ``collector_sequences`` row per invocation.

    Mirrors ``CollectorRun`` one level up, including its two contracts: the row
    is written on entry so a killed process leaves evidence, and the exception is
    never swallowed.
    """

    def __init__(
        self,
        store: Store,
        command: str,
        *,
        session_date: str,
        steps: tuple[str, ...],
        dry_run: bool = False,
    ) -> None:
        self._store = store
        self._command = command
        self._session_date = session_date
        self._steps = tuple(steps)
        self._dry_run = dry_run
        self._started_at = utc_stamp()
        self.sequence_id = uuid.uuid4().hex
        self.outcomes: list[StepOutcome] = []
        self.notes: dict[str, Any] = {}

    # ── recording ──

    def step(
        self,
        step: str,
        status: str,
        *,
        reason: str | None = None,
        **detail: Any,
    ) -> StepOutcome:
        """Record one step's verdict, and persist the running count immediately.

        Persisted as it happens rather than at the end, because the case this
        table exists for is the one where the end never arrives.
        """
        outcome = StepOutcome(step=step, status=status, reason=reason, detail=detail or None)
        self.outcomes.append(outcome)
        log.info(
            "sequence step finished",
            extra={
                "sequence_id": self.sequence_id,
                "command": self._command,
                "step": step,
                "status": status,
                "reason": reason,
            },
        )
        if not self._dry_run:
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE collector_sequences SET steps_completed = ?, detail = ? "
                    "WHERE sequence_id = ?",
                    (self.steps_completed, self._detail_json(), self.sequence_id),
                )
        return outcome

    # ── derived state ──

    @property
    def steps_planned(self) -> int:
        return len(self._steps)

    @property
    def steps_completed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status in _COMPLETING)

    def statuses(self) -> list[str]:
        return [outcome.status for outcome in self.outcomes]

    def status(self) -> str:
        """The sequence's verdict, worst step wins.

        A sequence that ran every step and lost one of them is PARTIAL, not
        ERROR: what the other steps collected is real and is in the store. Only a
        sequence where nothing at all reported is ERROR, and one where nothing
        was even asked to run is SKIPPED — never OK, because success over nothing
        is the report this package exists to refuse.

        An INTERRUPTED step counts against the verdict without counting toward
        ``steps_completed``, and both halves of that matter. It reached no verdict
        of its own, so it is not in ``_COMPLETING`` and must not make the sequence
        look like it did more work than it did — but a `chain` step abandoned at
        its wall-clock budget beside five healthy ones would otherwise return OK
        and exit 0, which is the quietest possible way to lose the one series no
        amount of money recovers.
        """
        interrupted = [o for o in self.outcomes if o.status == RunStatus.INTERRUPTED]
        reported = [outcome.status for outcome in self.outcomes if outcome.status in _COMPLETING]
        if not reported:
            if interrupted:
                return RunStatus.ERROR
            return RunStatus.SKIPPED if self.outcomes else RunStatus.ERROR
        failed = [status for status in reported if status in (RunStatus.ERROR, RunStatus.PARTIAL)]
        if failed:
            everything_failed = len(failed) == len(reported) and not interrupted
            return RunStatus.ERROR if everything_failed else RunStatus.PARTIAL
        if interrupted:
            return RunStatus.PARTIAL
        if any(status == RunStatus.DEGRADED for status in reported):
            return RunStatus.DEGRADED
        return RunStatus.OK

    def exit_code(self) -> int:
        status = self.status()
        if status in (RunStatus.ERROR, RunStatus.PARTIAL, RunStatus.INTERRUPTED):
            return EXIT_FAILED
        if status == RunStatus.DEGRADED:
            return EXIT_DEGRADED
        return EXIT_OK

    def _detail_json(self) -> str:
        detail: dict[str, Any] = {"steps": [outcome.as_dict() for outcome in self.outcomes]}
        detail["planned"] = list(self._steps)
        if self.notes:
            detail["notes"] = self.notes
        return json.dumps(detail, default=str)

    # ── lifecycle ──

    def __enter__(self) -> SequenceRun:
        if self._dry_run:
            return self
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO collector_sequences (sequence_id, command, session_date, "
                "started_at, status, steps_planned, steps_completed, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    self.sequence_id,
                    self._command,
                    self._session_date,
                    self._started_at,
                    RunStatus.RUNNING,
                    self.steps_planned,
                    self._detail_json(),
                ),
            )
        log.info(
            "sequence started",
            extra={
                "sequence_id": self.sequence_id,
                "command": self._command,
                "session_date": self._session_date,
                "steps_planned": self.steps_planned,
            },
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        status = self.status() if exc_type is None else RunStatus.ERROR
        code = self.exit_code() if exc_type is None else EXIT_FAILED
        if exc is not None:
            self.notes["error"] = repr(exc)[:DETAIL_EXCERPT_CHARS]

        log.info(
            "sequence finished",
            extra={
                "sequence_id": self.sequence_id,
                "command": self._command,
                "status": status,
                "exit_code": code,
                "steps_planned": self.steps_planned,
                "steps_completed": self.steps_completed,
            },
        )

        if not self._dry_run:
            with self._store.transaction() as connection:
                connection.execute(
                    "UPDATE collector_sequences SET finished_at = ?, status = ?, "
                    "steps_completed = ?, exit_code = ?, detail = ? WHERE sequence_id = ?",
                    (
                        utc_stamp(),
                        status,
                        self.steps_completed,
                        code,
                        self._detail_json(),
                        self.sequence_id,
                    ),
                )
        return False  # never swallow the exception
