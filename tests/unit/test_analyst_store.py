"""The analyst's records: append-only calls, drafts and second opinions; what is owed and
counted; segments; and the second opinions graded by their Brier score.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from thesis_rig import thesis

from obliskiq.domain.analyst import (
    UNSTORED_CALL,
    AnalystCall,
    CallKind,
    DraftOutcome,
    DraftRecord,
    SecondOpinion,
    brier,
    segment_label,
)
from obliskiq.domain.orders import ExecutionMode
from obliskiq.domain.sources import ExtractionStatus
from obliskiq.domain.thesis import ThesisGrade, ThesisSource, ThesisState
from obliskiq.engines.thesis.grading import opinion_record, record
from obliskiq.errors import StoreError
from obliskiq.store import Store
from obliskiq.store import analyst as analyst_repo
from obliskiq.store import theses as thesis_repo

NOW = datetime(2026, 10, 26, 21, 30, tzinfo=UTC)
AS_OF = NOW.date()
D = Decimal


def call(**changes: Any) -> AnalystCall:
    fields: dict[str, Any] = {
        "call_id": UNSTORED_CALL,
        "kind": CallKind.DRAFTS,
        "thesis_id": None,
        "as_of": AS_OF,
        "sheet_hash": "a" * 64,
        "produced_at": NOW,
        "provider": "gemini",
        "model_id": "gemini-3.1-flash-lite",
        "prompt_version": 1,
        "prompt_hash": "b" * 64,
        "status": ExtractionStatus.OK,
        "attempts": 1,
        "output": "{}",
        "error": None,
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.25,
    }
    fields.update(changes)
    return AnalystCall(**fields)


def grade(thesis_id: int, *, reached: bool, source: ThesisSource) -> ThesisGrade:
    return ThesisGrade(
        thesis_id=thesis_id,
        graded_at=NOW,
        mode=ExecutionMode.SHADOW,
        source=source,
        conviction="normal",
        exit_reason="target" if reached else "invalidation",
        quantity=D(10),
        entry_price=D(50),
        exit_price=D(60) if reached else D(40),
        reached=reached,
        payoff=D("0.2") if reached else D("-0.2"),
        stated_payoff=D("0.2"),
        days_held=10,
        horizon_days=100,
    )


# ── the models ──


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"kind": CallKind.OPINION}, "names its thesis"),
        ({"thesis_id": 3}, "names its thesis"),
        ({"output": None}, "exactly when it is ok"),
        ({"status": ExtractionStatus.FAILED, "output": None}, "says why"),
    ],
)
def test_a_call_is_coherent(changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        call(**changes)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"outcome": DraftOutcome.ACCEPTED, "reason": None}, "names its thesis"),
        ({"thesis_id": 4, "reason": None}, "names its thesis"),
        ({"reason": None}, "says why"),
    ],
)
def test_a_draft_record_is_coherent(changes: dict[str, Any], match: str) -> None:
    fields: dict[str, Any] = {
        "call_id": 1,
        "seq": 1,
        "draft": "{}",
        "facts": "[]",
        "outcome": DraftOutcome.DROPPED,
        "reason": "no fact",
        "thesis_id": None,
    }
    with pytest.raises(ValueError, match=match):
        DraftRecord(**{**fields, **changes})


def test_brier_and_segment_labels() -> None:
    assert brier(1.0, reached=True) == 0.0
    assert brier(1.0, reached=False) == 1.0
    assert brier(0.5, reached=True) == 0.25
    assert segment_label("m", "0123456789abcdef") == "m 0123456789ab"
    assert call().label == f"gemini-3.1-flash-lite {'b' * 12}"
    assert call(segment="lite").label == f"lite {'b' * 12}"


# ── the store ──


def test_calls_are_numbered_read_back_and_append_only(store: Store) -> None:
    first = analyst_repo.record_call(store, call())
    second = analyst_repo.record_call(store, call(produced_at=NOW + timedelta(days=1)))
    assert (first.call_id, second.call_id) == (1, 2)
    assert analyst_repo.calls(store, as_of=AS_OF) == [first]
    assert analyst_repo.get_call(store, 2) == second
    assert analyst_repo.get_call(store, 9) is None
    for statement in ("UPDATE analyst_calls SET cost_usd = 0", "DELETE FROM analyst_calls"):
        with pytest.raises(StoreError, match="append-only"), store.transaction():
            store.execute(statement)


def test_the_month_s_spend_counts_both_roles(store: Store) -> None:
    analyst_repo.record_call(store, call(cost_usd=0.5))
    analyst_repo.record_call(store, call(cost_usd=0.25, produced_at=NOW - timedelta(days=40)))
    start = datetime(2026, 10, 1, tzinfo=UTC)
    assert analyst_repo.analyst_spend(store, start) == 0.5
    assert analyst_repo.llm_spend(store, start) == 0.5  # no extraction yet


def test_drafts_count_by_their_sheet_s_week_and_link_to_theses(store: Store) -> None:
    made = analyst_repo.record_call(store, call())
    stored = thesis_repo.insert_thesis(store, thesis(source=ThesisSource.ANALYST), actor="a")
    analyst_repo.record_drafts(
        store,
        [
            DraftRecord(call_id=made.call_id, seq=1, draft='{"a": 1}', facts='["F1 | x"]',
                        outcome=DraftOutcome.ACCEPTED, reason=None, thesis_id=stored.thesis_id),
            DraftRecord(call_id=made.call_id, seq=2, draft="{}", facts="[]",
                        outcome=DraftOutcome.DROPPED, reason="no fact", thesis_id=None),
        ],
    )  # fmt: skip
    assert analyst_repo.drafts_handed(store, first=AS_OF, last=AS_OF) == 1
    assert analyst_repo.drafts_handed(store, first=AS_OF + timedelta(days=1), last=AS_OF) == 0
    assert [d.seq for d in analyst_repo.drafts_of(store, made.call_id)] == [1, 2]
    found = analyst_repo.draft_for_thesis(store, stored.thesis_id)
    assert found is not None
    assert found[1] == made
    assert analyst_repo.draft_for_thesis(store, 99) is None
    assert analyst_repo.thesis_segments(store) == {stored.thesis_id: made.label}
    with pytest.raises(StoreError, match="append-only"), store.transaction():
        store.execute("DELETE FROM analyst_drafts")


def test_an_opinion_is_written_once_and_owed_only_to_open_owner_ideas(store: Store) -> None:
    owner = thesis_repo.insert_thesis(store, thesis(), actor="t")
    analyst = thesis_repo.insert_thesis(store, thesis(source=ThesisSource.ANALYST), actor="a")
    ended = thesis_repo.insert_thesis(store, thesis(), actor="t")
    thesis_repo.move(store, ended.thesis_id, ThesisState.WITHDRAWN, at=NOW, actor="t", detail="x")
    owed = analyst_repo.awaiting_opinion(store, as_of=AS_OF, max_failures=2)
    assert [t.thesis_id for t in owed] == [owner.thesis_id]
    del analyst
    asked = analyst_repo.record_call(store, call(kind=CallKind.OPINION, thesis_id=owner.thesis_id))
    given = SecondOpinion(
        thesis_id=owner.thesis_id,
        call_id=asked.call_id,
        probability=0.4,
        critique="thin",
        facts="[]",
    )
    analyst_repo.record_opinion(store, given)
    assert analyst_repo.awaiting_opinion(store, as_of=AS_OF, max_failures=2) == []
    assert analyst_repo.opinion_for(store, owner.thesis_id) == (given, asked)
    assert analyst_repo.opinion_for(store, 99) is None
    assert analyst_repo.opinions(store, as_of=AS_OF) == [(given, asked)]
    with pytest.raises(StoreError, match="written once"), store.transaction():
        store.execute("UPDATE thesis_opinions SET probability = 1")
    with pytest.raises(StoreError, match="never deleted"), store.transaction():
        store.execute("DELETE FROM thesis_opinions")


def test_failed_opinions_are_owed_until_the_cap(store: Store) -> None:
    owner = thesis_repo.insert_thesis(store, thesis(), actor="t")
    failed = call(
        kind=CallKind.OPINION,
        thesis_id=owner.thesis_id,
        status=ExtractionStatus.FAILED,
        output=None,
        error="down",
    )
    analyst_repo.record_call(store, failed)
    assert len(analyst_repo.awaiting_opinion(store, as_of=AS_OF, max_failures=2)) == 1
    analyst_repo.record_call(store, failed)
    assert analyst_repo.awaiting_opinion(store, as_of=AS_OF, max_failures=2) == []
    other = thesis_repo.insert_thesis(store, thesis(), actor="t")
    analyst_repo.record_call(
        store,
        failed.model_copy(
            update={"thesis_id": other.thesis_id, "status": ExtractionStatus.INVALID}
        ),
    )
    assert analyst_repo.awaiting_opinion(store, as_of=AS_OF, max_failures=2) == []


def test_a_row_that_cannot_be_read_is_an_error(store: Store) -> None:
    analyst_repo.record_call(store, call())
    store.execute("DROP TRIGGER analyst_calls_no_update")
    store.execute("UPDATE analyst_calls SET sheet_hash = ?", ("z" * 64,))
    with pytest.raises(StoreError, match="cannot be read"):
        analyst_repo.calls(store, as_of=AS_OF)


# ── grading ──


def test_second_opinions_are_graded_by_segment_once_their_thesis_closed() -> None:
    first = call(call_id=1, kind=CallKind.OPINION, thesis_id=1)
    other = call(call_id=2, kind=CallKind.OPINION, thesis_id=2, prompt_hash="c" * 64)
    given = [
        (SecondOpinion(thesis_id=1, call_id=1, probability=0.8, critique="c", facts="[]"), first),
        (SecondOpinion(thesis_id=2, call_id=2, probability=0.3, critique="c", facts="[]"), other),
        (SecondOpinion(thesis_id=3, call_id=3, probability=0.9, critique="c", facts="[]"), first),
    ]
    grades = [
        grade(1, reached=True, source=ThesisSource.OWNER),
        grade(2, reached=False, source=ThesisSource.OWNER),
    ]
    scores = {s.segment: s for s in opinion_record(given, grades)}
    assert scores[first.label].graded == 1  # thesis 3 has not closed
    assert scores[first.label].brier == pytest.approx(0.04)
    assert scores[other.label].brier == pytest.approx(0.09)
    assert opinion_record(given, []) == []


def test_the_analyst_s_record_is_kept_per_segment() -> None:
    grades = [
        grade(1, reached=True, source=ThesisSource.ANALYST),
        grade(2, reached=False, source=ThesisSource.ANALYST),
        grade(3, reached=True, source=ThesisSource.OWNER),
    ]
    groups = {g.group: g for g in record(grades, {1: "m aaa", 2: "m bbb", 3: "m ccc"})}
    assert groups["analyst m aaa"].closed == 1
    assert groups["analyst m bbb"].reached == 0
    assert "analyst m ccc" not in groups  # the owner's idea is not the analyst's
    assert groups["source analyst"].closed == 2
    assert {g.group for g in record(grades)} >= {"book", "source analyst", "source owner"}
