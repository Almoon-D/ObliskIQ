# Collectors — operation and deployment

Four collectors plus a universe sync, running as a **standalone entrypoint**
entirely separate from the MCP server process. They bind no port, hold no chat
session and need no human, so the same image deploys unchanged to a small
always-on cloud VM under a scheduler.

## Why this is the highest-priority component

Option-chain history **cannot be retrieved retrospectively from IBKR**. Corrected in Phase 0: this holds for the three EUROPEAN listings (ASML, SAN, TTE), where IBKR is the only route. US chains are retrievable retrospectively through `vendor-chain`.
`get_price_history` on an option contract returns
`{"error":"No historical market data available"}` even at 49,114 open interest,
and there is no as-of endpoint for chains. Daily self-collection is the only
route to per-strike IV history.

**Futures curves are different, and were wrongly grouped with chains for most of
this build.** `get_price_history` works on an individual futures contract, and
`search_futures` takes `include_expired=true`, so the curve on a past date can be
rebuilt from each then-listed rung's bars. Collecting daily is still cheaper than
reconstructing — one call against a call per rung per day — but a missed curve day
is recoverable. A missed chain day is not.

Everything else in this repository can be rebuilt from the APIs at any time. These
two cannot. **A day not collected is gone permanently** — for the European listings on the `chain` route. The US names collect through `vendor-chain`, which serves history.

Bars and macro are recoverable, which is why they sit later in the sequence — but
the chain collector sizes its strike bands from daily bars, so the bars still have
to run first.

---

## Commands

```
obliskiq-collect universe    seed universe.yaml + the live account into the store
obliskiq-collect nav         one account NAV observation into nav_history
obliskiq-collect backfill    historical and incremental bars
obliskiq-collect vendor-chain  US option chains, today or history (marketdata.app)
obliskiq-collect chain         EU option-chain snapshot           (irreplaceable)
obliskiq-collect curve         daily futures term structure       (recoverable)
obliskiq-collect macro       FRED / ECB / CFTC / EIA / ONS / StatCan
obliskiq-collect daily       the scheduled sequence, in order
obliskiq-collect status      what the last runs did. Reads the store, collects nothing
```

Flags:

| Flag | Effect |
|---|---|
| `--source cp_api\|fixture\|mcp-capture` | IBKR route. `fixture` replays recorded responses; `mcp-capture` replays a capture taken by hand through the connector. Neither touches IBKR. `vendor-chain` ignores this flag — see the warning under *Testing without IBKR* |
| `--fixtures DIR` | fixture directory, required with `--source fixture` and `--source mcp-capture` |
| `--only SYMBOL` | restrict to these symbols, roots or series ids. Repeatable. **Refused on `universe`**, which reconciles the whole file against the account in one pass: a subset would retire rows on evidence it never gathered |
| `--dry-run` | fetch and report, write nothing. **On `vendor-chain` the call is still made and still billed**: it checks the request shape, not the cost |
| `--force` | ignore the refetch caches. On macro that is free; **on `vendor-chain` it re-buys every session already held** |
| `--backfill-chains` | `vendor-chain` only: walk back `option_vendor.backfill_sessions` sessions, newest first, resumable, stopping at `option_vendor.max_credits_per_run` |
| `--as-of YYYY-MM-DD` | `vendor-chain` only: the newest session to collect, default today in New York. **It picks the credit tier**: the current session is requested undated and billed per option symbol, an earlier one carries `date=` and is billed as history. Refused on any other command, and refused for a future session |
| `--skip STEP` | omit a step from the `daily` sequence. Repeatable |
| `--days N` | `status` only: how far back to report. Defaults to `collectors.status_report_days` |
| `--config PATH` | override `$OBLISK_CONFIG_PATH` |
| `--log-level`, `--log-format` | override the environment |

### Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | every run reported `ok` | nothing |
| 1 | something failed | read `collector_runs.detail` for the per-target reasons |
| 2 | a run was `degraded` | everything was collected, but on a weaker input than intended. See below |

A scheduler can alert on 2 without treating it as an outage. The same code is
also written into `collector_sequences.exit_code`, so the thing systemd saw and
the thing a human reads afterwards are one row and can be checked against each
other.

---

## The `daily` sequence, and why the order is fixed

```
nav  ->  backfill (daily bars)  ->  freshness check  ->  chain  ->  curve  ->  vendor-chain  ->  macro
```

`src/obliskiq/collect/cli.py`'s `DAILY_SEQUENCE` is authoritative and states the
order once. It is not a config key: the order is architecture, and a setting that
could reorder it would make the guarantee below switchable.

**The order is a ranking by what a missed day costs, irrecoverable first.**

`nav` first. It is one call, so it cannot meaningfully delay anything behind it,
and `/portfolio/{account}/ledger` serves **today's** net liquidation value and no
other — a day not recorded is an observation nobody can ask for again. What reads
it is below.

