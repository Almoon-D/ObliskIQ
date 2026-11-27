"""The LLM analyst in the store (015_analyst, 022_analyst_segments): its calls, its drafts, its
second opinions and its reviews, all append-only, and the month's spend on the LLM, which every
role shares, with each analyst segment's own.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime

from ..domain.analyst import (
    AnalystCall,
    CallKind,
    DraftOutcome,
    DraftRecord,
    SecondOpinion,
    ThesisReview,
    segment_label,
)
from ..domain.sources import ExtractionStatus
from ..domain.thesis import OPEN_STATES, Thesis, ThesisSource
from ..errors import StoreError
from . import theses as thesis_repo
from .db import Store
from .mappers import cutoff_text, date_text, read_date, read_stamp, stamp_text
from .panel import panel_spend
from .sources import extraction_spend

_CALL_COLUMNS = (
    "call_id, kind, thesis_id, as_of, sheet_hash, produced_at, provider, model_id, "
    "prompt_version, prompt_hash, status, attempts, output, error, input_tokens, output_tokens, "
    "cost_usd, segment"
)


def _read[T](table: str, row: sqlite3.Row, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:
        raise StoreError(f"{table} row {tuple(row)[:3]!r} cannot be read: {error}") from None


def _call(row: sqlite3.Row) -> AnalystCall:
    return _read(
        "analyst_calls",
        row,
        lambda: AnalystCall(
            call_id=row["call_id"],
            kind=CallKind(row["kind"]),
            thesis_id=row["thesis_id"],
            as_of=read_date(row["as_of"], "analyst_calls", "as_of"),
            sheet_hash=row["sheet_hash"],
            produced_at=read_stamp(row["produced_at"], "analyst_calls", "produced_at"),
            provider=row["provider"],
            model_id=row["model_id"],
            prompt_version=row["prompt_version"],
            prompt_hash=row["prompt_hash"],
            status=ExtractionStatus(row["status"]),
            attempts=row["attempts"],
            output=row["output"],
            error=row["error"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
            segment=row["segment"],
        ),
    )


def record_call(store: Store, call: AnalystCall) -> AnalystCall:
    """Append one call; the store numbers it."""
    with store.transaction():
        cursor = store.execute(
            f"INSERT INTO analyst_calls ({_CALL_COLUMNS.removeprefix('call_id, ')}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.kind.value,
                call.thesis_id,
                date_text(call.as_of),
                call.sheet_hash,
                stamp_text(call.produced_at),
                call.provider,
                call.model_id,
                call.prompt_version,
                call.prompt_hash,
                call.status.value,
                call.attempts,
                call.output,
                call.error,
                call.input_tokens,
                call.output_tokens,
                call.cost_usd,
                call.segment,
            ),
        )
    return call.model_copy(update={"call_id": cursor.lastrowid})


def calls(store: Store, *, as_of: date) -> list[AnalystCall]:
    """Every call produced by the end of ``as_of``, in the order produced."""
    rows = store.execute(
        f"SELECT {_CALL_COLUMNS} FROM analyst_calls WHERE produced_at < ? ORDER BY call_id",
        (cutoff_text(as_of),),
    ).fetchall()
    return [_call(row) for row in rows]


def get_call(store: Store, call_id: int) -> AnalystCall | None:
    row = store.execute(
        f"SELECT {_CALL_COLUMNS} FROM analyst_calls WHERE call_id = ?", (call_id,)
    ).fetchone()
    return None if row is None else _call(row)


def analyst_spend(store: Store, since: datetime) -> float:
    """What the analyst's calls produced since ``since`` cost."""
    row = store.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM analyst_calls WHERE produced_at >= ?",
        (stamp_text(since),),
    ).fetchone()
    return float(row["spent"])


def segment_spend(store: Store, segment: str, since: datetime) -> float:
    """What one analyst segment's calls produced since ``since`` cost."""
    row = store.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM analyst_calls "
        "WHERE segment = ? AND produced_at >= ?",
        (segment, stamp_text(since)),
    ).fetchone()
    return float(row["spent"])


def label_spend(store: Store, *, as_of: date) -> dict[str, float]:
    """What each segment (as the record names it) spent on the calls it produced by the end of
    ``as_of``, every pass counted."""
    rows = store.execute(
        "SELECT segment, prompt_hash, SUM(cost_usd) AS spent FROM analyst_calls "
        "WHERE segment IS NOT NULL AND produced_at < ? GROUP BY segment, prompt_hash",
        (cutoff_text(as_of),),
    ).fetchall()
    found: dict[str, float] = {}
    for row in rows:
        label = segment_label(row["segment"], row["prompt_hash"])
        found[label] = found.get(label, 0.0) + float(row["spent"])
    return found


