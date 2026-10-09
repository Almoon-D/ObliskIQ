# Calendars

What `exchange_calendars` reports, how it differs from the exchanges' published hours, and the
venue facts `config/defaults/venues.yaml` cites. Observations are of `exchange-calendars`
4.13.2 on pandas 3.0.6 and numpy 2.5.3, read through `obliskiq.calendars` with the shipped
configuration on 2026-10-09, unless a tag says otherwise. A pin of the library's answers is
`tests/fixtures/calendars/golden_sessions.json`; it proves what the library says, not that the
library is right. Re-check against each exchange's published calendar when a value matters.

Tag: `[xcal ✓ 2026-10-09]` = read from the library on that date. `VERIFY-LIVE(<what>)` = not
yet seen against the exchange or the broker (queued in `docs/ROADMAP.md`).

## The library's coverage

- Precomputed calendars end on 31 December of their last recorded year; a query past it
  raises `DateOutOfBounds`. `Calendars.coverage()` reports each venue's window and the
  schedule layer treats a date outside it as blocked, never open. `[xcal ✓ 2026-10-09]`
  - XSHG, XBOM and XSES end on 2026-12-31. Every other covered venue extends to the end of
    the configured horizon (`calendars.horizon_years`, three years).
  - The upgrade is the remedy: Dependabot keeps `exchange-calendars` current because these
    holiday lists lapse every year.
- Absent from the library: XSHE (Shenzhen), XNSE (NSE India), CHIA (Shanghai–HK Connect
  segment) and XSPX (the South Pacific Stock Exchange, Fiji). They are configured as synthetic venues with no
  holiday list, so every date is `blocked(calendar_not_covered)` until a holiday list is
  sourced. `[xcal ✓ 2026-10-09]` `VERIFY-LIVE(holiday lists for XSHE, XNSE, CHIA, XSPX)`
- XEUR (Eurex) exists in the library since 4.13.2. Its session is the whole venue's, 08:00–22:00
  Europe/Berlin, wider than the hours single-stock options trade, so an option order always
  intersects the option venue's windows with the underlying venue's
  (`Calendars.common_windows`). `[xcal ✓ 2026-10-09]`

## Sessions, breaks and early closes

- XTKS has a lunch break: 09:00–11:30 and 12:30–15:30 Asia/Tokyo. IBKR calls the venue
  `TSEJ`. `[xcal ✓ 2026-10-09]`
- XHKG half days (2026-12-24 and 2026-12-31) close at 12:00 with no break; the library removes
  the break on special-close dates, and the schedule layer treats `break_start >= close` as
  that case and any other inconsistency as `blocked(schedule_inconsistent)`.
  `[xcal ✓ 2026-10-09]`
- XNYS closes at 13:00 New York on 2026-11-27 (the day after Thanksgiving); XNAS likewise.
  `[xcal ✓ 2026-10-09]`
- XSES: the library does **not** model SGX's midday break. The configuration adds it as a lunch
  override (12:00–13:00 Asia/Singapore). `VERIFY-LIVE(SGX mid-day break hours)` — a library
  release that adds the break turns the override into a config error at build, by design.
- BVMF (B3): the library closes at 18:00 Sao Paulo on both sides of the US daylight-saving
  change, but B3's regular close follows New York's clock (pulse's README: earlier while New
  York is on daylight saving time). The configuration overrides the close to 17:00 with
  `reference_timezone: America/New_York`. `VERIFY-LIVE(B3 seasonal close)`
- Good Friday (2026-04-03) and Easter Monday (2026-04-06) are closed on XETR, XEUR, XPAR, XMAD
  and XLON; XNYS is open on the Monday. The consequence for options is in "Expiry" below.
  `[xcal ✓ 2026-10-09]`

## Time-zone arithmetic

- Daylight saving moves a venue's UTC open without moving its local open: on
  2026-10-25 (last Sunday of October) Europe's clocks go back, so Friday 10-23's open is one
  UTC hour earlier than Monday 10-26's. A duration between two instants must be taken between
  UTC instants. Subtracting two wall-clock times of one `ZoneInfo` is wrong across a change of
  offset; the pulse bot did exactly that (`_minutes_until`), and the port computes durations
  in UTC. The US change is 2026-11-01.
- A local midnight crossing is an instant question, not a date question: ask the venue's
  `session_day(mic, at)`, never `at.date()`.

## Expiry and the last tradable session

- An option's last tradable session is the last session on or before the contract's last
  trading day on which **both** the option venue and the underlying's venue are open (planning
  view). A European option expiring on Good Friday trades last on the Thursday. One expiring
  on Easter Monday (2026-04-06) with a US underlying also trades last on Thursday 2026-04-02:
  the European venue is closed on Friday and Monday, and XNYS is closed on Friday.
- Every calendar day of 2026–27 as an expiry, for XEUR and XPAR with an XNYS underlying, is
  checked against a second code path (an oracle over the library's own sessions) in
  `tests/unit/test_calendars_service.py`.
- Lite counted "N days back from expiry", excluding the expiry; the anchor here is the last
  tradable session itself, so N sessions *including* it is `session_before(..., sessions=N - 1)`.

## Venues and the broker's codes

- `config/defaults/venues.yaml` holds 27 venues and 23 IBKR exchange codes. Every code is a
  claim from the legacy repositories or from lite's observed routes; none is pinned against a
  contract id yet. `SMART` is a routing alias, not a venue; an unlisted code raises
  `UnknownExchangeCodeError`, never a default venue. `VERIFY-LIVE(IBKR exchange codes → venues)`; Session 8 pins
  them with contracts.
- The phase windows (pre-market, opening auction, closing auction, post-market) are the pulse
  bot's, unmeasured. `VERIFY-LIVE(venue phase windows)`. The order window — 30 minutes after
  the open, 15 before the close — is an expert default that Session 18's cost analysis
  calibrates.

## Incidents and cross-checks

- An incident blocks from its start to its expected end plus a grace (`calendars.incidents.
  grace_minutes`), or indefinitely when it has no end, and is a pure function of the record.
  A malformed manual file puts the whole facade into `incident_file_invalid`: every query is
  blocked until a good file is loaded.
- A second schedule source (Session 3 registers IBKR's trading schedule) is compared on
  regular hours only, within `calendars.cross_check.time_tolerance_minutes`; product hours
  differ by instrument, so comparing them would be a permanent false disagreement. A
  disagreement alerts once per (venue, day, kind, source) and blocks only date-critical orders.
