"""The idea ledger's records (``021_idea_ledger``; DECISIONS D187): the probability a source stated
with its idea, the calls written into the ledger, and each segment's calibration by day.

Every table is append-only and every read is bounded by the time it is made: a call or a
probability counts from its ``known_at``, a segment's calibration from the day it was graded.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from ..domain.thesis import ThesisSource
from ..errors import StoreError
from .db import Store
from .mappers import cutoff_text, date_text, read_date, read_stamp, stamp_text


@dataclass(frozen=True, slots=True)
class IdeaCall:
    """An idea written into the ledger (``call_id`` is ``None`` until it is stored)."""

    thesis_id: int
    conid: int
    symbol: str
    source: ThesisSource
    segment: str
    event_tags: tuple[str, ...]
    rises: bool
    target: Decimal
    invalidation: Decimal
    horizon: date
    probability: float | None
    known_at: datetime
    call_id: int | None = None


@dataclass(frozen=True, slots=True)
class SegmentState:
    """A segment's calibration as one day's grading found it."""

    segment: str
    as_of: date
    passed: bool
    shrink: float
    base_rate: float | None
    reason: str

    def p_eff(self, stated: float) -> float | None:
        """The calibrated probability of a stated one; ``None`` unless the segment passed."""
        if not self.passed or self.base_rate is None:
            return None
        return self.base_rate + self.shrink * (stated - self.base_rate)


def record_probability(store: Store, thesis_id: int, probability: float, *, at: datetime) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO thesis_probabilities (thesis_id, probability, known_at) VALUES (?, ?, ?)",
            (thesis_id, probability, stamp_text(at)),
        )


def probability(store: Store, thesis_id: int) -> float | None:
    """The probability stated with ``thesis_id``'s idea, if one was."""
    row = store.execute(
        "SELECT probability FROM thesis_probabilities WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    return None if row is None else float(row["probability"])


def record_call(store: Store, call: IdeaCall) -> IdeaCall:
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO idea_calls (thesis_id, conid, symbol, source, segment, event_tags, rises, "
            "target, invalidation, horizon, probability, known_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.thesis_id,
                call.conid,
                call.symbol,
                call.source.value,
                call.segment,
                json.dumps(list(call.event_tags)),
                int(call.rises),
                str(call.target),
                str(call.invalidation),
                date_text(call.horizon),
                call.probability,
                stamp_text(call.known_at),
            ),
        )
    return replace(call, call_id=cursor.lastrowid)


def _call(row: sqlite3.Row) -> IdeaCall:
    try:
        tags = json.loads(row["event_tags"])
        return IdeaCall(
            thesis_id=row["thesis_id"],
            conid=row["conid"],
            symbol=row["symbol"],
            source=ThesisSource(row["source"]),
            segment=row["segment"],
            event_tags=tuple(str(tag) for tag in tags),
            rises=bool(row["rises"]),
            target=Decimal(row["target"]),
            invalidation=Decimal(row["invalidation"]),
            horizon=read_date(row["horizon"], "idea_calls", "horizon"),
            probability=row["probability"],
            known_at=read_stamp(row["known_at"], "idea_calls", "known_at"),
            call_id=row["call_id"],
        )
    except (ValueError, ArithmeticError) as error:
        raise StoreError(f"idea_calls row {row['call_id']} cannot be read: {error}") from None


def calls(store: Store, *, as_of: date) -> list[IdeaCall]:
    """The calls known by the end of ``as_of``, oldest first."""
    rows = store.execute(
        "SELECT * FROM idea_calls WHERE known_at < ? ORDER BY known_at, call_id",
        (cutoff_text(as_of),),
    ).fetchall()
    return [_call(row) for row in rows]


def called_theses(store: Store) -> set[int]:
    """The theses already written into the ledger."""
    return {row[0] for row in store.execute("SELECT thesis_id FROM idea_calls").fetchall()}


def record_segment(store: Store, state: SegmentState, *, at: datetime) -> bool:
    """Store a segment's calibration for its day; ``False`` when that day is stored already."""
    with store.transaction():
        cursor = store.execute(
            "INSERT OR IGNORE INTO ledger_segments (segment, as_of, passed, shrink, base_rate, "
            "reason, known_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                state.segment,
                date_text(state.as_of),
                int(state.passed),
                state.shrink,
                state.base_rate,
                state.reason,
                stamp_text(at),
            ),
        )
    return cursor.rowcount == 1


def segment_states(store: Store, *, as_of: date) -> dict[str, SegmentState]:
    """Each segment's latest calibration graded on or before ``as_of``."""
    rows = store.execute(
        "SELECT * FROM ledger_segments s WHERE as_of = (SELECT max(as_of) FROM ledger_segments "
        "WHERE segment = s.segment AND as_of <= ?)",
        (date_text(as_of),),
    ).fetchall()
    return {
        row["segment"]: SegmentState(
            segment=row["segment"],
            as_of=read_date(row["as_of"], "ledger_segments", "as_of"),
            passed=bool(row["passed"]),
            shrink=float(row["shrink"]),
            base_rate=row["base_rate"],
            reason=row["reason"],
        )
        for row in rows
    }
