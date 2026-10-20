"""Run records: every job the service runs leaves a row before it works and a verdict after.

Ported from lite's ``SequenceRun`` and ``CollectorRun`` (legacy/lite collect/sequence.py,
runlog.py; DECISIONS "Running unattended"): the run's row goes in as ``running`` with the steps it
plans **before** any step runs, each step's row before the step works, and each verdict as it
lands, because the case these rows exist for is the one where the end never arrives. A killed
process's rows are closed as ``interrupted`` by the next service start (``store.runs.reap_running``
under the service lock), never by guessing from a PID.

**Every step is guarded, and the guard is deliberately broad** (lite): a step that raises anything
is recorded ``error`` with its reason and the next step runs, because the failure the guard exists
for is the one nobody anticipated. The run's verdict is the worst step's: every step failed is
``error``, some failed ``partial``, a weaker input ``degraded``, nothing asked to run ``skipped``,
never ``ok`` over nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..logging_setup import get_logger
from ..store import runs as repo
from ..store.worker import StoreWorker

log = get_logger("orchestration.runlog")

#: How much of a failure's text a step's record keeps (lite's DETAIL_EXCERPT_CHARS).
DETAIL_EXCERPT_CHARS = 500

OK = "ok"
DEGRADED = "degraded"
PARTIAL = "partial"
ERROR = "error"
SKIPPED = "skipped"
INTERRUPTED = repo.INTERRUPTED

#: Exit codes (lite's): what systemd and a person read the morning after agree.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 2

#: Step verdicts that report something about the work (``skipped`` collected nothing).
_COMPLETING = frozenset({OK, DEGRADED, PARTIAL, ERROR})


@dataclass(frozen=True, slots=True)
class StepOutcome:
    name: str
    status: str
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"step": self.name, "status": self.status}
        if self.reason:
            payload["reason"] = self.reason[:DETAIL_EXCERPT_CHARS]
        payload.update(self.detail)
        return payload


def verdict(outcomes: list[StepOutcome]) -> str:
    """The run's status, worst step wins (lite's ``SequenceRun.status``)."""
    reported = [o.status for o in outcomes if o.status in _COMPLETING]
    if not reported:
        return SKIPPED if outcomes else ERROR
    failed = [status for status in reported if status in (ERROR, PARTIAL)]
    if failed:
        return ERROR if len(failed) == len(reported) else PARTIAL
    return DEGRADED if DEGRADED in reported else OK


def exit_code(status: str) -> int:
    if status in (ERROR, PARTIAL, INTERRUPTED):
        return EXIT_FAILED
    return EXIT_DEGRADED if status == DEGRADED else EXIT_OK


class RunRecord:
    """One job's row and its steps' rows (``dry_run``: nothing is written)."""

    def __init__(
        self,
        worker: StoreWorker,
        *,
        command: str,
        session_date: date,
        steps: tuple[str, ...],
        now: Callable[[], datetime],
        dry_run: bool = False,
    ) -> None:
        self._worker = worker
        self.command = command
        self._session_date = session_date
        self._steps = steps
        self._now = now
        self._dry_run = dry_run
        self.run_id = str(uuid.uuid7())
        self.outcomes: list[StepOutcome] = []
        self.notes: dict[str, Any] = {}

    def _detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "planned": list(self._steps),
            "steps": [outcome.as_dict() for outcome in self.outcomes],
        }
        detail.update(self.notes)
        return detail

    @property
    def steps_completed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status in _COMPLETING)

    @property
    def status(self) -> str:
        return verdict(self.outcomes)

    async def begin(self) -> None:
        if self._dry_run:
            return
        await self._worker.run(
            repo.begin_run,
            run_id=self.run_id,
            command=self.command,
            session_date=self._session_date,
            started_at=self._now(),
            steps=self._steps,
        )

    async def step[T](
        self, name: str, work: Callable[[], Awaitable[T]], judge: Callable[[T], StepOutcome]
    ) -> T | None:
        """Run one step: its row first, then ``work``; ``judge`` turns the result into the
        step's verdict. Anything the step raises is its ``error``, and ``None`` is returned."""
        index = len(self.outcomes)
        step_id = None
        if not self._dry_run:
            step_id = await self._worker.run(
                repo.begin_step,
                run_id=self.run_id,
                step_index=index,
                name=name,
                started_at=self._now(),
            )
        result: T | None = None
        try:
            done = await work()
            outcome = judge(done)
            result = done
        except Exception as exc:  # every step is guarded, deliberately broadly (lite)
            log.exception("a step failed", extra={"run": self.command, "step": name})
            outcome = StepOutcome(name, ERROR, f"{type(exc).__name__}: {exc}")
            result = None
        self.outcomes.append(outcome)
        log.info(
            "step finished",
            extra={"run": self.command, "step": name, "status": outcome.status},
        )
        if step_id is not None:
            await self._worker.run(
                repo.finish_step,
                step_id=step_id,
                run_id=self.run_id,
                finished_at=self._now(),
                status=outcome.status,
                detail=outcome.as_dict(),
                steps_completed=self.steps_completed,
                run_detail=self._detail(),
            )
        return result

    async def finish(self, error: BaseException | None = None) -> str:
        """Record the run's verdict; with ``error`` (the run itself broke) it is ``error``."""
        status = self.status if error is None else ERROR
        if error is not None:
            self.notes["error"] = repr(error)[:DETAIL_EXCERPT_CHARS]
        log.info(
            "run finished",
            extra={"run": self.command, "status": status, "steps": self.steps_completed},
        )
        if not self._dry_run:
            await self._worker.run(
                repo.finish_run,
                run_id=self.run_id,
                finished_at=self._now(),
                status=status,
                exit_code=exit_code(status),
                steps_completed=self.steps_completed,
                detail=self._detail(),
            )
        return status