`backfill` next because `chain` sizes its strike bands from 30-day realised
volatility, which comes from daily bars in the store. Bars are recoverable at any
time, so a *failed* backfill costs a worse band rather than a lost day — but the
freshness is **checked, not assumed**, because a backfill that failed quietly
would otherwise push every underlying onto a fallback sigma with nothing saying
why.

`chain` next. The three European listings are the only series in this package
that cannot be retrieved retrospectively at any price, so nothing that could
prevent them running is allowed to run first.

`curve` after it: reconstructable rung by rung from per-contract history, but a
snapshot is one call against many.

`vendor-chain` fourth. It is the one step that spends money and the one whose
data can be re-bought later, so it is the step that yields when something ahead
of it is at stake. **It used to run first**, which meant a marketdata.app outage
could cost a European snapshot; `docs/DECISIONS.md` records the reorder.

`macro` last, because it is the only part of the sequence that can be caught up
tomorrow for free.

### `nav`, and why it is recorded twice

`nav_history` had no writer at all. The affordability ceiling every option gate
is measured against is a fraction of NAV, and `obliskiq-compute census` refuses
outright without one — correctly, since the budget defines the universe — so on
a store the collectors filled alone the weekly census was scheduled to report
that same refusal forever. The rolling-month drawdown breaker was in the same
position: it needs `portfolio_circuit_breaker.min_observations` readings inside
`window_days` and had none, so it could only ever report `n/a`, which
`server/guards.py` treats as untested rather than clear.

The figure comes from the consolidated `BASE` row of
`/portfolio/{account}/ledger`, which is already in `account.base_currency`.
Summing the per-currency rows at today's FX would give a number that drifts with
the rate rather than with the book.

Two writers, one implementation (`store/nav.py`):

* **the weekday `nav` step**, for the breaker, which wants points in a window;
* **the weekly `universe` sync**, gated by `collectors.universe.record_nav`, for
  the census — that sync already resolves the account for the portfolio union and
  runs an hour ahead of `compute@census`.

**A NAV whose currency is not `account.base_currency` is refused, not converted.**
Every gate reading it compares against a premium already converted to base, so a
figure in another currency does not fail — it mis-sizes the sleeve by the exchange
rate, silently. `as_of` is the day the reading was taken and is never back-dated:
the breaker reads the table as a series.

The freshness check is `collectors.chain.bars_freshness_days`. When it fails the
sequence logs a high-priority warning, records the verdict on the sequence row,
and **continues**: a worse strike band is a bad day, a missed chain snapshot is a
permanent hole. It is a note rather than a step, because it collects nothing and
counting it would make a sequence look like it did more work than it did.

`--skip STEP` omits a step from the sequence — useful during an incident. A
skipped step records a `skipped` verdict and does not count toward
`steps_completed`.

### Macro needs no IBKR credential, and no key for four of its six sources

`obliskiq-collect macro` builds no market-data route: it reaches FRED, ECB, CFTC,
EIA, the ONS and Statistics Canada, and no IBKR endpoint, so it runs with every
`OBLISK_IBKR_*` variable unset. `NEEDS_MARKET_DATA` in `collect/cli.py` is where
that is declared, and the same is true of `status`, `preflight` and `connector`.

**Every macro source is verified live** — the original four as of 2026-09-04, the
ONS and Statistics Canada as of 2026-09-15. What each actually returned, and the
defects that were sitting in the ones that had never run, are in
[`references/macro-playbook.md`](../references/macro-playbook.md). There were
five before: the OECD is gone, with the one series it served and nothing read.

The two national offices exist for one reason. A source is usable here only if it
states WHEN each observation was published — `known_at` drops an undated row —
and most publishers do not. The ONS carries `updateDate` per month and StatCan
carries `releaseTime` per data point, which is what made the UK and Canadian CPI
collectable when the OECD family FRED redistributed them from stopped. Japan,
Korea, Australia, Brazil, India and Israel have no such route and declare no CPI
at all; the playbook says what was probed for each.

Only FRED and EIA need an API key. **CFTC, ECB, the ONS and StatCan need none**,
and a source that fails repeatedly is abandoned on its own rather than stopping
the others — so an unset `FRED_API_KEY` costs the FRED series and nothing else. A
run that gives up on a source records one finding naming it, not one per series
left in it.

### A vendor block is remembered between runs

marketdata.app permits one IP per account and answers a changed address with HTTP
403 and a five-minute block (`references/marketdata-playbook.md`). That refusal
is account-wide, so the run stops at the first one rather than confirming it
eleven more times, writes a cooldown into `collector_state`, and reports
**degraded**. A later run inside the cooldown makes no call at all and reports
degraded too — it collected nothing, and `ok` over nothing is the report this
package exists to refuse. The window is `option_vendor.ip_block_cooldown_seconds`
and it lapses on its own; nothing has to clear it by hand.

