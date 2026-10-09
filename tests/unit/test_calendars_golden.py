"""The sessions the library produced when the golden snapshot was reviewed.

A library upgrade that moves a session fails here, to be reviewed rather than absorbed.
See ``tests/fixtures/calendars/golden_sessions.md``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from obliskiq.calendars.service import Calendars

GOLDEN = Path(__file__).parents[1] / "fixtures" / "calendars" / "golden_sessions.json"
ROWS: list[dict[str, Any]] = json.loads(GOLDEN.read_text(encoding="utf-8"))["rows"]


def _local(instant: Any, like: Any) -> str | None:
    return None if instant is None else instant.astimezone(like.tzinfo).isoformat()


@pytest.mark.parametrize("row", ROWS, ids=lambda row: f"{row['venue']}-{row['day']}")
def test_a_session_matches_the_reviewed_snapshot(calendars: Calendars, row: dict[str, Any]) -> None:
    bounds = calendars.session_bounds(row["venue"], date.fromisoformat(row["day"]))
    if row["session"] is None:
        assert bounds is None
        return
    assert bounds is not None
    assert {
        "open": bounds.open_local.isoformat(),
        "close": bounds.close_local.isoformat(),
        "break_start": _local(bounds.break_start_utc, bounds.open_local),
        "break_end": _local(bounds.break_end_utc, bounds.open_local),
        "early_close": bounds.early_close,
    } == row["session"]


def test_the_snapshot_pins_every_library_covered_venue(calendars: Calendars) -> None:
    pinned = {row["venue"] for row in ROWS}
    covered = {mic for mic, window in calendars.coverage().items() if window is not None}
    assert pinned == covered