def llm_spend(store: Store, since: datetime) -> float:
    """What every LLM call since ``since`` cost: the extractor's, the analyst's and the weekly
    panel's, against the one monthly budget (``llm.budget.monthly_usd``)."""
    return extraction_spend(store, since) + analyst_spend(store, since) + panel_spend(store, since)


# ── drafts ──


def record_drafts(store: Store, records: Sequence[DraftRecord]) -> None:
    """Append what became of each draft of a call."""
    with store.transaction():
        store.executemany(
            "INSERT INTO analyst_drafts (call_id, seq, draft, facts, outcome, reason, thesis_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (r.call_id, r.seq, r.draft, r.facts, r.outcome.value, r.reason, r.thesis_id)
                for r in records
            ],
        )


def _draft(row: sqlite3.Row) -> DraftRecord:
    return _read(
        "analyst_drafts",
        row,
        lambda: DraftRecord(
            call_id=row["call_id"],
            seq=row["seq"],
            draft=row["draft"],
            facts=row["facts"],
            outcome=DraftOutcome(row["outcome"]),
            reason=row["reason"],
            thesis_id=row["thesis_id"],
        ),
    )


def drafts_of(store: Store, call_id: int) -> list[DraftRecord]:
    rows = store.execute(
        "SELECT * FROM analyst_drafts WHERE call_id = ? ORDER BY seq", (call_id,)
    ).fetchall()
    return [_draft(row) for row in rows]


def drafts_handed(store: Store, *, first: date, last: date, segment: str | None = None) -> int:
    """The drafts handed to the intake (accepted or refused there) by calls whose fact sheet is
    dated between ``first`` and ``last``, both included; by one segment's calls, when named."""
    row = store.execute(
        "SELECT COUNT(*) FROM analyst_drafts d JOIN analyst_calls c ON c.call_id = d.call_id "
        "WHERE d.outcome IN ('accepted', 'refused') AND c.as_of >= ? AND c.as_of <= ? "
        "AND (? IS NULL OR c.segment = ?)",
        (date_text(first), date_text(last), segment, segment),
    ).fetchone()
    return int(row[0])


def draft_for_thesis(store: Store, thesis_id: int) -> tuple[DraftRecord, AnalystCall] | None:
    """The draft an analyst thesis came from, with its call."""
    row = store.execute("SELECT * FROM analyst_drafts WHERE thesis_id = ?", (thesis_id,)).fetchone()
    if row is None:
        return None
    record = _draft(row)
    call = get_call(store, record.call_id)
    if call is None:  # pragma: no cover - the foreign key keeps it
        raise StoreError(f"draft {record.call_id}.{record.seq} has no call")
    return record, call


def thesis_segments(store: Store) -> dict[int, str]:
    """The segment (its name, or before segments its model, and its prompt hash) each analyst
    thesis was proposed in."""
    rows = store.execute(
        "SELECT d.thesis_id, c.model_id, c.prompt_hash, c.segment FROM analyst_drafts d "
        "JOIN analyst_calls c ON c.call_id = d.call_id WHERE d.thesis_id IS NOT NULL"
    ).fetchall()
    return {
        int(row["thesis_id"]): segment_label(row["segment"] or row["model_id"], row["prompt_hash"])
        for row in rows
    }


def thesis_segment_names(store: Store) -> dict[int, str | None]:
    """The configured segment that proposed each analyst thesis (``None`` before segments)."""
    rows = store.execute(
        "SELECT d.thesis_id, c.segment FROM analyst_drafts d "
        "JOIN analyst_calls c ON c.call_id = d.call_id WHERE d.thesis_id IS NOT NULL"
    ).fetchall()
    return {int(row["thesis_id"]): row["segment"] for row in rows}


# ── second opinions ──


def record_opinion(store: Store, opinion: SecondOpinion) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO thesis_opinions (thesis_id, call_id, probability, critique, facts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                opinion.thesis_id,
                opinion.call_id,
                opinion.probability,
                opinion.critique,
                opinion.facts,
            ),
        )


def _opinion(row: sqlite3.Row) -> SecondOpinion:
    return _read(
        "thesis_opinions",
        row,
        lambda: SecondOpinion(
            thesis_id=row["thesis_id"],
            call_id=row["call_id"],
            probability=row["probability"],
            critique=row["critique"],
            facts=row["facts"],
        ),
    )