### The vendor route is off two ways, and only one of them is fine

`option_vendor.enabled: false` is **told not to run**: the step records `skipped`,
the sequence stays `ok` and the exit code is 0, because honouring a flag somebody
set is not a fault.

An unset `MARKETDATA_API_TOKEN` while the route is ENABLED is **could not run**,
and it reports differently: one finding, the step `degraded`, the sequence
`degraded` and exit code 2. Both cost the same — nothing is attempted, so there
are no retries and no credits, which is the property that stops twelve targets
failing nightly after a full retry backoff for a condition known before the first
call. What changed is that only one of them now reads as healthy.

It matters because `deploy/collect.env.example` ships the token empty while
`config.yaml` ships the route enabled, so this is the state a **fresh install** is
in: zero US option chains photographed, every night, and — while it was a skip —
a scheduler reporting success. `docs/DEPLOY.md` §7b warned a human about it
in prose; the code says it now too.

### A step abandoned at its wall-clock budget

`collectors.daily.step_deadline_seconds` gives every step of `daily` its own
budget in seconds. A step that outruns one is cancelled, recorded `interrupted`
on the sequence, and **the steps after it still run**; the sequence is `partial`
and the exit code is 1.

This is a different guard from the one below, and it catches what that one cannot.
The `try/except` catches a step that RAISES. A step that simply does not return
runs until the unit's `TimeoutStartSec` expires — and that is a SIGTERM, which is
process death rather than an exception, so nothing in Python sees it, every step
after it is lost, and `Restart=on-failure` re-runs the whole sequence from the
top, re-spending marketdata.app credits each time.

The budgets are a ranking rather than an even share, and it is the same ranking
`DAILY_SEQUENCE` states: `chain` gets the largest, because the three European
listings are the only series here that no amount of money recovers, and a budget
that let `backfill` spend the window `chain` needs would have the priority
exactly backwards. Their sum sits inside `TimeoutStartSec` so the sequence always
reaches its own deadline first; `tests/test_deploy_units.py` fails if that stops
being true.

`interrupted` and not `error` is deliberate, on the distinction `collector_runs`
already draws for a reaped row: an error names what went wrong, an interruption
names that nothing got the chance to reach a verdict. It does not count toward
`steps_completed` — the step wrote an unknown amount and claims nothing about it
— and it does move the sequence's verdict, because an abandoned `chain` beside
five healthy steps reading `ok` at exit 0 would be the quietest possible way to
lose the one series that cannot be re-collected.

### What a failing step does to the ones after it

**Nothing. Every step runs inside its own guard.** A step that raises is recorded
against the sequence with its reason and stepped over; the steps after it run.
That was not true before PR 3 — the sequence caught only `NoMatchingTargets` and
the backfill step sat outside the loop entirely, so a `StoreError` from a
collector with no tracked targets, or an upstream failure the client did not
wrap, abandoned everything after it.

The guard is deliberately broad — it catches `Exception`, not `ObliskError`. The
failure it exists for is the one nobody anticipated: an upstream returning
something unparseable, a library raising its own type, a bug in a collector.
Narrowing it would leave exactly those escaping, which is the behaviour being
fixed.

Two things are worth being precise about:

* **A step still knows nothing about the step before it.** The order protects the
  irreplaceable collection; it does not make a later step depend on an earlier
  one succeeding. The one real dependency — bars feeding the chain's sigma — is
  handled by the ladder, which records which rung sized each band and marks the
  run `degraded`. A chain snapshot on a fallback sigma is a worse photograph, not
  a wrong one.
* **A step that ran on a worse input says so.** `sigma_source` per row, `degraded`
  on the run, the reason on the sequence step, and `daily_bars_fresh` in the
  sequence's notes. A step running on the wreckage of the one before it and
  reporting success is the failure this repository exists to prevent; four
  independent records is what stops it.

---

## Which chain gets collected, and from which venue

Three things decide it, and until Phase 5 the first two could disagree without
anything noticing.

**1. The conid.** `universe.yaml` may pin `conid:` on a row, and where it does,
nothing is searched. That is not a convenience — `/iserver/secdef/search` ranks
by its own relevance and a short ticker names several listings, so `ASML`
resolved to the NASDAQ NY-registered line under a row declaring Amsterdam and
EUR. All three European rows now pin theirs.

Where a row does **not** pin one, the row's declared `exchange:` goes with the
search and breaks the tie: among matches carrying the ticker, one whose listing
venue is the declared one wins. This matters more than it sounds, because the
exact-ticker rule it backs up cannot separate listings at all — every listing of
a company answers the bare ticker, so that rule matches all of them and returns
whichever the API ranked first. The venue is a **tiebreak and not a filter**: one
matching nothing falls through, so it can sharpen a resolution and never refuse
one. `references/ibkr-playbook.md` has the sweep, including the row that resolved
to a euro-denominated crypto index future on CME.

