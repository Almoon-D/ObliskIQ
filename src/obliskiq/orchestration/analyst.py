"""The analyst's run in the service: the fact sheet, each segment of the LLM analyst in turn (the
trading segment first), and their drafts handed to the thesis book's intake (MASTER_PLAN §5.5,
§5.10; DECISIONS D188); then the weekly forecast panel, which keeps one cohort an ISO week
(``llm/panel.py``) within the same budget.

``llm`` imports nothing from ``decision``: this module is where a draft crosses. Each valid draft
goes through :class:`~..decision.thesis_intake.ThesisIntake` as source ``analyst``, exactly as an
owner's idea does: the intake refuses it with its reasons or stores it as a candidate, and the
book's gate judges that at the next cycle. Every draft, dropped, refused or accepted, is stored
with the fact lines it cited. The run never touches the order path: with the LLM unreachable or
its budget spent, the cycles and the book's exits run as before.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final

from ..config.schema import ObliskConfig
from ..decision.thesis_intake import ThesisIntake
from ..domain.analyst import DraftOutcome, DraftRecord
from ..engines.thesis.gate import describe
from ..errors import ObliskError
from ..grid.facts import build_fact_sheet
from ..llm.analyst import Analyst, AnalystRun, facts_json
from ..llm.panel import ForecastPanel, PanelRules, PanelRun
from ..scoreboard.rules import RULES_PATH, load_locked
from ..store import analyst as analyst_repo
from ..store.worker import StoreWorker
from .runlog import DEGRADED, OK, SKIPPED, StepOutcome

#: The run log's name for an analyst run, and its first step.
ANALYST: Final = "analyst"
#: The step of the weekly forecast panel.
PANEL: Final = "panel"
#: The actor the intake logs a draft as.
ACTOR: Final = "analyst"
#: Digits a run's cost is recorded with (US dollars).
COST_DIGITS: Final = 6


@dataclass(frozen=True, slots=True)
class SegmentOutcome:
    """One segment's run and what the intake made of its drafts."""

    run: AnalystRun
    accepted: tuple[int, ...] = ()
    refused: int = 0
    dropped: int = 0


@dataclass(frozen=True, slots=True)
class AnalystOutcome:
    """A run of every segment on one fact sheet."""

    segments: tuple[SegmentOutcome, ...]
    lines: int

    @property
    def accepted(self) -> tuple[int, ...]:
        return tuple(n for s in self.segments for n in s.accepted)

    @property
    def cost_usd(self) -> float:
        return sum(s.run.cost_usd for s in self.segments)


class AnalystJob:
    """Builds the fact sheet, runs each segment and hands its drafts to the intake."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        analysts: Sequence[Analyst],
        worker: StoreWorker,
        now: Callable[[], datetime],
        panel: ForecastPanel | None = None,
    ) -> None:
        self._settings = settings
        self._analysts = tuple(analysts)
        self._worker = worker
        self._intake = ThesisIntake(settings, worker=worker, now=now)
        self.panel = panel

    async def run(self, as_of: date) -> AnalystOutcome:
        sheet = await self._worker.run(build_fact_sheet, self._settings, as_of=as_of)
        done = []
        for analyst in self._analysts:
            done.append(await self._hand(await analyst.run(sheet)))
        return AnalystOutcome(tuple(done), len(sheet.lines))

    async def _hand(self, result: AnalystRun) -> SegmentOutcome:
        """Each valid draft of a segment's run to the intake; every draft recorded."""
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
                    call_id=proposal.call_id,
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
        return SegmentOutcome(
            result, tuple(accepted), count[DraftOutcome.REFUSED], count[DraftOutcome.DROPPED]
        )


def panel_rules(root: Path | None) -> PanelRules | str:
    """The panel's numbers from the scoreboard's locked rules, or why they cannot be read."""
    if root is None:
        return "no repository holds the research files (an installed wheel)"
    try:
        spec = load_locked(root / RULES_PATH).spec
    except ObliskError as exc:
        return str(exc)
    return PanelRules(
        horizon_days=spec.panel.horizon_days,
        max_mark_age_days=spec.grading.max_mark_age_days,
        min_names=spec.panel.min_names,
        min_coverage=spec.panel.min_coverage,
    )


def judge_panel(run: PanelRun) -> StepOutcome:
    """A panel run's verdict: skipped when no call was made (already kept, no key, no rules, a
    stale feed, too few fresh names), degraded when calls kept no cohort (the week is tried
    again) or a batch failed, ok otherwise."""
    detail: dict[str, object] = {
        "week": run.week,
        "universe": run.universe,
        "forecast": run.forecast,
        "calls": run.calls,
        "failed": run.failed,
        "invalid": run.invalid,
        "cost_usd": round(run.cost_usd, COST_DIGITS),
    }
    if run.state == "skipped" or (run.state == "incomplete" and run.calls == 0):
        return StepOutcome(PANEL, SKIPPED, run.reason, detail)
    status = OK if run.state == "ok" and not (run.failed or run.invalid) else DEGRADED
    return StepOutcome(PANEL, status, run.reason, detail)


def describe_segment(outcome: SegmentOutcome) -> str:
    run = outcome.run
    said = f"{run.segment} ({run.state}): {run.reason}"
    if run.proposals:
        said += (
            f"; to the intake: {len(outcome.accepted)} accepted, {outcome.refused} refused, "
            f"{outcome.dropped} dropped"
        )
    return said


def judge_analyst(outcome: AnalystOutcome) -> StepOutcome:
    """An analyst run's verdict: skipped when no segment has its key, degraded when the budget
    stopped a segment or a call failed or stayed invalid, ok otherwise (proposing nothing is ok,
    and a segment without its key is said, not a fault)."""
    detail: dict[str, object] = {
        "fact_lines": outcome.lines,
        "segments": {
            s.run.segment: {
                "state": s.run.state,
                "label": s.run.label,
                "opinions": s.run.opinions,
                "reviews": s.run.reviews,
                "candidates": s.run.candidates,
                "accepted": list(s.accepted),
                "refused": s.refused,
                "dropped": s.dropped,
                "invalid": s.run.invalid,
                "failed": s.run.failed,
                "cost_usd": round(s.run.cost_usd, COST_DIGITS),
            }
            for s in outcome.segments
        },
        "cost_usd": round(outcome.cost_usd, COST_DIGITS),
    }
    reason = "; ".join(describe_segment(s) for s in outcome.segments) or "no segment configured"
    runs = [s.run for s in outcome.segments]
    if all(run.state == "no_key" for run in runs):
        return StepOutcome(ANALYST, SKIPPED, reason, detail)
    faulty = any(run.state == "budget" or run.failed or run.invalid for run in runs)
    return StepOutcome(ANALYST, DEGRADED if faulty else OK, reason, detail)
