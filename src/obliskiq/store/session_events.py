"""The broker session's repairs as the service heard them (018_h13_sessions): a competing session,
a session lost, a re-login that succeeded and one that failed. Append-only; the promotion gate
reads them for L1's "every daily reset re-logged unattended" (MASTER_PLAN §5.19).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .db import Store
from .mappers import read_stamp, stamp_text

type SessionEventKind = Literal[
    "competing_session", "session_lost", "reauthenticated", "reauthentication_failed"
]


@dataclass(frozen=True, slots=True)
class SessionEvent:
    at: datetime
    kind: SessionEventKind
    detail: str


def record_session_event(store: Store, event: SessionEvent) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO session_events (at, kind, detail) VALUES (?, ?, ?)",
            (stamp_text(event.at), event.kind, event.detail),
        )


def session_events(store: Store, *, since: datetime, as_of: datetime) -> list[SessionEvent]:
    """The events from ``since`` to ``as_of``, oldest first."""
    rows = store.execute(
        "SELECT at, kind, detail FROM session_events WHERE at >= ? AND at <= ? "
        "ORDER BY at, event_id",
        (stamp_text(since), stamp_text(as_of)),
    ).fetchall()
    return [
        SessionEvent(
            at=read_stamp(row["at"], "session_events", "at"), kind=row["kind"], detail=row["detail"]
        )
        for row in rows
    ]