**2. The OPT section.** The months and the exchange list are read from the search
match **whose conid is the one the universe holds**, never from the first exact
symbol match. The exchange list is a property of one listing, so for a European
underlying it can be a true statement about New York and a false one here. A
search returning no row for that conid yields no months at all, and the target
fails — rather than collecting some other listing's chain.

**3. The venue.** `option_exchange:` on a universe row is sent verbatim and is
**not** validated against the list the search returned, for the reason above.
Undeclared, the row's own exchange is used when the chain lists it, then whatever
the chain reported first. Every collected row records the venue in
`chain_snapshots.option_exchange`, beside `source`: a route says who was asked, a
venue says what answered, and only the second distinguishes a European chain from
a US one collected under a European row.

**A target that resolves no option contract at all is a FAILURE.** It used to
report `ok` over zero rows, which is how two European listings looked healthy
while collecting nothing on 2026-09-10. Neither half is written in that case —
not the chain rows and not the underlying snapshot — because the two commit
together precisely so a snapshot with no chain against it cannot exist. The
failure names the venue that was sent, the months tried and the exchanges the
search reported, which is what tells a wrong venue from an empty band.

---

## The strike band, and why it is volatility-scaled

A fixed percentage window is wrong in both directions at once: far too narrow for
a high-IV name and far too wide for a low-IV one. The band is scaled by the
underlying's own volatility and by real time to expiry instead:

```
lower = spot * exp(-k * sigma * sqrt(T / trading_days_per_year))
upper = spot * exp(+k * sigma * sqrt(T / trading_days_per_year))
```

`k` is `data.strike_bounds_sigma` and `T` is time to expiry in **trading** days,
so calendar DTE is converted before it reaches the formula. That captures the
bulk of the terminal distribution while collecting only the two to four expiries
inside the tenor window rather than the whole chain — roughly a 90% reduction in
what has to be requested and stored.

Bounding is not optional: liquid names return fragmented or truncated sets when
the request is unbounded.

---

## The sigma fallback ladder, and the `degraded` status

`chain_collector` needs a volatility to size its band. The rungs, in
`collectors.chain.sigma_ladder`:

| Rung | Source | Fires when |
|---|---|---|
| 1 | realised vol over `data.strike_bounds_rv_tenor_days` daily bars | enough history |
| 2 | realised vol over `collectors.chain.realised_fallback_tenor_days` | some history |
| 3 | vendor implied vol from the snapshot | the quote carries one |
| 4 | `data.sigma_default` | nothing else did |

Rungs 2–4 mark the target **degraded**, and the run status follows — unless the
instrument listed within `collectors.chain.new_listing_grace_days`, where thin
history is expected rather than a symptom.

Every row records `sigma_used` and `sigma_source`, so a band can be audited months
later instead of reconstructed from whatever the store holds by then.

**A degraded run means the daily-bar backfill is not keeping up**, and it will keep
not keeping up until somebody looks. That is the entire reason the status exists
rather than being folded into `ok`.

Rung 4 is a fixed *sigma*, not a fixed *band*: it is still scaled by each expiry's
real time to expiry, so a 120-day expiry gets a wider band than a 95-day one. A
fixed percentage band is never produced anywhere in this package.

---

## Reading a run

Start here:

```bash
obliskiq-collect status
```

It reads the store and collects nothing — no market-data provider is built, no
credit is spent, no socket is opened — and prints the last
`collectors.status_report_days` days of sequences with the step that degraded or
failed and why, any runs a killed process left behind, and the most recent run of
every collector. **This is the failure channel.** It is deliberately not a
webhook or a notification service: a collector that phones home is a new outbound
path, a new secret and a new thing to get wrong on the one host whose job is to
need no attention. `docs/DECISIONS.md` records the choice.

### The two tables

Every run writes one `collector_runs` row, inserted as `running` **before** the
work starts — so a run the machine killed leaves evidence rather than nothing.
Every `daily` invocation additionally writes one `collector_sequences` row, also
on entry, carrying `steps_planned` before any step has run.

That second row is what makes **a day half collected identifiable as such from
the store alone**. Six unrelated `collector_runs` rows could not answer "did last
night happen?"; a sequence that says it planned five steps and reported two can.

```sql
-- Was yesterday collected in full?
SELECT session_date, status, steps_completed || '/' || steps_planned AS steps, exit_code
FROM collector_sequences ORDER BY started_at DESC LIMIT 7;

-- Which step failed, and why:
SELECT session_date, detail FROM collector_sequences
WHERE status NOT IN ('ok') ORDER BY started_at DESC LIMIT 3;

-- The runs behind one sequence:
SELECT collector, step_index, status, rows_written FROM collector_runs
WHERE sequence_id = ? ORDER BY step_index;

-- Which underlyings are running on a fallback sigma:
SELECT snapshot_date, underlying_id, sigma_source, COUNT(*)
FROM chain_snapshots WHERE sigma_source NOT LIKE 'realised_%'
GROUP BY 1, 2, 3 ORDER BY 1 DESC;
```