def opinions(store: Store, *, as_of: date) -> list[tuple[SecondOpinion, AnalystCall]]:
    """Every second opinion produced by the end of ``as_of``, with its call, in order."""
    found = []
    for call in calls(store, as_of=as_of):
        if call.kind is not CallKind.OPINION or call.status is not ExtractionStatus.OK:
            continue
        row = store.execute(
            "SELECT * FROM thesis_opinions WHERE call_id = ?", (call.call_id,)
        ).fetchone()
        if row is not None:
            found.append((_opinion(row), call))
    return found


def opinion_for(store: Store, thesis_id: int) -> tuple[SecondOpinion, AnalystCall] | None:
    row = store.execute(
        "SELECT * FROM thesis_opinions WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    if row is None:
        return None
    opinion = _opinion(row)
    call = get_call(store, opinion.call_id)
    if call is None:  # pragma: no cover - the foreign key keeps it
        raise StoreError(f"the opinion on thesis {thesis_id} has no call")
    return opinion, call


# ── reviews ──


def record_review(store: Store, review: ThesisReview) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO thesis_reviews (call_id, thesis_id, probability, critique, facts) "
            "VALUES (?, ?, ?, ?, ?)",
            (review.call_id, review.thesis_id, review.probability, review.critique, review.facts),
        )


def _review(row: sqlite3.Row) -> ThesisReview:
    return _read(
        "thesis_reviews",
        row,
        lambda: ThesisReview(
            call_id=row["call_id"],
            thesis_id=row["thesis_id"],
            probability=row["probability"],
            critique=row["critique"],
            facts=row["facts"],
        ),
    )


def reviews(store: Store, *, as_of: date) -> list[tuple[ThesisReview, AnalystCall]]:
    """Every review produced by the end of ``as_of``, with its call, in order."""
    found = []
    for call in calls(store, as_of=as_of):
        if call.kind is not CallKind.REVIEW or call.status is not ExtractionStatus.OK:
            continue
        row = store.execute(
            "SELECT * FROM thesis_reviews WHERE call_id = ?", (call.call_id,)
        ).fetchone()
        if row is not None:
            found.append((_review(row), call))
    return found


def reviewed(store: Store, *, segment: str, first: date, last: date) -> set[int]:
    """The theses ``segment`` asked a review of (whatever came of it) on a sheet dated between
    ``first`` and ``last``, both included."""
    rows = store.execute(
        "SELECT DISTINCT thesis_id FROM analyst_calls WHERE kind = 'review' AND segment = ? "
        "AND as_of >= ? AND as_of <= ?",
        (segment, date_text(first), date_text(last)),
    ).fetchall()
    return {int(row["thesis_id"]) for row in rows}


def awaiting_opinion(store: Store, *, as_of: date, max_failures: int) -> list[Thesis]:
    """The owner's open theses the analyst owes an opinion: none written, no call that failed
    closed (``invalid``), and fewer than ``max_failures`` calls that failed outright."""
    rows = store.execute(
        "SELECT thesis_id, status, COUNT(*) AS n FROM analyst_calls WHERE kind = 'opinion' "
        "GROUP BY thesis_id, status"
    ).fetchall()
    tried: dict[int, dict[str, int]] = {}
    for row in rows:
        tried.setdefault(int(row["thesis_id"]), {})[row["status"]] = int(row["n"])
    owed = []
    for thesis in thesis_repo.theses(store, as_of=as_of, states=OPEN_STATES):
        if thesis.source is not ThesisSource.OWNER:
            continue
        seen = tried.get(thesis.thesis_id, {})
        if seen.get("ok") or seen.get("invalid") or seen.get("failed", 0) >= max_failures:
            continue
        owed.append(thesis)
    return owed


def review_targets(store: Store, *, segment: str, as_of: date) -> list[Thesis]:
    """What ``segment`` reviews: the owner's open theses, then its own theses that are open or
    whose idea-ledger call has not reached its horizon (an open call), newest first."""
    own = {thesis_id for thesis_id, name in thesis_segment_names(store).items() if name == segment}
    calling = {
        int(row["thesis_id"])
        for row in store.execute(
            "SELECT thesis_id FROM idea_calls WHERE horizon >= ? AND known_at < ?",
            (date_text(as_of), cutoff_text(as_of)),
        ).fetchall()
    }
    known = thesis_repo.theses(store, as_of=as_of)
    owner = [t for t in known if t.source is ThesisSource.OWNER and t.state in OPEN_STATES]
    mine = [
        t
        for t in reversed(known)
        if t.thesis_id in own and (t.state in OPEN_STATES or t.thesis_id in calling)
    ]
    return [*owner, *mine]
