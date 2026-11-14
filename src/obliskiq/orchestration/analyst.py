"""The analyst's run in the service: the fact sheet, the LLM analyst, and its drafts handed to the
thesis book's intake (MASTER_PLAN §5.5, §5.10).

``llm`` imports nothing from ``decision``: this module is where a draft crosses. Each valid draft
goes through :class:`~..decision.thesis_intake.ThesisIntake` as source ``analyst``, exactly as an
owner's idea does: the intake refuses it with its reasons or stores it as a candidate, and the
book's gate judges that at the next cycle. Every draft, dropped, refused or accepted, is stored
with the fact lines it cited. The run never touches the order path: with the LLM unreachable or
its budget spent, the cycles and the book's exits run as before.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from ..config.schema import ObliskConfig
from ..decision.thesis_intake import ThesisIntake
from ..domain.analyst import DraftOutcome, DraftRecord
from ..engines.thesis.gate import describe
from ..errors import ObliskError
from ..grid.facts import build_fact_sheet
from ..llm.analyst import Analyst, AnalystRun, facts_json
from ..store import analyst as analyst_repo
from ..store.worker import StoreWorker
from .runlog import DEGRADED, OK, SKIPPED, StepOutcome

#: The run log's name for an analyst run, and its one step.
ANALYST: Final = "analyst"
#: The actor the intake logs a draft as.
ACTOR: Final = "analyst"
#: Digits a run's cost is recorded with (US dollars).
COST_DIGITS: Final = 6


@dataclass(frozen=True, slots=True)
class AnalystOutcome:
    """A run of the analyst and what the intake made of its drafts."""

    run: AnalystRun
    lines: int
    accepted: tuple[int, ...] = ()
    refused: int = 0
    dropped: int = 0


class AnalystJob:
    """Builds the fact sheet, runs the analyst and hands its drafts to the intake."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        analyst: Analyst,
        worker: StoreWorker,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._analyst = analyst
        self._worker = worker
        self._intake = ThesisIntake(settings, worker=worker, now=now)

    async def run(self, as_of: date) -> AnalystOutcome:
        sheet = await self._worker.run(build_fact_sheet, self._settings, as_of=as_of)
        result = await self._analyst.run(sheet)
        call = result.drafts_call
        if call is None:
            return AnalystOutcome(result, len(sheet.lines))
        records: list[DraftRecord] = []
        accepted: list[int] = []
        for proposal in result.proposals:
            reason: str | None = proposal.dropped
            thesis_id: int | None = None
            if proposal.answer is None:
                outcome = DraftOutcome.DROPPED
            else:
                try:
                    stored = await self._intake.submit(proposal.thesis_draft(), actor=ACTOR)
                except ObliskError as exc:  # the intake could not judge it: nothing was stored
                    outcome, reason = DraftOutcome.REFUSED, f"the intake failed: {exc}"
                else:
                    if isinstance(stored, list):
                        outcome, reason = DraftOutcome.REFUSED, describe(stored)
                    else:
                        outcome, thesis_id = DraftOutcome.ACCEPTED, stored.thesis_id
                        accepted.append(stored.thesis_id)
            records.append(
                DraftRecord(
                    call_id=call.call_id,
                    seq=proposal.seq,
                    draft=proposal.raw,
                    facts=facts_json(proposal.facts),
                    outcome=outcome,
                    reason=reason,
                    thesis_id=thesis_id,
                )
            )
        if records:
            await self._worker.run(analyst_repo.record_drafts, records)
        count = {o: sum(1 for r in records if r.outcome is o) for o in DraftOutcome}
        return AnalystOutcome(
            result,
            len(sheet.lines),
            tuple(accepted),
            count[DraftOutcome.REFUSED],
            count[DraftOutcome.DROPPED],
        )


def judge_analyst(outcome: AnalystOutcome) -> StepOutcome:
    """An analyst run's verdict: skipped without a key, degraded when the budget stopped it or a
    call failed or stayed invalid, ok otherwise (proposing nothing is ok)."""
    run = outcome.run
    detail: dict[str, object] = {
        "fact_lines": outcome.lines,
        "opinions": run.opinions,
        "accepted": list(outcome.accepted),
        "refused": outcome.refused,
        "dropped": outcome.dropped,
        "invalid": run.invalid,
        "failed": run.failed,
        "cost_usd": round(run.cost_usd, COST_DIGITS),
    }
    reason = run.reason
    if run.drafts_call is not None:
        reason += (
            f"; to the intake: {len(outcome.accepted)} accepted, {outcome.refused} refused, "
            f"{outcome.dropped} dropped"
        )
    if run.state == "skipped":
        return StepOutcome(ANALYST, SKIPPED, reason, detail)
    status = DEGRADED if run.state == "budget_spent" or run.failed or run.invalid else OK
    return StepOutcome(ANALYST, status, reason, detail)