| Status | Means |
|---|---|
| `ok` | everything collected, on the inputs intended |
| `degraded` | everything collected, on a weaker input. Alert-worthy, not an outage |
| `partial` | some targets failed, the rest were written |
| `skipped` | nothing to do — a filter matched none of this step's targets |
| `error` | the run could not proceed, or every target failed |
| `interrupted` | the process was killed before it reported anything |

### `interrupted`, and who writes it

**A killed process cannot record its own death.** So `interrupted` is never
written by the run it describes; it is written by the *next* run, which reaps any
row still saying `running` after `collectors.run_stale_after_seconds`. Every
entrypoint reaps on startup, `obliskiq-collect status` included.

The test is **age, not liveness**. A PID is reused and means nothing across the
reboot that is the most likely reason a run died in the first place, and a lock
file has the same problem one layer up. The window is set above the longest run
this package can perform — the unit's own `TimeoutStartSec` is 7200 — and below
the cadence it runs at. The cost of that choice, stated plainly: a run that
genuinely took longer than the window would be mislabelled while it was still
working, which is why the window is generous rather than tight.

It needs its own value rather than either neighbour. Recorded as `error` it would
be indistinguishable from a run that reached a verdict and reported failure —
which is a different fact, because an error names what went wrong and an
interruption names that nothing got the chance to. Recorded as `ok` it would
report success over an unknown amount of work.

---

## Idempotency and gaps

Every collector is **append-only across days and idempotent within one**. The
primary keys do the work:

* `chain_snapshots (snapshot_date, underlying_id, expiry, strike, right)`
* `curve_snapshots (snapshot_date, root, expiry)`
* `bars (contract_id, freq, ts)`
* `macro (series_id, source, reference_date)`

Re-running today updates today and leaves every previous day untouched. Running
twice is always safe, and `tests/test_collect_idempotence.py` proves it by
counting rows and vendor calls rather than by reading the code: a second
`vendor-chain` run over a held day makes **zero requests**, which matters because
the credit is spent by the request and not by the rows.

**Two writes are atomic because the gap between them was killable.**

* `vendor_fetch_log` and the chain rows it accounts for commit in **one**
  transaction. The log used to be written `ok` first, so a failing write could not
  cause a re-buy — but the window between the two commits is exactly where a
  reboot or the OOM killer lands, and `_already_held` never re-buys a settled past
  session logged `ok`. A kill there bought a day, wrote nothing and suppressed
  every future attempt at it. `status = 'ok'` now *implies* the rows are present;
  a failure records `error`, which keeps the credit and stays retryable.
* `underlying_snapshots` and `chain_snapshots` commit together per underlying. The
  snapshot used to be written at the top of the underlying and the chain rows at
  the bottom, so anything raising in between left a snapshot with no chain for
  that date — indistinguishable from a chain that legitimately returned nothing,
  and `underlying_snapshots.implied_vol` is the series the self-computed IV
  percentile is built from.

The two writes that are deliberately **not** atomic are the bars and their
watermark, and the macro rows and their fetch log. Both are self-correcting: the
watermark is recomputed from `MIN(ts)`/`MAX(ts)` of the table rather than from the
request, and a macro series with no fetch-log row is simply re-fetched, which is
free.

**Gaps are tolerated by construction.** `bar_watermarks` records what is *held*,
not an assumption that the series is unbroken, and it is recomputed from the table
rather than from the request — so a day the machine or the API missed costs that
day, not a restart. The backfill extends both ends and never re-requests the
middle.

---

## Deployment

### Install

```bash
useradd --system --home /opt/obliskiq obliskiq
install -d -o obliskiq -g obliskiq /opt/obliskiq /var/lib/obliskiq /var/log/obliskiq
install -d -m 0700 -o obliskiq -g obliskiq /etc/obliskiq /etc/obliskiq/secrets

git clone <repo> /opt/obliskiq && cd /opt/obliskiq
python3 -m venv .venv && .venv/bin/pip install -e .

install -m 0600 -o obliskiq -g obliskiq deploy/collect.env.example /etc/obliskiq/collect.env
$EDITOR /etc/obliskiq/collect.env      # fill in the keys
```

Python 3.11+; the floor matches whatever a small VM's distribution ships
(Ubuntu 24.04 is on 3.12, Debian 13 on 3.13).

### IBKR access

The collectors reach IBKR over the **Client Portal Web API with OAuth 1.0a
self-service**, registered through a web form in IBKR account settings. It runs
headless, which is what an unattended VM needs.

The downloadable **Client Portal Gateway and IB Gateway are out of scope**. Both
require an interactive local login and cannot run with nobody present.

