"""Regenerate ``golden_sessions.json`` from the installed ``exchange_calendars``.

    uv run python tests/fixtures/calendars/generate_golden.py

Review the diff against each exchange's published holiday and trading-hours calendar before
committing it (see ``golden_sessions.md``). A change after a library upgrade is the signal
this file exists to give.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from datetime import date
from importlib.metadata import version
from pathlib import Path

from obliskiq.calendars.service import Calendars
from obliskiq.config import load_config

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
REFERENCE = date(2026, 10, 9)
DAYS = (
    date(2026, 3, 6),  # B3 on standard time, a Friday
    date(2026, 3, 9),  # B3 once New York is on daylight saving time
    date(2026, 4, 3),  # Good Friday
    date(2026, 4, 6),  # Easter Monday
    date(2026, 10, 9),  # an ordinary Friday
    date(2026, 10, 26),  # the Monday after Europe's clocks go back
    date(2026, 11, 2),  # the Monday after New York's clocks go back
    date(2026, 11, 27),  # the day after US Thanksgiving: an early close
    date(2026, 12, 24),  # Christmas Eve: early closes in Europe and Hong Kong
)
VENUES = (
    "BVMF", "XAMS", "XASX", "XBOM", "XETR", "XEUR", "XHKG", "XKRX", "XLON", "XMAD", "XMEX",
    "XMIL", "XNAS", "XNYS", "XNZE", "XPAR", "XSES", "XSGO", "XSHG", "XSWX", "XTAI", "XTKS",
    "XTSE",
)  # fmt: skip


def main() -> None:
    settings = load_config(ROOT / "config").settings
    calendars = Calendars.from_settings(settings, reference=REFERENCE, venues=VENUES)
    rows = []
    for mic in VENUES:
        for day in DAYS:
            bounds = calendars.session_bounds(mic, day)
            session = None
            if bounds is not None:
                session = {
                    "open": bounds.open_local.isoformat(),
                    "close": bounds.close_local.isoformat(),
                    "break_start": _local(bounds.break_start_utc, bounds.open_local),
                    "break_end": _local(bounds.break_end_utc, bounds.open_local),
                    "early_close": bounds.early_close,
                }
            rows.append({"venue": mic, "day": day.isoformat(), "session": session})
    payload = {
        "library": f"exchange-calendars {version('exchange-calendars')}",
        "generated_on": REFERENCE.isoformat(),
        "rows": rows,
    }
    (HERE / "golden_sessions.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(rows)} rows")


def _local(instant, like):  # type: ignore[no-untyped-def]
    return None if instant is None else instant.astimezone(like.tzinfo).isoformat()


if __name__ == "__main__":
    main()
