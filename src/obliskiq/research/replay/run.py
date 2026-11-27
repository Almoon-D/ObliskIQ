"""The replay's runs: the probe, then every registered segment on every registered sheet (H15).

**Order.** The probe runs first, for every replayed model; no segment is replayed until each of
its models has answered every question (the pre-registration: the probe runs before any replay
call).

**A unit** is one segment's run on one sheet date. The sheet is the forward builder's with the
registered sections (``grid/facts.py``), read on the replay's store as of that date; the segment
is the forward analyst with those sections in its hash (a replay segment is never the forward
one) and every call it makes stored with the record ``replay``. Its valid drafts are recorded as
refused at no intake (nothing is handed to the thesis book), its dropped ones as dropped, and the
unit as done. A unit with calls stored and no record was stopped part-way: it is recorded
``interrupted`` and never run again, so **a stopped run repeats no paid call**.

**The budget** (``llm.replay.max_usd``, the owner's). A run is refused when what the replay has
spent plus the worst case of what it has left would pass it: the worst case of a unit is its scan
and as many dossiers as the scan may name, each at its full output allowance and retried once,
on the whole sheet; the plan's is the first unit's times the units left. Before each unit its own
worst case, on its own sheet, is checked again, so the replay never spends past the cap.

Nothing here reads the forward store, writes a thesis or reaches a gate.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

import httpx

from ...config.schema import ObliskConfig
from ...config.schema_llm import LlmModelConfig
from ...domain.analyst import REPLAY, DraftOutcome, DraftRecord
from ...errors import ReplayRefusedError
from ...grid.facts import FactSheet, build_fact_sheet
from ...llm.analyst import Analyst, AnalystRun, facts_json
from ...llm.analyst_prompts import (
    DOSSIER_SYSTEM,
    MAX_TEXT_CHARS,
    SCAN_SYSTEM,
    STOCK_SECTION,
    describe_candidate,
    dossier_prompt,
    scan_prompt,
)
from ...llm.extractor import TOKENS_PER_PRICE_UNIT
from ...store import analyst as analyst_repo
from ...store import replay as replay_repo
from ...store.replay import Unit
from ...store.worker import StoreWorker
from ..prereg_replay import AnalystReplaySpec, ReplaySegmentSpec
from .plan import Dates, check_segment, replay_settings, sheet_dates
from .probe import Probe, Question

#: Why a valid replayed draft is not a thesis: nothing of the replay reaches the intake.
REPLAYED: Final = "replayed: no intake (H15)"


@dataclass(frozen=True, slots=True)
class Plan:
    """The segments to replay, each with its dates."""

    spec: AnalystReplaySpec
    segments: tuple[tuple[ReplaySegmentSpec, Dates], ...]

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(
            sorted({name for s, _ in self.segments for name in (s.scan_model, s.dossier_model)})
        )


def plan_of(spec: AnalystReplaySpec, settings: ObliskConfig) -> Plan:
    """Every registered segment checked against the configuration, with its dates; a segment
    whose models are not the registered ones refuses the whole replay."""
    found = []
    for segment in spec.segments:
        check_segment(spec, settings, segment)
        found.append((segment, sheet_dates(spec, segment)))
    return Plan(spec, tuple(found))


def _cost(model: LlmModelConfig, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * model.input_usd_per_million + output_tokens * model.output_usd_per_million
    ) / TOKENS_PER_PRICE_UNIT


def unit_worst(settings: ObliskConfig, segment: str, sheet: FactSheet) -> float:
    """A unit's worst case: its scan and a dossier for every candidate the scan may name, each
    at its full output allowance and retried once, each dossier given the whole sheet.
    VERIFY-LIVE(the replay's spend): a replayed sheet's cost against this."""
    llm = settings.llm
    config = llm.analyst
    chosen = config.segments[segment]
    attempts = config.retries_on_invalid + 1

    def tokens(characters: int) -> int:
        return int(characters / llm.budget.chars_per_token_estimate) + 1

    def worst(model: LlmModelConfig, characters: int, cap: int) -> float:
        return attempts * _cost(model, tokens(characters) + cap, cap)

    scan_chars = len(SCAN_SYSTEM) + len(
        scan_prompt(
            as_of=sheet.as_of, wanted=config.scan.max_candidates, open_theses=(), facts=sheet.text()
        )
    )
    candidate = describe_candidate(STOCK_SECTION, "X" * MAX_TEXT_CHARS, "x" * MAX_TEXT_CHARS)
    dossier_chars = len(DOSSIER_SYSTEM) + len(
        dossier_prompt(
            settings,
            as_of=sheet.as_of,
            last_day=sheet.as_of + timedelta(days=config.max_horizon_days),
            open_theses=(),
            candidate=candidate,
            facts=sheet.text(),
        )
    )
    scan = worst(llm.models[chosen.scan_model], scan_chars, config.scan.max_output_tokens)
    dossier = worst(
        llm.models[chosen.dossier_model], dossier_chars, config.dossier.max_output_tokens
    )
    return scan + config.scan.max_candidates * dossier


@dataclass(slots=True)
class RunReport:
    """What a run did."""

    done: int = 0
    skipped: int = 0
    interrupted: int = 0
    cost_usd: float = 0.0
    lines: list[str] = field(default_factory=list)


class Replayer:
    """Runs the probe and the segments on the replay's store."""

    def __init__(
        self,
        settings: ObliskConfig,
        plan: Plan,
        *,
        client: httpx.AsyncClient,
        worker: StoreWorker,
        keys: Mapping[str, str | None],
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._plan = plan
        self._spec = plan.spec
        self._client = client
        self._worker = worker
        self._keys = keys
        self._now = now
        self._cap = settings.llm.replay.max_usd

    def _missing_keys(self, names: Sequence[str]) -> list[str]:
        llm = self._settings.llm
        return sorted(
            {
                llm.provider_of(name).key_env
                for name in names
                if self._keys.get(llm.models[name].provider) is None
            }
        )

    async def _spent(self) -> float:
        return await self._worker.run(replay_repo.replay_spend)

    def _refuse_past_cap(self, spent: float, worst: float, what: str) -> None:
        if spent + worst > self._cap:
            raise ReplayRefusedError(
                f"{what}: spent {spent:.2f} plus its worst case {worst:.2f} USD passes "
                f"llm.replay.max_usd {self._cap:.2f}; nothing is asked (raise the cap to at "
                f"least {spent + worst:.2f} to run it)"
            )

    # ── the probe ──

    async def probe(self, questions: Sequence[Question]) -> RunReport:
        report = RunReport()
        missing = self._missing_keys(self._plan.models)
        if missing:
            raise ReplayRefusedError(f"{', '.join(missing)} unset: the probe asks nothing")
        probe = Probe(
            self._settings,
            self._spec,
            client=self._client,
            worker=self._worker,
            keys=self._keys,
            now=self._now,
        )
        owed = {name: await probe.pending(name, questions) for name in self._plan.models}
        worst = sum(probe.worst_cost(name, pending) for name, pending in owed.items())
        self._refuse_past_cap(await self._spent(), worst, "the probe")
        for name, pending in owed.items():
            for question in pending:
                answer = await probe.ask(name, question)
                report.done += 1
                report.cost_usd += answer.cost_usd
            report.skipped += len(questions) - len(pending)
            report.lines.append(f"probe {name}: {len(pending)} asked, {len(questions)} in all")
        return report

    async def _probe_complete(self, questions: Sequence[Question]) -> list[str]:
        probe = Probe(
            self._settings,
            self._spec,
            client=self._client,
            worker=self._worker,
            keys=self._keys,
            now=self._now,
        )
        return [name for name in self._plan.models if await probe.pending(name, questions)]

    # ── the segments ──

    async def _sheet(self, day: date) -> FactSheet:
        return await self._worker.run(
            build_fact_sheet, self._settings, as_of=day, sections=self._spec.sheet.sections
        )

    def _analyst(self, segment: str) -> Analyst:
        return Analyst(
            replay_settings(self._settings, segment),
            segment=segment,
            client=self._client,
            worker=self._worker,
            keys=self._keys,
            now=self._now,
            sections=self._spec.sheet.sections,
            record=REPLAY,
        )

    async def run(self, questions: Sequence[Question]) -> RunReport:
        """Every unit not yet done, after the probe; refused past the cap."""
        unanswered = await self._probe_complete(questions)
        if unanswered:
            raise ReplayRefusedError(
                f"the probe has questions unanswered for {', '.join(unanswered)}: it runs first "
                "(obliskiq research replay probe)"
            )
        missing = self._missing_keys(self._plan.models)
        if missing:
            raise ReplayRefusedError(f"{', '.join(missing)} unset: no segment is replayed")
        held = {(u.label, u.sheet_date) for u in await self._worker.run(replay_repo.units)}
        analysts = {segment.name: self._analyst(segment.name) for segment, _ in self._plan.segments}
        todo = [
            (segment, dates, day)
            for segment, dates in self._plan.segments
            for day in dates.all
            if (analysts[segment.name].label, day) not in held
        ]
        report = RunReport(skipped=sum(len(d.all) for _, d in self._plan.segments) - len(todo))
        if not todo:
            return report
        first = await self._sheet(todo[0][2])
        estimate = unit_worst(self._settings, todo[0][0].name, first) * len(todo)
        self._refuse_past_cap(await self._spent(), estimate, f"the {len(todo)} sheets left")
        for segment, dates, day in sorted(todo, key=lambda item: (item[2], item[0].name)):
            analyst = analysts[segment.name]
            await self._unit(analyst, dates, day, report)
        return report

    async def _unit(self, analyst: Analyst, dates: Dates, day: date, report: RunReport) -> None:
        side = dates.side(day)
        stored = await self._worker.run(
            replay_repo.unit_calls,
            segment=analyst.segment,
            prompt_hash=analyst.prompt_hash,
            sheet_date=day,
        )
        if stored:
            await self._record(
                analyst,
                day,
                side,
                "interrupted",
                "stopped part-way: its calls "
                "are stored, its drafts are not, and it is never run again",
                sum(c.cost_usd for c in stored),
            )
            report.interrupted += 1
            return
        sheet = await self._sheet(day)
        self._refuse_past_cap(
            await self._spent(),
            unit_worst(self._settings, analyst.segment, sheet),
            f"segment {analyst.segment} on {day.isoformat()}",
        )
        result = await analyst.run(sheet)
        if result.state == "no_key":  # pragma: no cover - the keys are checked before any sheet
            raise ReplayRefusedError(result.reason)
        if result.state == "budget":
            await self._record(analyst, day, side, "interrupted", result.reason, result.cost_usd)
            report.interrupted += 1
            raise ReplayRefusedError(result.reason)
        await self._drafts(result)
        await self._record(analyst, day, side, "ok", result.reason, result.cost_usd)
        report.done += 1
        report.cost_usd += result.cost_usd
        report.lines.append(f"{analyst.label} {day.isoformat()}: {result.reason}")

    async def _drafts(self, result: AnalystRun) -> None:
        records = [
            DraftRecord(
                call_id=proposal.call_id,
                seq=proposal.seq,
                draft=proposal.raw,
                facts=facts_json(proposal.facts),
                outcome=DraftOutcome.DROPPED if proposal.answer is None else DraftOutcome.REFUSED,
                reason=proposal.dropped if proposal.answer is None else REPLAYED,
                thesis_id=None,
            )
            for proposal in result.proposals
        ]
        if records:
            await self._worker.run(analyst_repo.record_drafts, records)

    async def _record(
        self, analyst: Analyst, day: date, side: str, state: str, reason: str, cost: float
    ) -> None:
        await self._worker.run(
            replay_repo.record_unit,
            Unit(
                label=analyst.label,
                segment=analyst.segment,
                sheet_date=day,
                side="after" if side == "after" else "before",
                state="ok" if state == "ok" else "interrupted",
                reason=reason,
                cost_usd=cost,
                finished_at=self._now(),
            ),
        )