Three key files go in `/etc/obliskiq/secrets/`, mode 0600:
`signature.pem`, `encryption.pem`, `dhparam.pem`.

TLS verification is never disabled. `OBLISK_IBKR_CA_BUNDLE` may point at a custom
bundle; an unreadable one is a startup error, not a downgrade.

### Schedule

```bash
install -m 0644 deploy/obliskiq-collect@.service         /etc/systemd/system/
install -m 0644 deploy/obliskiq-collect@daily.timer      /etc/systemd/system/
install -m 0644 deploy/obliskiq-collect@macro.timer      /etc/systemd/system/
install -m 0644 deploy/obliskiq-collect@universe.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now obliskiq-collect@daily.timer obliskiq-collect@macro.timer \
                       obliskiq-collect@universe.timer
```

`Persistent=true` on the timers is load-bearing: a run the VM slept through
happens at boot rather than being skipped. For an irreplaceable series that is the
difference between a gap and no gap. `deploy/crontab.example` is the fallback for a
machine without systemd, and it does **not** have that property — nor can cron
tell exit 2 from exit 0, so a degraded run there is invisible.

`obliskiq-collect@universe.timer` is new in PR 3. The weekly universe sync existed
in `crontab.example` and had no timer, so the documented *fallback* ran more than
the documented default.

The compute units read **`/etc/obliskiq/compute.env`**, not `collect.env`. That
file holds every IBKR credential and API key on the machine, and the computation
layer runs with `PrivateNetwork=true` because it reaches no upstream. Handing it
the keys anyway would make the isolation rest on one guard where it can rest on
two: a process that cannot connect *and* has nothing to connect with.
`tests/test_compute_isolation.py` enforces both halves — it walks the entrypoint's
transitive import graph and asserts it names no HTTP client at all, and it runs
the whole `all` sequence with every socket call rigged to raise.

### First run

Order matters on a cold store:

```bash
# OBLISK_ENV_FILE is not optional here: `sudo -u` does not read the unit's
# EnvironmentFile=, so without it every command below stops at "required
# environment variable OBLISK_DB_PATH is unset or empty". The obliskiq user has
# no login shell either, so the venv is named in full rather than assumed on PATH.
RUN="sudo -u obliskiq OBLISK_ENV_FILE=/etc/obliskiq/collect.env \
     /opt/obliskiq/.venv/bin/obliskiq-collect"

$RUN preflight     # the six OAuth steps, writes nothing — do this one first
$RUN universe      # seed the universe; every other step walks it
$RUN backfill      # full history: slow, run once
$RUN daily         # then the sequence takes over
```

`backfill` on an empty store walks the full daily and weekly windows and the
20-year commodity monthly history. Every later run is incremental.

### Checks

```bash
systemctl list-timers 'obliskiq-collect@*'
journalctl -u 'obliskiq-collect@daily.service' -n 100
sqlite3 /var/lib/obliskiq/obliskiq.db \
  "SELECT collector, status, rows_written FROM collector_runs ORDER BY run_id DESC LIMIT 10;"
```

---

## Testing without IBKR

Every collector runs against recorded fixtures, which is how the whole package was
built — there is no live IBKR access in the sandbox it was written in.

```bash
export OBLISK_DB_PATH=/tmp/oblisk.db
FIX="--source fixture --fixtures tests/fixtures/ibkr"

obliskiq-collect universe $FIX
obliskiq-collect daily $FIX --skip vendor-chain \
    --only SPY --only QQQ --only FCX --only ALB \
    --only CL --only NG --only HG --only CC
obliskiq-collect status
```

`--source fixture` replays recorded **IBKR** responses, and macro is unaffected
either way: it reaches five upstreams of its own and never the IBKR route, so it
runs live in the recipe above. Its determinism is covered separately by
`tests/test_macro_pull.py`, which drives each fetcher through a mock transport.
The `--only` list is what the shipped fixtures cover; without it, every other
symbol correctly fails for want of a recorded response and the run reports
`partial`.

> **`vendor-chain` is not fixture-backed, and `daily` runs it.** The step builds
> a real marketdata.app client whatever `--source` says, so without
> `--skip vendor-chain` and with `MARKETDATA_API_TOKEN` set in the environment,
> the sequence **reaches the network and spends credits**. The recipe above skips
> it explicitly rather than relying on the token being unset — a fixture route is
> scoped in `ROADMAP.md`. With the token unset the step finds no targets and
> records itself `skipped`, which is a second safety net and not the first.

That run exits **2**. SPY has ample fixture history and uses the primary sigma
rung; QQQ has 14 daily bars so it falls to the short tenor; FCX has three and no
vendor IV, so it lands on the configured default. Two fallbacks, so the run is
`degraded` — the ladder and the run status both doing their job. `obliskiq-collect
status` then names which underlyings fell back and reports the sequence as
`3/5 steps` with the two skips recorded as skips rather than as gaps.

