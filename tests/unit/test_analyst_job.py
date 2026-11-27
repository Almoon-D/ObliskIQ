"""The analyst's run in the service: each draft handed to the thesis book's intake as source
``analyst`` and recorded with what became of it; the intake refusing or failing loses no record.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.decision.thesis_intake import ThesisIntake
from obliskiq.domain.analyst import UNSTORED_CALL, AnalystCall, CallKind, DraftOutcome
from obliskiq.domain.sources import ExtractionStatus
from obliskiq.domain.thesis import ThesisSource
from obliskiq.errors import StoreError
from obliskiq.grid.facts import FactLine, FactSheet
from obliskiq.llm.analyst import AnalystRun, Proposal, check_draft
from obliskiq.orchestration import analyst as job_module
from obliskiq.orchestration.analyst import AnalystJob, judge_analyst
from obliskiq.store import Store
from obliskiq.store import analyst as analyst_repo
from obliskiq.store import theses as thesis_repo
from obliskiq.store.worker import StoreWorker

NOW = datetime(2026, 10, 26, 21, 30, tzinfo=UTC)
AS_OF = NOW.date()
SHEET = FactSheet(
    AS_OF,
    (FactLine("F1", date(2026, 10, 23), "ok", "instrument", "SPPW: close 45"),),
    ("SPPW", "FCX"),
    {"SPPW": 45.0, "FCX": 45.0},
)


def written(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "section": "instrument",
        "symbol": "SPPW",
        "view": "bullish",
        "expectation": "world equities hold their trend",
        "target": 60.0,
        "invalidation": 40.0,
        "horizon": "2027-04-30",
        "probability": 0.4,
        "facts": ["F1"],
    }
    return {**fields, **changes}


class CannedAnalyst:
    """An analyst whose run returns the drafts given, after storing its call."""

    def __init__(self, worker: StoreWorker, proposals: tuple[Proposal, ...], *, call: bool) -> None:
        self._worker = worker
        self._proposals = proposals
        self._call = call

    async def run(self, sheet: FactSheet) -> AnalystRun:
        if not self._call:
            return AnalystRun("no_key", "no key", "lite")
        made = await self._worker.run(
            analyst_repo.record_call,
            AnalystCall(
                call_id=UNSTORED_CALL,
                kind=CallKind.DOSSIER,
                thesis_id=None,
                as_of=sheet.as_of,
                sheet_hash=sheet.sheet_hash,
                produced_at=NOW,
                provider="gemini",
                model_id="m",
                prompt_version=1,
                prompt_hash="b" * 64,
                status=ExtractionStatus.OK,
                attempts=1,
                output="{}",
                error=None,
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.0,
                segment="lite",
            ),
        )
        proposals = tuple(replace(p, call_id=made.call_id) for p in self._proposals)
        return AnalystRun("ok", "drafts", "lite", calls=(made,), proposals=proposals)


def job(
    settings: ObliskConfig, worker: StoreWorker, monkeypatch: pytest.MonkeyPatch, *drafts: object
) -> AnalystJob:
    def sheet(_store: Store, _settings: ObliskConfig, *, as_of: date) -> FactSheet:
        assert as_of == AS_OF
        return SHEET

    monkeypatch.setattr(job_module, "build_fact_sheet", sheet)
    proposals = tuple(
        check_draft(n, d, SHEET, wanted=5, last_day=date(2027, 10, 26))
        for n, d in enumerate(drafts, start=1)
    )
    canned = CannedAnalyst(worker, proposals, call=bool(drafts))
    return AnalystJob(settings, analysts=[canned], worker=worker, now=lambda: NOW)  # type: ignore[list-item]


async def test_each_draft_is_handed_to_the_intake_and_recorded(
    store_worker: StoreWorker,
    store: Store,
    shipped_settings: ObliskConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = job(
        shipped_settings,
        store_worker,
        monkeypatch,
        written(),
        written(symbol="FCX", view="bearish"),  # the intake refuses a bearish non-defensive idea
        written(symbol="ACME"),
    )
    outcome = await ran.run(AS_OF)
    (segment,) = outcome.segments
    assert (outcome.accepted, segment.refused, segment.dropped, outcome.lines) == ((1,), 1, 1, 1)
    (thesis,) = thesis_repo.theses(store, as_of=AS_OF)
    assert (thesis.source, thesis.instrument.symbol) == (ThesisSource.ANALYST, "SPPW")
    assert thesis_repo.log_entries(store, 1)[0].actor == "analyst"
    (call,) = outcome.segments[0].run.calls
    records = analyst_repo.drafts_of(store, call.call_id)
    assert [r.outcome for r in records] == [
        DraftOutcome.ACCEPTED,
        DraftOutcome.REFUSED,
        DraftOutcome.DROPPED,
    ]
    assert records[0].thesis_id == 1
    assert json.loads(records[0].facts) == ["F1 | 2026-10-23 | ok | instrument | SPPW: close 45"]
    assert records[1].reason is not None
    assert records[1].reason.startswith("bearish_not_defensive")
    judged = judge_analyst(outcome)
    assert judged.status == "ok"
    assert judged.reason == "lite (ok): drafts; to the intake: 1 accepted, 1 refused, 1 dropped"


async def test_an_intake_that_fails_loses_no_record(
    store_worker: StoreWorker,
    store: Store,
    shipped_settings: ObliskConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: ThesisIntake, draft: object, *, actor: str) -> object:
        raise StoreError("database is locked")

    monkeypatch.setattr(ThesisIntake, "submit", broken)
    outcome = await job(shipped_settings, store_worker, monkeypatch, written()).run(AS_OF)
    assert (outcome.accepted, outcome.segments[0].refused) == ((), 1)
    (call,) = outcome.segments[0].run.calls
    (record,) = analyst_repo.drafts_of(store, call.call_id)
    assert record.reason == "the intake failed: database is locked"
    assert thesis_repo.theses(store, as_of=AS_OF) == []


async def test_a_run_without_a_drafts_call_records_nothing(
    store_worker: StoreWorker,
    store: Store,
    shipped_settings: ObliskConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = await job(shipped_settings, store_worker, monkeypatch).run(AS_OF)
    assert outcome.segments[0].run.state == "no_key"
    assert judge_analyst(outcome).status == "skipped"
    assert analyst_repo.calls(store, as_of=AS_OF) == []


def test_a_segment_without_its_key_is_said_not_a_fault() -> None:
    def runs(*states: str) -> job_module.AnalystOutcome:
        return job_module.AnalystOutcome(
            tuple(
                job_module.SegmentOutcome(AnalystRun(state, "why", name))  # type: ignore[arg-type]
                for name, state in zip(("lite", "strong"), states, strict=True)
            ),
            lines=3,
        )

    judged = judge_analyst(runs("ok", "no_key"))
    assert (judged.status, judged.reason) == ("ok", "lite (ok): why; strong (no_key): why")
    assert judge_analyst(runs("no_key", "no_key")).status == "skipped"
    assert judge_analyst(runs("ok", "budget")).status == "degraded"
