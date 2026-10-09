# golden_sessions.json

Regular sessions (local open, close and lunch break, and whether the close is early) for 23
venues on nine dates: the Good Friday and Easter Monday of 2026, both daylight-saving changes,
the US post-Thanksgiving half day, Christmas Eve and ordinary days.

- **Source:** `exchange_calendars` 4.13.2 (`pip` package `exchange-calendars`), read through
  `obliskiq.calendars.Calendars.session_bounds` with the shipped configuration, so the
  Singapore midday break and the B3 daylight-saving close come from `config/defaults/venues.yaml`.
- **Generated:** 2026-10-09, by `generate_golden.py` in this directory.
- **Re-check against:** each exchange's published trading calendar (holidays and trading
  hours) for the dates above. The file pins what the library says; it does not prove the
  library right.
- **Why it exists:** `exchange_calendars` precomputes holidays and publishes fixes. A library
  upgrade that changes a session fails `test_calendars_golden.py` and is reviewed instead of
  being absorbed silently. Regenerate with
  `uv run python tests/fixtures/calendars/generate_golden.py`, read the diff, commit it.
- **Not covered:** the four venues the library lacks (`CHIA`, `XNSE`, `XSHE`, `XSPX`); they are
  blocked as `calendar_not_covered` until a holiday list is sourced.