Run the same command twice: the second pass writes no new row in `bars`,
`chain_snapshots`, `underlying_snapshots` or `curve_snapshots`. That is the
idempotence property, and it is asserted in
`tests/test_collect_idempotence.py` rather than left to this paragraph.

Regenerate the fixtures with `python tests/fixtures/build_fixtures.py`.

A missing fixture **raises**. `FixtureMarketData` never invents an empty result: a
collector reporting success over nothing is the exact failure this package exists
to prevent, and a test that passes because its fixture was never found is the same
bug wearing a lab coat.

---

## UNVERIFIED

**The Client Portal route ran live for the first time on 2026-09-10**, and most
of what stood here is now settled — see *The Client Portal route, live for the
first time* in `references/ibkr-playbook.md`, which is where corrections belong.
Confirmed on that run: the OAuth 1.0a handshake end to end, the snapshot field
units (`7283`/`7284` arrive percent-suffixed, so `implied_vol_units: auto`
resolves them without a fallback), the `market_data_status` id `6509`, and the
per-call history cap (**257 bars**, probed rather than assumed).

Four defects it exposed are fixed and pinned by tests: the `secdef/search` request
must carry a JSON **body**, both section lists are **semicolon**-separated, the
`/iserver/accounts` precondition must be re-satisfied by re-running `ssodh/init`
when it is lost mid-run, and snapshot numerics must be read from `<id>_raw` where
IBKR abbreviates the display value (`"53.4K"`).

**The European chains were read on CP for the first time on 2026-09-10**, and the
Phase 5 rewiring holds: `secdef/search` serves an `OPT` section for each European
conid on its own venues (`EUREX`/`FTA`, `EUREX`/`MEFFRV`, `EUREX`/`MONEP`), and
`FTA`, `EUREX` and `MONEP` each serve strikes. Also settled that day: bars are
reachable for all three European listings — ASML on its corrected conid, SAN and
TTE with the exchange **omitted**, which `history_exchange: null` is what
declares. `"Chart data unavailable"` is therefore a statement about the exchange
on the request, not about the instrument.

What remains open:

* `/trsrv/futures` — the ladder **works on CP**: the 2026-09-10 `curve` run
  collected 12 of 16 roots, 142 rungs, priced, with contango and backwardation
  resolved per root. The response shape was not separately audited, and the
  connector's own ladder (2026-09-09: `contract_month` / `last_trading_date` per
  rung, **not** in expiry order, 132 rungs for CL) is still the only structural
  reading. Four roots did not collect. `references/ibkr-playbook.md`.
* `/iserver/marketdata/history` — **the cap is measured: 257 bars for 400 days
  requested**, so the chunking loop is load-bearing on CP rather than a
  precaution. IBKR serves a span it derives from the request rather than the
  window asked for (a 2-year request returned three years), the same behaviour
  already recorded for `step_count` on MCP. How `startTime` bounds a window is
  still unaudited. **The connector route is settled and is a different answer**:
  one call, no chunking, columnar arrays, ISO-8601 timestamps at the session
  OPEN, completed sessions only.
* Snapshot field units — **settled on CP, 2026-09-10**: `7283` and `7284` arrive
  as percent-suffixed **strings** (`"13.126%"`), so `implied_vol_units: auto`
  resolves them on the `%` and never reaches its fallback. `7283` is an implied
  volatility on this route, not the `7633` ratio. The connector remains a trap by
  contrast — a `_pct` suffix states no unit there, with
  `historical-vol.annual_pct` a fraction and `dividend-yield.yield_pct` a percent
  in the SAME response — which is why neither route's units are inferred from a
  field name and no magnitude heuristic exists.
* **The dividend yield is NOT SERVED on CP, and the field id is not the problem.**
  Measured 2026-09-10: a sweep of `7280`–`7299` plus seven further candidates on
  SPY never returned `7286`, `7287` or `7288`, while their documented neighbours
  `7292`–`7295` came back correctly — so the id mapping is right and the data is
  withheld. `underlying_snapshot_fields.dividend_yield` therefore stays `null` for
  a measured reason, and the column stays NULL. The connector's reading (SPY
  `0.98`, XLP `2.62`, both PERCENT, two decimals, `[MCP ✓ 2026-09-09]`) cannot be
  cross-checked against a second route, and a single-route figure does not earn
  the right to price with it — so the solve keeps reporting `q_none_declared`.
* Whether a CP OAuth session and an IBKR MCP session can coexist for one account.
  Every row carries `source` so collection can be migrated wholesale if not.
  **Narrowed, not closed:** the two authenticate through separate systems — CP is
  OAuth 1.0a, the connector is OAuth 2.0 bearer — so the credentials cannot
  collide. The only shared resource is the `/iserver` brokerage session, and the
  connector serves market data without this build calling `ssodh/init` at all.
  Settling the rest still needs a consumer key. See `docs/DECISIONS.md`.


