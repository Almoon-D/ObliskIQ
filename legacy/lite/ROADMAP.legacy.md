# ObliskIQ-lite-MCP — what is open

**This file says what is open and which phase is next.** When a phase is open,
its paste-ready prompt lives in
[`docs/NEXT-SESSIONS.md`](docs/NEXT-SESSIONS.md), one session each; that file
also keeps the record of the prompts that have run and the one item that is not
a session. What is already settled, and why, is in
[`docs/DECISIONS.md`](docs/DECISIONS.md); neither is repeated here, because a
decision restated in two places drifts in one of them.

| Phase | State |
|---|---|
| 0 — real option data in the store | ✅ done |
| 1 — the defects real data will expose | ✅ done |
| 2 — grid scope | ✅ done |
| 3 — cleanup | ✅ done |
| 3b — the machine is static, and the ledger records what a position is | ✅ done |
| 3c — the compute layer says what it measured | ✅ done |
| PR 3 — the collectors run themselves | ✅ done |
| PR 4 — the IBKR route: spike the Custom Connector | ✅ done |
| PR 5 — a grid that can be filled | ✅ done |
| PR 6 — the macro layer collects without IBKR | ✅ done |
| PR 7 — a realised-vol estimator that uses the whole bar | ✅ done |
| PR 8 — the FRED collector's request shape, verified live | ✅ done |
| PR 9 — the OECD SDMX envelope, and empty is not ok | ✅ done |
| PR 10 — the three open corrections | ✅ done |
| PR 11 — the grid says which way it is moving | ✅ done |
| PR 12 — Phase 2 bring-up, run live | ✅ done |
| Phase 3 Part B — dividend yield, and the ATM IV solve | ✅ done |
| Phase 4 — the first live photograph, and the first real session | ✅ done — the route runs live; what it found is below |
| Phase 5 — the European chains, and a census that can run itself | ✅ done — built and pinned; the live run it wanted is Phase 6 below |
| Phase 6 — the corrected chain path, run against live `cp_api` | ✅ done — the chains collect from their own venues; what it found is below |
| Phase 7 — the European collection hour, and whether it needs its own timer | ✅ done — **no second timer**; the premise did not survive the measurement |
| Phase 8 — the market axis, realigned for multi-asset allocation | ✅ done — `south_africa` out, four markets in, and a chain nobody expected |
| Phase 9 — a CPI print from this decade | ✅ done — eight markets publish a real policy rate where one did; what it found is below |
| Run the backfill | probed in Phase 4 Part C, 2 credits — seven of eleven cell-measuring names still untested |
| Session 9.5 — pre-deployment hardening and pruning | ✅ done — three blockers closed, four dead functions gone, a universe guard that found live drift, and the skew leaf |
| Session 10 — the VM, provisioned and proven — on Oracle or Google | 🔧 **OPEN** — needs a machine, a reserved address and OAuth approval; the walkthrough now covers both providers |
| Session 11 — the depth the audit found missing | ✅ done — the sixteenth tool, the implied term structure, a cross-asset matrix, and the two portable halves of Session 10 |

**One phase is open.** It and Sessions 9.5 and 11, which have now run, came from
a read-only pre-deployment audit of this branch at `cbe63b2`, taken on
2026-09-16. The paste-ready prompt is in
[`docs/NEXT-SESSIONS.md`](docs/NEXT-SESSIONS.md); what it is for is below, and
neither file restates the other.

**Session 11 ran before Session 10, and that reverses the stated order.** It was
sequenced last because two of its three items want more history than a sandbox
store holds. They still do, and they report honest `n/a`s on fixtures — but the
code and the decisions behind it needed no history, and Session 10 is blocked on
a machine nobody in a session can conjure. What was deferred is the measurement,
not the build. Session 10's two PORTABLE items landed here: the reserved-IP
requirement, which is now `docs/DEPLOY.md` §4b, and the secrets-manager
decision. Key rotation needs account access and stays open below.

**What the audit found, in one paragraph.** The error boundaries hold: every
broad handler in the collectors binds its exception and records a failure row,
there is no bare `except` anywhere in `src/`, and no path was found that turns a
partial failure into a plausible wrong number rather than a visible gap. What it
found instead was three deployment defects that bite only on a VM, five
genuinely dead functions, a `docs/DECISIONS.md` bullet that Phase 9 falsified in
the same commit that merged it, and one analytical gap larger than any of them:
the collectors buy up to four expiries and sixty strikes per name and the grid
publishes a single `implied_vol_atm` off all of it. `Smile.skew` has been
implemented and uncalled since it was written.

It also reported that all 54 structural keys in `universe.yaml` are consumed.
**That one did not survive being re-measured.** Session 9.5 wrote the guard the
audit asked for and it failed on arrival: ten of the sixteen `futures_roots` rows
are YAML flow mappings whose unquoted `untradeable_reason` carries a comma, so
each value was truncated at the comma and its tail parsed as a key of its own —
five orphans, and five reasons reaching `compute/gates.py` missing half their
text. See *What Session 9.5 measured* below.

**The audit's largest finding is now closed.** It reported that the collectors
buy up to four expiries and sixty strikes per name while the grid published a
single `implied_vol_atm` off all of it. Session 9.5 closed the direction half
with the skew leaf; Session 11 closed the time half with the term structure. See
*What Session 11 measured* below.

**The three deployment blockers are closed by Session 9.5**, each against this
repository's own recorded numbers: `TimeoutStartSec=3600` bounding a six-step
sequence whose chain step alone took 57 minutes, with no internal wall-clock
budget; `deploy/crontab.example` firing at 22:30 UTC, the hour the timer's own
header records as being on the far side of the price collapse; and an enabled but
untokened `vendor-chain` recording itself `skipped` at exit 0, so a fresh install
collected nothing on twelve of the fifteen chain names while the scheduler
reported success. See *What Session 9.5 measured* below.

**The reserved-IP requirement is in the walkthrough now** — `docs/DEPLOY.md` §4b
gives the reason and each provider's page the click path. marketdata.app permits
one egress address per account, and both Oracle and Google attach an ephemeral
public IP by default. What Session 10 still carries is doing it on a real machine.

**Phase 7 has run**, on 2026-09-14, and its own premise did not survive it: at
the timer's hour the European basket reads 16 of 24 priced, not the dead chain a
single 21:08 reading had implied. No second timer is declared. See *What Phase 7
measured* below.

**Phase 8 has run**, on 2026-09-14. `south_africa` is out because nothing on this
route measures it; `france`, `italy`, `canada` and `india` are in, each on a
tracker in its own currency with a pinned conid. `coverage_pct` went **up**, from
90.3% to 90.4%, which is what the scope correction predicted. The surprise was
Canadian: `XIU`'s chain is not merely reachable but PRICED, so canada collects one
— and chasing that turned up a defect in the chain collector itself, which is
carried open below. See *What Phase 8 changed*.

**Phase 9 has run**, on 2026-09-15. `policy_rate_real` is `ok` on **eight of the
fourteen markets that declare a policy rate**, where it was `ok` on `us` alone,
and it is `stale` on **none**. The euro area reads Eurostat's HICP through FRED,
which serves it current while Eurostat's own API does not; the UK and Canada read
their national statistics offices directly; six markets declare no CPI at all,
with the route that was probed and the reason it failed written into
`universe.yaml`. The egress question the prompt asked to settle first is closed,
and not by a second egress. See *What Phase 9 measured* below.

The backfill is still open and is not a session: it wants a stable egress IP.
Session 10 is where that IP gets reserved, so the backfill becomes runnable
when Session 10 lands rather than needing a phase of its own.

**The largest thing Phase 6 found was in none of the five questions it was sent
to answer.** `resolve_conid`'s exact-ticker rule cannot separate the listings of
one company, because every listing answers the bare ticker — so a rule the
codebase relied on to avoid exactly this had nothing to discriminate on. See
*What Phase 6 measured* below.

**The `cp_api` route has now run live.** Phase 4 had the credentials that
every previous session lacked, and the first photograph was taken on 2026-09-10:
**3,832 option-chain rows across 21 underlyings, 23 of 24 targets**, against 0 of
24 an hour earlier. What that hour consisted of — four defects that fixtures could
not have caught — is *What Phase 4 measured* below.

Phase 3 Part B did run, and it closes the two items *Known, and not fixed here*
carried: the dividend yield now has an owning route, a dated column and measured
units, and an ATM implied vol is **solved** where the vendor served none — 61 of
61 contracts converging on real chains. What is left on the backfill is a coverage
question rather than a method one, and it is smaller than this file assumed: see
below. It still wants a **stable egress IP** — marketdata.app permits one per
account.

The rest is in *Known, and not fixed here* in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## What the second provider measured (2026-10-04)

**There is no Oracle VM yet, so the question was whether Google's free one would
do.** Google's Always Free `e2-micro` is 2 shared vCPUs and **1 GB**, x86_64, in
three US regions. The walkthrough was Oracle-only; it is now `docs/DEPLOY.md` (what
the two share), `docs/ORACLE-CLOUD.md` and `docs/GOOGLE-CLOUD.md`. The decision, and
what stays unverified, is in `docs/DECISIONS.md` under *Two providers, one
walkthrough*. **Session 10 stays open: nothing here was run on a VM.**

### Memory is not what limits a 1 GB machine