## Capturing a chain through the MCP connector

**A bridge, for the window before IBKR approve the OAuth application.** The
collectors run on the Client Portal Web API, which is the route this build can
authenticate on its own. Until it is reachable, no photograph is taken at all, and
option-chain days are the ones that cannot be recovered.

The MCP connector serves chains today — from a chat session, or through IBKR's
Custom Connector endpoint, which the spike showed answers a headless container but
takes an OAuth 2.0 token this build cannot yet mint. Either way a session records
raw responses, `collect/mcp_capture.py` translates them into the layout the
collectors already read, and the ordinary collectors run over them writing
`source='mcp'`.

**Bars are worth capturing alongside the chain, and cost one call each.** Verified
on the connector route: `period=TWO_YEARS`/`step=ONE_DAY` returns the whole
`data.daily_window_years` window in a single response, and five years of weekly
likewise, so the probed per-call cap and the chunking loop never engage. Take
`history_<conid>_1d.json` and `history_<conid>_1w.json` into the capture and run
`obliskiq-collect backfill --source mcp-capture --fixtures <dir>`.

**Request `implied_vol_underlying` on every underlying snapshot.** The IV
percentile is the rank of the current IV, and IBKR serve it even when that IV is
invalid — where it reads `0.0` rather than being omitted. A genuine zeroth
percentile reads `0.0` too, so the decoder cannot tell them apart without the
validity flag and drops the percentile rather than guess. One extra field name in
the request is the whole cost.

Nothing about the collectors changes. The only difference is the tag on the rows,
and that tag is the point: a day collected this way depended on somebody being
present, and `source` is how those days stay identifiable — and droppable — later.

### Recording

From a session with the connector, save each response verbatim into one
directory. File names carry the meaning:

```
option_parameters_<underlying_conid>.json    get_option_parameters
option_data_<anything>.json                  get_option_data, one per expiry
snapshot_<contract_conid>.json               get_price_snapshot
futures_<ROOT>.json                          search_futures
history_<conid>_<STEP>.json                  get_price_history
```

plus a `manifest.json` naming only what a filename cannot:

```json
{
  "session": "2026-08-24",
  "underlyings": [
    {"symbol": "ASML", "conid": 117589399, "exchange": "FTA",
     "trading_class": "ASL"}
  ],
  "futures": {"GC": 17340718}
}
```

`trading_class` pins the class where a date lists several — ASML lists four on
2026-12-18. Left out, the alphabetically first regular class wins, which is
deterministic but may not be the one you meant.

**Bound `get_option_data` generously.** The collector computes its own
volatility-scaled band and will use it; the capture only has to be wider. A
narrow capture silently caps the band — the first ASML pass captured ±3% of spot
where the band wanted ±40%.

**One snapshot per strike is enough for the smile.** Implied vol is per strike
and shared across the right: the 1500 call and put returned byte-identical IV.
Open interest still differs by side.

**Take the underlying's snapshot with the full field set in ONE call.** Requesting
`["last", "bid_ask", "volume", "top_status", "historical_vol",
"implied_volatility_percentile"]` together. Two calls saved as two files leaves
the builder using one of them: the first ASML capture took price and volatility
separately and `underlying_snapshots.volume` came out NULL for a session that
traded 164,455 shares. Neither response alone is a complete underlying reading.

An empty `"bid-ask": {}` is normal and means no quote is available right now — it
decodes to absent, not zero, and can occur while the instrument is trading and
reading `REALTIME`.

### Ingesting

```bash
python scripts/build_mcp_capture.py --raw capture/raw --out capture/2026-08-24
obliskiq-collect universe --source mcp-capture --fixtures capture/2026-08-24
obliskiq-collect backfill --source mcp-capture --fixtures capture/2026-08-24
obliskiq-collect chain    --source mcp-capture --fixtures capture/2026-08-24
```

`--source fixture` still tags rows `cp_api`, which is right: the committed
fixtures *are* recorded CP responses. Only `mcp-capture` tags them `mcp`.

Run backfill before chain. The chain collector sizes its strike band from stored
daily closes, and without them it drops to a fallback rung and marks the run
degraded.

### What it costs

A capture taken at whatever hour somebody was present is **not** the same series
as one taken at 22:30 UTC every night. Mixing them without recording when would
corrupt the per-strike IV history the whole build exists for — a volatility
measured mid-session and one measured at the close are different measurements.

`collected_at` and `market_data_status` (migration 006) are what keep them
distinguishable. `market_data_status` holds whatever the route reported, verbatim;
`NULL` means the route said nothing, which is **not** the same as realtime.

This is a real cost, and the reason the bridge should end when OAuth arrives
rather than becoming a second architecture.