Taken on an x86_64 sandbox (4 cores, Python 3.11, **not a Google machine**), in a
fresh venv, `OBLISK_DB_PATH` pointing at a scratch store holding the offline
fixtures plus: 75,412 daily and weekly bars on all 76 universe rows (**synthetic**
random walks, sized to the 74,562 the live store held in Phase 6), 65,709 macro rows
from a live `macro` run (55 of 55 series ok), and 11,520 chain rows on 24 underlyings
(**synthetic**, priced from the repo's own Black-Scholes so the solver had work).

| command | wall | CPU | peak RSS |
|---|---|---|---|
| `pip install -e .`, fresh venv, prebuilt wheels | 19 s | 15 s | **127 MB** |
| `obliskiq-collect universe`, fixture | 6.3 s | 0.6 s | 50 MB |
| `obliskiq-collect daily`, fixture, SPY only | 9.1 s | 0.4 s | 47 MB |
| `obliskiq-collect macro`, **live** | 231 s | 2.6 s | 65 MB |
| `obliskiq-compute all`, the scaled store above | 30 s | 30 s | 68 MB |
| `obliskiq-compute solve-iv`, 24 underlyings, no vendor IV | 0.8 s | 1.2 s | 60 MB |
| `obliskiq-mcp http`, idle | — | — | 85 MB |

The largest process anywhere is the install, at 127 MB. **The claim in the old
walkthrough that 1 GB cannot install the project did not survive being measured**:
`numpy` and `scipy` arrive as prebuilt wheels on x86_64, so nothing compiles. It was
plausible for Oracle's ARM machine and was written for Oracle's AMD micro, which is
also x86_64. Swap is still recommended on 1 GB, as headroom rather than as a
requirement of the install.

### What does limit it is a slice of a CPU

`compute all` is **single-threaded and CPU-bound** — 30 s of CPU in 30 s of wall —
where the collectors are the opposite (`macro` used 2.6 s of CPU in 231 s). Google
describes `e2-micro` as sustaining about a quarter of a core across its two vCPUs,
which for one thread is roughly an eighth of a core, with short bursts above it
(UNVERIFIED: a search summary of Google's machine-type page, not the page). Taken
at the pessimistic eighth, `compute all` is about 4 minutes against the compute
unit's own `TimeoutStartSec`, which was 900 s when this was written. A margin of
under four, built on an estimate, so the first run on a real machine should record
its wall time (`docs/NEXT-SESSIONS.md` asks for it).

**Not measured, and not claimable from here:** a real `e2-micro`, including how much
of its 1 GB the operating system takes and how its CPU actually bursts; whether
IBKR and marketdata.app treat a Google address differently from any other; the chain
collection step at scale on any machine; and Oracle's ARM machine.

### What it costs, which is not nothing

Google bills an external IPv4 address in use by a VM at $0.005 an hour — about
**$3.65 a month** — static or ephemeral, since 1 February 2024, and the free-tier
page does not exempt it. A static address is required anyway (marketdata.app's
one-address rule), so the Google machine is free apart from its address.

### Following the old walkthrough, and reviewing the change, found eight defects

None needed a machine to find. Each is corrected.

- **The macro timer has never loaded.** `deploy/obliskiq-collect@macro.timer` said
  `OnCalendar=*-*-* 06,14 :00:00 UTC`, with a stray space, and `systemd-analyze
  calendar` answers *Invalid argument*. systemd would refuse the unit, `list-timers`
  would show four timers where the walkthrough promises five, and the 06:00 and 14:00
  refresh would never run. It dates from Session 2. `_timer_times` in the test
  suite re-joined the spaced form and read it as 06:00 and 14:00, so the schedule
  tests called it verified. The unit is fixed, the parser now rejects the form, and
  a test asks `systemd-analyze` itself about every timer.
- **The off-machine backup could not leave the machine.** It sat in
  `/var/lib/obliskiq`, which Ubuntu creates unreadable to other users for a new
  account, so a login user's `scp` from it is refused (not run on a real image).
  It now goes through `/tmp` and is deleted after copying; the weekly `ls` and `du`
  take `sudo`. The upgrade now checks the GitHub token with `git fetch` *before*
  stopping the timers, since a failed `git pull` after the stop left them off.

- **§8 and §9 still said 22:30**, which is the hour the timer's own header records
  as being on the wrong side of the price collapse. Session 9.5 moved the timer and
  the crontab and not the walkthrough, and nothing compared them.
  `tests/test_deploy_units.py` now reads the table.
- **The `sqlite3` command-line tool was never installed**, though §10 and §10a use it.
- **§6c's swap claim was wrong** — see above.
- **§6f described the offline run wrongly**: it said only `universe` ends `partial`.
  `daily` does too, and its `chain` step fails, for the reason in the next section.
- **`server.env`**, the third environment file, was never mentioned.
- **The `ubuntu` user and the `-i` key file were hardcoded** into commands that only
  Oracle's image makes true.

### The offline fixture chain has aged out of the collection window

`obliskiq-collect daily --source fixture ...` now fails its `chain` step with *"SPY
lists no option month overlapping the window"*. The recorded SPY chain holds one
expiry that is still ahead of the calendar, 2026-12-18, and as that date approaches
it stops falling inside the DTE window the collector buys. Session 11 recorded the
same fixture at 93 days; on 2026-10-04 it is 75. Nothing in the code is wrong and a
re-run will not help: **a fixture dated in absolute time ages into a failure.**
The full suite was green on 2026-10-04 (1,847 tests, with the console scripts on
PATH), so whatever it pins, it does not pin this. What a newcomer meets is the offline
recipe in `docs/DEPLOY.md` §6f, which now documents the failure as expected.
**Carried open:** the fixture is not re-dated here, because doing it means choosing
between shifting recorded expiries, fixing the clock and re-recording, and that is a
decision with its own test.

---

## What Phase 9 measured (2026-09-15)

**`policy_rate_real`, per market, off the rebuilt board.** Fourteen markets
declare a policy rate; `china` and `hong_kong` declare none and are
`inapplicable` on this indicator whatever the CPI does.

| status | markets | was |
|---|---|---|
| `ok` | us, eurozone, germany, spain, france, italy, uk, canada — **8** | 1 (`us`) |
| declared gap, `n/a` | japan, korea, australia, brazil, india, israel — **6** | `stale` on a dead id |
| `stale` | **none** | 12 |
| `inapplicable` | china, hong_kong — 2 | 2 |

Seven markets moved off `stale` onto a live print and five moved onto one
outright; `eurozone` moved off `n/a`, where it had been since its HICP series
carried no release date at all. The remaining six moved from `stale` to a
declared gap, which is the trade the phase was sent to make.

**Nulling those six costs no coverage, and the plan that proposed it was wrong
about that.** `GridReport.populated` is `statuses[STATUS_OK]` and nothing else —
a `stale` cell has never been in the numerator. Both `stale` and `n/a` sit in
`collectible` and neither in `populated`, so moving a cell between them leaves
`coverage_pct` exactly where it was. The cost of declaring a market's CPI null is
that the cell stops naming a vintage, not that the board reports less filled.

**What the collector did.** One `obliskiq-collect macro --force`: **55 of 55
targets ok, 0 failed**, 65,586 rows, across five sources where there were four.
Every one of the eight live CPI series carries a release date on every row, and
the macro store holds **no undated row at all**.

**The cutoff is at the publishers, and the egress question is closed.** The open
caveat asked for a re-run from the VM before believing the data was absent at
source. The stronger test was available here: the same hosts, asked both kinds of
question in the same seconds. `api.stlouisfed.org` served `CPIAUCSL` at 2026-08
beside `CPALTT01JPM659N` at 2022-04; `data-api.ecb.europa.eu` served its policy
rate at 2026-09-15 beside its HICP at 2025-12. No path artefact does that. The VM
run is still owed for the backfill's stable-egress item and is not owed for this.

**The finding the phase turned on.** FRED serves Eurostat's HICP **fresher than
Eurostat's own dissemination API does** — current to 2026-07 against a hard stop
at 2025-12 on every unit and every dataset. The earlier reading that ruled
Eurostat out was right about Eurostat's API and wrong to conclude the statistic
was gone with it.

**The trap inside that finding, which a fixture would not have caught.** Those
series are index levels and Eurostat rebased them to 2025=100 during 2026, so
ALFRED's initial-release column holds two bases. A year-on-year ratio across the
join reads about **-22%** on two fresh, release-dated observations and would have
been stamped `ok`. It is why `value_vintage` exists, why FRED now stamps its
units on every row, and why `cpi_yoy` refuses a ratio whose ends disagree. The
regression is pinned against two recorded vintages of the same series.

**What could not be sourced, and why it is the release date every time.** e-Stat
serves Japan's current CPI and one `UPDATED_DATE` for a whole table; Israel's CBS
serves a current print with no date of any kind; ABS, IBGE, KOSIS and MoSPI are
key-gated or dateless. A value with no publication date is invisible to
`known_at`, so collecting it would add rows and no reading. The application key
for e-Stat is in hand and does not change that answer — what Japan needs is a
release-date policy that can express a day-of-month rule, which is a decision of
its own. `references/macro-playbook.md` has the route-by-route table.

**Carried, not fixed.** The offline fixture pipeline
(`--skip vendor-chain --skip macro --only SPY`) reports two failed steps, and
reports the same two on `main` with this branch stashed. The IBKR fixtures are
documented UNVERIFIED — shaped from docs rather than recorded live — and that is
what those steps are hitting. Untouched by this phase and still open.

---

## What Session 11 measured (2026-09-16)

**The sixteenth tool, the implied term structure, a cross-asset correlation
matrix, and the two halves of Session 10 that needed no machine.** The suite went
from **1,732 to 1,759** tests, all green; `ruff check` and `ruff format --check`
clean; all five standalone guards pass.

### The board, on the offline fixture store

| | before | after |
|---|---|---|
| distinct grid leaves | 44 | **47** |
| indicator families | 15 | **15** |
| `correlation_matrix` cells | 360 | **1,944** |
| asset-class pairs computed | 1 | **3** |

### The term structure reaches as far as the level beside it, and measures nothing

All 192 market × sector cells carry the three new leaves — 180 `inapplicable`
where family 7 already was, and 12 `n/a`, which are exactly the cells
`implied_vol_atm` reaches. **The reason is the fixtures, not the code.** The
recorded chain holds four expiry months and only `2026-12-18` is still ahead of
the session, so QQQ reports *"only one listed expiry (93d)"* with the window
label it asked for, and the other eleven cells have no reachable chain at all.

**A constraint the prompt did not know about decided the design.** The prompt
said a fixed DTE pair is unavailable because listed expiries vary by name and by
day. True, and not the binding constraint: `chain_collector` buys ONLY expiries
inside `tenor.entry_dte_min`–`entry_dte_max`, so a front month was never
collected on ANY name and a 30-versus-90 slope is absent from the store by
construction. Both targets sit inside the collected band and a test pins that
they do — outside it the leaf would be `n/a` on every cell for ever with nothing
saying why.

### Every cross-asset pair is `n/a`, and the row says which series it could not read

| pair | `ok` | `n/a` |
|---|---|---|
| equity × equity | 120 | 240 |
| equity × fx | 0 | **528** |
| equity × rate | 0 | **1,056** |

The fixture pipeline runs `--skip macro`, so the macro store is empty and every
non-equity leg reports *"has no release-dated observations of \<series\> on or
before this session"*. That is the honest answer rather than a gap: the row names
the series, and `n_obs` and `norm_independent` are now columns, so the count
behind a refusal is countable in SQL instead of readable only inside a sentence.

**Two refusals will survive a full store.** Every declared `long_rate_series` is
a monthly OECD/FRED id while equity bars are daily, so equity × long-rate will be
`n/a` on `mixed_frequency` across the whole board until a daily long-rate source
exists — refused rather than resampled. And
`grid.correlation_norm_min_independent` is unchanged, so most cross-asset pairs
will carry a correlation with no norm for a long time.

### Two of Session 10's items needed no machine, and one of them fails quietly

`docs/ORACLE-CLOUD.md` gained §4c (now `docs/DEPLOY.md` §4b, with the click path on
each provider's page), the reserved public IP, which the walkthrough
did not mention at all — the words "reserved" and "ephemeral" appeared nowhere in
it. It is the one requirement in that phase whose failure is silent: on an
ephemeral address `vendor-chain` meets `HTTP 403`, writes a cooldown, and the run
reports **degraded** rather than failed, so the symptom is a nightly job exiting
almost-successfully while collecting none of the twelve US chain names. The
secrets-manager decision is in `docs/DECISIONS.md`: none is adopted, and if one
ever is it generates the three env files at provisioning time and never fetches
at collector runtime.

**Key rotation is not closed.** `FRED_API_KEY`, `EIA_API_KEY` and
`MARKETDATA_API_TOKEN` need account access nobody in a session has. It stays open
under *Housekeeping that is not a phase*.

### What reviewing the diff found, before it merged

Eight defects in this session's own work, and the two that mattered were the
same shape: a check that looked like it was checking something.

**`get_system_health` would have called a dead collector healthy.**
`CollectorStatus` reports the newest run per collector with no window on it, so a
collector whose timer stopped firing three months ago arrives as a tidy `ok` row
beside an empty sequence list — exactly the machine the tool exists to catch. The
verdict is now derived from a named `concerns` list with age in it, bounded by
`server.stale_after_days` and measured against the `as_of` asked for.

**The frequency guard failed open on a NULL.** `macro.frequency` is nullable, so
a leg declaring no cadence unioned into `{daily}` and left one element: the pair
was correlated as though both sides had agreed to be daily. It now refuses an
undeclared cadence and a leg whose own rows disagree. Chasing it turned up a
false value upstream — `macro/rates.py` stamped every cached row `"daily"` while
`macro.risk_free_rate.series_by_currency` resolves CAD to a **monthly** id — so
that cache now writes NULL rather than a guess.

Six smaller ones: two status→count maps merged with `{**a, **b}` instead of
summed, so a mostly-frozen pair could report live; `n_obs` documented as the
correlation's window while holding the aligned intersection; an equity leg with
no bars returning an empty series instead of absent, which blamed the pair for a
one-sided gap; a shared series emitting a leg per claimant so `market_b` was
whichever the iteration reached first; and one dead, inverted line.

Nine tests were added for them, and each was checked to fail against the code it
describes before being kept — the quote-state one passed under the bug on its
first draft, because the counts happened to tie.

### Carried, not fixed

The offline fixture pipeline still reports two failed steps, unchanged from
`main` and from Phase 9's note: the IBKR fixtures are documented UNVERIFIED,
shaped from docs rather than recorded live. Untouched by this session.

---

## What Session 9.5 measured (2026-09-15)

**The three deployment blockers are closed, four of the five dead functions are
gone, `universe.yaml` has the guard `config.yaml` has always had, and the grid
carries the direction of the smile beside its level.** Two of the audit's claims
did not survive being re-measured, and both are recorded rather than quietly
worked around.

**The sequence now bounds itself.** `TimeoutStartSec` is 7200, and that is the
OUTER bound: `collectors.daily.step_deadline_seconds` gives every step its own
wall-clock budget, summing to 7020 so the sequence always reaches its own deadline
before systemd reaches one. `chain` holds the largest slice — it is the only
series no amount of money recovers, and it walked 24 targets in 57 minutes on
2026-09-11 — and a test asserts it stays the largest. A step that outruns its
budget is cancelled, recorded `interrupted`, and **the steps after it still run**;
the sequence is `partial` and the exit code is 1. Before this, an abandoned
`chain` beside five healthy steps returned `ok` at exit 0.

**A fresh install now says it cannot collect.** `deploy/collect.env.example` ships
`MARKETDATA_API_TOKEN` empty while `config.yaml` ships `option_vendor.enabled`
true, so `vendor-chain` collected zero US option chains every night and reported
`skipped` at exit 0. It records one finding and reports the sequence **degraded**
at exit 2, while still costing zero retries and zero credits.
`option_vendor.enabled: false` is untouched and still a skip.

**The cron fallback fires at its timer's hour, and a test compares the times.**
`daily` moved from 22:30 to 20:15 UTC. The audit also asked for the compute line
to shift by the same amount; **it had not drifted** — `30 23` already equals
`obliskiq-compute@all.timer` — so it was left alone and the test now asserts that
equality for all five jobs.

**The universe guard failed on arrival, which is the opposite of what the audit
predicted.** It reported all 54 structural keys consumed. Ten of the sixteen
`futures_roots` rows are YAML flow mappings whose unquoted `untradeable_reason`
carries a comma, so each value was truncated there and its tail parsed as a key:
five orphans, and five reasons reaching `compute/gates.py` half-length. Quoted
now. The scan walks 50 distinct structural key names across 63 block-qualified
positions.

**The skew leaf ships, so `Smile.skew` stops being dead.** `implied_vol_skew` is a
risk reversal at `smile.skew_delta`, inside `FAMILY_IMPLIED_VOL`, read off the
hygiene-filtered smile. The other four dead functions were deleted —
`Calendar.add_trading_days`, `report_coverage`, `days_between` and `get_config`.

**What the fixture store shows, and it is a refusal on every reachable cell.**
The leaf reaches all 192 market x sector cells: 180 `inapplicable` where family 7
already was, and 12 `n/a` — exactly the cells `implied_vol_atm` reaches, so the
new leaf is no narrower than the one beside it. None carries a value, and the
reason is the fixtures rather than the code: QQQ's collected band spans |delta|
**0.40–0.50** and FCX's surviving band **0.28–0.48**, so neither brackets 0.25 on
both sides. Each cell says so with the spans it reached. Asked for a target those
bands DO reach, the same code path returns a number — 0.0 on FCX, which is what a
symmetric synthetic surface should give. A real asymmetric smile is covered by
test rather than by fixture, with the sign asserted.

A cell held **44 leaves** after this phase, up from 43 — Session 11's three term-
structure leaves took it to 47, which is the table above. The prose that had said
"thirty-seven" in five places was already six behind before this phase and is now
phrased as the config-derived count it is.

---

## What Phase 7 measured (2026-09-14)

Six hourly ticks, 15:07–20:06 UTC, one fixed `SEP26` basket of 8 near-ATM
contracts on ASML (`FTA`), SAN (`EUREX`) and TTE (`MONEP`), with SPY and QQQ as
controls open throughout. Every sample committed as it was taken. The full table
is in `references/ibkr-playbook.md` under *What the European close actually does
to a chain*; the decision is in `docs/DECISIONS.md`.

### The phase's own premise did not survive it

**At 20:06 — the hour `deploy/obliskiq-collect@daily.timer` fires — the basket
read SAN 8/8, TTE 6/8, ASML 2/8: 16 of 24 priced.** Phase 7 existed because a
single hand-taken reading at 21:08 on 2026-09-11 (ASML 1/8, TTE 0/8, SAN 8/8)
was taken to mean the 20:15 run reaches dead European chains. It does not.

**No second timer is declared**, on the refusal condition the prompt set in
advance. See `docs/DECISIONS.md`.

### It is a step at the close, then a plateau — not a decay

| UTC | vs close | ASML | SAN | TTE |
|---|---|---|---|---|
| 15:07 | −23m | 8/8 | 8/8 | 8/8 |
| 16:06 | +36m | 2/8 | 8/8 | 6/8 |
| 17:06 | +1h36m | 3/8 | 8/8 | 6/8 |
| 18:06 | +2h36m | 2/8 | 8/8 | **8/8** |
| 19:06 | +3h36m | 2/8 | 8/8 | 6/8 |
| 20:06 | +4h36m | 2/8 | 8/8 | 6/8 |

SPY and QQQ held 8/8 at all six ticks, which is what makes the step attributable
to the close rather than to the sampler. ASML falls within 36 minutes and then
does not move for four hours; SAN never degrades at all; TTE alternates and
**returns to fully priced at 18:06**, which no decay produces. How partial the
post-close quote set is turns out to be a property of the name, not of elapsed
time.

The status letter flips `DPBd`/`RBd` → `ZBd` on all three European names in the
same tick their prices drop, while the controls stay `DPBd` — a second signal of
the same event that does not share the price count's failure mode.

### Two of the earlier readings were noise, and are now excluded by rule

On 2026-09-11 TTE read 0/8 at 14:40 and SAN 0/8 at 14:55, each **with its venue
open** and each bracketed by 8/8. A zero must now persist across consecutive
ticks before it counts. Two of that day's five surviving readings were this
artefact.

### The mechanism worked, which is the other thing this phase settles

Two earlier attempts at this measurement produced nothing because a `nohup`'d
sampler did not survive between turns. An hourly trigger committing each sample
before the container can be reclaimed produced six of six. The credentials
carried into every triggered firing — the open risk when it was armed.

### What it cost, and what would change the answer

One day. Three or four more Mondays of the same six ticks would say whether
ASML's 2/8 is its steady state and whether 2026-09-11's 21:08 was the outlier.
A second timer would buy ASML +6 contracts, TTE +2 and SAN nothing, against a
second unit, a second session lifecycle and a run overlapping the US open.

---

## What the collector fixes changed (2026-09-15)

Both defects Phase 8 carried open, measured before being fixed. **Both write-ups
turned out to be wrong in a detail that decided the fix** — which is the argument
for measuring a stored diagnosis rather than implementing it, and now the sixth
phase running to make it.

### The snapshot settle loop

| | first pass, fresh store |
|---|---|
| `market_snapshot`, 12 cold XIU option conids — before | **0 of 12 priced** |
| the same, 12 cold FCX conids — before | **0 of 12 priced** |
| after, on a different still-cold slice — XIU | **12 of 12 priced** in 6.95s |
| after — FCX | **12 of 12 priced** in 4.65s |
| `obliskiq-collect chain --only XIU` — after | **264 rows, 264 priced** |

The carried-open note blamed `6509`. Timed against the route, `6509` **and all
four Greeks** arrive together about 1.3s in and the prices about 7s in — so
excluding the status field, which is what the note implied, would have left
`delta` ending the poll five seconds before any price existed. Settlement is now
defined by the price fields arriving. The retry budget was too small on its own
as well: a price-fields-only basket, whose predicate was correct by construction,
needed 4.78s against a budget of about four seconds.

The budget is ten retries on that measurement, and batches are **primed** before
any of them is polled — the same field set settled in 7.03s cold and 4.78s
primed. The cost is bounded and was measured rather than argued: the `curve`
step, whose COMEX snapshots are documented as returning nothing, went **31s to
54s** with identical output.

### The backfill

| | before | after |
|---|---|---|
| `obliskiq-collect backfill` | `partial`, **8 targets failed** | **`ok`, 171 of 171** |
| `BOVA11` daily / weekly bars | **none** | **315 / 53** |
| `BARN`, `LISN` daily / weekly | none | 730 / 261 and 729 / 261 |
| `brazil` `ok` leaves | 60 | **89** |
| board `coverage_pct` | 90.4% | **91.8%** |

Four failures, three causes. `BARN` and `LISN` were the `history_exchange`
inversion holding in Zurich. **`BOVA11` was not**: it serves `1y` with `B3` on
the request and refuses `2y` with the exchange omitted too, so the endpoint is
refusing a window longer than the instrument's own history rather than objecting
to a venue. A refused span is now asked again for half of it, and — a layer only
visible once that was in — a refusal during the backward walk stops the walk
rather than failing the target and discarding the bars already collected. `JDEP`
was a dead listing and stops collecting.

## What Phase 8 changed (2026-09-14)

Every figure here was read off a store collected live in the session, twice: once
on the axis as it stood and once on the axis as it now stands, with the same
collectors, the same day and no option chains on either side. The account's own
numbers are not in this file by policy — `tests/test_no_account_history.py` scans
it.

### The board, on the metric the build gates

| board | populated / collectible | `coverage_pct` | `ambition_pct` |
|---|---|---|---|
| before — 13 markets | 1,889 / 2,092 | **90.30%** | 31.19% |
| after — 16 markets | 2,017 / 2,231 | **90.41%** | 27.02% |

**Collection health went up and the ambition gap widened, and both are the
intended reading.** Adding a market that measures only its broad cell adds a
well-collected row to the numerator and a handful of leaves to the collectible
denominator; its eleven declared sector gaps land in the ambition denominator by
design, which is what `ambition_pct` exists to keep visible. Judging this change
on `ok` over every leaf written would have reported a degradation, which is the
arithmetic the scope correction was about.

### Per market, `ok` leaves of 516

| market | before | after |
|---|---|---|
| us | 450 | 450 |
| japan | 397 | 397 |
| eurozone | 395 | 395 |
| australia | 164 | 164 |
| korea | 131 | 131 |
| uk | 68 | 68 |
| israel | 64 | 64 |
| hong kong | 61 | 61 |
| brazil | 60 | 60 |
| **canada** | — | **34** |
| **france** | — | **33** |
| **italy** | — | **33** |
| germany | 32 | 32 |
| **india** | — | **32** |
| spain | 32 | 32 |
| china | 31 | 31 |
| **south africa** | **4** | — |

Every market added arrives at or above germany, which has been on the axis since
it was declared. `canada` leads them because it carries a full curve and a
currency return; `india` matches germany exactly, carrying a currency return in
place of the long rate and slope it cannot source. The row that left was
publishing four leaves — a currency return, a policy rate, a long rate and a
slope — on the macro-blanket exception alone, with no instrument behind any cell.

### What the route and the sources actually served

`obliskiq-collect universe` seeded all four new conids live; the only failure was
`TOPIX`, which is pre-existing and measures no cell. `backfill` wrote daily bars
for each — CAC 730, CSMIB 735, XIU 729, NIFTYBEES 730, against DAX's 730 and
SPX's 729. `macro` reported **62 of 62 targets ok**, with the ten new series
landing and the four South African ones gone: `EURCAD` and `EURINR` at 2,557 rows
each to 2026-09-14, the three new `IRLTLT01*` and two new `IRSTCI01*` to 2026-06,
and the four new `CPALTT01*` to 2025-03 — the CPI cutoff, inherited on arrival
exactly as *What the axis probe measured* said any new market would inherit it.

### The Canadian chain, which the phase was not expecting to find

`XIU` carries an `OPT` section on `CDE` with 14 expiry months, and near-ATM calls
price **4 of 4** at `OCT26`, `DEC26` and `MAR27` — two of those inside the
configured DTE band — with the venue closed. So `collect_chain` is true on `XIU`,
and `obliskiq-collect chain --only XIU` writes **264 rows, 264 priced**. This is
the first non-US option chain on the axis that is both reachable and priced.

Its cost is the discount rate: FRED serves no daily Canadian short rate, so
`macro.risk_free_rate.series_by_currency` gains its first MONTHLY entry. And it
brought a calendar with it — an option venue with no holiday list computes a
mandatory close date on weekends alone, so `calendars.yaml` gained `XTSE`.

### There is still no sovereign spread in this grid, and italy did not create one

`italy` now publishes `IT 10y − ECB DFR` beside germany's `DE 10y − ECB DFR`.
Those are **the two legs of a BTP-Bund spread and not the spread**: every
`slope_label` in `universe.yaml` is a within-market slope, and nothing in
`indicators.py` subtracts one market's leaf from another's. A cross-market
sovereign-spread family is a real thing to want and is **a phase of its own** —
its own leaves, its own reason codes — not a side effect of this one. Stated here
so nobody later assumes it fell out of Phase 8.

## What the axis probe measured (2026-09-13)

Not a phase — the measurement Phase 8's prompt is built on, taken so that the
axis question starts from evidence rather than from what anyone believes about
IBKR's country coverage. Reference data and delayed history only: no market data
requested, no vendor credit spent, exchanges closed. The full tables are in
`references/ibkr-playbook.md` and `references/macro-playbook.md`; what they mean
is here.

### IBKR reachability is not what limits the axis

**Nine of nine candidates resolved on `cp_api`, and nine of nine served daily
bars** — France, Italy, Canada, India, Taiwan and Saudi Arabia alike. `NSE`,
`TWSE` and `TADAWUL` all answer this account for reference data and delayed
history; SPY on the same run returned the same bar count, so this is the route
working rather than a short window. The expected finding was that India and Saudi
Arabia would be out of reach, and it was wrong.

Two of the candidates carry the bare-ticker trap Phase 6 measured, in markets
that do not yet exist on the axis: `CAC` resolves first to a **USD US bank**, and
`CACX` is the same Amundi CAC 40 fund **quoted in GBP in London**. Any new market
pins its conid.

### What limits the axis is that the CPI input is dead almost everywhere

**No market except the US has a CPI print newer than 2025-12, from either source
this build uses.** The OECD `CPALTT01*` family that eleven of thirteen declared
markets read stops between **2022-04** (japan) and **2025-04**; the ECB HICP
series — the obvious euro-area replacement — stop at **2025-12** on the euro-area
aggregate and on all four national series, answering `HTTP 200` with a zero-length
body for any window after that. `CPIAUCSL` runs to **2026-08**.

The long-rate, short-rate and FX families are **unaffected and live**:
`IRLTLT01*` and `IRSTCI01*` serve to 2026-06, the ECB euro reference rates to
2026-09-11, the ECB policy rate to 2026-09-13. It is the inflation prints
specifically that stop.

**What it costs, read off the 2026-09-11 board rather than reasoned about:**
`policy_rate_real` is `ok` on **`us` alone**. It is `stale` on nine markets, each
naming its own vintage and age — japan at **1,550 days** — `n/a` on eurozone for
an absent `HICP_YOY`, and `inapplicable` on china and hong kong, which declare no
policy rate. The honesty layer built for the deployment pass is doing exactly its
job and the indicator is dead anyway. **A market added to the axis today inherits
that on arrival.**

### The metric to judge an axis change on, and the one that misleads

A session read these tables and concluded that adding three broad-only markets
degrades the board, on the arithmetic `ok / total`: 28.5% today, 25.9% after.
**That is the metric `GridReport.coverage_pct` exists to replace**, and its
docstring already said why — counting cells no collector could ever fill once
capped coverage at about 9.6% against a 20% floor, so every build reported
DEGRADED and the signal said nothing on the day collection actually broke.

On the metric the build actually gates:

| | populated / collectible | coverage_pct |
|---|---|---|
| today | 1,909 / 2,092 | **91.3%** |
| without south africa | 1,905 / 2,087 | **91.3%** |
| plus three broad-only markets | ~2,001 / ~2,189 | **91.4%** |
| germany alone, a broad-only market | 32 / 34 | **94.1%** |

**A market that measures only its broad cell is a well-collected market**, and
adding one moves coverage up rather than down. The eleven sector cells it cannot
fill are declared gaps, which land in the ambition denominator by design. Judge
an axis change on `coverage_pct`, and report `ambition_pct` beside it so the gap
stays visible.

This matters beyond the arithmetic. `SKILL.md` names **"viewing the world through
a US lens"** as one of three dominant failure modes, and an argument that reaches
"the option chains are all in the US, so do not widen" is that failure mode
wearing a measurement. Broad momentum, Parkinson and Garman-Klass realised vol,
policy rates and curve slopes are the product for most of the book; the
implied-versus-realised screen is a sub-routine of the workflow, not its purpose.

### `south_africa` is dropped because nothing can measure it

| market | `ok` leaves of 516 | sectors touched |
|---|---|---|
| us | 470 | 12 |
| japan | 397 | 12 |
| eurozone | 395 | 12 |
| australia | 164 | 5 |
| korea | 131 | 4 |
| uk | 68 | 2 |
| israel | 64 | 2 |
| hong kong | 61 | 2 |
| brazil | 60 | 3 |
| germany | 32 | 1 |
| spain | 32 | 1 |
| china | 31 | 1 |
| **south africa** | **4** | **1** |

Its four are a currency return, a policy rate, a long rate and a slope. It is the
only declared market with **no instrument at all** — no JSE Top 40 tracker exists
on this route, the SATRIX funds being offshore feeders and a bond portfolio — and
the one case where the macro-blanket exception in `build_grid` is carrying the
entire row. **It is removed for that, not for lacking an option chain**: france
and italy will never carry a chain either and are being added.

### Two instrument corrections, both from a second probe

`C40` is **AMUNDI CAC 40 *ESG* UCITS** (305372184), a different index from the
CAC 40 — a france row declaring it would measure the ESG-screened subset under
France's name. Use `CAC` / 11183823, pinned.

`CSMIB` (74707801, EUR) is *iShares FTSE MIB **ACC***, an accumulating share
class, and is the better italy candidate rather than a duplicate of `ETFMIB`: an
accumulating fund's price series is total return with no ex-dividend gaps, which
is a cleaner input to realised vol and momentum than a distributing one.

### There is no sovereign spread in this grid, and adding italy does not create one

Every `slope_label` in `universe.yaml` is `10y_minus_2y` or `10y_minus_policy` —
a **within-market** curve slope. The only thing named `spread` anywhere in
`indicators.py` is `iv_hv_spread`. So italy would publish `IT 10y − ECB DFR`
beside germany's `DE 10y − ECB DFR`: **the two legs of a BTP-Bund spread, and not
the spread.** A cross-market sovereign-spread family is a real and reasonable
thing to want, and it is a new indicator family with its own leaf and reason
codes — a phase of its own, not a side effect of Phase 8.

### Eurostat is not the fix for the CPI, measured

Probed 2026-09-13 on `prc_hicp_manr`: EA, DE, FR, IT and ES all return
**1997-01 .. 2025-12**, and the newest values are **identical to the ECB's** (FR
0.7, IT 1.2, ES 3.0, DE 2.0). The ECB redistributes Eurostat faithfully and both
stop at the same month, so a euro-area replacement has to be a national
statistics office rather than Eurostat.

**One caveat that outranks the finding.** Both probes ran from a cloud sandbox
behind an agent proxy. Whether these series are absent *at source* or absent *on
this path* is not decidable from here, and two hostnames agreeing exactly is
suggestive either way. Re-run them from the deployment VM before building a
collector — it is one `curl` each, and it decides whether Phase 9 is a collector
or a network note.

### Taiwan's euro cross is the interesting refusal

`EXR/D.TWD.EUR.SP00.A` **exists, answers 200, and stopped on 2020-10-30**. A
taiwan row declaring it would publish a six-year-old exchange rate as a currency
return rather than refusing one — which is worse than the honest 404
`EXR/D.SAR.EUR.SP00.A` gives for Saudi Arabia. A series that is absent fails
loudly; a series that is stalled does not, and that is the one to refuse.

---

## What Phase 6 measured

Every figure here is from a run in this session, read from a run log or the
store. The account's own numbers are not in this file by policy —
`tests/test_no_account_history.py` scans it — so the ledger's shape is in
`references/ibkr-playbook.md` instead.

### Everything Phase 5 built holds on `cp_api`

The reading that could not be taken before. `secdef/search`, sent as a JSON body,
returns an `OPT` section for **every** European conid, on European venues:

| symbol | universe conid | `OPT` section | exchange list |
|---|---|---|---|
| ASML | `117589399` AEB | yes | `EUREX`, `FTA` |
| SAN | `30314144` BM | **yes** | `EUREX`, `MEFFRV` |
| TTE | `29612193` SBF | yes | `EUREX`, `MONEP` |

Identical to the connector's reading for the same conids. And the US listing is
in the same response carrying `SMART;AMEX;BATS;…`, **ranked first in all three
cases** — so Phase 4's "the search returns US venues for all three" was reading
the wrong row of a multi-listing answer. **Keying the section on the conid was
the whole fix, not half of it**, which is what this phase existed to settle.

### The chains collect, and the run in full

`obliskiq-collect universe`, then `obliskiq-collect daily --skip vendor-chain`,
22:58–23:59 UTC. `vendor-chain` is skipped on the live run for the same reason it
is offline: marketdata.app blocks this sandbox's rotating egress account-wide.

| symbol | venue sent | rows | `sigma_source` |
|---|---|---|---|
| ASML | **`FTA`** | 112 | `realised_30d` |
| SAN | **`EUREX`** | 86 | `realised_30d` |
| TTE | **`MONEP`** | 44 | `realised_30d` |

**All three on the primary realised-vol rung, not the sigma ladder** — the bars
arrived in the same run, so the strike bands are volatility-scaled rather than
guessed. Phase 5 said they would stay on the ladder until this was settled; they
are off it.

| step | result |
|---|---|
| `universe` | 91 ok, 1 failed (`TOPIX`, which resolves to nothing on this route) |
| `nav` | ok, one observation, currency `EUR` |
| `backfill` | **74,562 bars across 76 instruments**, 157 ok, 8 failed |
| `chain` | 3,714 rows, 22 of 24 ok |
| `curve` | 142 rungs, 12 of 16 roots |
| `macro` | **55 of 55, 62,896 rows**, no failed target |
| `vendor-chain` | skipped |

Sequence `partial`, exit 1, 5 of 6 steps. The eight backfill failures are four
symbols at two frequencies (`BARN`, `BOVA11`, `JDEP`, `LISN`), all on the
permanent no-data 500 — **and none of them is a European listing**, against Phase
4's fifteen failures which were.

After the two chain defects below were fixed and their targets re-run, the store
holds **4,502 chain rows across 25 underlyings**.

### The two chain failures were two different defects, and both are fixed

**QQQ had lost its chain to `HTTP 429` in three consecutive live runs**, which is
the pattern by the criterion Phase 5 set in advance. The body — recorded for the
first time — is `{"error":"Exceed number of conids"}`, a **per-request cap rather
than a rate**, so the pacing hypothesis was never the right one. Walked one
request at a time: 100 conids accepted, 150 refused.
`max_conids_per_snapshot` was **200**, and QQQ is the only target whose band
reaches `max_strikes_per_expiry` — 120 conids for one expiry, in one request.
Set to **100**, QQQ collected **466 rows**, the largest single chain in the set.

**XLE's `HTTP 401` was the brokerage session dropping**, not the token expiring:
31 minutes into a run with ~22 hours of token left, with the next target
collecting normally and a re-run of XLE alone collecting 130 rows. The repair —
`ssodh/init` — already existed for the `500 /accounts` form of the same condition
and was one branch away. A 401 re-arms once now; a **403** does not, because
re-opening a session cannot change an entitlement.

### The collection hour, measured — and the one it does not fix

The section below left the timer at 22:30 because two readings bracketed the
window and neither named an hour. Sampled a fixed option basket on 2026-09-11:

| UTC | SPY | QQQ | ASML `FTA` | SAN `EUREX` | TTE `MONEP` | `6509` |
|---|---|---|---|---|---|---|
| 15:00–15:10, both open | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | `DPBd`/`RBd` |
| **21:08, US +1h08** | **7/8** | **7/8** | 1/8 | 8/8 | 0/8 | **`ZBd`** |

**US option prices survive the close by hours, not minutes**, frozen rather than
withdrawn — so the collapse to ~1% sits between 21:08 and 23:04, and 22:30 was on
the wrong side of a transition nobody had located.

**And the run is an hour long, which is what settles the hour.** A full `daily`
collected 21:12–22:09 UTC and recorded **899 of 4,192 rows priced (21.4%)**
against Phase 6's ~0.1% at 23:04 — while a basket at 21:08 was 87% priced. Both
readings are right: the basket is an instant, the chain step is 57 minutes, so a
run's recorded fraction is an average across the transition rather than a reading
at its start. **The hour has to be chosen for where the run ENDS.** A 20:15 start
finishes near 21:15 and keeps the whole walk inside the window.

**It does not fix the European chains, and no US-close hour can.** Their venues
close at 15:30 UTC, so a 20:15 run reaches them 4h45m late and two of the three
are already unpriced. That is two markets with different closes sharing one
timer, not a schedule that needs tuning. A second, earlier timer is the likely
shape of the answer and is **not** declared: no sample exists between 15:30 and
20:00, so the hour European prices decay at is unmeasured, and this project does
not declare a schedule on "earlier is probably better".

**A measurement hazard worth carrying.** `market_snapshot` settles as soon as any
requested field arrives, so asking for the status field beside the prices can
return a settled row with no price. Two early zeros were artefacts of exactly
that; the readings above ask for price fields alone. A controlled test with the
market open found the collector's own field set returned 12 of 12 priced either
way, so it is recorded as a hazard rather than a defect.

### The 22:30 schedule does not deliver prices

The finding that contradicts a premise this file records. Phase 4 concluded that a
chain photographed pre-open carries greeks and no prices and named the 22:30 UTC
timer as the remedy. Measured, the remedy does not work:

| run | clock | rows | priced | delta |
|---|---|---|---|---|
| ASML, US listing | **20:08 UTC**, 8 min after the close | 192 | **123** | 190 |
| the `daily` chain step | **23:04–23:57 UTC** | ~4,300 | **~34** | 3,542 |

`market_data_status` is `ZBd` on 4,478 of 4,502 rows either way, so the account's
entitlement is constant and the **hour** is the variable: frozen closing quotes
are served for a while after the close and are gone by 19:00 ET. `solve-iv`
inverts a mid price, so on the schedule as deployed there is nothing to invert.

**The timer is not moved.** Two readings bracket the window and neither names the
right hour, and moving a production schedule onto an unmeasured hour is the
mistake this phase spent its first hour avoiding elsewhere. The probe that settles
it is named in `references/ibkr-playbook.md`.

### The bars, and the hypothesis that survived being tested

`/iserver/marketdata/history`, each conid asked twice:

| symbol | native exchange | exchange omitted |
|---|---|---|
| ASML `117589399` | **22 bars** (`AEB`) | 22 bars |
| SAN `30314144` | HTTP 500 `"Chart data unavailable"` (`BM`) | **22 bars** |
| TTE `29612193` | HTTP 500 `"Chart data unavailable"` (`SBF`) | **22 bars** |

**ASML's bars were a conid problem and the conid fixed them** — Phase 5 marked
that *plausible, UNVERIFIED* and it is verified. **SAN and TTE are the TSEJ
inversion**, so `history_exchange: null` is declared on both, probed both ways
first. Two consequences bigger than the declaration: `"Chart data unavailable"`
is a statement about the exchange on the REQUEST rather than about the
instrument, and the inversion is a property of the history endpoint rather than a
peculiarity of one Japanese venue.

### The rule that could not fire, and the row it let through

`resolve_conid` promised to prefer an exact ticker match because the search
"routinely returns a foreign listing ahead of the one asked for". Measured: every
row of a dual-listed search carries the **bare ticker** as `symbol` — NASDAQ, TSE
and AEB all answer `"ASML"` — so the rule matches every row and returns whichever
the API ranked first. The listing venue is in `description`, and it was declared
on the universe row all along without ever entering the decision.

Both rules applied to one search per declared row:

| | |
|---|---|
| declared rows | 74 |
| same conid under both rules | 54 |
| **different conid with the declared venue** | **19** |
| resolving to nothing at all | 1 (`TOPIX`) |

Eighteen of the nineteen were already pinned, and the venue tiebreak returns
*exactly* the pinned conid in every one — a check on the pins taken from the
search rather than from the file. **The nineteenth was `EEM`, and it was not
pinned**: it resolved to `690179584`, which `contract_info` reports as **"Micro
Ether Euro", an `IND` on CME denominated in EUR**, under a row declaring an ARCA
USD emerging-markets ETF with `collect_chain` and `collect_bars` both set.

So the failure mode this route offers is not "a foreign listing of the right
company" — that is the benign case this project has reasoned about since Phase 4.
It is an unrelated instrument sharing a ticker. `EEM` is pinned now; the tiebreak
is what covers the row found wrong next time.

### Three more defects the live route found

| defect | how it presented |
|---|---|
| `secdef/search` answers HTTP 200 with a JSON **object** on a miss | iterating it yields its keys, so the decoder called `.get()` on the string `"error"` and the target died of an `AttributeError` naming nothing — after two retries of a condition that could not change. Measured on `TOPIX` |
| the ledger's `BASE` row labels its currency `"BASE"` on **both** keys | the NAV mismatch refusal could never fire on the only route that writes a NAV |
| `--only` was silently ignored by `universe` | it walked all 91 targets while the CLI and `docs/COLLECTORS.md` both advertised the flag |

The NAV currency is identified from the per-currency row whose `exchangerate` is
one, which is by definition the currency the consolidation is into; no single such
row, or more than one, falls back to the `None` that travelled before. `--only` is
**refused** on `universe` rather than implemented, mirroring `--as-of`: that sync
reconciles the whole file in one pass, so a subset would retire rows on evidence
it never gathered.

### The retirement, measured rather than assumed

| | |
|---|---|
| `superseded_rows_retired` | **4** — ASML, EEM, SAN, TTE |
| the superseded ASML row | present, `collect_chain=0`, `collect_bars=0` |
| its chain rows | **192, intact** — nothing is deleted |
| `nav_history` | one row, `source=cp_api`, dated the day it was read |
| the NAV's currency | **`EUR`**, matching `account.base_currency` |

The phase brief predicted **1**. It was 4 because the brief assumed only ASML had
been mis-resolved, and three more rows had the same defect. The store this ran
against was seeded from the pre-Phase-5 file to reproduce the Phase 4 → Phase 5
transition, so this is a **reproduction on a fresh store** rather than the
production store's own transition, and the count is a property of that seed.

### Open after this phase

* **The 22:30 schedule serves no option prices**, so `solve-iv` has nothing to
  invert and the implied-vol half of the grid stays dark. The hour is the
  variable and the replacement is unmeasured.
* **`TOPIX` resolves to nothing** on `secdef/search` with `secType: IND`, so the
  row has never entered the store. It fails naming the remedy now instead of
  dying on an `AttributeError`, which is the fix this phase made; finding its
  conid is not.
* **Four futures roots serve no ladder** (`KC`, `NG`, `TTF`, `ZS`), unchanged.
* **The dividend yield still has no second route**, unchanged.
* **`secdef/search` does not rank stably within a day**, so the conid an unpinned
  row resolves to depends on when it is asked. Every declared row that a live
  search can mis-resolve is pinned now, which closes it for this universe; adding
  an unpinned row re-opens it.

### The resolver is not deterministic, and that corrects this phase, not Phase 5

This section previously read as a correction to Phase 5, which had concluded that
SAN and TTE "already held the correct European conid" and ASML alone was wrong.
Phase 6 measured all three resolving to their US line on CP with nothing pinned
and called Phase 5 wrong. **Reading the Phase 4 store settles it the other way:**

| symbol | conid in the Phase 4 store | correct? |
|---|---|---|
| ASML | `117902840` NASDAQ | **no** — 192 US chain rows under an AEB row |
| SAN | `30314144` BM | yes |
| TTE | `29612193` SBF | yes |
| EEM | `6604766` ARCA | yes |

**Phase 5 was right about that store, and Phase 6 was right about its own
measurement.** Both readings are real and they disagree because the thing being
read changes: `secdef/search` ranked the European and ARCA lines first at 09:48
UTC and the US and CME lines first at 20:05 UTC, the same day, same account.

So the resolver's exposure is **intermittent**, which is worse than a stable bug
and not better: a rule that is right most mornings will pass every test anybody
runs and mis-resolve a row on the day nobody looks. The venue tiebreak is what
makes it deterministic, and every declared row a search can mis-resolve is now
pinned as well.

**This also withdraws a claim Phase 6 made about data it had not read.** That
phase asserted the Phase 4 store held EEM bars collected against "Micro Ether
Euro". It does not — the store holds `6604766` with no row against `690179584`.
The assertion was an inference from the 20:05 reading, stated as fact about a
store this project could not open at the time. It could have been checked before
it was written, and was not.

---

## What Phase 5 changed, and the diagnosis it corrected

**The reason the European chains failed was not the one on file.** `ROADMAP.md`,
`references/ibkr-playbook.md` and the Phase 5 prompt itself all recorded that
"their US options sit under a different conid". Measured against the IBKR
connector on 2026-09-10, that is true of **ASML and only ASML** — and backwards:
it was the UNIVERSE that held the US conid.

| symbol | universe conid held | what that conid actually is | the European listing |
|---|---|---|---|
| ASML | `117902840` | **NASDAQ**, "ASML HOLDING NV-NY REG SHS", `country_code: US` | `117589399`, AEB, NL |
| SAN | `30314144` | BM, "BANCO SANTANDER SA", ES — **already correct** | itself |
| TTE | `29612193` | SBF, "TOTALENERGIES SE", FR — **already correct** | itself |

And the European chains are reachable, which the recorded exchange list never
said because it belonged to a different listing:

| symbol | option venues on the European conid | declared |
|---|---|---|
| ASML | `EUREX`, `FTA` — current `FTA` (Session 7 capture, 2026-08-24) | `FTA` |
| SAN | `EUREX`, `MEFFRV` — current `EUREX` | `EUREX` |
| TTE | `EUREX`, `MONEP` — current `MONEP` | `MONEP` |

### One defect, presenting as three

The months and exchange list were read from a **symbol** search while the strikes
were requested against the **conid** the universe holds, and nothing checked they
were the same instrument. So `ASML` collected New York's chain and looked
successful, while `SAN` and `TTE` were sent `SMART` against Madrid and Paris
conids, got an empty ladder — and **reported `ok` over zero rows**, which is the
part that made it invisible.

Four changes, and each is pinned by a test:

| | before | after |
|---|---|---|
| the OPT section is keyed on | the first exact SYMBOL match | **the conid the universe holds** |
| the venue sent | the row's exchange if listed, else the search's first — `SMART` | **the declared `option_exchange`, verbatim** |
| a target resolving zero contracts | `run.succeeded(symbol, 0)` | **failed**, naming the venue, the months and the search's list |
| the venue a row was collected from | nowhere | **`chain_snapshots.option_exchange`** |

All three European rows now pin `conid:`, including the two that happened to be
right — they were right only because the search ranked the European line first,
which is the same ordering that got ASML wrong. Re-pointing ASML would have left
the NASDAQ row collecting alongside it, so a declared row whose symbol now
resolves elsewhere has its collection flags cleared; **nothing is deleted**, since
`bars` and `chain_snapshots` cascade off `universe` and the 192 US rows Phase 4
collected are real.

### The bars question, answered by half

Phase 4 asked whether a different conid also fixes the `HTTP 500` on
`/iserver/marketdata/history`. **For ASML, plausibly** — asking for `AEB` history
against a NASDAQ conid is what `"Contract details are not available"` looks like,
and it is the only one of the three with that message. **For SAN and TTE, no**:
they already held the right conid, so `"Chart data unavailable"` is about the
data. No `history_exchange` was guessed at.

### `nav_history` has writers, and the census computes

| | before | after |
|---|---|---|
| collectors writing `nav_history` | **none** | `nav` (weekdays) and `universe` (weekly) |
| `obliskiq-compute census` with no `--nav` | *"census could not run: no NAV is available"* | **scans the chain and reports a verdict** |
| NAV observations per 30 days, scheduled | 0 | ~20 |
| drawdown circuit breaker | permanently `n/a` | measurable |

Verified on the offline fixture pipeline: the census reported *0 of 24 contracts
clear every gate simultaneously* with an affordability ceiling computed and no
`--nav` — the honest floor answer on a store with no prices, where before it
refused outright. The figure comes from the ledger's consolidated `BASE` row; one
in another currency is **refused rather than converted**, and `as_of` is the day
the reading was taken.

### Two smaller things

**`HTTP 500` carrying a no-data body is permanent now**, matched on the body the
way the `/accounts` precondition is. A permanently absent target cost nine calls —
three HTTP attempts inside two further per-target attempts — and costs one.

**QQQ's `429` changed no threshold.** One failure in twenty-four is not a pattern,
the HTTP layer already honours an explicit `Retry-After`, and retuning
`collectors.request_spacing_seconds` on n=1 would look like a decision without
being one. `docs/DECISIONS.md` records what would make it a pattern.

### Suite

1,588 tests before, **1,629 after**, all green; `ruff check` and
`ruff format --check` clean; the four standalone guards pass. The 41 new ones
cover the conid-keyed section and its refusal, the declared venue reaching the
wire even when the search's list omits it, a zero-row target failing and writing
neither half, the superseded-conid retirement and the outage that must not trigger
it, the permanent 500 costing one call while an ordinary 500 still retries, the
ledger decode and the currency refusal, and both NAV cadences ending in a census
that computes.

### Open after this phase

* **No `cp_api` run has collected a European chain yet.** Everything here about
  the venues is connector evidence; conids are IBKR-wide, but `FTA`, `EUREX` and
  `MONEP` have never been sent to `/iserver/secdef/strikes` on the collection
  route. One 22:30 UTC run settles it — and a silent failure is no longer one of
  the outcomes.
* **`SAN` and `TTE` still have no daily bars**, so their strike bands stay on the
  sigma ladder. `history_exchange: null` is the untested hypothesis.
* **The dividend yield still has no second route**, unchanged.

---

## What Phase 4 measured

Every number here is from a run in this session, read from a run log or the store.
Nothing is inferred from an absence.

### The route works, and four things were wrong with it

`obliskiq-collect preflight` passed all six OAuth 1.0a steps on the first attempt
— DH prime 2048 bits, 32 bytes of prepend, a **511-character (odd-length) hex
challenge that IBKR accepted**, signature verified, brokerage session
authenticated, tickle holding. The handshake was never the problem.

The chain collector then failed **24 of 24 targets**. Four separate defects, each
of which failed as something else:

| # | defect | how it presented |
|---|---|---|
| 1 | `/iserver/secdef/search` sent as query parameters, not a JSON body | `HTTP 200`, symbol matched, `sections: []` — read as "no options listed" |
| 2 | `option_exchanges()` split on `,` where CP uses `;` | whole exchange list sent as one exchange → `HTTP 410` |
| 3 | `/iserver/accounts` never called, and `/portfolio/accounts` does not substitute | `HTTP 500 {"error":"Please query /accounts first"}` |
| 4 | the same precondition **lost mid-run**, and re-calling `/accounts` does not restore it | nine re-arms cleared nothing; `ssodh/init` does |

Measured on SPY for defect 1: query parameters returned **55 matches and 0
sections**; the same search as a JSON body returned **3 matches and 6 sections**,
including the `OPT` months. Two tests now pin the encoding and the re-arm, because
nothing did — which is why the fixtures were green throughout.

### The first photograph

| | before | after |
|---|---|---|
| chain targets ok | 0 of 24 | **23 of 24** |
| chain rows | 0 | **3,832** across **21 underlyings** |
| universe | — | **81 instruments, 91 of 91 targets**, all `source=cp_api` |
| bars | 0 | **72,490** (52,887 daily / 18,660 weekly / 943 monthly) across 74 instruments |
| macro | — | **55 of 55 targets**, 62,861 rows, no failed target |
| futures curves | — | **12 of 16 roots**, 142 rungs, priced, contango/backwardation resolved |

QQQ is the one chain failure, on an `HTTP 429` — a pacing limit, not a structural
one. **`SAN` and `TTE` returned zero rows** while reporting ok: `secdef/strikes`
serves no strikes against the conid the universe holds for either, because their
listed options sit under the US-listing conid. `ASML` collected 192 rows through
its US listing. **The three European listings are therefore not yet collected as
European chains**, which is a universe-resolution question and is open.

### The grid, built on live data

| | fixture-seeded | after this run |
|---|---|---|
| grid leaves populated | 238 | **1,898** |
| collection coverage (populated ÷ collectible) | 11% | **91%** (1,898 / 2,092) |
| ambition gap (collectible ÷ total) | — | **31%** (2,092 / 6,708) |
| ranked views with rows | **1 of 4** | **4 of 4**, 70 rows |
| `implied_vol_atm` leaves | 1 | **10** |

**`widest_iv_hv` returns rows for the first time**, and the reason is the CP route
rather than the vendor one: field `7283` on the underlying snapshot serves an
implied volatility directly, so ten sector SPDRs populate `implied_vol_atm` and
`iv_minus_hv` without any chain being solved. That is the half of the grid the
marketdata.app route was never going to fill.

### What the market being closed costs, exactly

The run took place 10:00–11:00 UTC, US pre-market. Every option row carried a
**delta** — 671 distinct values, calls positive, puts negative — and **no bid, ask,
last, iv, open interest or volume**: IBKR serves model greeks when it is not
serving quotes. Two consequences, both correct behaviour rather than faults:

* **Nothing could be solved.** `solve-iv` inverts a mid price and there were none,
  so `implied_vol_solved` is NULL throughout and no row carries a solved
  provenance.
* **The census reports a floor failure honestly.** 0 of 3,832 contracts clear
  every gate, and **3,831 failed nothing but left a gate untested** — `tenor` was
  the only gate testable without prices. *An untested gate is not a passed gate*,
  and the census says so rather than passing them.

The 22:30 UTC schedule in `deploy/` is after the US close for exactly this reason.
This run is not evidence against it.

### The dividend yield is not served, and that is the answer

Part B set out to find one CP field id. The measured answer is that there is no id
to find on this account. A sweep of **7280–7299** plus seven further candidates on
SPY, then `7287` alone, then `7286`+`7287`, never returned a dividend field —
while the documented neighbours did: `7293` 52-week high, `7294` 52-week low,
`7295` open, `7292` cost basis. **The id mapping is right and the data is
withheld.**

So `underlying_snapshot_fields.dividend_yield` stays `null` and the column stays
NULL — now for a measured reason rather than an untested one. The connector's
`[MCP ✓ 2026-09-09]` figures remain the only ones this account has, and **a
single-route figure has not earned the right to price with it**, so
`_dividend_yield_for` still resolves `q_none_declared`. The cross-check Phase 3
Part B designed cannot be performed, because the second route does not serve the
number.

`market_data_status` **did** resolve: `6509` is confirmed live (SPY and XLP `DP`,
ASML `DPB`) and is set in config, where it had been `null`.

### A silent, biased data loss in the snapshot decoder

Field `87` (volume) comes back **abbreviated**: `"53.4K"` for SPY, with
`"87_raw": 53400.0` beside it. `safe_float` cannot parse the suffix, so volume was
NULL for every liquid name — while BG, whose volume printed as a bare `"219"`,
stored fine. **A NULL that appears only on liquid instruments is the worst shape a
gap can take, because the sample that survives is biased.** The decoder now
prefers `<id>_raw` wherever IBKR supplies it, which fixes the parse and the
precision together.

### The history endpoint's real per-call cap is 257 bars

The backfill probes rather than trusting `ibkr.cp_api.history.max_bars_per_request`,
and wrote the result to `collector_state`: **257 bars for 400 calendar days
requested**. The chunking loop is load-bearing on this route. Separately, a 2-year
daily request returned bars from 2023-08-08 against `data.daily_window_years: 2`,
so IBKR serves a span it derives from the request rather than the window asked for
— the behaviour already recorded for `step_count` on MCP, now confirmed on CP.

**Bars are unavailable for all three European listings** (`HTTP 500 "Chart data
unavailable"` / `"Contract details are not available"`), 15 of 167 backfill
targets failing. This matters more than the count: the chain collector sizes its
strike band from 30-day realised vol off daily bars, so the three listings whose
chains cannot be re-bought are exactly the three that fall back to the sigma
ladder.

### Part C: two credits, and XLP was the exception

| symbol | result | credits |
|---|---|---|
| XLF | **21 rows** | 1 |
| XLE | **30 rows** | 1 |
| XLV | **HTTP 403 IP block** — no coverage answer | 0 |

**XLP's 404 does not generalise**: both SPDRs that got an answer served history.
The block arrived on the third call and named both addresses — authorized
`...132`, blocked `...131`, **one step apart** — which is the single-IP rule
catching the sandbox's egress pool rotating mid-walk. A blocked call is free
(`credits` NULL against 1 for each success), the block is account-wide rather than
per symbol, and the collector wrote a 15-minute cooldown and reported `degraded`
rather than failing. Still untested: **XLB, XLC, XLI, XLRE, XLU, XLV, XLY**.

### Open after this phase — **all but one closed in Phase 5**

* **`SAN` and `TTE` collect no chain**, and `ASML` collects a US-listed one.
  **Closed, and the diagnosis in this bullet is wrong**: the universe holds the
  European conid for SAN and TTE and the US one for ASML. See *What Phase 5
  changed* above.
* **No collector writes `nav_history`.** **Closed** — `nav` weekdays and
  `universe` weekly.
* **The dividend yield has no second route**, per above. **Still open.**
* **`HTTP 500` is used for "no data"** on this route, so a permanent answer wears
  a retryable status and every such target burns three retries. **Closed** —
  matched on the body, one call.

---

## What Phase 3 Part B measured

Every number here is from a run in this session. Nothing is inferred from an
absence — `references/macro-playbook.md` records what that cost last time.

### Part A did not run, and exactly why

```
MISSING OBLISK_IBKR_CONSUMER_KEY          MISSING OBLISK_IBKR_SIGNATURE_KEY
MISSING OBLISK_IBKR_ACCESS_TOKEN          MISSING OBLISK_IBKR_ENCRYPTION_KEY
MISSING OBLISK_IBKR_ACCESS_TOKEN_SECRET   MISSING OBLISK_IBKR_DH_PARAM
MISSING OBLISK_IBKR_CP_BASE_URL           MISSING OBLISK_IBKR_REALM
                                          MISSING OBLISK_IBKR_ACCOUNT_ID
```

No preflight, no `universe`, no `backfill`, no live `daily`, no first photograph,
and the three European listings are another set of sessions not collected. Three
of the nine are filesystem paths to `.pem` files rather than values, and there is
no route that accepts key contents, so they cannot be supplied any other way.

### Two configured risk-free rates were never collected

The finding that was not on anybody's list. `DTB3` (USD) and
`ECBESTRVOLWGTTRMDMNRT` (EUR) are named by
`macro.risk_free_rate.series_by_currency` and neither was declared under
`macro.series`:

| | before | after |
|---|---|---|
| macro targets | 53 of 53 ok, 59,173 rows | **55 of 55 ok** |
| `DTB3` rows | **0** | **2,493**, latest 2026-09-08 |
| `ECBESTRVOLWGTTRMDMNRT` rows | **0** | **1,196**, latest 2026-09-08 |

A fully green macro run held no USD or EUR discount rate. Nothing errored:
`cached_risk_free_rate` returned `None` and every option gate needing a rate
reported itself untested, which is indistinguishable from a young store.
`tests/test_macro_series.py` now pins that every series `series_by_currency` names
is one `macro.series` collects.

### The rate is point-in-time now, and it was not

Measured solving 2026-09-02, same store, same series:

| reader | reference date | rate |
|---|---|---|
| `risk_free_rate_as_of` (bounded on reference AND release) | **2026-09-01** | **3.78%** |
| `cached_risk_free_rate` (unbounded) | 2026-09-08 | 3.80% |

The bounded read correctly excludes the 2026-09-02 row, whose `release_date` is
2026-09-03 — a figure that did not exist on the session being solved. The
unbounded one reaches six days past it.

### The IV solve, on real chains

Two dated vendor calls on the historical tier, then `obliskiq-compute solve-iv`:

| | |
|---|---|
| chains bought | SPY **608 rows**, QQQ **592 rows**, both `iv` null throughout |
| underlyings solved | **2 of 2** |
| contracts converged | **61 of 61** (0% non-converged, ceiling 20%) |
| SPY ATM implied vol, 2026-09-02 | **0.1561** |
| QQQ ATM implied vol, 2026-09-02 | **0.2074** |
| grid `implied_vol_atm` leaves populated | **1** (`us/technology`) — was **0** |

The grid cell carries its own provenance: *"implied vol SOLVED from mid prices on
the 2026-09-02 chain (solved_bsm_european/q_none_declared). Black-Scholes-Merton
prices a EUROPEAN option and this underlying may be American-style, so early
exercise is unmodelled here."*

`iv_minus_hv` stays `n/a` on that cell for a reason it states plainly — *"fewer
than 31 usable daily closes"* — because no QQQ bars were in that store. That is a
bar-coverage gap, not an implied-vol one.

### Why one cell and not eleven: the vendor tier does not cover the SPDRs

| symbol | measures a grid cell | historical tier |
|---|---|---|
| SPY | no — `us/broad` is measured on **SPX** | 608 rows |
| QQQ | yes — `us/technology` | 592 rows |
| XLP | yes — `us/consumer_staples` | **HTTP 404 `no_data`**, on all three retries |

Eleven of the twenty-four `collect_chain` instruments measure a cell — the nine
sector SPDRs plus QQQ and XLE. **SPY is not one of them**, so no quantity of SPY
history moves the grid: `us/broad` is measured on SPX, and `universe.yaml`
declares no index chain reachable on any route, leaving that cell `inapplicable`
with `reason_code: options_not_available`.

So the backfill's value is smaller again than PR 12 concluded. It buys prices and
open interest over the window, the solve now turns those into implied vol, and the
**grid** still only lights up where the tier serves an instrument that measures a
cell. Whether the other eight SPDRs behave like XLP is untested — one 404 is not a
rule, and each probe costs a credit.

### The dividend yield: everything but the field id

Owner, shape, units and precision are all settled and built. What is missing is
one CP field id, which needs the credentials above.

| | |
|---|---|
| owning route | **`cp_api`** — the connector cannot run unattended, so it cross-checks |
| CP field id | **`null`**, dropped from the request, column stays NULL |
| connector reading `[MCP ✓ 2026-09-09]` | SPY **0.98**, XLP **2.62** |
| units | **PERCENT**, while `historical-vol.annual_pct` in the same response is a fraction |
| precision | **2 decimal places** — the connector's own tool description says 1, and is wrong |
| basis | **trailing**, stored as `trailing_12m`; BSM wants continuous, so it is recorded as an approximation |

### Suite

1,552 tests before, **1,586 after**, all green, `ruff check` and
`ruff format --check` clean. The 34 new ones cover the solver round trip (price a
chain at 24% vol, discard it, recover 0.24000000002546 — ten places), the
point-in-time rate refusing a
future vintage, non-convergence landing NULL and degrading the run, the two
mechanisms that keep solved values out of the vendor percentile series, the
dividend precedence chain, the percent→decimal unit conversion, and the lazy
source ordering that keeps a 156-cell grid from reading all three implied-vol
sources on every cell.

---

## What PR 12 measured, and the assumption it broke

**Phase 2 of the bring-up, run for real.** No Oracle VM exists yet, so it ran in
a cloud sandbox holding the reissued FRED, EIA and marketdata.app keys. Three
things came out of it: a live macro sweep, an answer to the one question gating
the backfill, and three documents that told an operator to do something that
spends money while claiming it touched no network.

### The macro layer, live, with both keys

| | fixture-seeded store | after a live macro run |
|---|---|---|
| macro targets collected | 0 | **53 of 53**, no failed target |
| macro rows | 0 | 59,160 |
| FRED / ECB / CFTC / EIA series | 0 / 0 / 0 / 0 | 35 / 13 / 4 / 1 |
| grid leaves populated | 0 | **238** |
| grid collection coverage | 0 of 2,092 (0%) | 238 of 2,092 (11%) |
| families with a populated leaf | none | policy_rate 82, yield_curve 76, currency_return 44, positioning 36 |

Exit 0, status `ok`. `references/macro-playbook.md` recorded 53 of 53 and 59,165
rows on 2026-09-04; five days later the same sweep on reissued keys returned
59,160, the difference being the rolling
`macro.collection.default_window_years` window and not a regression.

The fixture pipeline was run beside it and behaved exactly as documented:
`universe` `partial` / exit 1, `curve` `skipped` under `--only SPY`, the sequence
`degraded` / exit 2 on the sigma ladder, and a second identical run wrote no new
row in `bars`, `chain_snapshots`, `underlying_snapshots` or `curve_snapshots`.

### The backfill buys prices, and not implied vol

**`docs/NEXT-SESSIONS.md` said to settle whether HISTORICAL chains carry `iv` and
greeks before spending anything. They do not.** One dated call, SPY, the
2026-09-02 session:

| | |
|---|---|
| contracts returned | 608 |
| `iv` non-null | **0 of 608** |
| `delta` non-null | **0 of 608** |
| `bid` / `ask` / `last` / `open_interest` / `volume` non-null | 608 of 608 each |
| credits consumed | **1** |
| implied-vol grid leaves populated by those 608 rows | **0** |
| grid coverage, before and after | 238 of 2,092, unchanged |

Two corrections fall out of it, and they point opposite ways:

- **The backfill is cheaper than budgeted.** The documented 1-per-1000 historical
  price is real — 608 contracts for 1 credit — against the live tier's 3 credits
  for 3 contracts. `option_vendor.max_credits_per_run` bounds the live tier and
  is not what bounds a walk over history.
- **It buys less than this file implied.** `widest_iv_hv` ranks on `iv_minus_hv`,
  so no quantity of historical chain turns it on. The vendor route fills the
  price, open-interest and volume series over the window and stops there.
  Solving IV from mid is the remaining path; it needs a dividend yield, which is
  the item below.

The collector needed no change to handle it: it wrote `iv NULL`, counted, and
reported `degraded` past `option_vendor.max_null_iv_fraction` with the count in
the reason. The behaviour was built for this answer before the answer was known.

### The dividend yields are blocked on a decision now, not on a source

This file and `references/marketdata-playbook.md` both said every
`universe.dividend_yield` is `null` because **no source reachable from a sandbox
serves one**. That is no longer true. The IBKR Custom Connector returns
`dividend-yield` on a plain snapshot — SPY came back `yield_pct: 0.98` on
2026-09-09. What remains is a collection pass and a decision about which route
owns the column, which is a phase rather than a probe. Nothing was filled here,
and the values stay UNVERIFIED.

### Three documents contradicted a safety rule the fourth one owned

`docs/COLLECTORS.md` says `vendor-chain` builds a real marketdata.app client
whatever `--source` says, so a `daily` run without `--skip vendor-chain` and with
`MARKETDATA_API_TOKEN` set reaches the network and spends credits.
`docs/DEPLOY.md` §6f (then `docs/ORACLE-CLOUD.md`), `README.md` and `docs/COMPUTE.md` each gave an
"offline" smoke-test recipe that omitted the skip, and §6f asserted the recipe
"touches no network and needs no IBKR account". `--only SPY` was no protection:
SPY is one of the twelve names that step collects. All three are fixed, and each
now names `docs/COLLECTORS.md` as the owner rather than restating its warning.

### `--as-of`, and the two dates that were one

Answering the question above needed a dated request, and no command could make
one. `VendorChainCollector.run` took a single `today` that served as both the end
of the session walk and the reference deciding whether a request carries `date=`,
so the newest session collected was always the current one, always on the live
per-symbol tier. Buying one past session meant paying for today first.

They are two parameters now — `today` the tier reference, `as_of` the newest
session to collect — and `obliskiq-collect vendor-chain --as-of YYYY-MM-DD`
exposes the second. The first attempt at this passed the pinned date as `today`,
which parses and runs and is wrong: it makes the pinned date look current again,
sends an undated live-tier request, and stamps the CURRENT chain with a past
date. `docs/DECISIONS.md` carries the argument; a test pins the stamping.

---

## What PR 11 changed, and what it measured

**A ranked row stated a LEVEL and nothing about trajectory.** "94th percentile"
does not say whether the reading is still building or already unwinding, and
those are opposite trades behind one number. The trajectory was in the `grid`
table the whole time — same market x sector, same `as_of` — and reading it meant
a `get_grid_cell` call per ranked row, which is the raw-table reading the four
ranked views exist to replace.

Every ranked row now carries a **chosen, bounded set** of those leaves, served
rather than recomputed: `Rankings._cells_for` already read every measured leaf of
the ranked cells in one query to build the conditions map. Each leaf travels with
its own `status`, `window` and `units`, and a leaf the store could not measure is
absent rather than zero. Migration 016 gives them a column.

**Positioning velocity is the one reading that genuinely did not exist.** Family
15 emitted a percentile and nothing else, so a crowded book still being built and
one being dumped were the same number. It now emits a change leaf per window in
`commodities.cot.change_windows_weeks`, measured on the open-interest-normalised
share migration 015 established, release-aligned at both ends, and `n/a` with the
count below the readings a trend needs.

Measured against a store seeded from `tests/fixtures/ibkr`, same store, before
and after:

| | before | after |
|---|---|---|
| leaves on a ranked row | 1 | 1 + up to 11 declared, absent where unmeasured |
| `most_extended` view, 20 rows, encoded | 9.8 KB | 20.9 KB |
| bytes per ranked row | 500 | 1,069 (1,736 with every leaf present) |
| `positioning` leaves per cell | 1 | 3 |
| grid leaf cells | 6,396 | 6,708 |
| indicator families | 15 | 15 |

The row that is the deliverable, `most_extended` rank 1 on that store, is in the
pull request. `most_compressed` still ranks nothing there and this phase does not
change that: sixty daily bars per measuring instrument hold one independent
30-day reading against `grid.realised_vol_percentile_min_independent`, which PR
10 recorded above and which a real backfill fixes.

---

## What PR 6 changed, and what it measured

The macro layer could not be collected without IBKR credentials, for two
independent reasons, and both are now fixed. `docs/DECISIONS.md` has the
argument; the short version is that `obliskiq-collect macro` built a Client
Portal route it never used, and `macro_pull`'s failure guard counted across the
whole run, so an unset `FRED_API_KEY` abandoned CFTC, ECB and OECD too.

Measured on a host holding **no IBKR credential and no FRED or EIA key**:

| | before | after |
|---|---|---|
| macro series declared | 81 | 55 (every one read) |
| macro rows | 0 | 39,419 |
| series collected | 0 | 19 (CFTC, ECB) |
| grid cells populated | 0 | 56 |
| cells reading `fx_series_absent` | 172 | 0 |
| `positioning_percentile` cells | 0 | 12 |

CFTC positioning also stopped measuring the wrong quantity: it is now net
non-commercial as a share of open interest rather than a raw contract count, so
its percentile ranks crowding rather than the growth of the market. Migration
015 deletes the rows written under the old units — they cannot be converted,
because open interest was never collected.

Twenty-six series that nothing read were deleted rather than kept, and
`COT_USD` before them makes twenty-seven on this branch. The scanner that finds
them now has no allowlist, so a series earns its place by having a consumer.
`docs/DECISIONS.md` has the argument.

**This does not replace the backfill.** Everything above is macro; bars, chains
and curves still need the stable-IP host. What it changes is that the macro
layer no longer waits on that host.

### The FRED key was never the blocker — the request was

**This section previously said the 216 cells reading `macro_series_absent` were
waiting on a free FRED key, and called that the single largest unlock short of
the backfill. That was wrong**, and it stood for two sessions. A key was supplied
and every FRED series still failed, with HTTP 400 on our own request shape:
`output_type=4` was sent with no real-time window, so FRED defaulted the vintage
window to today, and most days have no vintage. Bounding it was not sufficient
either — the window is capped at `macro.sources.fred.max_vintages_per_call`
vintage dates, and a daily series over `macro.collection.default_window_years`
is roughly 2,500 of them.

Both are fixed and both are now **verified live** in
[`references/macro-playbook.md`](references/macro-playbook.md). Measured on a
fixture-seeded store, same day, before and after:

| | before | after |
|---|---|---|
| macro targets collected | 19 of 55 | **53 of 55** |
| macro rows | 33,721 | **56,194** |
| grid cells populated | 1 | **215** |
| collection coverage | 0% | **10%** |
| cells reading `macro_series_absent` | 216 | **44** |

Of the 55 series something reads, 18 need no key (CFTC, ECB, OECD) and 37 are
FRED (36) and EIA (1). **Superseded by PR 10 below**, which deleted two of them
and the OECD source with the first: 53 series, 17 needing no key. Both keys
remain free and instant —
[FRED](https://fredaccount.stlouisfed.org/apikeys),
[EIA](https://www.eia.gov/opendata/register.php). See *Housekeeping* below: the
keys once committed to `.env.template` are burned and need reissuing.

**A correction to this section's own first draft.** It said EIA "is still
unexercised live" and carried that as an open item. That was false: the very run
used to measure this phase collected EIA successfully — `WCESTUS1`, 521 rows —
and the claim was written by inferring EIA's status from the shape of the FRED
bug rather than by reading the run log. It is the same mistake as the sentence
above it, made in the PR that corrected the sentence above it, and it pointed
attention away from the source that really was broken. **All five macro sources
were verified live at that point**; `references/macro-playbook.md` says what each
returned, and what the source count is now that PR 10 has deleted one of them.

The lesson is the one `docs/DECISIONS.md` now records: a per-source failure guard
that lets the other sources continue makes a keyless run and a *broken keyed* run
print the same summary line. The guard is right; what was missing was ever
running it with a key.

---

## What PR 7 changed, and what it measured

Parkinson and Garman-Klass now compute beside the close-to-close figure, as their
own named leaves inside indicator family 5, and `most_compressed` orders on the
Parkinson percentile. `iv_minus_hv`, the option screen and the chain collector's
sigma ladder keep the close-to-close figure they have always used.

Measured against a store seeded from `tests/fixtures/ibkr`:

| | before | after |
|---|---|---|
| grid leaf cells | 5,772 | 6,396 |
| realised-vol estimators per cell | 1 | 3 |
| `most_compressed` reachable / ranked over | 57 / 56 | 57 / 56 |
| narrowest family | `implied_vol`, 11/156 | `implied_vol`, 11/156 |

Every cell with bars produced all three estimators, so re-pointing the ranked
view cost it no reach. **The narrowest family did not move and could not have**:
this phase sharpens a lens rather than widening one, and it was done first only
because it is the one item that needs no credential.

The finding that shapes the design is that the range estimators are not lower-
variance readings of the same number — they cannot see the overnight gap, which
on the Session 7 ASML capture is four fifths of the intraday move, so they read
materially below close-to-close and always will on a market that closes at
night. `docs/DECISIONS.md` has the measurement and the four rules that follow
from it, including why an implied-vol spread must never be measured against one.

---

## What PR 10 changed, and what it measured

Three corrections, unrelated to each other, all of them closed. **One of the
three had the wrong premise on file**, and finding that out was the largest thing
in the phase; `docs/DECISIONS.md` carries the argument.

| | before | after |
|---|---|---|
| macro series declared | 55 | 53 (every one read, every one collecting) |
| macro targets collected | 52 of 55 | **53 of 53**, no failed target |
| macro rows | 56,194 | **59,165** |
| macro sources | 5 | 4 |
| `DHHNGSP` rows | 0 | **2,967** |
| realised-vol percentile readings, full store | 474 overlapping | **16 independent** |

**`OECD_CLI` and the OECD fetcher are deleted.** Nothing in `src/` read the
series, no market declared it, and it never returned a row, so deleting it
removes no data. `macro.sources.oecd` went with it. **The SDMX decoder stays**,
and so does every test that exercises the 2.0 envelope, the exact `TIME_PERIOD`
match and the under-specified-key refusal — re-pointed at `EcbFetcher`, which
shares the decoder, with their assertions unchanged. Those shapes belong to SDMX,
not to that publisher.

**The realised-vol percentiles are sampled non-overlapping**, all three
estimators together, floored by `grid.realised_vol_percentile_min_independent`.
It is a window-honesty fix and not a ranking correction, and the measurement is
what says so: across twenty-four two-year windows of real daily price history the
readings behind the number fall from 474 to 16 while the percentile itself moves
0.030 on median, and the two orderings correlate at 0.98.

On the fixture-seeded store, though, it removes the leaf outright. Sixty daily
bars hold one independent 30-day close-to-close reading, so all 57 populated
percentiles become `n/a` with the count, and `most_compressed` ranks nothing.
The LEVEL leaves are unchanged at 56 populated for all three estimators: this
refuses a ranking, not a measurement. Every cell in that view had held the
identical value — a fifty-six-way tie across one shared bar fixture — so what the
change removes is a view that looked full and was not. **A real backfill restores
it**: two years of daily bars is sixteen independent readings, comfortably above
the floor.

**The two FRED series were two different defects, and neither was the one on
file.** This file, `docs/DECISIONS.md` and `references/macro-playbook.md` all said
`IRLTLT01CNM156N` and `DHHNGSP` "exist in FRED but not in ALFRED" and that closing
it meant a per-series release policy. Probed against the API with the key that
was in the environment the whole time:

- `DHHNGSP` **is** in ALFRED, from vintage 2014-12-10. What failed was the FIRST
  chunk of its twenty-year commodity window, which ended four months earlier and
  therefore contained no vintage date at all — a condition ALFRED reports with a
  message about the series. `FredFetcher` now asks `fred/series/vintagedates`
  where the vintages are and intersects the window with the answer: 0 rows →
  2,967, every one carrying a real first-publication date. `DCOILWTICO` returns
  the identical 3,858 rows before and after, which is what a clamp inside an
  answerable range should do.
- `IRLTLT01CNM156N` is not in FRED at all — `The series does not exist`, and no
  replacement exists in FRED for a China long rate. It is deleted, and `china`
  declares no `long_rate_series`. The other nine `IRLTLT01*` are unaffected.

No per-series release policy was declared and no plain-observations fallback was
added. `DHHNGSP` does not need a lag because `from_api` works for it, and its
vintages are weekly against daily observations, so a fixed lag would have been
wrong for four days in five.

**What is still short.** `output_type=4` serves nothing before a series' second
vintage, so Henry Hub's history starts at 2014-12-09 and the twenty years
`commodities.history.monthly_window_years` asks for are short by fourteen. That
is ALFRED's gap, reported rather than filled.

---

## What the connector spike concluded

Half the founding premise is false and the half that binds is untouched, so the
plan of record does not move. Why, and on what evidence, is in
[`docs/DECISIONS.md`](docs/DECISIONS.md); the observations are in
[`references/ibkr-playbook.md`](references/ibkr-playbook.md). In short:

- The Custom Connector **does** answer a headless container, so "the MCP
  connector lives inside a chat session" is no longer true. It is an OAuth 2.0
  protected resource whose authorization server advertises `client_credentials`.
- This build **cannot yet mint a token** for it. Registration was deliberately not
  attempted; it creates state on a live brokerage account.
- So **`cp_api` remains the collection route**, the OAuth 1.0a application still
  goes in, and the connector is the fallback that works today through a
  human-present capture. `obliskiq-collect connector` re-runs the discovery walk
  on demand and needs no credentials.

Apply for OAuth self-service access as planned: nothing the spike found removes
the need for it. [`docs/DEPLOY.md` §3](docs/DEPLOY.md) walks it
through end to end, and §3d records the **untested** OAuth 2.0 alternative.

### What a holdable connector token would unlock

Not started, and blocked on the credential rather than on design:

- **A connector `MarketData` implementation.** The seam is `collect/source.py`'s
  protocol and the tag would stay `mcp`, so no migration is involved. It was left
  unwritten on purpose: the brief was not to port collectors to a route that
  cannot be authenticated unattended, and an HTTP client with no token to test it
  against would ship unverified.
- **Retiring the `record_iv_percentile` bridge.** The connector serves all three
  percentile windows headlessly, so the human-present workaround exists only
  because the token does not.
- **Bars without OAuth.** Verified: the whole daily and weekly windows arrive in
  one call each on this route, so the probed cap and the chunking loop do not
  engage. A capture already feeds `backfill` today via `--source mcp-capture`.

---

## Carried open, not forgotten

These are inputs to PR 5 rather than phases of their own. Each is a declared,
reasoned gap — **with one exception, the first entry, which IS a bug.**

- ~~**The chain collector writes its first pass over a set of option conids
  UNPRICED.**~~ **CLOSED 2026-09-15**, and the diagnosis it was carried open with
  was incomplete in a way that would have defeated the obvious fix: it is not
  only `6509` — all four Greeks arrive with it about 1.3s in and the prices about
  7s in — and the retry budget was too small independently of the predicate. See
  *What the collector fixes changed*.
- ~~**`brazil/broad` seeds but does not backfill.**~~ **CLOSED 2026-09-15, and it
  was NOT the `history_exchange` inversion.** `BOVA11` serves with `B3` on the
  request and refuses the long window with the exchange omitted too: the endpoint
  refuses a window longer than the instrument's own history rather than returning
  a short series. `BARN` and `LISN` WERE the inversion, in Zurich, and declare
  `history_exchange: null` on a measurement; `JDEP` was a dead listing.
- **Coffee has no live equity proxy.** `JDEP` is the only `KC` entry in
  `commodity_proxies` and the `KC` futures root names it in its own
  `untradeable_reason` — and its listing is gone: `AEB` is absent from its valid
  exchanges and the only series any venue serves stops on 2026-04-29. Its row
  stays with `collect_bars: false` rather than being deleted, because deleting it
  would quietly remove a declared capability inside a collector fix. Finding a
  replacement roaster — or concluding there is none and saying so — is a universe
  decision with its own probe.
- ~~**`south_africa/broad` has no measuring instrument.**~~ **CLOSED by Phase 8**,
  by removing the market: nothing on this route measures South African equities
  at any level. `china/broad` is closed the other way — `510300` on SEHKNTL
  reaches the Shanghai listing at its own quote. Its CNY/CNH currency stamp is
  the one thing a sandbox could not verify — see `references/ibkr-playbook.md`.
- **Korea's other eight sectors, and Hong Kong's other ten**, have no instrument
  because none exists on this route — not because a probe stopped early. The
  codes that were searched and returned nothing are recorded in the declaration.
- **Six of thirteen declared native indices did not resolve.** Five markets
  measure on a native-currency local fund; one does not measure at all. The
  evidence is in `docs/DECISIONS.md`.
- **Most of the board still has no instrument.** Each such cell carries a written
  reason and counts as the ambition gap, not as a collection failure — Phase 8
  widened that gap on purpose by adding four markets that measure their broad
  cell and declare the rest. Closing one means finding an instrument, which means
  a probe, not a config change. `canada` and `india` declare theirs as UNSWEPT
  rather than empty, which is the weaker and more honest claim: nobody has probed
  a TSX or NSE sector range.
- **The option families now reach a third non-US market.** `XIU` on `CDE` joins
  the three European listings and the US vendor chains, and every ranked view
  carries the denominator it ranked over. Widening further is a `collect_chain`
  decision — on IBKR venues a data-entitlement question, on the vendor route a
  credit cost — and never a threshold.
- **A ranked view with nothing rankable writes no row at all**, rather than one
  row carrying its denominators and a `detail` saying why it is empty. It is how
  `widest_iv_hv` and `largest_correlation_shift` have always behaved on a young
  store, and `most_compressed` joined them when its percentile started refusing
  an overlapping sample. `grid.min_cells_for_ranked_view` labels a THIN view, and
  only bites once there is a row to label. Not a bug in the corrections that
  exposed it, and a change to three dimensions of the ranked table rather than
  to one; `docs/DECISIONS.md` records why it was left.
- **The correlation norm's longest window reports `n/a`** on the history the
  backfill fetches, and says so. Widening it is a collector change, not a config
  change: a norm window beyond the bars collected is a window with no data in it.
- **The live-session-token has no renewal path.** It does not bite today —
  collectors are `Type=oneshot` against a far longer TTL, and the server holds no
  IBKR session — but it is a trap for whoever writes a daemon.
  `tests/test_deploy_units.py` now pins `Type=oneshot` so the assumption cannot
  be removed without the test saying so.
- **`vendor-chain` has no fixture route**, so an offline `daily` run has to pass
  `--skip vendor-chain` rather than relying on `MARKETDATA_API_TOKEN` being unset.
  Recorded in `docs/COLLECTORS.md` at the recipe that needs it.
- **Curve prices need a futures market-data subscription.** The collector writes
  a complete ladder with no price on any rung and reports it as **degraded**
  rather than as success.

---

## Housekeeping that is not a phase

The FRED, EIA and marketdata.app keys that were committed to `.env.template` in
this repository's history need **reissuing**. History is permanent, so the values
in it are burned; the template now ships empty and
`tests/test_no_committed_secrets.py` keeps it that way.

**`grid`'s CHECK constraint enforces the wrong direction of the ok/value rule.**
The fault-tolerance audit of 2026-09-17 (`docs/DECISIONS.md`) closed the
reachable case at the one write site — `GridBuilder._write_cells` now demotes a
non-finite computed value to `n/a` before it reaches the store — but the schema
itself still only says `CHECK (status = 'ok' OR reason IS NOT NULL)`, which says
nothing about an `ok` row needing a value. The natural companion,
`CHECK (status != 'ok' OR value IS NOT NULL)`, needs a migration that rebuilds
`grid` — it is `WITHOUT ROWID`, so SQLite cannot alter a CHECK in place, the
same constraint 010 hit adding the fourth status. Deferred rather than done
alongside the code guard: that is a schema migration against a table every
build writes, and is real risk to take on for defence in depth behind a guard
that already closes the one path that reaches it.
