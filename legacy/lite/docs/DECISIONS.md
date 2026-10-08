# Why the design is as it is

The record of decisions taken and, more usefully, decisions **rejected** — with
the evidence that settled each. It is the hardest thing in this repository to
reconstruct: the code says what happens, `config.yaml` says with what numbers,
and neither says why the obvious alternative was wrong.

Nothing here is an instruction. `ROADMAP.md` says what is open. This file says
what is closed and must not be relitigated without new evidence.

Sources folded in: the per-session findings of sessions 0–8 (the prompts that
produced them have all run and are gone), the closed phases of `ROADMAP.md`, and
the rationale rescued from config keys deleted in Phase 3.

---

## The two components

**The MCP server owns state and computation; the skill owns judgment.** Not one
component, and conflating them is the main way this build goes wrong. The design
depends on state that persists between sessions and collectors that run on a
schedule with no chat session open; a skill is a stateless instruction set and
can maintain neither a penalty balance nor a series of chain snapshots.

The payoff is that the workflow runs from Claude Code **or** claude.ai, because
the server is reachable from both. Scripts inside a skill folder only run where
there is a shell.

**Three entrypoints, not one process.** The collectors bind no port, hold no
session and need no human; the computation layer touches no network at all,
which is why its systemd unit can enforce `PrivateNetwork=true` and why the whole
analysis layer is testable in a sandbox with no IBKR access; the server is the
only one that binds.

---

## The option sleeve

**The budget is a shared pool, not a per-position allowance.** Session 6 found
`affordability` computing `available = budget_pct × NAV − penalty_balance` and
never subtracting premium already committed to open positions. With a cap of five
concurrent positions, five could each be sized against the whole sleeve — so a
"hard cap on current NAV" did not cap. `log_entry` now books what an entry
commits and `available = max(0, budget_pct × NAV − penalty_balance −
committed_premium)`.

**Unspent budget stays unspent.** It is never redistributed across the remaining
concurrency slots: a rule that hands the leftovers to the next position is a rule
that fills the sleeve by construction.

**The basis is current NAV, and that is arithmetic rather than a setting.** Every
caller passes the NAV of the day, so the budget falls when NAV falls. A
`budget_basis` key offering one value was deleted in Phase 3;
`tests/test_validation_real_data.py` asserts the behaviour directly, which is
worth more than a declaration nothing read.

**The penalty is a continuous balance, never a binary lock.** A loss adds
`loss_penalty_multiple × loss`; realised gains work it back down 1:1. A
`penalty_model` key offering `continuous | binary_lock` was deleted: the lock was
never built, and a key selecting between one implementation and one fiction is a
decision that looks made and is not.

**Booked at detection, not at close.** Booking the penalty when a zombie is
detected removes the incentive to defer closing in order to postpone the penalty.

**Recovery is on realised gains, never on NAV.** NAV measures beta, not skill.

**The recovery channel had to include the trailing stop.** While
`qualifying_exit_reasons` held only `[target, time_window]`, the equity
discipline's own preferred exit for a winner earned nothing back, and the key
that would have produced `time_window` exits was read by no code — so the sleeve
was close to being permanently locked by a rule nobody had chosen. A trailing
stop only arms once a position is profitable, so the channel does not leak. The
change is forward-only: the penalty already booked, and the realised gain the
narrower rule had excluded, were **not** re-opened, because a balance that
recomputes itself when a threshold moves is not auditable. The figures are in
`docs/archive/`; the rule is what belongs here.

**The 25%-of-budget minimum was retired for an absolute floor.** Measuring a
floor against *available* budget made it shrink as the pool drained, permitting
ever-smaller positions — the opposite of what a floor is for. A sleeve holding
one position at a fiftieth of budget and another at four fifths is allowed by
design. The replacement is an absolute figure in base currency, derived from real
IBKR commission rates: below it, commission costs more than the widest spread the
gates would ever accept. `max_concurrent_positions` now carries the
anti-fragmentation rule alone — concentration beats diversification here because
splitting does not reduce the penalty, it raises the probability of paying it.

**The sleeve is viable at this account size.** Session 6 counted, against live
chains, ten contracts clearing every gate simultaneously against a floor of
three, and three still clearing on the reduced budget. This was asked as a real
question with a real possibility of the answer being "cut it".

**Gates run at entry, not at review.** The theta budget existed as review-time
reporting, which is the wrong end of the trade: by review the decay has already
happened and the decision it should have informed was made months earlier. The
same correction applies to the payoff ratio and the top-decile catalyst rule —
each was in config from the start and enforced nowhere.

**An option entry that withholds its own terms is refused.** An input only the
caller can state — strike, target, contract count, where in the range the entry
sits — is required. An input the *store* may not hold — NAV, a holiday calendar,
a collected rate, a chain to build a smile from — leaves its gate UNTESTED and
reported. An untested gate is never a passed gate, and the two absences are never
conflated.

---

## The equity sleeve

**No holding-period bounds.** A time-window exit was considered and rejected: it
caps upside on a compounding winner and contradicts `positions.rebalance_winners:
false`. The keys that declared one were deleted in Phase 3 rather than left as a
limit nothing enforced.

**Mandatory scaling out was rejected** for the same reason. A trailing stop
protects the gain without capping it, which is why it is used and scaling out is
not.

**Stops are volatility-scaled, never fixed-percentage.** A 20% stop is noise on a
90%-realised-vol name and far too loose on a 15%-vol one.

**Concentration caps evaluate against NAV at the purchase date**, and a winner
that has grown past its cap is not flagged for trimming. The cap governs what is
bought, not what is held.

**Averaging down requires a logged re-underwrite carrying a new dated catalyst.**
A falling price is not a catalyst, and an undated one is a hope with a timestamp.

---

## The ledger

**The thesis is frozen at entry, in SQL.** `thesis_text` and
`invalidation_trigger` are written once and never edited; changes append to
`reunderwrite_log` as dated entries, which makes thesis drift detectable rather
than a matter of recollection. It is enforced by the trigger
`trg_position_thesis_frozen`, not by a flag: a `freeze_thesis_at_entry` key
implying the guarantee was switchable was deleted in Phase 3, because a flag is
weaker than the trigger it appeared to control.

**Ledger writes validate server-side.** `store.ledger.open_position` can enforce
integrity — a thesis exists, an invalidation trigger exists, the price is
positive — but not the mandate, because it can see neither the chain nor the
sleeve. `server/guards.py` composes the same gates the census, the screener and
the risk engine use, so the skill is never the only thing standing between a bad
trade and the ledger. A rule enforced twice is a rule that will eventually be
enforced two different ways.

**The first reconciliation, and the two judgment calls in it.** A closed option
whose realised loss never reached `option_ledger` left available sleeve budget
overstated by most of its own size. Which FX rate books a foreign-currency loss
into a base-currency ledger, and whether a trade closed before this system
existed carries the penalty at all, were both decided by the account holder
rather than assumed. The answers: the exit-date rate the broker itself printed,
because it is dated and reproducible; and yes, in full, because the penalty is a
rule about capital rather than about whether the software was watching.

Reconciling means finding every trade the ledger never saw, not balancing the
total — a 90-day window looked like a clean null result and only a
year-to-date pull found it. The figures, and the script that applied them, are in
`docs/archive/reconciliation/`; that script is history and must not be run again.

---

## Instruments, PRIIPs and wrappers

**Futures are never a tradeable candidate; their curve remains a data call.** The
contract carries undefined loss and violates the no-leverage mandate, but the
curve drives wrapper selection, so `search_futures` stays. The two are easy to
conflate when implementing the ban, and conflating them blinds wrapper selection.

**Leveraged and inverse ETPs are rejected at ingestion, not at recommendation
time.** An instrument that never enters the universe cannot be recommended by a
future code path that forgot to check, and there is always a future code path.
Matching is on the resolved *description*, never the ticker: "BULL" is a
substring of plenty of innocent names and no exchange guarantees a 3x product
spells itself out in four characters.

**PRIIPs is declared per row, not derived from the account.** `universe.yaml`
carries `priips_restricted` per instrument and the gate reads it there. An
`account.jurisdiction` key whose comment claimed it "drives the PRIIPs filter"
drove nothing and was deleted in Phase 3. The rule itself — an EU retail account
cannot purchase US-domiciled ETFs, and options on them must never be held into
expiry — is stated in SKILL.md and enforced at the gate.

**Wrapper preference is structural, not incidental.** Options on single stocks
carry no PRIIPs exposure at all; European-listed UCITS ETFs with Eurex options
come next; US-listed ETF options are a last resort and carry a mandatory close.
For commodities the order is different — physically-backed EU ETCs, then producer
equity, then a futures-tracking wrapper, and that last only near backwardation,
because in contango beyond threshold it bleeds roll over a 90–200 day hold.

**The mandatory close is computed, not trusted.** A close date supplied by the
caller and never checked is how an option on a US-listed ETF gets held into an
OCC auto-exercise. The calendar recomputes it and the gate refuses a disagreement.

---

## Data and collection

**What is irreplaceable, and what only looked it.** The founding premise was that
option-chain history cannot be retrieved retrospectively, so a day not collected
is gone forever. That is true **only of the three European listings** (ASML/AEB,
SAN/BM, TTE/SBF), where Eurex and Euronext historical option data has no free or
cheap source. It is false for the twelve USD-listed names: marketdata.app serves
historical end-of-day chains with IV and full greeks at roughly one credit per
thousand option symbols.

It was also false for futures curves, and that error was ranked alongside option
chains in five documents. `get_price_history` works on an individual futures
contract, and `search_futures` takes `include_expired=true`, so the curve on any
past date is reconstructable after the fact. Daily collection is still the right
default — reconstruction is many calls where a snapshot is one — but a missed day
is an inconvenience, not a hole.

Two further corrections came from reading the code rather than the docs: **nothing
in this repository reads chain history** (every query against `chain_snapshots` is
single-date; the only historical series consumed is
`underlying_snapshots.implied_vol`), and equity bars backfill at any time. The
sequencing argument — collectors first, at any cost — was right about the European
chains and wrong about the scale of what it was protecting.

**Every row records the route that collected it** (`mcp | cp_api |
marketdata_app`), so collection can be migrated wholesale if a CP OAuth session
turns out to invalidate the MCP session for the same account.

**The route question, and what the connector spike settled.** IBKR's Custom
Connector — the MCP tools behind a remote HTTPS endpoint — was spiked in Session
10 against the premise the whole collection layer rests on: that Client Portal
over OAuth 1.0a is the only headless route, because the connector "lives inside a
chat session".

*Half that premise is false.* The endpoint answers a headless container with no
browser and nobody present, so the connector is not confined to a chat session. It
is an **OAuth 2.0** protected resource — bearer token, scopes `mcp.read` /
`mcp.write` — sharing no credential machinery with OAuth 1.0a, and its
authorization server advertises `client_credentials`, a grant needing no browser
at any point, which the token endpoint recognises rather than merely listing.

*The half that binds is untouched.* Nothing here can yet **mint** such a token.
The working token belongs to the MCP client, not to this build; obtaining our own
needs either a one-time browser authorization plus a refresh token, or a
registered `client_credentials` client. Registration was **not** attempted — it
creates state on a live brokerage account, which is the account holder's decision
and not a spike's. So:

- **`cp_api` remains the collection route of record**, and the OAuth 1.0a
  application still goes in. Nothing was ported to a route that cannot yet be
  authenticated unattended.
- **The connector is the fallback that works today**, through a human-present
  capture. It is strictly better than waiting: it serves the full daily and weekly
  bar windows in one call each, and it serves the IV percentiles the CP field id
  for is still unknown.
- **No fourth `source` value, and no migration.** The Custom Connector *is* the
  MCP connector; the transport changed, the data origin did not. Rows stay `mcp`,
  which `bars`, `universe` and `curve_snapshots` already admit — so nothing had to
  rebuild three tables to widen a CHECK constraint, which would have been real
  risk bought for a relabelling.
- **`obliskiq-collect connector` is the evidence, kept executable.** It walks the
  discovery documents and reports what the server accepts. It states in its own
  output what it has *not* established, because a green report that read as "this
  build can authenticate headlessly" would move the collection route on the
  strength of a metadata document.

**The coexistence question is narrowed, not closed.** Whether a CP OAuth session
and an MCP session can coexist for one account has been open since Session 0 and
is still not answered — but it is smaller than it was. The two routes authenticate
through entirely separate systems, so the *credentials* cannot collide; the only
shared resource is the `/iserver` brokerage session, and the connector served
market data without this build ever calling `/iserver/auth/ssodh/init`. What
remains untestable without a CP consumer key is whether IBKR's own server-side
session for the connector collides with one `ssodh/init` would open.
`obliskiq-collect preflight` is still the cheapest experiment, and it needs
credentials that have not arrived. It also stops being load-bearing in the world
where the connector collects.

**Macro series align on release date, never reference date.** Aligning CPI to its
reference month builds look-ahead bias into every percentile derived from it.
Session 8 found `known_at` with exactly one call site in all of `src/` and
everything else aligned on the reference period — which `value_on_or_before`'s own
docstring forbids in as many words. No test could see it, because every compute
fixture set `release_date = reference_date`.

That one call site was itself inert, and it silently disabled the census: all
eleven FX series are ECB, ECB's policy was `unknown`, so every dated conversion
returned None, leaving every non-EUR price ceiling UNTESTED and `passed_all_gates`
at zero for a plumbing reason. `release_date_policy` is now resolvable per series.

**A macro source qualifies on its release date, not on its data.** Phase 9 went
looking for a live CPI for thirteen markets and found that live is the easy half.
e-Stat serves Japan's current print and one `UPDATED_DATE` for a whole table;
Israel's CBS serves a current print with no date of any kind; Eurostat's own API
serves a release-dated nothing. `known_at` drops an observation with a NULL
release date, so each of those would add rows and no reading — a series
collected, reported ok, and invisible. Three routes passed: FRED/ALFRED, the
ONS's per-month `updateDate`, and Statistics Canada's per-point `releaseTime`.
**That is why FRED-first is a rule rather than a convenience.** It is not that
FRED is already wired; it is that a publisher who will not say when it published
cannot be read point-in-time at all, and most of them will not say.

The rejected alternative was `reference_plus_lag`. Japan publishes on the Friday
preceding the 26th, Israel around the 15th of the following month: real,
published rules that a fixed day count cannot express, and PR 10's guardrail
exists because a lag was once invented where the evidence said none was needed.
Six markets are therefore declared `cpi_series: null` with the route and the
missing piece written into universe.yaml, rather than pointed at an id that last
printed in 2022. `ESTAT_APP_ID` is not added to any env template, because a slot
nothing reads is the `OBLISK_IBKR_REALM` mistake again.

**A last-updated stamp is a safe release date; a table-level one is not.** The
ONS's `updateDate` and StatCan's `releaseTime` both move later when a figure is
revised, so neither is strictly a first-publication date. That is safe in the
only direction that matters: a later date delays the moment an observation
becomes readable and can never advance it, so there is no look-ahead in it — the
cost is conservatism, which is the side this build errs on everywhere else. A
TABLE-level date is not the same thing and is not safe: it is one value for every
row, so it does not delay the old rows, it backdates the whole history to today.

**A rebased index cannot be assembled from initial releases.** Eurostat moved
HICP to 2025=100 during 2026, so ALFRED's initial-release column for
`CP0000DEM086NEST` holds 2015=100 through 2025-12 and 2025=100 from 2026-01.
`cpi_yoy` divides the newest observation by the one a year older; across that
join it divides one unit by another and returns about -22%, on two fresh,
release-dated observations, stamped `ok`. Nothing downstream could see it.

So `value_vintage: latest` is declared per series: the vintage walk still
supplies the first-publication date, and one plain call supplies values that are
all on one base. The rejected alternative was FRED's own `units=pc1`, which
computes the rate server-side and would have been immune — FRED refuses it,
`"If output_type is '3' or '4', then units must be 'lin'"`. The cost of what was
chosen is that the stored value is the revised print rather than the first one;
for an HICP annual rate that is hundredths of a point, against a -22% artefact.

Two guards were added rather than one, because the first is a collector setting
and a collector setting can be wrong. FRED now stamps its `units_short` on every
row — it was the one source serving both percent rates and rebasable index levels
whose rows could not say which — and `cpi_yoy` refuses a ratio whose two ends
carry different units, reporting `mixed_units_in_window` and naming both bases
rather than "absent". The remedy differs: re-collect on one vintage, not wait for
a print.

**Every store read is bounded at `as_of`,** the snapshot `COUNT(*)` included.
Without it, a grid rebuilt for a past date read data recorded after it — freshness
went negative and the cell was stamped `ok`.

**Freshness is per input family, not per cell.** A market × sector cell is
forty-odd leaves fed by four stores on four cadences. A family reading nothing but macro
does not go stale because an equity bar is old, and a months-old vendor IV
percentile does not read `ok` because the bars beside it are fresh.

**A stale reading is reported as stale, and absence is reported as absence.** A
staleness probe that was windowed returned nothing for an instrument whose newest
bar fell outside the lookback, which read as "no bars at all" — so the stalest
instruments were the ones reported `ok`.

**Dividend yield is declared in `universe.yaml`, not collected.** There is no free
global dividend feed, and a yield inferred from a trailing payment is a number
nobody could audit afterwards. It carries the date it was sourced, because a yield
is a fact about a date and undated cannot be told from stale. `NULL` means nobody
has declared one — not a claim that the instrument pays nothing — and
`check_position_risk` reports which of the two it used. Priced at zero, a 5%
payer's N(d₂) and delta are both overstated, and the zombie test compares today's
N(d₂) against the one recorded at entry, which puts the bias directly on the exit
gate.

**`market_data_status` has three states, not two.** FROZEN, DELAYED and REALTIME
are quote states; NULL is *unreported*, which is a third answer rather than either
of the others in disguise. It travels from the snapshot into every table it
appears in, because anything frozen or delayed must be labelled indicative.

**Smile hygiene, and no unfiltered second reading.** Spread, book depth, session
volume, delta band and butterfly convexity filter the chain, reusing the
`liquidity` keys wherever the question is the same one. Drop counts are reported.
A chain where nothing survives is **absent**; it is never re-read unfiltered.

**Strike bounds are volatility-scaled.** A fixed ±25% window is far too narrow for
a high-IV name and far too wide for a low-IV one.

---

## Running unattended

**The sequence is ordered by what a missed day costs, and `chain` moved ahead of
`vendor-chain`.** The order was `backfill → freshness → vendor-chain → chain →
curve → macro`, and `docs/COLLECTORS.md` named that as the defect in its own
prose: a marketdata.app outage taking down the step after it would cost a
European chain snapshot, which is the only thing here that no amount of money
recovers. The argument that put the vendor first was rate limiting — "a
rate-limited day still gets the twelve US names in" — and it does not survive
contact with the rest of the design, because IBKR request budget and
marketdata.app credits are different budgets and nothing competes between them.
The order is now `nav → backfill → freshness → chain → curve → vendor-chain →
macro`. `nav` was added at the head in Phase 5 under the same rule: the account
ledger serves today's net liquidation value and no other, so a day not run is an
observation nobody can ask for again, and one call cannot meaningfully delay the
step behind it.

**The order is a module constant, not a config key.** It was tempting to put it
in `config.yaml` beside everything else that varies. It is architecture rather
than a threshold, and a key that could reorder it would make the guarantee above
switchable by a setting — the exact failure the Phase 3 sweep deleted nineteen
keys for. `BUILD-BRIEF.md` and `docs/COLLECTORS.md` own it; `DAILY_SEQUENCE` in
`collect/cli.py` states it once, and `tests/test_deploy_units.py` fails if the
timer's header comment disagrees with it.

**Every step is guarded, and the guard is deliberately broad.** Each step runs in
its own `try/except Exception`, is recorded on the sequence with its reason, and
is stepped over. Catching `ObliskError` instead was considered and rejected: the
failure the guard exists for is the one nobody anticipated — an upstream
returning something unparseable, a library raising its own type, a bug in a
collector — and those are precisely what a narrow catch would let escape.

Guarding a step is *not* the same as letting a step run on the wreckage of the
one before it. The single real dependency is bars feeding the chain's sigma, and
it was already handled honestly: the ladder records which rung sized each band,
the run reports `degraded`, and now the freshness verdict is a note on the
sequence rather than a log line nobody reads. A chain snapshot on a fallback
sigma is a worse photograph, not a wrong one, and losing the day to protect its
quality would be the wrong trade in the one place it cannot be retaken.

**A killed process is reported as interrupted by the NEXT run, on age rather than
liveness.** `collector_runs` always inserted `running` before the work started so
a crash left evidence — but nothing ever reclaimed the row, so `running` meant
both "in flight" and "died last Tuesday". Two alternatives were rejected. A PID
is reused, and means nothing across the reboot that is the likeliest reason a run
died at all. A lock file has the same problem one layer up and adds a file that
can outlive its holder. So a row is reaped once it is older than
`collectors.run_stale_after_seconds`, set above the longest run this package can
perform and below the cadence it runs at. The cost is stated rather than hidden:
a run that genuinely exceeded the window would be mislabelled while still
working, which is why the window is generous.

`interrupted` is its own status. As `error` it would be indistinguishable from a
run that reached a verdict and reported failure, and those are different facts —
an error names what went wrong, an interruption names that nothing got the chance
to. As `ok` it would report success over an unknown amount of work.

**A day half collected is identifiable from the store alone, because the
invocation has a row of its own.** `daily` wrote five unrelated
`collector_runs` rows; nothing tied them together and nothing recorded how many
steps were *meant* to run, so a sequence that died after two of five was
indistinguishable from one that collected two and legitimately skipped the rest.
`collector_sequences` writes `steps_planned` on entry, for the same reason the run
row goes in as `running`: the count of intended work has to survive the process
that intended it. A step omitted by `--skip` records a `skipped` verdict and does
not count toward `steps_completed` — a filtered-out step collected nothing, and
counting it would make a sequence look like it did more work than it did.

**The failure channel is an exit code and a row, and explicitly not a webhook.**
The requirement was one channel that works with no chat session open. An outbound
notifier was rejected before it was designed: it is a new egress path, a new
credential to store on the one host whose job is to need no attention, and a new
failure mode where the alert about the failure also fails. The two channels that
already existed do the work instead — the exit code systemd records, and
`collector_sequences` — and `obliskiq-collect status` is the reader for the
second. It opens no market-data provider, spends no credit and touches no
network, which it must not, because the case it is for is the one where every
upstream is down.

**Two writes were made atomic because the gap between them was killable.**

`vendor_fetch_log` and the chain rows it accounts for now commit in one
transaction. This **reverses** an earlier decision, which logged the fetch `ok`
first on the grounds that a failing write must not cause a re-buy. That traded
the wrong way. The window between the two commits is exactly where a reboot, the
OOM killer or the unit's own `TimeoutStartSec` lands, and `_already_held` never
re-buys a settled past session logged `ok` — so a kill there bought a day, wrote
nothing, and suppressed every future attempt at it. The new cost is one re-bought
day after a crash; the old cost was a day permanently absent while the log said
it was held. A day silently missing is worse than a day bought twice, and this is
the route where a day *can* be bought twice — the European chain, where it cannot,
does not run through here at all. The credit is not forgotten either way: a
failure writes an `error` row carrying what the call actually cost, and an error
is always retryable.

`underlying_snapshots` and `chain_snapshots` now commit together per underlying.
The snapshot was written at the top of the underlying and the chain rows at the
bottom, so an underlying that listed no month in the tenor window, or a
connection dropped mid-walk, left a snapshot with no chain for that date. That
reads exactly like a chain that legitimately returned nothing, and
`underlying_snapshots.implied_vol` is the series the self-computed IV percentile
is built from, so the half-state did not merely look untidy: it fed a statistic.

The two write pairs that are deliberately **left** non-atomic are the bars and
their watermark, and the macro rows and their fetch log. Both are self-correcting
— the watermark is recomputed from the table rather than from the request, and a
macro series with no log row is simply re-fetched, which is free — so wrapping
them would buy nothing and lengthen the transaction that holds the write lock.

**`PrivateNetwork=true` is now enforced by a test, and the import graph was
pruned to match.** The compute unit's isolation was asserted in every document
that describes the layer and tested nowhere, and the entrypoint transitively imported the whole httpx and IBKR
client stack — through `util/__init__` re-exporting `.http`, and
`collect/__init__` reaching `ibkr.cp_client` for a type annotation. No socket was
ever opened, because every client happens to be constructed inside a function
body, so the guarantee held by placement rather than by design. Both are checked
now: the whole `all` sequence runs with every socket call rigged to raise, and
the transitive import graph is walked and asserted to name no HTTP client. The
run-time check alone would have passed on the code as it stood, which is why
there are two. `asyncio` is deliberately not on the forbidden list — it is a
concurrency library rather than a client, and it is what the collectors' target
pacing is built on.

The compute unit also stopped reading `collect.env`. That file holds every IBKR
credential and API key on the machine, and a process that cannot reach an
upstream has no use for them; two independent guards — cannot connect, and has
nothing to connect with — are worth more than one.

---

## The grid

**Markets are measured on their native index contract, never a US-listed
wrapper.** The level and direction of a market are properties of that market.

**Six of thirteen declared native indices did not resolve.** `universe.yaml`'s own
warning — every exchange code and conid in it is documentation-derived — was well
founded. `SX5E` is `ESTX50`; `UKX`, `TA35`, `IBOV` and `TOP40` are not contracts;
`AS51` is on PSE, not ASX. `XIN9I` returns an **empty series**, which is not an
error and must not be read as one. Five markets now measure on a native-currency
local fund; two do not measure at all.

**An explicit `exchange=TSEJ` defeats history that the same call serves without
it** — reproduced on two contracts. This inverts the native-exchange rule, which
holds for indices and futures, so the difference is carried per row.

**Coverage was fixed by splitting the reason, not the denominator.**
`coverage_pct` counted populated cells over all 5,772 leaves, unmapped ones
included, so the ceiling was about 9.6% against a 20% floor and every build
reported DEGRADED whatever collection did. A fourth status, `inapplicable`, now
covers cells no collector could ever fill, and it is **declared** at every site
rather than inferred from an absence. Two numbers are reported: collection health
and the ambition gap.

**A cell names the sample it got, not the window it asked for.** `Cell.n_obs`
carries the count. A percentile over nine points is not a percentile, and a cell
saying `n/a` with a count is worth more than a number nobody can defend.

**The IV percentile has a bridge, not a guess.** IBKR's MCP connector serves
13/26/52-week percentiles; the collectors run on Client Portal, where the field id
is unverified. Guessing it is specifically forbidden — `7633` is *Implied
Vol./Historical Vol %*, a ratio, and would land in the percentile column looking
plausible. `record_iv_percentile` lets a session that holds the connector store
one with its source, and the grid says `iv_percentile_unavailable` rather than
inventing a figure. The vendor figure is also known to be unreliable for broad
ETFs: it reported S&P 500 IV at the 100th percentile while VIX sat near a
three-month low.

**A percentile is only meaningful with the validity of the IV it ranks, and the
two failure modes are byte-identical.** IBKR serves a populated
`implied-volatility-percentile` even when `implied-vol-underlying` reads
`is_valid: false`, and the rank of an invalid reading comes back as `0.0` rather
than being omitted. Session 6 read those zeros as the *tell*. They are not:
Session 10 caught ASML trading, REALTIME, with a valid current IV of 0.3779 and
`high_13w`/`high_26w` of exactly `0.0` — a true zeroth percentile, meaning its IV
is the lowest of both windows, which is the most compressed reading the field can
produce and precisely what this machine hunts.

So a genuine zero and a broken zero cannot be told apart by their value, and
`is_valid` is the only discriminator. Two rules follow, and the second is the one
that would have been got wrong: **check the flag**, requesting
`implied_vol_underlying` in the same call; and **never filter on the magnitude** —
"drop zeros to be safe" throws away the strongest signal on the board, while
keeping them ungated puts a quote IBKR declined to give at the top of the
compression screen, which is the same shape as the `-1.0` sentinel that drove
`implied_vol_min_plausible` one level down. A percentile arriving with no validity
flag at all is **refused**, because an untested gate is never a passed gate.

All three windows are now carried. The store has had `iv_percentile_13w` and
`iv_percentile_26w` since migration 005 and the capture wrote only the 52-week
one, so two thirds of a field the connector serves for free were being discarded
at the decoder.

**The grid and the screen were measuring IV−HV against different realised-vol
figures**, reaching opposite verdicts on live data — a factor of 42 on ASML.
Fixed; the vendor figure is not reproducible from IBKR's own bars.

**The correlation norm is built from readings that do not share data.** The shift
was a z-score against a series of rolling correlations sampled every session: two
consecutive readings differ by one observation, so the series is smooth by
construction and its standard deviation measures that smoothness rather than the
spread of the correlation. Dividing by it inflated every z-score by roughly the
overlap, which is a flag that fires on quiet pairs. The norm now steps by the
window, and `grid.correlation_norm_min_independent` says how many independent
readings it takes before their sd is a dispersion. The rejected alternative was
widening `grid.correlation_norm_window_years` to keep the same count: the
backfill fetches `data.daily_window_years` of daily bars, so a longer norm window
is a window with no data in it. The honest consequence is that the longest
correlation window reports `n/a` on a two-year history — an admission, not a bug,
and widening both together is a collector change in `ROADMAP.md`.

**A ranked row carries the state of the cell behind it.** Ranking admits `ok` and
`stale` cells deliberately — a stale reading is the best the store holds, and
dropping it would make the table look healthier than the store is — but
`grid_rankings` had nowhere to record which it was, so the four ranked views (the
tables a session actually reads) ordered stale cells beside fresh ones with
nothing to tell them apart, in the one place SKILL.md's indicative-labelling rule
was exempt in practice. Migration 012 adds `status`, `market_data_status` and
`conditions`.

**A ranked view orders on one indicator and declares the rest.** *Coiled* and
*extended* are four conditions each in SKILL.md; `most_compressed` tests one. The
rejected fix was ranking on a composite score, which would make the ordering
unauditable — the thing this design refuses everywhere else. Instead the other
conditions travel with the row as a small JSON map, each one true, false, or null
where the store could not measure it. Null is never reported as false: an
untested condition and a failed one are different claims.

**The realised-vol percentile is computed over `data.daily_window_years`,** the
key the collector actually fills, and SKILL.md names that key. Realised vol comes
off daily bars; claiming the percentile over the weekly span would be asserting a
statistic over bars nobody collects. The same reasoning already governs
`grid.correlation_norm_window_years`.

**Distance from the long-run high and maximum drawdown are different questions.**
Family 4 duplicated family 2's number. A series back at its high has no distance
from it and may still have fallen by half on the way there — which is the
volatility fact this machine hunts — so family 4 is now a true peak-to-trough
worst case over the weekly window.

**A structurally impossible cell is `inapplicable`, not `n/a`.** A benchmark has
no correlation to itself and an index has no share volume; both were reported as
missing data, which put a permanent, unfixable gap into the collection-health
number that the fourth status exists to keep out.

**A second sweep found instruments the first one missed, and the difference was
where it looked.** The first probe searched for index contracts and stopped where
none resolved; this one searched sector-fund families market by market. Eighteen
cells that read as structural gaps were not: the nine US Select Sector SPDRs, four
Australian sector funds, two Brazilian It Now index funds, an Israeli banks fund,
a UK property fund, and a Shanghai listing for China. Mapped cells went from 39 to
57 of 156, and the ambition gap from 21% to 32% of the declared grid. The lesson is
not that the earlier session was careless — it is that **an absence found by one
search is not a fact about the world**, which is precisely why the status now has
to be written down rather than inferred.

**Where the sweep found nothing, that is now a claim somebody made.**
`unmapped_cells` in `universe.yaml` declares every pair no instrument measures,
with its reason, and the loader refuses a universe where a pair is neither
measured nor declared, is claimed by both, or is covered by a `remaining` block
that has nothing left to claim. Until then the grid *synthesised* the reason and
stamped the same sentence on all 141 of them, which read as though a person had
considered each one. The rejected alternative was to keep inferring and simply
improve the sentence: the same failure `options_unavailable_reason` closed
earlier, where "no chain today" and "no chain ever" were told apart by a flag's
absence. The reasons that now reach the cells name the exchange searched, the
fund family that is not listed there, and the near-misses rejected — Korea's
KODEX semiconductor, autos, energy-chemicals, healthcare and IT codes all
returning no contract; the SATRIX funds on the JSE being offshore feeders and a
bond portfolio; the LSE's Invesco XLxS series tracking US sectors, which under
Britain's name would be the wrapper failure again.

**A mapped instrument the store does not hold was hiding a collector failure
inside the ambition gap.** Such a cell reported `no_instrument_mapped` —
structural, out of the denominator — so a store that had never run
`obliskiq-collect universe` divided its handful of populated leaves by its
handful of seeded ones and cleared the coverage floor comfortably. The
denominator shrank to match the failure. It is now `instrument_not_seeded`, an
`n/a` that stays in the denominator and names the remedy, and the CLI regression
test seeds every declared instrument rather than one.

**china/broad is measured after all, through Stock Connect.** The previous
session looked for an index contract and for a Hong Kong listing, found `XIN9` is
not a contract, `XIN9I` an empty series, and iShares FTSE China A50 quoted in HKD,
and correctly refused the A50 — an HKD fund under a CNY market's name reports the
wrong currency's return and hands indicator 11 an EURCNY series that does not
describe it. What it did not try is **SEHKNTL**, Shanghai–Hong Kong Stock Connect
northbound, which reaches Shanghai-listed funds at their own Shanghai quote.
`510300`, Huatai-PineBridge CSI 300, serves daily bars there.

The currency is the open point and it is **declared, not assumed**. Northbound
trades settle in offshore yuan while the quote is the onshore price, and neither
the connector's contract search nor its snapshot returns a contract currency, so
which of CNY and CNH IBKR stamps on the row could not be observed from a sandbox.
`CNY` is declared because it is the currency of the *price*, which is what
`fx_series: EURCNY` — an ECB onshore reference rate — actually describes; the row
carries `# UNVERIFIED`, the coverage note discloses the route and the settlement
basis rather than netting it out, and `references/ibkr-playbook.md` carries the
question. If a live check returns CNH the remedy is one field. The HKD proxy stays
rejected: a basis of a fraction of a percent between two quotes of the same
currency is not the same class of error as a different currency.

**The macro blanket lifts on the `broad` cell and nowhere else.** Four families
read the market spec and the macro store and touch no instrument data, so
blanketing them cost china and south_africa their entire macro row for want of an
equity fund — a claim about the wrong thing. Letting them through on all twelve
sectors was the rejected alternative: per-market macro is one fact, and twelve
copies of a single reading in the numerator would make a market with no
instrument look better collected than one with eleven.
`indicators.MACRO_ONLY_FAMILIES` is derived from `FAMILY_INPUTS` rather than
listed by hand, because a hand-kept list goes on asserting "touches no instrument
data" after a family stops satisfying it.

**A ranked view now carries the denominator it ranked over.** The four views
print identically whatever they ordered, so `widest_iv_hv` — which before this
session could reach two cells in the whole grid — looked exactly like
`most_extended`, which reaches every cell with bars. Mapping the US sector SPDRs
took the option families from 2 cells to 11, and that is still 7% of the grid, so
the number has to travel: `reachable` and `ranked_over` on every row, the reach
table in the census, and the narrowest family on the grid summary. Reach is
counted in **cells, not leaves**, because a family emits up to ten leaves per cell
and a leaf count would rank the wordiest families as the widest-reaching.

Below `grid.min_cells_for_ranked_view` the view is **labelled, not suppressed**.
Suppressing it was considered and rejected for the reason the whole design keeps
returning to: it would delete the only implied-vol signal the store holds in
order to avoid admitting the signal is thin, when the rule everywhere else is
that a cell says `n/a` with its sample count and a count with an honest label is
worth more than a blank.

**The nine US sector SPDRs collect chains, and the cost is disclosed rather than
absorbed.** They are the reason the option families reach eleven cells instead of
two. The vendor route's `option_vendor.max_credits_per_run` ceiling now spreads
over nine more symbols, so each is collected less often, and until a run reaches
one its cells report `no_option_chain` — an `n/a` counted against collection
health, which is the honest status for a chain that is reachable and absent.
Declaring `options_unavailable_reason` on them instead would have kept the metric
flattering and been false.

---

## The correlation matrix reaches beyond equities, and a rate is not a price

**The matrix was equity-index-to-equity-index only, and the other two asset
classes were already in the store.** Eleven ECB reference rates and a policy rate
and a 10-year yield per market, collected nightly, paid for, and absent from the
one table whose job is to say what is moving with what. Equity against rates
flipping sign is one of the cleanest regime markers there is.

**A market stopped being an identifier.** `germany` has an equity index, a policy
rate and a long rate, and they are three series. A leg is therefore
`(asset_class, series, market)`, and `correlation_matrix`'s primary key carries
the first two — which is a migration, because the table is `WITHOUT ROWID` and
SQLite cannot alter a key in place.

**An equity leg's series is the MARKET NAME, not the index symbol.** That looks
like an inconsistency and is the opposite: a market is measured on exactly one
index contract, so the market names that series uniquely, and using it meant
migration 019 could copy every existing row across EXACTLY rather than
back-filling a symbol it had no way to look up from SQL. The rejected alternative
was to write the native index symbol and leave legacy rows holding the market —
a convention invented in the migration and contradicted by the next build, for
the sake of one column.

**Rates are level changes and prices are log returns, and the transform has no
column.** A log return on a rate near zero is not a return; on a rate at zero it
does not exist, and `ECB_DFR` has sat at zero. So a rate's change is the first
difference of its level in the series' own units. One transform for everything
was rejected for that, and correlating the rate LEVEL against equity returns was
rejected because a level series is non-stationary — the correlation would mostly
measure shared trend. The transform is a pure function of the asset class, so it
gets no column: a derived fact stored beside the value is free to drift out of
step with it, which is exactly why the three realised-vol estimators put the
estimator in the leaf NAME instead of a provenance column.

**Filtered on release date, aligned on reference date — and the distinction is
the whole point-in-time rule.** `observations_known_at` keeps an observation only
once its release date has passed, which is what stops a correlation resting on
prints nobody had. Alignment is then on the reference date, because that is the
day the value applied and therefore the day a bar can be paired with.
Intersecting bar dates with RELEASE dates instead would pair a bar with almost
nothing — a monthly series has twelve release dates a year — and would look like
a data gap rather than a method error. Two columns, two jobs.

**Which pairs are computed is a module constant, not a config key.** Equity ×
equity, equity × FX, equity × rates. It is architecture rather than a threshold,
and the same argument `DAILY_SEQUENCE` is a module constant under applies: a key
able to widen it would make the shape of the board switchable by a setting. FX ×
FX and rate × rate were considered and refused on their own merits too — eleven
ECB crosses against each other is the dollar index measured eleven ways, and six
euro-area markets share `ECB_DFR`, so rate × rate would largely correlate one
series with itself.

**Mixed frequency is refused rather than resampled.** Correlating a daily equity
return against a monthly rate change compares twelve readings a year with two
hundred and fifty, and any alignment making the lengths match has resampled one
of them — the thing `MacroPull._sane` already refuses one layer up. Every
declared `long_rate_series` is monthly, so equity × long-rate is `n/a` across the
whole board today. That is not a defect to fix by resampling; it is a collector
question about what daily rate data exists.

**Two counts became columns, and this is the honest half of the change.** The
norm floor `grid.correlation_norm_min_independent` applies unchanged, and most
cross-asset pairs will sit below it for a long time. Migration 019 adds `n_obs`
and `norm_independent` so that is countable in SQL rather than only readable
inside a reason sentence — the same rule `grid.n_obs` has carried since 007: a
count is a claim about evidence. Measured on the offline fixture store, which
runs with `--skip macro`: 1,944 cells, 120 `ok` and every cross-asset pair `n/a`
naming the series it could not read. That is the store being empty of macro, and
the row says so.

---

## The implied term structure, and which expiry pair defines it

**The collectors buy a volatility surface and the grid published a point.** That
was the pre-deployment audit's blunt finding. Session 9.5 closed half of it with
the skew leaf — the DIRECTION of the smile beside its level — and the other half
is the shape across time: `implied_vol_atm` is symmetric in tenor as well as in
direction, so "vol is high" and "vol is high AND inverted" reached the board as
the same number, and they are opposite trades.

**The slope is front over back minus one, because `rv_term_slope` already is.**
Family 5 has emitted a realised term slope since it was built, computed exactly
that way. Defining the implied twin differently — a difference in vol points, or
back over front — would have made the two impossible to subtract, which is the
one comparison a reader most wants: an implied curve steepening while the
realised one does not is the regime signal, and it requires both to be the same
statistic. Positive is inverted, on both.

**A fixed DTE pair is not available, and the usual reason is the weaker one.**
"Listed expiries are what they are, per name, per day" is true and is not the
binding constraint. The binding constraint is that `chain_collector` buys ONLY
expiries inside `tenor.entry_dte_min`–`entry_dte_max`: a front month was never
collected on any name, so a 30-versus-90 slope is not unlisted, it is absent from
the store by construction. Both targets therefore sit inside the band the
collectors actually buy, and a test asserts it — outside it the leaf would be
`n/a` on every cell for ever with nothing saying why, which is the shape of
failure a `valuation_source_mode` is.

**Nearest-listed-to-a-declared-target won, and two alternatives were rejected on
the same objection.** *Front listed versus last listed* and *front versus Nth
listed* are both always available whenever two expiries exist, which is their
appeal. It is also the defect: the measured tenor pair drifts with the listing
cycle, so the leaf answers a different question each month while its name and its
value type stay identical. The grid ACCUMULATES history rather than re-measuring
it, so a leaf whose meaning moves is a leaf whose history cannot be read. Both
were rejected for that and not for being harder to compute — they are easier.

**The window label is the join key and carries nothing else.** It is the two
ACTUAL DTEs, `"<front>d vs <back>d"`, matching `rv_term_slope`'s form. A slope
between 28 and 91 days and one between 28 and 35 days are not comparable, and
`get_grid_cell(history=N)` already tells the caller to compare only where
`window` matches — so the label is what makes that check possible rather than
advisory. The provenance goes in `reason`, which an `ok` cell may carry: in the
window it would differ every session and nothing would ever compare. That is a
deliberate departure from `implied_vol_atm` and `implied_vol_skew`, which put
their provenance in the window and consequently have no comparable history.

**Two floors, and the order they are tested in is a finding of its own.** Below
`smile.term_structure.min_separation_days` the legs are not a term structure. And
a name with one listed expiry is `n/a` naming that expiry — never a slope against
itself, which is zero by construction. The single-expiry test runs BEFORE the
per-leg tolerance because a name listing one expiry also fails the back leg's
tolerance, and reporting that would send a reader looking for a tolerance to
widen when what is missing is a second expiry to widen it onto. Two true
statements, one useful; this repository has closed the same class of confusion
for `no_option_chain` and for `instrument_not_seeded`.

**No vendor and no solved fallback, and this is the strongest rule in the leaf.**
`implied_vol_atm` falls back through `data.atm_iv_prefer` to a vendor quote or to
one this repository solved. The term structure does not fall back at all.
`implied_vol_solved` exists in a SEPARATE column rather than as a tag inside
`implied_vol` because a series mixing solved and exchange-quoted values has a
methodology break at the join; a slope with a solved front and a quoted back
would have that break inside a single number, where nothing downstream could see
it. Reading both ends off one hygiene-filtered chain makes it impossible rather
than discouraged — the same argument the two columns were created under, applied
one layer up. A clamped ATM read is NOT refused: clamping is a fact about where
the strike sat, not about how the number was produced, and it is named in the
provenance.

**Measured on the offline fixture store, 2026-09-16.** The three leaves reach all
192 market × sector cells — 180 `inapplicable` where family 7 already was, 12
`n/a` — so the new leaves are no narrower than the level beside them. None
carries a value, and the reason is the fixtures rather than the code: the
recorded chain holds four expiry months and only `2026-12-18` is still ahead, so
QQQ reports *only one listed expiry (93d)* with the window label it asked for,
and the other eleven cells have no chain at all. A cell now holds 47 leaves, up
from 44.

---

## Volatility, risk and the screen

**Two numbers come off one smile, and they are read at different strikes.** The
option risk check took a single implied vol at the target level and used it for
both N(d₂) — the probability of finishing beyond that level, which is a property
of the target — and the greeks, which are properties of the contract held. On a
skewed smile that priced delta and theta at the wrong strike's vol. Both readings
now come from the strike each question belongs to, and both travel with their
provenance and a flag for a clamped reading, because a vol pinned at the edge of
the smile is an extrapolation and must not be reported as an observation.

**The base rate cancelling out of the carry test is covered interest parity, not
a bug.** The prose said the opposite and invited a "fix" that would have broken
the identity. What the test genuinely approximates is the hedge cost: overnight
policy rates standing in for the forward points on a long bond. That is a real
tenor mismatch, so `CarryTest.basis` states it rather than leaving the reader to
assume a clean hedge.

**Cheap and coiled are measured, never assumed.** The screen tagged `cheap` on
the IV−HV spread alone and `coiled` on compression alone, while SKILL.md defines
each as several conditions. Every condition is now read from the bars, and a
condition the bars cannot support leaves the row untagged rather than tagged on
the strength of the ones that could be checked.

**The screen ranks the cheapest volatility first.** It sorted the signed spread
descending, so the most expensive options came first and `limit` truncated
exactly the cheap tail a dislocation screen exists to surface. Rows with no
measurable spread sort last rather than sorting as zero.

**Point-in-time discipline is not optional for the one read that skipped it.**
`curve_shape` was the last store read not bounded by `as_of`, so a backtest of a
past session could see a curve published after it. Bounded.

**The seasonal inventory band excludes the year being tested.** It matched on ISO
week without matching on year, so the current year's own neighbouring weeks sat
in the band it was being compared against, pulling the band toward the
observation and understating every deviation. `commodities.inventory.min_band_observations`
now also says when too few prior years remain to call it a band.

**An uncovered calendar year is untested, not clear.** The mandatory-close check
read a holiday table that declares which years it covers; a date outside them was
being treated as verified.

---

## Valuation is out of scope

There is no free source for global historical fundamentals: IBKR does not serve
historical P/E, and free providers cover US filers only or cap at annual
frequency.

A `valuation_source_mode` key once offered `none | edgar_us_only | paid`. No
Python file ever read it — there is no valuation indicator among the fifteen
families, no column to hold one, and no fundamentals fetcher — so setting it to
`edgar_us_only` would have done exactly nothing while looking like a decision.
That is a worse failure than the absence it papered over, and it is the template
for the Phase 3 sweep below.

The mandate ranks on price, volatility, drawdown and breadth percentiles, all
sourceable globally and consistently. Reopening valuation means building the
indicator, the column and the fetcher — not setting a flag.

---

## Ownership: config, code and prose

**config.yaml owns every threshold, window, floor and cap.** `obliskiq.config`
raises on a missing key rather than supplying a default, because a default in code
is a magic number that has learned to hide.
`tests/test_no_magic_numbers.py` enforces it from the code side: a numeric literal
in `src/obliskiq/` must be structural arithmetic or a named module constant.

**SKILL.md owns judgment, and names config keys instead of restating values.**
SKILL.md once said "Sector cap 60%, country cap 80%, single foreign currency cap
65%" while config said 0.40 / 0.80 / 0.80 — two of the three wrong.
`tests/test_skill_config_consistency.py` now fails on a threshold-shaped number in
the guarded documents at all, not merely on one that contradicts config: a test
that only flagged values *also present* in config would have caught the 80% and
waved through the two that were wrong.

**Every config key has a consumer, or it is not in config.yaml.** The inverse
rule, added in Phase 3 as `tests/test_no_unread_config_keys.py`. A consumer is
code in `src/obliskiq/`, a test pinning a policy value, or SKILL.md naming the key
in dotted form — the skill reads config at runtime, so a key it cites is genuinely
consumed, but prose gesturing at a rule is not a citation.

Phase 3 deleted nineteen keys nothing read. The inert ones were merely clutter;
the dangerous ones read as guarantees while enforcing nothing —
`discipline.freeze_thesis_at_entry` beside an unconditional SQL trigger, and an
`execution.*` trio implying the server gated order staging when the server exposes
no order-staging tool at all. Staging runs through the IBKR MCP connector under a
rule SKILL.md states unconditionally, and a flag that could have switched the
confirmation off, enforced nowhere, was worse than no flag.

The rest were duplicates of a key that *was* read
(`macro.collection.request_spacing_seconds` beside
`collectors.request_spacing_seconds`; `positions.hold_days_min` beside
`tenor.hold_days_min`; `grid.min_observations.regression` beside
`commodities.proxies.min_observations_months`), or settings owned by the
environment rather than by config (`runtime.logging.format`, which
`OBLISK_LOG_FORMAT` has always supplied).

One key was wired up instead: `collectors.source` now supplies the collection
route when `--source` is omitted, so an unattended run reads its route from config
rather than from a hardcoded argparse default.

**`config.get_config` is deleted, and deleting it is the decision.** It was a
cached, process-wide config accessor with a lock and a `reload=True` escape
hatch — everything a global needs — and in the life of the project nothing ever
called it. That is not an oversight to tidy up: this architecture passes `Config`
explicitly into every collector, engine, gate and tool, which is what lets a test
build one config and a second test build another in the same process, and what
makes `--config` mean something. A cached global would have made both impossible
the first time something reached for it. Left in place it reads as the sanctioned
way to get config — it is the only accessor in the module that needs no argument —
so the next session reaching for one finds it and reintroduces global state
without ever deciding to. The rule is the same one this section already applies
to a key nothing reads, applied to a function: an unused thing that looks
sanctioned is worse than an absence, because the absence makes somebody think.

**One owner per fact.** `config.yaml` thresholds · `SKILL.md` judgment ·
`references/*-playbook.md` observed API behaviour · `BUILD-BRIEF.md` architecture
and schema · `ROADMAP.md` what is open · this file, why.

---

## A health verdict is about AGE, and a guard that unions sets fails open

Two defects found by reviewing Session 11's own diff before it merged, both of
the same shape: a check that looked like it was checking something.

**`get_system_health` would have called a dead collector healthy.**
`CollectorStatus` reports the newest run per collector with **no window on it at
all** — the sequence list is windowed, that one is not — so a collector whose
timer stopped firing three months ago arrives as a tidy `ok` row beside an empty
sequence list. A verdict built from run STATUSES therefore reported `ok` for
precisely the machine this tool exists to catch. The verdict is now derived from
a list of named `concerns`, and age is one: a run's status describes that run,
and the concern describes the silence since.

The bound is `server.stale_after_days` rather than a new key. It is the server's
own declared answer to "beyond this a read is stale", already applied to every
tool response by `envelope`, and the newest collector run and the newest grid
build are reads like any other; a second key would be one fact with two owners.
Every age is measured against the `as_of` the caller asked about, never the wall
clock, so asking about a past session reports what was true then — the one
exception being the handshake, whose TTL is a property of a real token's real
lifetime.

Naming the concerns rather than scoring them is the same rule the ranked views
follow: a composite nobody can reproduce is what this design refuses, and a
verdict is a composite unless the reasons travel with it.

**And the frequency guard failed open on a NULL.** `macro.frequency` is
nullable, so a leg declaring no cadence produced an EMPTY set; unioning that into
the equity leg's `{daily}` left one element and the pair was correlated as
though both sides had agreed to be daily. It now refuses an undeclared cadence,
refuses a leg whose own rows disagree, and only then compares the two — three
states rather than two, which is the same correction `market_data_status` needed
when NULL was being read as one of the other two.

**That guard also found a false value upstream.** `macro/rates.py` stamped every
row it cached `"daily"`, and `macro.risk_free_rate.series_by_currency` resolves
CAD to a **monthly** id because FRED serves no daily Canadian short rate. The
cache now writes `NULL` — the column's own word for "this row does not declare
one" — because a guessed cadence in a column other readers trust is worse than
an absent one, and the full `macro_pull` writes the declared frequency for the
same series anyway. A guard is only as good as the column it reads.

---

## Deploying to the VM: the address, and where the secrets come from

Two decisions taken while Session 10 waits on a machine. Neither needs one —
both are lost if nobody writes them down, and one of them fails silently. A third,
which provider to build the machine on, is the next section.

**The public IP has to be RESERVED, and an ephemeral one fails quietly.**
marketdata.app permits one egress address per account and answers a changed one
with `HTTP 403`; Oracle attaches an ephemeral address by default and changes it
on every stop/start. The collector handles the 403 as well as it can —
`VendorIpBlocked` is permanent within the run, the whole `vendor-chain` step
stops at the first one rather than letting twelve targets rediscover it, and a
cooldown goes into `collector_state` so the next run does not either — and the
run then reports **degraded**, not failed. That is the problem. The symptom of an
ephemeral address is a nightly job exiting almost-successfully while collecting
no US option chains at all, twelve of the fifteen chain names, with nothing in
the log saying the address moved. The requirement and its reason are in
`docs/DEPLOY.md` §4b, and each provider's page has the click path, before the
first collection run rather than after the first quiet week.

**No secrets manager is adopted, and that is a decision rather than an
omission.** One appears nowhere in the codebase, and the three environment files
are written once by hand at provisioning. What IS decided is the shape if one is
ever adopted: it **generates and writes the three files at provisioning time**,
and never **fetches at collector runtime**. A runtime fetch adds a network
dependency, a second credential and a new auth failure mode to the one machine
whose entire purpose is to need no attention — on the path where a missed day is
a European chain snapshot that no amount of money recovers. The failure mode is
the one the outbound notifier was rejected for: a new way for the thing that
protects the data to be the thing that loses it.

**And the three files stay three.** `collect.env` holds the IBKR, FRED, EIA and
marketdata credentials; `compute.env` holds no credential at all, because that
unit runs `PrivateNetwork=true` and a process that cannot open a socket has no
use for an API key; `server.env` holds the auth token and no IBKR key.
`tests/test_deploy_units.py` pins both the exact `EnvironmentFile` paths and that
`compute.env.example` names no credential. Consolidating them into one
`/etc/obliskiq.env` would hand every key on the machine to the unit that cannot
use them, and turn two independent guards — cannot connect, and has nothing to
connect with — into none.

Key rotation is **not** closed by this. `FRED_API_KEY`, `EIA_API_KEY` and
`MARKETDATA_API_TOKEN` were committed as live values to `.env.template` across
seven commits and git history is permanent; reissuing them needs account access
nobody in a session has. `ROADMAP.md` carries it under *Housekeeping that is not
a phase* and it stays open.

## Two providers, one walkthrough

Taken 2026-10-04, with still no machine. The walkthrough assumed Oracle's free ARM
shape, which Oracle very often has none of, and a plan that waits on someone else's
capacity is a plan that misses days that cannot be recovered. Google's Always Free
`e2-micro` was the obvious alternative, and the question was whether 1 GB would do.

**Memory is not the limit, and that was measured rather than argued.** The numbers
and where they were taken are in `ROADMAP.md`, *What the second provider measured*:
the largest process anywhere was the install at 127 MB, `compute all` peaked at
68 MB over a store scaled to the live one's bar count, and the MCP server idled at
85 MB. The
Oracle walkthrough's statement that 1 GB cannot install the project did not hold.
Two caveats belong with it, because both are the kind of thing this repository has
been wrong about before: the store was scaled with synthetic bars and chains, and the
machine was an x86_64 sandbox rather than a Google one.

**The limit is a slice of a CPU, and the cost is the address.** `compute all` is
single-threaded and CPU-bound, and an `e2-micro` sustains about a quarter of a core
across two vCPUs — an eighth for one thread — so the job takes several times longer
there than anywhere measured, inside a timeout it was sized for a faster machine to
meet. Google also bills the external address, $0.005 an hour in use, static or
ephemeral. A static address is required anyway, so the honest summary is that the
Google machine is free apart from its address.

**Decided: a provider-neutral core and one page per provider.** `docs/DEPLOY.md`
holds everything the two share — registering for IBKR, installing, secrets, the
schedule, reading a run, upgrading, backing up — and `docs/ORACLE-CLOUD.md` and
`docs/GOOGLE-CLOUD.md` each hold making the machine, its address, connecting, and
the failures only that provider has. It keeps the old section numbers, so every
`§7b` and `§9` anywhere in the repository stayed valid and only the filename moved.
Rejected:

- *A second complete walkthrough for Google.* Six hundred lines that would drift
  from the first, in a repository that has just watched one copy of a schedule go
  stale against the timers it describes.
- *Google appended to the Oracle file.* A reader on Google would be sent through a
  document named for a rival and dropped into the middle at §6.
- *Oracle only, until a machine appears.* The wait is Oracle's capacity, not ours.
- *A provisioning script or Terraform.* The reader is someone copying commands from
  a page, and a script is a second thing to keep correct. It also could not hold the
  secrets, for the reason in the section above.

**Recommended order: Oracle's ARM machine if it can be had, otherwise Google.** The
ARM machine has 24 GB and real cores. But a machine that exists beats a better one
that does not, and the page says so rather than leaving it to be rediscovered a week
into retrying.

**Unverified, and tagged where it appears.** Nobody has followed either provider's
page on a console, so every console path is a belief. Beyond that: how `e2-micro`
bursts and how much of its 1 GB the operating system takes; whether IBKR or
marketdata.app treat a Google address differently from any other; whether Oracle
charges for a reserved address; what Google does when the trial ends; which disk type
Google's console defaults to; and the rate for a reserved address left unattached.

**What would change this.** A first run on a real `e2-micro` that takes more than
about half of the compute unit's timeout, or that leaves too little memory beside
the collectors, would mean either raising the timeout — a number in a unit file, with
`tests/test_deploy_units.py` pinning how it relates to the others — or advising a
paid shape, and this section is where that gets recorded.

**A test pins the table the walkthrough shares with the timers**, because the one
drift this work found was that exact one: §8 said 22:30 for as long as the timer
said 20:15, and no test read it.

---

## Engineering

**Secrets, paths and ports come from the environment, never from config.yaml.**
They differ per deployment and a token in a committed file is permanent in
history.

**The Client Portal Web API over OAuth 1.0a is the collection route.** The
downloadable Client Portal Gateway and IB Gateway are out of scope: both need an
interactive local login, which is precisely what an unattended collector cannot
do.

**The OAuth signature check is never skipped.** It is the backstop for builds
where a wrong encryption key decrypts to indistinguishable random bytes instead of
raising. The DH shared secret must be encoded as a **signed** big integer — a
leading zero byte when the high bit is set — because omitting it derives a
different token and every later call fails authentication with no useful error.
`obliskiq-collect preflight` walks the handshake step by step, and the signed
integer is pinned against an independent reference rather than against a test that
shared its formula.

**A test that cannot fail is not a test.** `test_retry_after_is_honoured` could
not; rewriting it surfaced two real defects — the HTTP-date form of `Retry-After`
was unread, and a server's explicit wait was clamped to our own backoff ceiling.
The same reasoning is why the threshold, magic-number and unread-key scanners each
carry a positive control.

**The Python floor stays at 3.11.** Nothing in the code needs a newer language
feature, and the collectors run unattended on a small cloud VM whose system Python
is whatever the distribution ships. A higher floor would mean maintaining a
non-system interpreter on the one machine whose job is to need no attention. The
floor is not a ceiling: 3.14 is tested.

---

## Collection without credentials, and what positioning measures

### The macro layer could not be collected without IBKR credentials

Two couplings, both found by running the collectors rather than by reading them.

`collect/cli.py` built a market-data provider for **every** command, so
`obliskiq-collect macro` ran the OAuth 1.0a handshake and refused to start
without `OBLISK_IBKR_CP_BASE_URL` — while reaching FRED, OECD, ECB, CFTC and EIA
and no IBKR endpoint at all. `deploy/obliskiq-collect@macro.timer` runs it as a
unit of its own, so the whole macro layer was unreachable whenever the IBKR
credentials were absent, expired or not yet issued. `NEEDS_MARKET_DATA` now names
the commands that read one, and `Context.market_data` is Optional with an
accessor that refuses — deliberately not a null object, because a collector
handed a stub reports success over nothing.

Behind that sat the second: `macro_pull`'s consecutive-failure guard counted
across the whole run rather than per source. Its own message said a dead upstream
should not be walked through every remaining series, but counting globally meant
**any** dead upstream stopped the other four. FRED is first in the declared list
and needs an API key, so an unset `FRED_API_KEY` aborted the run before it
reached the three sources that need no key at all. It now counts per source and
records one finding when it gives up on one, rather than one per series left in
it.

Together these are the difference between a macro layer that is collectible
without credentials and one that is not. Measured on a host holding no IBKR
credential and no FRED or EIA key: **39,419 rows across nineteen series from CFTC
and ECB**, and the grid rebuilt on them went from 0 populated cells to 56, with
the `fx_series_absent` reason code falling from **172 cells to none**.

**What still needs a key, and what it costs.** Of the 55 series something reads,
18 need no key at all — CFTC, ECB and OECD — and those are the ones now
collectible from anywhere. The other 37 are FRED (36) and EIA (1). Both keys are
free and issued instantly against an email; `ROADMAP.md` records that the ones
once committed to `.env.template` need reissuing rather than replacing.

**This section used to end by calling a FRED key the largest remaining unlock,
naming the 216 cells reading `macro_series_absent` as what it would open. That
was wrong, and the correction is the next section.** The key was not the blocker;
the request was.

### The FRED key was never the blocker, and the guard is why nobody noticed

A key was supplied and **every FRED series still failed**, on our own request
shape rather than on the credential. Two conditions of ALFRED's
`output_type=4` — the only call that yields a publication date, and therefore the
only one this build will use for a macro series — had both been got wrong since
the fetcher was written from documentation.

*A real-time window is not optional.* Sent without `realtime_start` and
`realtime_end`, FRED defaults the vintage window to today and returns
`No vintage dates exist for the specified real-time period: 2026-09-04 to
2026-09-04`. A day with no vintage is most days — FRED publishes vintages on
release days, not daily — so the default is an error rather than the current
reading.

*And the window is capped at a number of VINTAGES, not a span.* FRED names the
ceiling in its own error: 2,000 vintage dates per request. A vintage is a
release, so the count is the window length times the series' publication rate,
and `macro.collection.default_window_years` is ten — which puts every DAILY FRED
series over the cap (`DGS10` at 2,472) and no monthly one near it (`CPIAUCSL`,
`IRLTLT01*`, `IRSTCI01*` all comfortably inside). So bounding the window was
necessary and not sufficient; it is split by the series' own declared frequency
against `macro.sources.fred.max_vintages_per_call`.

The chunk size is **derived from `spec.frequency` rather than listed per
series**. A hand-kept list of which series need splitting would go on asserting a
publication cadence after FRED changed one, and the arithmetic — cap divided by
releases per year — is visible either way. Over-estimating the release rate is
the safe direction, so an unknown frequency is chunked as though it were daily:
too many calls is a cost, a window FRED refuses is a failure.

Chunks are merged on the **earliest release per reference date**. A row dated
near the end of one window can be published inside the next, so the same
reference date arrives twice with two release dates, and `output_type=4` exists
to record the first. A row carrying no release date never displaces one that has
one: an unalignable observation is a coverage gap the caller reports, and
preferring it would turn a dated fact into an undated one.

**Why two sessions passed without anyone seeing it.** `macro_pull` abandons a
source after `macro.collection.max_consecutive_failures` and lets the others
continue — which PR 6 added deliberately, and which is right. The consequence is
that a keyless run and a *broken keyed* run print the same summary line: nineteen
CFTC and ECB targets collected, FRED abandoned. The symmetry is the finding. A
guard that keeps one dead upstream from stopping the others also makes a dead
upstream look like an absent credential, and the only thing that tells them apart
is running it with the credential. Nothing about the guard should change; what
should is that `references/macro-playbook.md` now records FRED as **verified
live** and says which sources still are not.

Measured on a fixture-seeded store, the same day, before and after: macro targets
19 → **53** of 55, rows 33,721 → **56,194**, grid cells populated 1 → **215**,
collection coverage 0% → **10%**, and cells reading `macro_series_absent`
216 → **44**.

### Two series exist in FRED but not in ALFRED, and are not worked around

`IRLTLT01CNM156N` and `DHHNGSP` answer `The series does not exist in ALFRED but
may exist in FRED`. No request shape fixes it, because `output_type=4` cannot
answer for a series ALFRED does not hold.

The obvious fallback — reissue as a plain observations call — was rejected. The
source's `release_date_policy` is `from_api`, so those rows would carry a NULL
release date, be excluded from every point-in-time read by `known_at`, and count
as collected in the run summary. That is a run reporting success over data no
consumer can use, which is the shape of failure this repository keeps closing.
They are reported as failed targets instead, and `ROADMAP.md` carries the remedy:
deciding a per-series release policy for the two, which is a judgment about their
real publication lag rather than a code change.

### Positioning was measuring the size of the market, not the crowding in it

CFTC positioning has been wired end to end since the grid gained its fifteenth
indicator family: a fetcher, five series, a commodity-engine read, SKILL.md
judgment and Tuesday-to-Friday release alignment. All five contract codes were
re-verified live against the CFTC dataset and are correct.

**But the stored value was a raw contract count, and both consumers rank it as a
percentile of its own multi-year window.** Open interest is not constant over
such a window: WTI's net non-commercial count spans 39,800 to 739,097 across ten
years of the series actually collected, so the percentile ranked how large the
market became as much as how crowded it was — which is not what
`commodities.cot.extreme_percentile` or SKILL.md claim it measures.
`open_interest_all` arrives in the same Socrata row and was being discarded. The
value is now that net divided by open interest, a decimal fraction. A week with
no usable open interest stores `None`, never a fall back to the count.

Migration 015 **deletes** every CFTC row rather than converting one: open
interest was never collected, so the denominator for a stored row does not exist,
and CFTC is free and serves full history, so refetching costs nothing. Because a
percentile taken across a units change is not a percentile of anything,
`MacroSeries.units_known_at` now reports the distinct units inside a window and
both consumers refuse a window carrying more than one. That guard is general: it
is what stops the same mistake being made silently for any series by any later
change.

### A series collected and read by nothing

`COT_USD` was pulled from CFTC on every run and read by nothing — no market
declared it, `fx_engine` holds no positioning input, and no MCP tool serves an
arbitrary macro series. It passed `test_no_unread_config_keys.py` only because a
series id is a value in a list rather than a key. The grid's positioning axis is
per market × sector and a dollar-index reading is a cross-market fact with no
cell to live in, so it is deleted; it belongs back in config the day `fx_engine`
grows an input for it.

That check now covers series as well as keys, which is the same standing rule —
a key nothing reads is a decision that looks made and is not — applied to the one
place it could not see. Applying it found **twenty-six more**: breakevens, credit
spreads, liquidity series, VIX, a second dollar index, duplicate EIA product
stocks, and two OECD series a market already declares for itself.

**All twenty-six were deleted rather than declared as a tolerated gap.** The
first draft of this rule carried them in an allowlist with a reason each, on the
argument that rediscovering the list later would be worse than keeping it. That
argument is weak: `config.yaml` is version-controlled, git carries the list, and
each is a one-line re-add the day something reads it. Carrying dead config so a
later session does not have to think is exactly how a repository fills up with
decisions that look made and are not — which is the failure this whole family of
tests exists to prevent. So the rule has **no allowlist**: a series earns its
place by having a consumer first.

The macro surface went from 81 declared series to 55, every one of which
something reads.

### Two failures that retrying could not fix

The per-target retry caught bare `Exception` and gave a rejected credential, a
missing fixture and a read timeout the same three attempts with backoff.
`PermanentUpstreamError` now raises straight through. **The default stays
"retry"** — anything unclassified keeps the old behaviour, so classifying can
only make the machine faster at failing, never quieter about it. An offline chain
rehearsal went from 3m17s to 1m02s, almost all of it backoff against answers that
could not change.

Separately, marketdata.app permits one IP per account and answers a changed
address with HTTP 403 and a five-minute block. 403 is not retryable, so all
twelve US targets rediscovered the same block independently, every run, forever.
It is now written into `collector_state`, checked before the first call of a run,
and a run inside it reports **degraded**, never `ok`, because it collected
nothing.

A breaker held in memory would have been dead code: the collectors are
`Type=oneshot`, so no two runs share a process and it would never see a second
failure. Anything that has to outlive a run has to be written down.

### Why sparse cells are not filled with a proxy

A recurring suggestion is to fill grid cells lacking an option chain with a
realised-vol proxy under a liquidity penalty, so cross-asset rankings "stay
whole". It is rejected, and the reasons are recorded here because the suggestion
keeps returning.

The leg a sparse cell is missing is **implied** vol, not realised. HV computes
from bars everywhere, so substituting a realised-vol estimator for the IV leg
computes `HV − HV` — estimator disagreement — and under a penalty coefficient,
nothing at all.

The damage would be to the three denominators built precisely to expose thinness.
Proxy cells counted as populated make `ranked_over` climb toward `reachable`, so
the `INSUFFICIENT REACH` label disappears exactly as the view becomes more
synthetic; `coverage_pct` and `family_reach` both inflate, so collection health
reads best when collection is worst. That is the failure the `inapplicable` status
was introduced to end, reintroduced through a different door.

**What is real in the neighbourhood** is that `most_compressed` ranked on
close-to-close realised vol — the noisiest estimator there is — while `bars` has
carried OHLC since migration 001 and nothing but ATR read the highs and lows.
That is a genuine improvement, it was taken as its own phase, and what it found
is the next section.

---

## Reading the whole bar, and what the range estimators are not

Parkinson and Garman-Klass now compute beside the close-to-close figure, as
their own named leaves with their own windows and their own counts, and
`most_compressed` orders on the Parkinson percentile.

**They are not lower-variance readings of the same number. They are estimators
of a different quantity**, and everything else here follows from that. Both
assume the price diffuses continuously through the session, so both measure the
INTRADAY range and see nothing of the overnight gap. Measured on the Session 7
ASML capture — 40 real daily OHLC bars, and the same capture every other figure
in `tests/test_validation_real_data.py` is anchored to — the mean absolute
overnight log move is **0.01602 against 0.01937 intraday**. Four fifths of the
intraday move happens while the market is shut, and neither estimator can see
any of it. So at thirty sessions Parkinson reads **0.4336 against 0.4623**
close-to-close and Garman-Klass **0.4303**, and at thirty-nine the gap widens to
0.4219 against 0.5029.

That is a bias, not noise. It does not average away on a market that closes at
night, which is every market this system tracks. The "five to eight times more
efficient" figure that put this phase on `ROADMAP.md` is a statement about
estimator VARIANCE under an assumption these bars violate; the variance gain is
real and so is the bias, and only one of them was in the argument.

Four rules follow, and each of them is a thing a later session would otherwise
get wrong:

**The ranked view orders on the PERCENTILE, never the level.** A rank of
Parkinson against Parkinson's own history is legitimate, because a bias common
to the whole series largely cancels in the ranking. A level compared against a
close-to-close level is a comparison of two quantities, which is what
`MacroSeries.units_known_at` refuses one layer down and for the same reason.

**`iv_minus_hv`, the option screen and the chain collector's sigma ladder keep
close-to-close, and this is arithmetic rather than caution.** An implied vol
prices the total return variance to expiry, gaps included. Subtracting a
Parkinson HV from it would bias every spread toward "expensive" by the size of
the overnight move — which is the same error, in the same place, as the
vendor-versus-self divergence that reached opposite verdicts on ASML by a factor
of 42. The asymmetry between the grid and the screen is therefore deliberate and
must not be "fixed".

**The estimator is the LEAF NAME, not a provenance column.** `parkinson_vol_30d`
and `realised_vol_30d` cannot be confused in the store, so constraint 3 —
never leave two estimators indistinguishable — is satisfied by construction,
with no migration and no field that could drift out of step with the value
beside it. The rejected alternative was one leaf whose estimator varied per cell
with a `grid.estimator` column recording which: that puts two quantities in one
ranked column, which is the failure above wearing a schema change.

**A refusal is `n/a`, never `inapplicable`.** `bars` has the columns and a route
that filled them would fill these cells, so the gap stays in the
collection-health denominator — the reasoning `instrument_not_seeded` was
introduced under.

### Garman-Klass and the open that is not one

Garman-Klass additionally reads the open, and some venues publish no opening
auction print and serve the previous close in its place. That makes `ln C/O` —
which the derivation expects to be the within-session move — carry the whole
close-to-close return, so the estimator computes a different quantity. The
DIRECTION of the error is not fixed: it depends on whether the restated open
falls inside the range the venue served, which is a fact about that instrument's
gaps. What is fixed is that the number stays positive and entirely plausible.
Nothing in the output betrays it, which is why `data.garman_klass_max_restated_open_share`
is tested BEFORE computing rather than as a sanity check afterwards.

Two things were established while building it, both by measurement rather than
assumption. First, **the ordering invariant is load-bearing**: because
`ohlc_bars` refuses a bar whose open or close falls outside its own high and
low, `|ln C/O| <= ln H/L` and the subtracted term can never exceed the first, so
a well-formed window cannot sum to a negative variance. The `<= 0` guard is a
backstop for a caller assembling its own bars, not the main defence — and the
first draft of this section had that backwards. Second, on a genuinely gapping
instrument a restated open often falls OUTSIDE the served range, so that filter
drops the rows and the leaf refuses for want of usable bars before the share
test is reached: a different reason code for the same defect, and an honest one.

**No fixture in this repository exhibits a restated open.** Every bar fixture and
the ASML capture carry a genuine one — 0 of 39 gaps restate — so the refusal
ships against a constructed series, and `references/ibkr-playbook.md` records the
restated-open case as UNVERIFIED on this route rather than as an observation.
The brief said to check what the fixtures actually contain before assuming an
open is real; they were checked, and the answer was the opposite of the one the
guard was written for.

### What it measured, and the honest bound on it

Against a store seeded from `tests/fixtures/ibkr`: the grid went from 5,772 leaf
cells to 6,396 — four new leaves on all 156 market x sector cells, absent ones
included — and every one of the 57 cells with bars produced all three
estimators, so re-pointing `most_compressed` cost it no reach at all
(`reachable` 57, `ranked_over` 56, unchanged). The narrowest family is still
`implied_vol` at 11 of 156 cells. It did not move, and nothing in this phase
could have moved it.

That last sentence is the point. This phase sharpens a lens; it does not widen
one. Ninety-nine of 156 cells have no instrument, the option families reach
eleven, and the backfill has never run. This was done first because it is the
only open item that needs no credential, not because it was the most valuable.

(The same paragraph also said 216 cells were waiting on a free FRED key. That
was the claim inherited from PR 6, and the section above records why it was
wrong: the key existed and the request was malformed. 172 of those cells are now
populated.)

### The realised-vol percentile is built from overlapping readings

Found while building the above, deliberately NOT fixed here, and recorded so it
is not re-found and quietly patched.

`realised_vol_30d_percentile` ranks today's reading against every 30-day rolling
reading in `data.daily_window_years` — on a full store, roughly 470 of them —
and `n_obs` reports that number. Consecutive readings share 29 of their 30 bars,
so the effective independent sample is nearer sixteen. This file already records
the same defect being found and fixed for the correlation norm, where
`grid.correlation_norm_min_independent` now demands non-overlapping readings
because the standard deviation of an overlapping series measures its own
smoothness. The objection is weaker for a percentile than for a dispersion — a
rank is not a standard deviation — but `n_obs` is still overstating what is
behind the number by a factor of thirty, and window honesty is the one thing
this build does not compromise on.

It is not fixed here because fixing it would move every realised-vol percentile
already in the store, which is precisely the swap this phase was forbidden from
making, and it would move the new leaves and the old one together under one
commit with nothing to compare against. It is on `ROADMAP.md` as its own item.

### One crash found in the same seam

Both `check_position_risk` and `get_portfolio_review` computed the trailing stop
from `max(bars.high)`. `bar_series` drops a row with no CLOSE and keeps one with
no high — a bar with a close still carries a return — so `high` holds `None` in
real stores, and `max` over it raises `TypeError` rather than returning
anything. One NULL high on one instrument failed the whole tool call, for every
position in it. `stats.true_ranges` beside it had always guarded; the same guard
now lives once on `BarSeries.trailing_high` so the two callers cannot diverge
again. Found because this phase is about the very columns that are nullable and
annotated as though they are not.

### One map deleted rather than corrected

`rankings.DIMENSION_INDICATOR_PREFIX` named the leaf each view ranks on and
nothing read it — `build` passes each indicator to `_rank` directly — so it
could disagree with the code indefinitely, and did: it still said
`realised_vol_` after this view was re-pointed. A second statement of a fact that
cannot contradict the first is the same shape as the nineteen keys Phase 3
deleted, so it was deleted rather than updated. The module docstring is the
reader's index and `build` is the authority.

---

## The three corrections, and the one whose premise was wrong

Three items left open after the macro layer was first run with real keys. They
are unrelated to each other, and one of them turned out not to be the thing it
had been recorded as for two sessions.

### `OECD_CLI` deleted, and the SDMX decoder kept

Deleted under the standing rule a series earns its place by having a consumer.
Nothing in `src/` read it, no market declared it, and — the part that makes this
the easiest of the three — **it had never returned a row**. The decoder was
broken from the day it was written, so deleting the series removes no data, and
the OECD fetcher went with it as the only caller of an endpoint nothing now
addresses. `macro.sources.oecd` went too: a source with no series left is a
decision that looks made and is not, which is the same rule one level up.

An outside review offered a different account of the precedent — that `COT_USD`
was deleted for a **contract mismatch**. That is false and worth recording as
false, because it would license keeping `OECD_CLI` on the argument that its
mismatch is fixable. `fx_engine.py` contains no positioning input of any kind;
there was nothing for a dollar-index series to mismatch WITH. It was deleted for
having no consumer, and so is this.

**The SDMX decoder stays**, and so does every test that exercises the 2.0
envelope, the exact `TIME_PERIOD` match and the under-specified-key refusal. The
ECB is thirteen live series on the same format family, and the three shapes the
OECD taught this build are properties of SDMX rather than of that publisher: a
2.0 envelope where the ECB serves 1.0, a content dimension whose id starts with
`TIME`, and a key that addresses several series at once. The tests were
re-pointed at `EcbFetcher`, which shares the decoder, with their assertions
unchanged. Deleting them would have taken the evidence out with the caller.

**What reopening it would cost, stated so the next session does not have to
rediscover it.** The reason `OECD_CLI` is not simply "wired in" is not that
wiring is hard. SKILL.md fixes the indicator set at **fifteen families**, and a
composite leading indicator belongs to none of them: it is not a return, a
volatility, a drawdown, a correlation, an implied vol, a policy rate, a yield
curve, a currency return, positioning, breadth or turnover. Serving it means a
sixteenth family, which is a change to the mandate the skill states — and a
mandate change is a decision for the account holder, not a config edit. Pinning
`REF_AREA` per market, the alternative to deleting, is the small half of that
job and would leave the series collected and still read by nothing.

### The realised-vol percentile is sampled non-overlapping now

Recorded as a defect twice — once for the correlation norm, where it was fixed,
and once here, where it was found while building the range estimators and
deliberately left. `realised_vol_<tenor>d_percentile` ranked today's reading
against every rolling reading in `data.daily_window_years` and reported that
count as `n_obs`, while consecutive readings share all but one of their bars.

The same remedy as the correlation norm, through a **sibling key** rather than
the same one. `grid.realised_vol_percentile_min_independent` is its own floor
because `grid.correlation_norm_min_independent` is documented as what a standard
deviation of correlations needs and this one floors a rank — one key governing
two families is a fact with two owners. It could not reuse
`grid.min_observations.realised_vol` either: that floor is 21 against a ceiling
of sixteen independent readings, so it would have refused every cell for ever.

That key had a second reader, and the change exposed it. `turnover` took
`max(grid.min_observations.realised_vol, data.technical_window_days)`, which at
the configured values is always the second — so a key named for one family was
being read only by another, and inertly. It is renamed
`grid.min_observations.turnover` at the same value: nothing moves, and the name
now says who reads it.

**All three estimators step together**, close-to-close by the tenor and the two
range leaves by the tenor as well. Two percentiles built on different sampling
would be the estimator confusion PR 7 exists to prevent, reintroduced through
the window. The history is also walked BACKWARDS from the newest reading, as
`rolling_correlations` already was: forwards, a stride that does not divide the
history leaves the remainder at the new end, and the window a percentile of
today's reading cannot do without is precisely the newest one.

**What this is.** A window-honesty fix, not a ranking correction, and the
measurement says so. Over two-year windows of real daily price history — WTI and
Henry Hub, twenty-four windows of 504 sessions — the readings behind the number
go from **474 to 16**, while the percentile itself moves by 0.030 on median and
0.083 at worst, and the two orderings correlate at **0.98** with a maximum rank
shift of three places in twenty-four. Every cell carries the same overlap, so
almost all of it cancels in a ranking. What was wrong was `n_obs`, which is a
claim about evidence, and it was overstating it by a factor of about thirty.

**On a young store it removes the leaf outright, and that is the honest answer.**
Against a store seeded from `tests/fixtures/ibkr`, where nearly every instrument
carries the 60-bar generic daily fixture, sixty bars hold **one** independent
30-day close-to-close reading and two range readings. All 57 populated
realised-vol percentiles therefore become `n/a` with the count and the reason,
and `most_compressed` — which orders on the Parkinson percentile — ranks nothing
at all. The LEVEL leaves are untouched: 56 populated before and 56 after, for
all three estimators. Sixty bars never contained a two-year percentile; the
overlap was making thirty readings out of them and the ranked view was ordering
on a number that had no history behind it. Every cell in that view held the
identical value, 5/31, which is what a tie across fifty-six cells reading one
shared fixture looks like — so nothing informative was lost, and what the change
really removes is a view that looked full and was not.

### Two FRED series, two different defects, and neither was the recorded one

`ROADMAP.md`, `docs/DECISIONS.md` and `references/macro-playbook.md` all recorded
that `IRLTLT01CNM156N` and `DHHNGSP` "exist in FRED but not in ALFRED", and that
closing it meant deciding a per-series release policy for them. **All of that
came from reading one error message.** Probed against the API, the two series
fail for unrelated reasons and neither is the one recorded.

`DHHNGSP` **is** in ALFRED, from vintage 2014-12-10, and answers `output_type=4`
normally inside that range. What fails is a real-time window containing no
vintage date at all, which ALFRED reports as `The series does not exist in ALFRED
but may exist in FRED` — a message about the series for a fact about the window.
`DHHNGSP` is in the `commodity_history` group, so the FIRST chunk of its
twenty-year window ended in 2014-08, four months short of ALFRED's first vintage,
and the target failed on it. `DCOILWTICO` and `DCOILBRENTEU` are chunked
identically and survive that same chunk only because their own first vintage,
2011-04-06, falls inside it — which is luck, not design. It is the same failure
as the `today to today` default the previous phase fixed, at the other end of the
history.

`IRLTLT01CNM156N` is not in FRED at all. `fred/series` answers `The series does
not exist`, and a search returns nothing for a China long-term government bond
yield. The other nine `IRLTLT01*` series are alive, so this is one series retired
rather than the family.

**So the remedy that was planned is not the remedy.** The plan of record was to
declare `reference_plus_lag` on both with a researched lag and take the plain
observations route for them. Neither half survives the evidence: no lag brings
back a series FRED does not serve, and `DHHNGSP` does not need one because
`from_api` works for it. It would also have been the worse answer where it could
have been applied — `DHHNGSP` publishes on a WEEKLY vintage cadence while
carrying daily observations, so one fixed `release_lag_days` would be wrong for
four days in five, and adopting it would have discarded the real
first-publication dates ALFRED holds for the 11.7 years it does cover. The
guardrail on the plan is what rejected the plan: a lag must be sourced, and the
source says no lag is needed.

What was done instead: `FredFetcher` asks ALFRED where its vintages are, on
`fred/series/vintagedates`, and intersects the requested window with the answer.
Two requests per series, each asking for one date, because a daily series carries
thousands of them and reading only the ends is bounded whatever the series turns
out to be. The bound is then **declared by ALFRED** rather than recovered by
watching a request fail, which is the distinction the whole macro layer turns on
— and unlike a date typed into config it cannot go stale against a series added
later. Clamping is a no-op wherever the window was already answerable: verified
by probing `SOFR` and `PCOPPUSDM` clamped, half-clamped and unclamped, and by
`DCOILWTICO` returning the identical 3,858 rows before and after.

`IRLTLT01CNM156N` is deleted and `china` declares no `long_rate_series`, which
`indicators.yield_curve` already turns into an `inapplicable` cell naming the
missing declaration. A dead id left in config is a collection failure every run;
a declared null is a gap somebody decided.

**The fallback stays rejected**, and is now asserted rather than described:
`tests/test_macro_pull.py` pins that a series ALFRED holds no vintages for fails
and that no observations call is made. Those rows would carry a NULL release
date under a `from_api` policy, be excluded from every point-in-time read by
`known_at`, and still count as collected.

**What is genuinely lost is ALFRED's, not the request's.** `output_type=4` serves
nothing before a series' SECOND vintage, so `DHHNGSP`'s earliest available
observation is 2014-12-09 and the fourteen years before it carry no
first-publication date anywhere in ALFRED. The twenty-year window
`commodities.history.monthly_window_years` asks for is short by that much. It is
reported as the shortfall it is, and not filled with a synthetic date.

Measured live, same day, before and after: **52 of 55 macro targets collected →
53 of 53**, no failed target, 59,165 rows.

**The lesson, which is the same one this file recorded two sections ago.** A
status was inferred from an error message rather than read from a probe, and it
stood in three documents for two sessions while both series were fixable — one
by a request change, the other only by deletion. The FRED key had been in the
environment the whole time. Reading the message was cheaper than asking the API,
and it cost more.

---

## The grid says which way it is moving

### A ranked row was a level, and the trajectory was already in the store

A percentile is a level. "94th percentile" does not say whether the reading is
still building or already unwinding, and those are opposite trades behind one
number: crowding, compression and extension are all conditions a move happens
FROM, while the same reading on the way back down is the move happening.

Nothing had to be computed to fix this. The `grid` table already held, for the
same market x sector and the same `as_of`, returns at five horizons in local and
base currency, realised vol at every configured tenor with the slope between the
ends, the maximum drawdown, and the distance from both extremes. What was missing
was that the four ranked views — the tables a session actually reads instead of
the raw cells — carried one value and a map of booleans, so reading a trajectory
meant a `get_grid_cell` call per ranked row. That is the raw-table reading the
ranked views exist to replace.

`Rankings._cells_for` already read every measured leaf of the ranked cells in ONE
query to build the conditions map. The change is that the same read now also
fills a chosen set of leaves onto the row, and migration 016 gives them a column
so they survive into what the server reads back.

**Chosen, and declared in one place.** A cell holds forty-odd leaves — the count
is derived from config and moves whenever a tenor or window list does, and it was
thirty-seven when this was written, 44 after the skew leaf and 47 after the
implied term structure. A row carrying all of them is the raw table again, so
`TRAJECTORY_LEAVES`
names ten — returns at a short and a medium horizon in local and base currency,
the realised-vol term slope, both 52-week distances, the long-run drawdown, and
the positioning level — with a comment saying what question each answers, and the
positioning change windows appended from `commodities.cot.change_windows_weeks`.
Both currencies at both horizons rather than one of each: SKILL.md requires every
market's return in local AND base wherever it is reported, and a trajectory read
off the wrong one is a trajectory the account never had.

**Every leaf carries its own `status`, `window` and `units`.** A cell is fed by
four stores on four cadences, so a fresh percentile beside a stale return is an
ordinary row — and one that cannot say which is which reads as healthier than the
store is. That is precisely what migration 012 added `status` for, one column
over, and this applies the same rule to every value travelling beside the ranked
one. A leaf the store could not measure is ABSENT from the map rather than
carried as zero or as a flat trajectory: zero is a reading.

**The leaf a row ranks on is not repeated in its own trajectory.** It is the
row's `value`; one number under two names invites a reader to look for a
difference that cannot exist.

**The cost against the row cap was measured, not assumed.** `server.max_rows_per_response`
caps ROWS, so widening a row spends the response rather than the cap — but it is
still spent. Measured on the fixture-seeded store, encoded as the tool returns
it: a `most_extended` view of twenty rows goes from **9.8 KB to 20.9 KB**, which
is 500 to 1,069 bytes a row against four trajectory leaves present. A row
carrying every declared leaf is **1,736 bytes**, so the worst case — four full
views at `data.grid_ranking_top_n`, every leaf measured on every cell — is about
**136 KB against 39 KB**. Two things make that the right trade. The alternative
was twenty `get_grid_cell` round trips per view to answer the first question
anybody asks of a ranked row, and each of those returns the whole
cell. And a caller who wants the old size still has both
dials: `dimension` for one view, `top_n` for fewer rows.

### Positioning velocity: the one reading that genuinely did not exist

Family 15 emitted `positioning_percentile` and nothing else, so a crowded book
still being built and one being dumped were the same number — which is the whole
distinction a positioning measure exists to make. It now emits a change leaf per
window in `commodities.cot.change_windows_weeks`, inside family 15: the indicator
set is fifteen families and a test asserts it.

**Measured on the share of open interest, not on the percentile.** The quantity
is the one migration 015 established, and differencing the RANK instead would
compare a past reading against today's distribution and call the result a flow.
The consequence is that the level and the change are not in the same units — a
percentile and share-of-open-interest points — and the leaf names, units and
SKILL.md all say so, because two quantities under one family is exactly how a
number gets read as something it is not.

**Weeks, and both endpoints release-aligned.** CFTC reports Tuesday's positions
on the following Friday. A window in days would span a different number of
reports depending on which weekday the grid was built on, and reading a report on
its reference Tuesday would put it in the change three days before the market saw
it. The older endpoint is the newest report published on or before the window's
start, so the span is never shorter than the label claims; where a publication gap
makes it longer, the label carries both release dates and `n_obs` carries the
count.

**A units change refuses the change as well as the percentile**, and for a
stronger reason than it refuses the rank: a difference across that boundary
subtracts a share from a contract count and reports the contract count.

### No `regime` label, and no trajectory boolean either

An outside review proposed a synthesised `regime` string per row. Refused, on the
rule this file has already recorded twice: a composite nobody can reproduce is
what the ranked views exist to avoid, and a one-word label is a composite with
prose in front of it.

The second half of that decision is less obvious and was nearly got wrong.
`conditions` was the sanctioned place to name a trajectory verdict — "volatility
still contracting", against a flat band around `rv_term_slope` — and it was NOT
added. Two reasons, either sufficient:

- `RankingRow.meets_full_definition` is the conjunction of every entry in the
  map. SKILL.md's *coiled* is four conditions about a LEVEL, so a trajectory
  condition dropped into the same map would silently redefine it: a cell that is
  coiled and merely no longer tightening would start reporting `False`, and the
  view would be answering a question the mandate never asked. Splitting the map
  into definition and trajectory halves needs a second declaration that has to
  stay in step with `_conditions` — which is exactly what `DIMENSION_INDICATOR_PREFIX`
  was, and it was deleted for drifting.
- The threshold would have been invented. `_conditions` already declines to
  assert SKILL.md's "parabolic trailing return" because no config key defines
  parabolic, and inventing a flat band for the term slope is the same mistake
  with a different name.

What replaces it is strictly more informative and needs no threshold: the slope
itself, with its own window and units, and SKILL.md's rule for reading it.

---

## The credit tier is a date comparison, and it was one parameter too few

**`--as-of` exists because a request's price on marketdata.app is decided by a
date comparison, and the CLI could not move either side of it.**

`VendorChainCollector._collect_one` derives `historical = trade_date != today`,
and that boolean is the whole difference between a request that carries `date=`
— billed at the documented 1 credit per 1000 option symbols — and one that does
not, billed **per option symbol**. Measured: 608 contracts for 1 credit dated,
against 3 credits for 3 contracts live. Three orders of magnitude on a liquid
name.

`run()` took a single `today` and used it for two jobs: the end of the session
walk, and that comparison. So the newest session in any walk was by construction
equal to the reference and therefore live-tier, and the CLI passed nothing at
all. Consequences, both real:

- **Buying one past session was impossible.** `--backfill-chains` reaches the
  historical tier for sessions two onward, but its first call is always today on
  the live tier, and `option_vendor.max_credits_per_run` is sized for exactly one
  liquid name there — so a walk could exhaust its budget before reaching the
  cheap part.
- **The question gating the whole backfill could not be asked.**
  `docs/NEXT-SESSIONS.md` opened with "settle whether HISTORICAL chains carry
  `iv` and greeks" and then listed `vendor-chain --only SPY` as the step that
  inspects a chain. That command never makes a historical request. The runbook
  asked for evidence its own steps could not produce.

**The fix splits the two dates rather than adding a mode.** `today` keeps its
meaning — the session this run considers current, the tier reference, a seam a
test pins — and `as_of` names the newest session to collect. A rejected
alternative was a `--historical` boolean forcing `date=`: it would have let the
current session be requested as history, which is a request for an end-of-day
chain that does not exist yet, and the vendor's answer to that is not something
this build should be guessing at.

### The first version of the fix was wrong in the direction that costs money

Worth recording because it parses, runs, and reports success. The first attempt
passed the pinned date to `run(today=...)`, the parameter that already existed.
That sets **both** ends of the comparison, so the pinned session equals the
reference and is called current again: an undated, live-tier request, whose rows
are then written under the pinned date. The store would hold **today's quotes
filed under a day they never described**, bought at the expensive price, with a
run status of `ok`.

Nothing about the run says anything is wrong. The row count is right, the date
column is the date that was asked for, and the only witness is a credit balance
nobody reads and a chain that quietly does not match its own session. So the
guard is a test that asserts the outgoing request carries `date=` and that
`chain_snapshots.snapshot_date` matches the session requested — not a comment,
because the failure is invisible at every level above the wire.

A future session is refused outright, in the collector and again in the CLI. It
is the same bug with a date that makes it obvious: the walk would call it
current, buy today's chain, and file it under a day that has not happened.

## An "offline" recipe that spends money is not a documentation nit

**Three documents told an operator to run a command they were told touches no
network, and with the environment the same documents ask for, it bills.**

`docs/COLLECTORS.md` has said for some time that `vendor-chain` builds its own
marketdata.app client whatever `--source` says, and that a `daily` run without
`--skip vendor-chain`, with `MARKETDATA_API_TOKEN` set, reaches the network and
spends credits. Meanwhile `docs/DEPLOY.md` §6f (then `docs/ORACLE-CLOUD.md`), `README.md` and
`docs/COMPUTE.md` each gave the smoke-test recipe **without** the skip, and §6f
introduced it with "This touches no network and needs no IBKR account".

The bring-up sequence makes the two collide rather than merely disagree: the
operator sets `MARKETDATA_API_TOKEN` in one step and is told to prove the
pipeline offline in another. `--only SPY` reads like a safety bound and is not —
SPY is one of the twelve US names that step collects.

This is the *one owner per fact* rule failing in the direction it is meant to
catch. The fix is not to repeat the warning in three more places, which is how
four copies drift into four versions: each recipe carries the flag, and each
names `docs/COLLECTORS.md` as the owner of why.

**What it does not do is make `--source fixture` mean "no network".** That was
considered and rejected. A flag that silently disables a step is a flag that
makes a scheduled `daily` run behave differently from a manual one for reasons
nobody sees in the log, and the vendor step is the one whose absence is
indistinguishable from an outage. The step stays explicit and the operator stays
told.

## The dividend yield is collected now, and the IV is solved in the right layer

### `cp_api` owns the dividend yield; the connector cross-checks it

`universe.dividend_yield` was **declared, not collected**, and migration 009
argued the case at length: there is no free global dividend feed, and a yield
guessed from a trailing payment is a number nobody could audit later. The first
half of that premise is no longer true — IBKR serves a dividend yield on a plain
snapshot of the underlying, on the quote the chain collector already fetches to
size its strike band.

**Which route owns the column is the decision, and it is not the one that found
it first.** The MCP connector serves the figure and cannot own it: this build
cannot mint the OAuth 2.0 bearer token the Custom Connector takes, so a column the
connector owned could only be refreshed by a human in a chat session. `CLAUDE.md`
forbids exactly that for collection — collectors run unattended, on a schedule,
with no human present. A column whose only refresh path needs a person is a column
that goes stale the first week nobody runs a session, and a stale yield is
indistinguishable from a current one once it is in the store.

So `cp_api` owns it, and the connector is the **cross-check**. The field id is
declared `null` until a live CP snapshot resolves it, which drops it from the
request and leaves the column NULL — the same treatment `implied_vol_percentile`
and `market_data_status` already get, and the same refusal to guess an id.

### The collected yield is dated, and the declaration stays

Both facts exist and they are different. `universe.yaml` says which instruments
need a yield; `underlying_snapshots.dividend_yield` says what one was measured at
on a session.

Putting the collected value on the `universe` row instead would have been simpler
and wrong. `universe` is current-state, one row per contract, and a grid rebuilt
for a past session would then reprice that session with today's yield — the same
point-in-time failure the rest of this build spent a phase closing. A yield is a
fact about a date, which is the argument 009 already made for
`dividend_yield_as_of`; this applies it to the collected figure too.

Precedence, wherever a yield is read: **caller → collected → declared →
none-declared**, and every path reports which of the four it used.
`check_position_risk` already reported three of them; the entry guard read only
the declaration and now reads the same four, because a gate that admits a position
and an assessment that later judges it must not price the identical contract
differently.

### The trailing yield is an approximation, and says so in the schema

Black-Scholes-Merton wants a **continuous** yield q over the option's life. IBKR
serves a **trailing** one. On broad ETFs at 90-200 DTE that is defensible, and it
is stored as an approximation rather than passed off as q:
`underlying_snapshots.dividend_yield_basis` carries `trailing_12m` beside every
collected figure. A continuous yield, if one is ever sourced, is a different value
of that column and not a silent reuse of the same one.

Two measured facts sit under this, both `[MCP ✓ 2026-09-09]` and both in
`references/ibkr-playbook.md`: the yield is served as a **percent** while
`historical-vol` in the same response is a **fraction**, so the unit is configured
rather than inferred from the `_pct` suffix; and the precision is **two** decimal
places, not the one the connector's own tool description claims.

### Solving IV is a compute-layer phase, and the collect-layer decision stands

`references/marketdata-playbook.md` recorded that solving IV from the mid price
"needs a risk-free rate at each contract's own maturity and a dividend yield" and
that it is "a compute-layer phase, not a collector change". That decision is
**scoped to the collect layer and is not overturned here.** A collector still
holds neither input, still writes `iv NULL`, and still reports `degraded` past
`option_vendor.max_null_iv_fraction`. Nothing in this change touches it.

The compute layer holds both — the rate from the macro store at the session's own
vintage, the yield from `underlying_snapshots` — so the objection that closed the
question there does not apply here.

### The solved value gets its own column, not a tag on the vendor's

`iv_percentile` ranks today's implied vol against its own history. A series whose
early half is solved and whose recent half is exchange-quoted has a **methodology
break at the join**, and the percentile reports a regime shift that did not
happen.

A single column plus a `source` tag would have made that break one forgotten
`WHERE` clause away, in a reader nobody has written yet. `implied_vol_solved` is
separate from `implied_vol` so the mixing is impossible rather than merely
discouraged: `_self_iv_percentile` reads `implied_vol` and only `implied_vol`, and
`underlying_snapshot_count` — which decides when the self-computed percentile
switches on at all — counts only rows with a non-null `implied_vol`. Two
independent mechanisms, both pinned by tests, because one of them is a filter and
filters get relaxed.

### An exchange quote outranks an inversion, by default and by declaration

`_atm_implied_vol` preferred the chain over the vendor snapshot, decided by which
branch happened to run first. That was harmless while both were quotes. It stops
being harmless once a solved number exists, so the order is read from
`data.atm_iv_prefer` and the default is `vendor`: a figure an exchange quoted
outranks one this repository inverted out of a mid price through a European model.
`chain_first` exists only so the old implicit ordering stays expressible.

### BSM is European, these instruments are not, and the label travels

Nothing in this repository handles early exercise — verified by grep, and still
true. SPY and the sector ETFs are American-style. The response is to bound where
the number is produced and to label it everywhere it goes, not to quietly invert
an American option through a European model:

- the solve runs only in `numerics.implied_vol.solve`'s `[min_dte, max_dte]` band
  and inside `moneyness_band` of spot, where the early-exercise premium is small;
- every value written carries `solved_bsm_european/q_<tier>` in
  `implied_vol_solved_source`, so the model AND the dividend tier that fed it are
  both readable from the row;
- the grid cell's own provenance says it in words — *"Black-Scholes-Merton prices
  a EUROPEAN option and this underlying may be American-style, so early exercise
  is unmodelled here"* — because a cell reading `0.207` is indistinguishable from
  a quoted one unless the row beside it says otherwise.

Labelling is not a substitute for the bound, and the bound is not a substitute for
the label. Both, or the number is false precision.

### Non-convergence is NULL, counted, and degrades the run

`ImpliedVolResult` already refused to present a non-converged sigma as an implied
volatility. `numerics.implied_vol.solve.max_non_converged_fraction` carries that
to the run level: past it the step reports **degraded**, with the count and the
solver's own `reason_counts()` in the reason — mirroring what the vendor collector
does with the vendor's own nulls. A run with nothing to attempt is **not**
degraded: an empty store is a young store, not a broken run.

### The rate had to become point-in-time, and it was not

`cached_risk_free_rate` reads `ORDER BY reference_date DESC` with no upper bound.
That is right for "what is the rate now" and wrong for anything repricing a past
session, and `BUILD-BRIEF.md` says every store read is bounded at `as_of` — this
one was not. `risk_free_rate_as_of` bounds **both** dates, and they answer
different questions: `reference_date` is the day the rate applies to,
`release_date` the day it became public. A figure for the 1st published on the 5th
did not exist on the 2nd.

Measured on the store this session built, solving 2026-09-02: the bounded read
returns DTB3 for reference date **2026-09-01 at 3.78%**, correctly excluding the
2026-09-02 row whose `release_date` is 2026-09-03; the unbounded read returns
**2026-09-08 at 3.80%**, a rate from six days after the session being solved.

A session with no rate knowable on it **fails** rather than defaulting:
`macro.risk_free_rate.on_failure` is `error` and explicitly never a literal
fallback, and a substituted rate would travel straight into the answer.

### Two configured risk-free rates were never collected

Found while wiring the above, and it had nothing to do with it. `DTB3` (USD) and
`ECBESTRVOLWGTTRMDMNRT` (EUR) are named by
`macro.risk_free_rate.series_by_currency` and neither was declared under
`macro.series`, so no collector ever asked for them. A macro run reported **53 of
53 targets ok** and wrote **59,173 rows**, and the store still held no USD or EUR
risk-free rate.

Nothing errored, which is why it survived. `cached_risk_free_rate` returned `None`,
every option gate needing a rate reported itself **untested**, and an untested gate
on a young store looks exactly like an untested gate on a store missing the one
series it needed. Only GBP resolved, because `IUDSOIA` happens to be declared for
indicator 12's policy rate.

Both are declared now — 55 targets, `DTB3` 2,493 rows and
`ECBESTRVOLWGTTRMDMNRT` 1,196 rows on the first run — and
`tests/test_macro_series.py` pins that every series
`series_by_currency` names is one `macro.series` collects. It is the same class as
the orphan-config-key failure `test_no_unread_config_keys` exists for, one level
along: there a key nothing reads, here a series nothing pulls.

### The compute unit could not import the rate reader, and that was correct

`obliskiq-compute` runs with `PrivateNetwork=true`, and
`tests/test_compute_isolation.py` refuses an import graph that so much as **names**
an HTTP client. `macro/rates.py` imports `httpx` because fetching is its job, so
the solve could not read a stored rate from there — and the package `__init__`
re-exported the provider eagerly, which put `httpx` in the graph through the
package door even for the network-free modules.

The store-reading half moved to `macro/series.py`, which names no client, and the
package re-exports lazily via PEP 562. `rates.py` imports the moved names back and
lists them in `__all__`, so there is still one definition of each and every
existing caller's import keeps working. The test was right and the design was
right; what was wrong was which module the functions sat in.

## What the first live Client Portal run settled (Phase 4, 2026-09-10)

**A route built entirely against fixtures was wrong in four places, and every one
of them was green in the test suite.** That is the decision-relevant fact, not the
individual bugs. Each defect lived in the gap between what a fixture author
believed the wire format was and what it is, and each failed by returning a
*plausible empty answer* rather than an error:

* a search sent as query parameters instead of a JSON body → `HTTP 200` with
  `sections: []`, indistinguishable from an instrument with no listed options;
* an exchange list split on the wrong separator → one malformed exchange, `410`;
* a missing session precondition → a `500` naming a *different* endpoint
  (`/accounts`) than the one that satisfies it;
* an abbreviated numeric (`"53.4K"`) → a NULL column, on liquid names only.

**The rule this justifies: a fixture is evidence about our parsing, never about
their format.** The two tests added here pin the *encoding* of a request and the
*recovery* from a lost precondition — both things a response fixture cannot
express. Where a fixture and a live reading disagree, the live reading wins and
the fixture is corrected; `references/ibkr-playbook.md` carries the measurements.

### Why the dividend yield stays null after a session that set out to fill it

Phase 3 Part B decided `cp_api` owns the dividend-yield column and the connector
cross-checks it. Phase 4 tried to resolve the field id and found there is nothing
to resolve: `7286`, `7287` and `7288` are never returned on this account, while
their documented neighbours `7292`–`7295` are. The ids are right; the data is
withheld.

That leaves one route serving the number and one column meant to hold it, which
is exactly the situation the cross-check rule was written to refuse. **A figure
from a single route does not earn the right to price with it**, and the connector
cannot be a collection path because it needs a human in a chat session — which
`CLAUDE.md` forbids. So the column stays NULL, `_dividend_yield_for` resolves
`q_none_declared`, and the solve says so in its provenance rather than quietly
using a number nothing corroborates. **The alternative — collecting the
connector's figure into a cp_api-owned column — was rejected**: it would make the
column's declared owner a fiction and hide a human-present dependency inside a
scheduled job.

### Why the /accounts precondition is repaired on the failure, not just at connect

`/iserver/accounts` is documented as a once-per-session precondition. It is not:
a run armed correctly at connect still had every later snapshot refused, and
calling both accounts endpoints again cleared nothing across nine attempts while a
fresh process served immediately. Re-running `ssodh/init` restores it.

Repairing it **on the refusal** rather than only at connect is deliberate. The
alternative — re-initialising periodically — spends a session-establishing call on
a schedule that has no relationship to when the session actually drops, and would
still miss a drop that happens between ticks. Matching on the error body is
narrow, self-limiting (one repair per call, guarded against recursion) and costs
nothing when the precondition is holding, which is almost always.

### Why the Part C probe stopped at the block instead of waiting it out

The session was asked to sleep 300 seconds and retry on a marketdata.app lockout.
It did not, and the reason is in the block message: the ban names an
`authorizedIP` and a `blockedIP` one address apart, and it is **account-wide**.
Retrying from a sandbox whose egress rotates per request does not eventually
succeed — it re-blocks, and each re-block locks out the real collection host too.
Two credits bought a real answer (XLP's 404 does not generalise); a third call
bought a cooldown. Stopping and reporting is what `option_vendor.ip_block_cooldown_seconds`
already encodes, and overriding it by hand would have been the collector's own
recorded lesson ignored.

## The European chains, and a census that can run itself (Phase 5, 2026-09-10)

### A section describing one instrument was used to query another

The defect underneath all three European failures is one sentence long: **the OPT
months and exchange list came from a SYMBOL search, and the strikes were requested
against the CONID the universe holds.** Nothing checked that those were the same
instrument, and for a dual-listed share they routinely are not.

That is why it presented as three unrelated problems. `ASML` collected 296
strikes, which looked like success. `SAN` and `TTE` collected nothing, which
looked like an entitlement gap. And the diagnosis on file — "their US options sit
under a different conid" — was true for one of the three and backwards for the
other two: `SAN` and `TTE` already held the Madrid and Paris conids, and it was
`ASML`'s universe row, declaring `AEB` and `EUR`, that carried a NASDAQ conid.

The fix keys the section on the conid. A search returning no row for it yields no
months at all, rather than some other listing's — because the alternative to a
failure here is a chain collected under the wrong underlying, in the wrong
currency, with the wrong multiplier, in a table whose implied-vol percentiles are
built per underlying across time. **A methodology break inside one series is worse
than a gap in it**, which is the same argument that gave `implied_vol_solved` its
own column rather than a tag.

### Why the declared venue is not validated against the list the search returns

It is the natural instinct and it is exactly wrong. The exchange list is a
property of one listing; the whole failure is that it can describe a different
one. Validating a declared `EUREX` against a list reading `SMART;AMEX;BATS;...`
would reject the correct answer in favour of the row that caused the bug.

So `option_exchange` in `universe.yaml` is sent verbatim, the disagreement is
logged, and a venue that serves no strike **fails the target**. The alternative —
falling back to whatever the search offered — is precisely the behaviour that
collected a New York chain under an Amsterdam row for a session.

The declarations are IBKR's own `current_exchange` per conid rather than a
preference of ours: `FTA` for ASML, `EUREX` for SAN, `MONEP` for TTE. That is the
venue whose quote a snapshot returns, and picking anything else would mean
collecting a book we are not being priced on.

### The European listing, not the US one recorded honestly

The phase could have relabelled `ASML` as the US listing it was actually
collecting. It was rejected once the measurement came back: the European chains
ARE reachable — EUREX and MEFFRV for SAN, EUREX and MONEP for TTE, EUREX and FTA
for ASML — so the honest-relabel option trades a reachable series for a
convenient one. The three European listings are also the only chains in this
package that no amount of money recovers, which is the whole reason they are
collected by the irreplaceable step rather than bought from a vendor.

### Zero rows was a success, and that is what made it invisible

`SAN` and `TTE` reported `ok`. The months resolved, the ladder came back empty on
every one of them, and the run summary counted two more healthy targets — so the
single channel that would ever have raised the alarm said the opposite.

A target that resolves no option contract now fails, and **neither half is
written**: not the chain rows and not the underlying snapshot. The two commit
together so that a snapshot with no chain against it cannot exist, and that
half-state is indistinguishable from a chain that legitimately returned nothing —
while `underlying_snapshots.implied_vol` feeds the self-computed IV percentile, so
it does not merely look untidy.

The cost is stated rather than hidden: on a target that fails this way, the
underlying snapshot is lost for the day too. That is the right trade for a series
where "collected" appearing in a log is the only alarm there is.

### Correcting a conid retires the row it replaces, and deletes nothing

Re-pointing `ASML` would otherwise leave the NASDAQ row in `universe` with its
collection flags set, so both listings would be walked under one symbol forever.
Deleting it is not available: `bars`, `chain_snapshots` and `curve_snapshots`
cascade off `universe`, and the 192 rows already collected against that conid are
real data honestly collected.

The rule is narrow on purpose — a declared row is retired only when **another
declared row with the same symbol resolved to a different conid in this run**. The
loader already refuses a duplicate symbol among declared instruments, so the match
is unambiguous; and a symbol that failed to resolve is not in the resolved set at
all, so an upstream outage retires nothing. The alternative considered — a
one-off migration naming the conid — was rejected as immutable and specific to one
mistake, where this catches the next one too.

### The NAV is recorded twice, on two cadences, by one writer

`nav_history` had no writer. The scheduled census was therefore correct and
useless: it refuses without a NAV, because the budget defines the universe, and
nothing supplied one — so a weekly timer was set up to report the same refusal
forever.

Two readers want different things. The **census** wants the latest observation and
runs weekly; the **drawdown breaker** measures across a rolling month and refuses
below `portfolio_circuit_breaker.min_observations` readings inside it, which a
weekly write clears only barely. So the weekly `universe` sync records one — it
already resolves the account for the portfolio union and runs an hour ahead of
`compute@census` — and the weekday sequence records one through a `nav` step. One
upsert, in `store/nav.py`, called by both: `RiskEngine.record_nav` had to move out
of `compute/` because the collect layer cannot import it, and two upserts would
eventually have disagreed about which currency the figure was in.

`nav` leads `DAILY_SEQUENCE` under that sequence's own rule. `/portfolio/{id}/ledger`
serves today's figure and no other, so a day not run is an observation nobody can
ask for again — and it is a single call, which cannot meaningfully delay the step
behind it.

**A NAV whose currency is not `account.base_currency` is refused, not converted.**
It would not fail; it would mis-size the sleeve by the exchange rate with nothing
visible. `option_ledger` already refuses an amount it cannot convert, and this is
the same direction.

### `HTTP 500` for "no data", and why the body decides it

This route answers `"Chart data unavailable"` and `"Contract details are not
available"` with a bare 500, which is in `runtime.http.retry_status_codes`. A
permanently absent target therefore cost three HTTP attempts and two further
per-target attempts — nine calls for an answer that could not change, and fifteen
of 167 backfill targets were in that state.

Matched on the body, exactly as `_ACCOUNTS_PRECONDITION` is, and raised as
`PermanentUpstreamError` so the per-target loop passes it straight through.
**Reading the status as permanent was rejected**: every genuine upstream fault
uses the same 500, and treating those as final would turn a real outage into a
target the collector abandons after one try. The markers are a module constant
rather than a config key, for the same reason the accounts precondition is — they
are observed API strings owned by the playbook, not thresholds.

### QQQ's 429: one failure is not a pattern, and pacing was left alone

QQQ was the single chain failure of twenty-four on 2026-09-10, on an `HTTP 429`.
`collectors.request_spacing_seconds` was **not** changed. The HTTP layer already
honours an explicit `Retry-After` up to `runtime.http.retry_after_max_seconds` —
deliberately a much higher ceiling than the ordinary backoff, because clamping a
rate limit is how a limit becomes a ban — and it absorbed every other 429 in that
run.

Retuning a threshold on n=1 is the move this project refuses everywhere else: it
would look like a decision and would not be one. What would make it a pattern is
more than one target losing a chain to 429 in a single run, or the same target
losing it across runs; either is visible in `collector_runs` without new
instrumentation.

## What the European chains looked like on CP (Phase 6, 2026-09-10)

### The conid-keyed section was the whole fix, and the evidence says so

Phase 5 keyed the `OPT` section on the conid the universe holds and could not test
it on `cp_api`. Measured now: `secdef/search` returns an `OPT` section for all
three European conids, on `EUREX`/`FTA`, `EUREX`/`MEFFRV` and `EUREX`/`MONEP` —
identical to the connector, and with the US listing sitting in the same response
carrying `SMART;AMEX;BATS;…` and **ranked ahead of the European line in all three
cases**.

That last detail is what closes the question. The old code took the first match
with options, and the first match with options is the US one for every one of
these symbols — so the failure was never selective and was never about
entitlements. Nothing was missing from the response; the wrong row was being read
out of it. `references/ibkr-playbook.md` has the tables.

**No fallback was added, and none is warranted.** Every declared venue served
strikes on the first try.

### `history_exchange: null` for SAN and TTE, probed before declared

The Phase 5 prompt named this the one open hypothesis and said to probe it live
rather than declare it on the TSEJ precedent. Probed, both ways, per conid:
`exchange=BM` and `exchange=SBF` each answer HTTP 500 `"Chart data unavailable"`,
and the same conids with the parameter **omitted** serve daily bars.

So the declaration is measured. Two things follow that are worth more than the
declaration itself:

* **`"Chart data unavailable"` does not mean "no history exists."** It is what
  this route says when it will not serve that contract on the exchange asked for.
  Phase 4 read it as a statement about the data and it is a statement about the
  request — the same category error as the `secdef/search` exchange list.
* **The inversion is not Japanese.** TSEJ was the only instance, which made it
  look like a quirk of one venue; Madrid and Paris behave the same way, so it is a
  property of the history endpoint.

**ASML declares nothing**, because it answers on `AEB` and with the exchange
omitted alike. Its bars were a conid problem and the conid fixed them. Declaring
`history_exchange: null` there too would have been a change with no measurement
behind it, on a row that already works.

### The declared listing venue now breaks the tie, because the ticker cannot

`resolve_conid`'s docstring promised a protection it could not deliver: *"scan for
an exact match first"*, against a search that *"routinely returns a foreign
listing ahead of the one asked for"*. Measured, the rule is a **no-op for exactly
the case it names** — every listing of one company answers the same bare ticker,
so all rows match, the first wins, and the first is the US line for all three
European names. That is not a ranking that happened to go wrong; it is a rule with
nothing to discriminate on.

The venue was on the universe row the whole time. `ASML` declared `exchange: AEB`
while holding a NASDAQ conid, and `docs/DECISIONS.md` and the playbook both
recorded that the declaration *"never entered the decision"* — as the cause, and
without fixing it. It enters now: among rows matching the ticker, one whose
`description` is the declared venue wins.

Three properties, each deliberate:

* **A tiebreak, not a filter.** A declared venue matching no row falls through to
  the previous behaviour. It can sharpen a resolution and can never turn a working
  row into a failure, so no existing row's conid is at risk.
* **The ticker rule still binds.** A row on the right exchange under a different
  ticker is a different company, and the venue never overrides that.
* **A pinned `conid:` still wins outright and issues no search.** This does not
  replace the pins Phase 5 added; those three rows never reach the search at all.

That row turned out to exist, and it was not hypothetical. Applying both rules to
one search per declared row put **19 of 74** on a different conid, and eighteen of
those were already pinned — the tiebreak returning the pinned value in every case,
which is a check on the pins taken from the search rather than from the file. The
nineteenth was `EEM`, unpinned, resolving at that hour to `690179584`: **"Micro Ether Euro", an
`IND` on CME denominated in EUR**, under a row declaring an ARCA USD
emerging-markets ETF with both collection flags set.

**Read on a day's delay, that finding gained a sharper edge and lost a claim.**
The Phase 4 store — opened 2026-09-11 — holds `EEM` at `6604766`, the correct
conid, with no row against `690179584`. Phase 6 had asserted it held the crypto
future's bars; it does not, and the assertion was an inference from the 20:05
reading stated as a fact about a store nobody had opened.

What the store does show is worse than a one-off. It holds ASML wrong and SAN,
TTE and EEM right — exactly what Phase 5 recorded — while the 20:05 sweep put all
four on the wrong line. Same account, same day, opposite answers: `secdef/search`
relevance is **time-varying**. An unpinned row therefore does not fail
consistently, which means it passes every test anybody runs and mis-resolves on
the run nobody reads. That is the argument for a deterministic tiebreak, and it is
stronger than the one this section was originally written on.

So the failure mode this route actually offers is not "a foreign listing of the
right company" — that is the benign case. It is an unrelated instrument sharing a
ticker, and nothing about the row's own declaration was consulted before this.
`EEM` is pinned now, because a row found wrong gets a declaration; the tiebreak is
what covers the row found wrong next time.

What it changes is the row nobody has thought about yet. Phase 5's answer was to
pin the three conids that were known wrong, which fixes those three and leaves the
next dual-listed row to fail the same silent way — collecting a real chain, for a
real instrument, under the wrong one. Pinning is the escape hatch for genuine
ambiguity; it should not also be the only defence against a rule that cannot fire.

### A NAV currency guard that could never fire

Phase 5 built the right refusal — *a NAV whose currency is not
`account.base_currency` is refused, not converted* — on top of a label that turns
out never to be a currency. The `BASE` row of `/portfolio/{id}/ledger` reports
`currency: "BASE"` **and** `secondkey: "BASE"`. Phase 5 guessed `secondkey` carried
the code and wrote the fixture to match, so every test agreed with a route that
does not exist.

The base currency is identified from the per-currency row whose `exchangerate` is
one, that being by definition the currency the consolidation is into. Three
choices inside that, and the reasons:

* **No tolerance is declared** on the comparison to one. A tolerance would be a
  threshold nothing measured, and anything not exactly one is a currency the
  consolidation converted — which is the case being excluded, not a near miss.
* **Zero matches and more than one both yield `None`**, which is exactly what
  travelled before this existed. A route that changes shape, or an account holding
  a pegged pair, degrades to the previous behaviour rather than to a guess — and
  `None` is already handled as a third answer that is not a claim of agreement.
* **A route that does name the code is believed over the arithmetic.** The rates
  are the fallback for a route that will not say, never a correction to one that
  does.

The two implementations of this decode were merged into one resolver at the same
time. They had drifted apart only in the sense that both were wrong; keeping two
copies of a rule that had just been proven wrong is two places to fix it next
time.

**The fixture was corrected rather than left passing.** A fixture that encodes the
shape we hoped for tests our parsing against our own hope, which is the failure
Phase 4 named and this is the second instance of it.

### `--only` on `universe` is refused, not implemented

The flag was silently ignored: `obliskiq-collect universe --only ASML` walked all
91 targets while the CLI help and `docs/COLLECTORS.md` both offered it.

Implementing the filter was rejected. `universe` is a whole-file
**reconciliation** — it resolves every declared row, retires the row a corrected
conid supersedes, and ages out portfolio rows it did not see this run. A subset
changes what "did not see" means, so a filtered sync would make retirement
decisions on evidence it never gathered. That is the exact failure Phase 5's
outage guard was built to prevent, arriving through a command-line flag instead of
an upstream outage.

Refused, mirroring `--as-of`, which is refused on every command that would ignore
it for the same reason: a flag accepted and unread leaves an operator believing
something about the run that is not true.

### QQQ's 429 was a pattern, and it was never about pacing

Phase 5 declined to retune `collectors.request_spacing_seconds` on QQQ's single
`429` and wrote down what would make it a pattern: *the same target losing a chain
across runs*. QQQ has now lost it in **three consecutive live runs**, including one
where it shared the run with a single other target — so the pattern is established
by the criterion that was set in advance rather than by a fresh judgement.

And the criterion earned its keep by proving the stored hypothesis wrong. The body,
recorded for the first time, is `{"error":"Exceed number of conids"}`: a
**per-request cap**, not a rate. Pacing could never have fixed it, and Phase 5's
refusal to retune pacing was right for a reason nobody had yet measured.

Walked one request at a time, 100 conids is accepted and 150 is not.
`max_conids_per_snapshot` was **200**, and what made QQQ uniquely unlucky is that
it is the only target whose volatility-scaled band actually reaches
`collectors.chain.max_strikes_per_expiry` — 60 strikes at both rights is 120
conids for a single expiry, over the limit, in one request. Every other band
resolves to fewer than 100. SPY, whose collected chain is *larger* than QQQ's,
never touched it.

The value is now **100**: measured-good, and IBKR's documented market-data line
limit, so two independent things agree on it. The alternative — catching the 429
and halving the batch adaptively — was rejected: it hides a known constant behind
a retry, and `config.yaml` owns thresholds precisely so that a limit is stated
once where somebody can see it.

### A 401 is repaired, a 403 is not

`XLE` lost its chain to a bare `HTTP 401` half an hour into a run whose live
session token had ~22 hours left, while the next target collected normally and a
re-run of XLE alone collected 130 rows. That is a dropped `/iserver` session, not
an expired credential — an expired credential does not recover by itself.

So a 401 now re-arms once through `ssodh/init` and repeats the call, exactly as
`HTTP 500 {"error":"Please query /accounts first"}` already did. The two are one
condition wearing two statuses, and the recognition lives in one predicate rather
than at two call sites.

**403 is deliberately excluded.** It is an entitlement answer and re-opening the
session cannot change an entitlement, so re-arming it would buy an extra round
trip and the same refusal. The bound on 401 is one attempt: a credential that is
genuinely wrong fails on the repeat and raises, which is what `self._arming`
already guaranteed for the 500 case.

### The hour is 20:15, and the European chains still are not served by it

Phase 6 established that 22:30 UTC produced an unpriced chain and deliberately
declined to move the timer: two readings bracketed the window — 123 of 192 priced
at 20:08, ~34 of ~4,300 at 23:04 — and neither named an hour. The bracket is
narrower now.

**US option prices survive the close by hours.** A fixed basket measured 7 of 8
SPY and 7 of 8 QQQ contracts still priced at **21:08 UTC**, an hour and eight
minutes after the close, with `6509` reading `ZBd` — frozen, which is what a
served closing quote looks like. So the collapse to ~1% happens between 21:08 and
23:04, and **22:30 was simply on the wrong side of a transition nobody had
located**. Any hour from the close to roughly 21:00 is inside the window.

`20:15 UTC` is therefore right, and it is right with margin rather than by luck:
it sits at the start of a window at least an hour wide, leaving the whole decay
ahead of it. `RandomizedDelaySec=300` spreads the real firing across 20:15–20:20,
comfortably inside.

**What the same measurement rules out is that one timer serves both regions.**
EUREX, FTA and MONEP close at 15:30 UTC. At 21:08 — five and a half hours later —
ASML was 1 of 8 priced and TTE 0 of 8, while SAN was 8 of 8. A 20:15 run reaches
the European chains four and three quarter hours after their own close, and their
prices are already partly gone. **No US-close hour fixes that**, because the
problem is not which hour was chosen; it is that two markets with different closes
share one schedule.

A second, earlier timer for the three European rows is the obvious shape of the
answer and **is not declared here**, because the measurement that would justify it
was not taken: no sample exists between 15:30 and 20:00, so the hour at which
European prices actually decay is unknown. Declaring a schedule on the strength of
"earlier is probably better" is the move this project refused for
`history_exchange` and refuses here for the same reason. The probe is a basket
sampled across the European close, and it is a weekday job.

**A measurement note that changed the readings.** `market_snapshot` settles as
soon as *any* requested field arrives, so asking for the status field alongside
the prices can return a settled row carrying a status and no price. The first
three samples were taken that way and produced two isolated zeros that were
artefacts of the instrument. Asking for the price fields alone removes the
ambiguity. A controlled test on cold conids with the market open found the
collector's own field set returned 12 of 12 priced either way, so this is recorded
as a measurement hazard rather than as a collection defect — the settle condition
is a latent fragility with no demonstrated harm, and changing it without evidence
would be the speculative fix this project avoids.

### The 22:30 schedule does not deliver prices, and no replacement hour is declared

Phase 4 recorded that a chain photographed pre-open carries greeks and no prices,
and named the 22:30 UTC timer as the remedy. Measured this phase, that remedy does
not work: the 23:04–23:57 UTC chain step produced ~34 priced rows out of ~4,300,
while an incidental ASML collection at **20:08 UTC — eight minutes after the US
close — priced 123 of 192**. Same account, same day, `market_data_status` `ZBd`
throughout, so the entitlement is constant and the hour is the variable. Frozen
closing quotes are served for a while after the close and are gone by 19:00 ET.

This matters beyond tidiness: `solve-iv` inverts a mid price, so a chain with no
prices cannot produce an implied vol, and the half of the grid that needs one
stays dark on the schedule as deployed.

**`deploy/obliskiq-collect@daily.timer` is nevertheless left at 22:30.** Two
readings bracket the window and neither identifies the right hour; moving a
production schedule onto an hour nobody has measured would be the same species of
mistake as declaring `history_exchange: null` on the TSEJ precedent instead of
probing it. The probe that settles it is named in `references/ibkr-playbook.md`
and is a phase of its own, because it costs several runs at different hours.

### Amending the standing rule about sandbox sessions

`CLAUDE.md` said *"Cloud sandbox sessions have NO live IBKR access. Build against
mocks and recorded fixtures."* Two consecutive phases have now run live from a
cloud sandbox, so the rule as written was contradicted by the two most recent
things in the repository — and a standing rule nobody follows is worse than no
rule, because the next session cannot tell which parts still bind.

The caution it encodes is real and is kept: what makes a session able to run live
is the **nine credentials**, not the machine, and a session without them must not
pretend otherwise. What is dropped is the false absolute. The obligation that
actually mattered — mark every finding with the route and date it was taken on,
and never read an MCP-verified line as a CP fact — is unchanged and is the thing
the playbook's tag convention exists for.

## No second timer for the European chains (Phase 7, 2026-09-14)

### The decision is to declare nothing, and the prompt named this outcome in advance

Phase 7 asked whether ASML, SAN and TTE need an `obliskiq-collect@euro.timer` at
an earlier hour, because the 20:15 run reaches them 4h45m after their 15:30
close. **No such timer is declared.** The prompt set the condition for refusing
before the data existed — *"if they are … still present at 20:15 on a different
day, say so and declare nothing"* — and that condition is met.

At 20:06 on 2026-09-14 the basket read **SAN 8/8, TTE 6/8, ASML 2/8**, 16 of 24
near-ATM contracts priced at the timer's own hour. The premise that the run
reaches dead European chains is not what a day of hourly sampling shows.

### Why the earlier reading was not enough, and this is the fourth time

The phase was built on **one hand-taken sample**: 2026-09-11 at 21:08, ASML 1/8,
TTE 0/8, SAN 8/8. Six ticks three days later disagree with it. Both are real
measurements; the difference between days exceeds the effect within a day, which
means a single sample could never have settled it and the confident reading drawn
from that sample was the error.

This is the fourth phase running in which a recorded diagnosis was the thing that
needed checking first — after Phase 5's conid claim, Phase 6's five questions, and
the coverage metric in the Phase 8 prompt. The pattern is specific enough to name:
**a single observation, written down confidently, becomes a premise nobody
re-tests.** `references/ibkr-playbook.md` now carries the six-tick table so the
next reader inherits a distribution rather than a point.

### What the evidence does support, and why it still is not a timer

A second timer at, say, 15:45 UTC would buy **ASML +6 contracts and TTE +2, and
SAN nothing** — SAN never degrades. Against that: a second systemd unit, a second
OAuth session and live-session-token lifecycle, a second failure mode running
while the US market is open, and a `chain` step that would overlap the daily
sequence. **That is a poor trade for one name on one day's evidence**, and the
`DEGRADED` path already reports a thin chain rather than hiding it.

If ASML's ATM implied vol turns out to matter, the cheaper remedies come first: it
is one row in `universe.yaml`, and widening its strike band or accepting a
non-ATM anchor costs no new unit. A timer is the most expensive answer to the
narrowest version of the problem.

### What would change this

Not another opinion — **more days**. The measurement is one Monday. Three or four
more sessions of the same six ticks would say whether ASML's 2/8 is its steady
state or Monday's, and whether 2026-09-11's 21:08 reading was the outlier or
today's was. The sampler and the trigger recipe are both recorded; re-arming them
is minutes. Until then the 20:15 timer stands on the US-close measurement that
put it there, which Phase 6 took and this phase does not disturb.

### The transient zero, promoted to a rule

Two of 2026-09-11's five surviving readings were `0/8` **with the venue open**,
bracketed by `8/8` either side. Any future reading of this basket requires a zero
to persist across consecutive ticks before it counts as an event. Recorded because
a curve fitted through those two points would have looked like evidence.

---

## The market axis, realigned for allocation (Phase 8, 2026-09-14)

### A single-market `broad` cell is a first-class citizen, and the dislocation screen is a sub-routine

**The decision this phase existed to take, and it had been taken the wrong way
once.** A session read the axis probe and concluded that adding broad-only
markets degrades the board, on the arithmetic `ok / total`: 28.5% before, 25.9%
after. That is the metric `GridReport.coverage_pct` was written to replace, and
its own docstring already recorded what counting uncollectible cells does —
coverage capped near a tenth against the floor, every build reporting DEGRADED,
and the flag saying nothing on the day collection actually broke.

Measured on the ratio the build gates, on a store collected live in this session:

| board | populated / collectible | `coverage_pct` |
|---|---|---|
| before, 13 markets | 1,889 / 2,092 | **90.3%** |
| after, 16 markets | see `ROADMAP.md` | |

and `germany`, which measures nothing but its broad cell, ran at 32 `ok` leaves
against a collectible count that made it one of the best-collected rows on the
board.

**Why this is a judgment and not an arithmetic preference.** Most of the capital
is allocated on readings that need no option chain: broad returns and momentum
(family 4), Parkinson and Garman-Klass realised vol (family 5), the policy rate
and its real counterpart (family 1), the sovereign curve and its slope (family
2). The implied-versus-realised screen is one step of the workflow, not the
workflow. `SKILL.md` names *viewing the world through a US lens* as one of three
dominant failure modes, and option chains cluster in the United States — so an
argument that reaches "the chains are all in the US, therefore do not widen the
axis" **is that failure mode wearing a measurement.** The axis is decided by what
can be measured in a market's own currency, and by nothing else.

Recorded so it is not reopened: `SKILL.md` carries it in its own voice under
*Use native indices, not US-listed wrappers*.

### `south_africa` is dropped because nothing can measure it

Not because it has no option chain — france and italy will never have one either
and were added in the same change. It was the **only declared market with no
instrument at all**: no JSE Top 40 index contract and no local Top 40 tracker,
the SATRIX funds IBKR serves being offshore feeders (MSCI World, S&P 500, Nasdaq
100) and a bond portfolio. It published **4 `ok` leaves** against germany's 32,
and those four were a currency return, a policy rate, a long rate and a slope —
the macro-blanket exception in `build_grid` carrying an entire row on its own.

Its consumers were followed rather than guessed at, and there were five beyond
the market block itself: the `unmapped_cells` entry, `ZAR: EURZAR` in
`account.fx_series_by_currency`, and four `macro.series` declarations —
`EURZAR`, `CPALTT01ZAM659N`, `IRLTLT01ZAM156N`, `IRSTCI01ZAM156N` — each of which
south_africa was the only reader of. `tests/test_point_in_time.py` named `EURZAR`
as one of four fx series pinning the per-series `release_date_policy` override,
and now names `EURCAD`.

**What its removal cost, and how that was repaid.** south_africa was the one
market declaring `native_index: null`, so it was also the only live exercise of
the DECLARED half of the macro-blanket exception in `build_grid._cells_for`. No
market declares a null native index now. Rather than let that branch go unpinned,
`tests/test_compute_grid.py` gained
`test_a_declared_gap_still_computes_macro_on_its_broad_cell`, which builds a
universe with a null native index for the purpose; the pre-existing tests moved to
a market whose instrument is declared and unseeded, which is the other half.

### `france`, `italy`, `canada` and `india` are added

Each resolves to a tracker in its **own** currency, pinned by conid and read back
from `/iserver/contract/{conid}/info` on 2026-09-14 before being written down.
The anti-wrapper rule in `_load_markets` would refuse any of them otherwise, and
on this axis the near-misses are the point:

- **`CAC` (11183823, EUR)** — the bare ticker resolves first to CAMDEN NATIONAL
  CORP, a USD US bank. `CACX` is the same Amundi fund quoted in **GBP** in London
  and would report sterling returns under France's name. **`C40` (305372184) is
  AMUNDI CAC 40 *ESG* UCITS**, a different index whose currency is right — the
  one a currency check cannot catch, which is why the conid is pinned.
- **`CSMIB` (74707801, EUR)** — chosen over `ETFMIB` on a reading taken against
  the bars, not the prospectus. Over the 504 sessions the two share, their daily
  tracking difference has a standard deviation of 0.28pp and exactly two sessions
  sit outside it: 2025-12-09 (**+0.715% vs −3.357%**) and 2024-12-09 (−0.324% vs
  −4.238%). Those are the distributing fund's ex-dividend dates, and on the first
  of them the index rose while ETFMIB fell over three percent. A close-to-close
  realised-vol estimator reads each as a genuine four-percent move once a year;
  the accumulating class has no such day. `ETFMIB` stays the declared fallback.
- **`XIU` (74580921, CAD)** — see the chain decision below.
- **`NIFTYBEES` (396135052, INR)** — see the entitlement question below.

`france` and `italy` declare their eleven non-broad sectors in `unmapped_cells`
with germany's and spain's reason: European sector exposure is measured on the
`eurozone` row and a national sector fund does not exist on this route.
`canada` and `india` declare theirs as **unswept** rather than empty, which is a
weaker claim and the honest one — nobody probed a TSX or NSE sector range, and
"nobody looked" and "somebody looked and there is nothing" are different claims
this file refuses to merge anywhere else.

### `TTE` stays on `eurozone`, and this was decided rather than allowed to happen

`TTE` is declared `market: eurozone, country: FR` and `ASML` `market: eurozone,
country: NL`. **A `france` row does not thereby claim TTE**, and the germany
precedent settles why: germany holds `DAX` on its broad cell while German sector
exposure stays on the eurozone row, because `market` groups measurements and
`country` is the dimension the concentration caps read. Moving TTE to `france`
would take `eurozone/energy` — one of only three European chains this build
collects, and the name Phase 7 measured — off the eurozone row to gain nothing
france's broad cell does not already carry. Nothing is lost by leaving it: the
French-domicile fact is already on the row, in `country`.

### `india` is added, and the entitlement caveat resolves structurally

The 2026-09-13 sweep raised it and was right to: delayed daily bars are not the
same entitlement as a snapshot, and NSE market data was never probed during NSE's
own hours. It does not bite, and the reason is structural rather than lucky.
**Nothing in a market's broad-cell collection path takes a snapshot.** Bars come
from `iserver/marketdata/history`, which is measured — 730 daily bars seeded live
on 2026-09-14, newest 2026-09-11. The only step that snapshots is `chain`, and
`NIFTYBEES` collects none. The question would bite the day india acquired a
chain, and there is none to acquire.

`IRLTLT01INM156N` does not exist — FRED answers HTTP 400 `The series does not
exist`, re-measured 2026-09-14 — so india's long rate and slope are declared
null. That is the failure `IRLTLT01CNM156N` gave in PR 10, china is already on
the axis on those terms, and brazil on the structurally identical ones. Refusing
india for it would have been inconsistent with two markets already carried.

### `russia`, `taiwan` and `saudi_arabia` are refused, and the reasons differ

- **`russia`** — sanctioned; IBKR halted Russian securities. **Not probed**, and
  that is stated rather than dressed up as a sourcing finding.
- **`taiwan`** — `0050` (165197063, TWD, TWSE) resolves and serves 22 daily bars.
  It is refused on the macro side, and on the more interesting of the two
  failures: `CPALTT01TWM659N` does not exist, and **`EXR/D.TWD.EUR.SP00.A`
  exists, answers HTTP 200, and stopped on 2020-10-30.** A taiwan row would
  publish a six-year-old exchange rate as a currency return rather than refusing
  one. A series that is absent fails loudly; a series that is stalled does not,
  and that is the one worth refusing a market over. **A market added with a dead
  series declared as live is worse than a market not added.**
- **`saudi_arabia`** — `TASI` does not resolve at all; `KSA` is the iShares MSCI
  Saudi Arabia ETF, **USD and US-listed**, which is the wrapper trap; the only
  native-currency instrument that resolves is Saudi Aramco (708373937, SAR,
  TADAWUL), a single stock and not a market. `EXR/D.SAR.EUR.SP00.A` is HTTP 404
  and `CPALTT01SAM659N` does not exist. Refused on sourcing; revisit if a Tadawul
  tracker appears.

### `canada` collects an option chain, and what that costs

**The chain was measured, not assumed.** `/iserver/secdef/search` returns an
`OPT` section on `CDE` — Bourse de Montréal, the only venue any of the three
Canadian candidates names — with 14 expiry months for `XIU`. Reference data is
not a quote, so the entitlement was asked directly: four near-ATM calls per
month, price fields requested alone and `6509` read separately, with the venue
closed. `OCT26`, `DEC26` and `MAR27` each returned **4 of 4 priced**, and `DEC26`
and `MAR27` both sit inside the configured `option.min_dte`/`max_dte` band. This
account is entitled to Bourse de Montréal option data.

**So `collect_chain` is true on `XIU`, and it takes on one cost.** `iv_solve`
fails an underlying whose currency has no risk-free rate rather than discounting
it at a number nobody chose, and `macro.risk_free_rate.series_by_currency` had
EUR, USD and GBP. Without a CAD entry every canada solve would skip with
`no_rate`, leaving IBKR's own field `7283` as the sole IV source with no
independent check — which is the failure mode `SKILL.md` names as *trusting one
vendor's IV percentile*. So `CAD: IRSTCI01CAM156N` is declared, and it is the
**only monthly entry in that map**. That is a sourcing limit and not a
preference: asked on 2026-09-14, FRED serves no daily Canadian short rate —
`INTGSTCAM193N` stops in 2017-04, `IRSTCB01CAM156N` in 2023-12,
`IR3TCD01CAM156N` does not exist, and the two live candidates are both monthly to
2026-06. The vintage is not hidden by using it: `risk_free_rate_as_of` returns
the rate with its own `reference_date` and `stale=True`, so the rate a canada
solve discounted at is readable off the answer.

`XIC` and `ZCN` carry the same `OPT` section on `CDE` with 8 and 11 months and
are **not** declared: one instrument may measure a cell, and the S&P/TSX 60 is
the market-level index of the three.

**One consequence that only a test found.** `collect_chain` on a Canadian venue
made `tests/test_compute_calendar.py` fail: a mandatory close date computed on a
venue with no holiday list skips weekends only, and IBKR force-liquidates a
non-compliant position at market. `calendars.yaml` gained `XTSE` for 2026-2027
with both `TSE` and `CDE` mapped to it. Declared and **UNVERIFIED** against the
exchanges' published schedules, like every other calendar in that file.

### This phase does NOT add a sovereign spread, and it could have

Every `slope_label` in `universe.yaml` is `10y_minus_2y` or `10y_minus_policy` —
a **within-market** curve slope — and the only thing named `spread` anywhere in
`indicators.py` is `iv_hv_spread`. Adding italy therefore publishes `IT 10y − ECB
DFR` beside germany's `DE 10y − ECB DFR`: **the two legs of a BTP-Bund spread,
and not the spread.** A cross-market sovereign-spread family is a real thing to
want and is a new indicator family with its own leaves and its own reason codes.
It is stated here and in `ROADMAP.md` precisely so that nobody later assumes it
fell out of this phase.

---

## Two collector defects, and what measuring them changed (2026-09-15)

Both were carried open out of Phase 8, written down and unfixed. Neither turned
out to be quite what its write-up said, and in both cases the difference mattered
to the fix.

### A snapshot has settled when a PRICE arrives, not when any field does

`market_snapshot` polls because the first call for an unsubscribed conid returns
a skeleton. It used to stop polling as soon as **any** requested field appeared,
and Phase 8 diagnosed that as `6509` — the market-data status — arriving early.
That diagnosis was read from the code. Timed against the route on 2026-09-15 it
is incomplete in a way that would have defeated the obvious fix: **all four
Greeks arrive with `6509`, about 1.3s in, and the prices about 7s in.** Excluding
the status field would have left `delta` ending the poll five seconds before any
price existed.

So settlement is defined by **inclusion**: `QUOTE_FIELD_NAMES` is `last`, `bid`,
`ask`, and nothing else ends the poll. An exclusion list would have had to name
five fields to be right that day and would break again the first time IBKR served
a sixth one early. **The rule is "the caller got what it came for", which is a
statement about intent rather than about IBKR's current field ordering.**

One degenerate case is handled rather than assumed away: a caller asking only for
metadata — a status probe — has no price to wait for, so for that call every
requested field settles it and it returns on the first response.

### The retry budget was too small as well, and priming is why it can afford not to be

The same measurement killed the assumption that the predicate was the whole bug.
A basket asked for price fields **alone** — a correct predicate by construction —
still took 4.78s against a budget of four retries at one second. So the poll gave
up before any price existed no matter what the predicate said.

`snapshot_settle_retries` is 10 on that evidence rather than on a guess. What
makes that affordable is the second change: **batches are primed before any of
them is polled.** A subscription starts warming when it is first asked for, so
polling batch one to completion before touching batch two serialised the
warm-ups. Measured on the same run, the same field set settled in 7.03s cold and
4.78s primed.

The cost is bounded and was measured rather than argued: it is only ever paid by
a batch that never quotes, and the `curve` step — whose COMEX snapshots are
documented as returning nothing — went from 31s to 54s with identical output.
The gain is the whole first pass of every chain: `obliskiq-collect chain --only
XIU` against a fresh store now writes **264 rows, 264 priced**, where the same
command on the previous commit wrote 264 rows and priced none of them.

### A history request refused is not the same as a target that failed

`BOVA11` — which measures `brazil/broad` — was recorded as the `history_exchange`
inversion because its error string matched. **It is not.** Asked both ways on
2026-09-15 it serves `6m` and `1y` with `B3` on the request and refuses `2y` and
`5y` with the exchange on the request and with it omitted alike, while SPY, STW,
BARN and LISN serve the full window on the same run. It is the instrument's own
available history, and `iserver/marketdata/history` **refuses a window longer
than it can serve rather than returning a short series**.

Because `Backfill` opens by asking for the whole window in one call, that refusal
failed the entire target and the instrument collected nothing — for a
grid-measuring row, every night. Two changes, and the second was only visible
once the first was in:

1. **A refused span is asked again for half of it**, down to
   `collectors.backfill.span_stepdown_min_days`. Halving needs no per-instrument
   declaration, costs one extra call per halving, and only on a target that would
   otherwise have collected nothing. **A refusal at the floor still raises** —
   this widens what can be collected, it does not hide a target that cannot be.
2. **A refusal during the BACKWARD WALK stops the walk instead of failing the
   target.** That walk is speculative by construction: it reaches for history
   that may not exist, and it already treats an empty chunk as the signal to
   stop. A refusal is the same fact served differently. Letting it propagate was
   throwing away the recent bars the same run had already collected.

`obliskiq-collect backfill` now reports **171 of 171 targets ok** against live
`cp_api`, where it reported `partial` with eight failures. `BARN` and `LISN`
were the inversion after all, in Zurich, and declare `history_exchange: null` on
a measurement. Collection coverage moved from **90.4% to 91.8%**, all of it
`brazil/broad`, which went from 60 to 89 `ok` leaves.

### `JDEP` stops collecting, and its row stays

The fourth failure was neither thing. `/iserver/secdef/search` returns JDE Peet's
on `GETTEX` now, `/info` lists its valid exchanges with **`AEB` absent**, and the
only series any venue serves **stops on 2026-04-29**. Declaring
`history_exchange: null` would have made the target succeed while collecting a
series that ended months ago — the instrument-level form of the rule that refuses
`taiwan`, where a stalled series is worse than an honest absence. So
`collect_bars` is false.

**The row is not deleted, and that is the decision rather than an omission.**
`JDEP` is the only `KC` entry in `commodity_proxies`, and the `KC` futures root
names it in its own `untradeable_reason`. Deleting the instrument would have
quietly deleted coffee's equity expression while looking like a collector fix.
Coffee now has no live equity proxy on this route; finding a replacement roaster,
or concluding there is none, is a universe decision with its own probe, and it is
carried open in `ROADMAP.md`.

---

---

## The pre-deployment pass (Session 9.5, 2026-09-15)

Four groups from a read-only audit of `main` at `cbe63b2`. The prompt said not to
take the audit on trust, and that was the right instruction twice: one claim it
made about a defect was wrong, and one claim it made about the ABSENCE of a
defect was wrong. Both are recorded below, because a repository that has now had
six phases whose premise did not survive measurement should also record the
premises that did.

### The sequence bounds itself per step, and the unit's timeout is the outer bound

`TimeoutStartSec=3600` bounded a six-step sequence whose `chain` step alone walked
24 targets in **57 minutes**. Raising it to 7200 was necessary and is not the
fix, because of what expiring actually does: systemd sends **SIGTERM**, which is
process death rather than an exception, so the per-step `try/except` the whole
sequence design rests on never sees it, every step after the slow one is lost,
and `Restart=on-failure` re-runs the sequence **from the top** — up to three
times, re-spending marketdata.app credits on each.

So `collectors.daily.step_deadline_seconds` gives every step its own wall-clock
budget and the sequence abandons a step that outruns one. Three choices inside
that, and each is a thing a later session would otherwise get wrong.

**Per step, not one number for the run.** A single budget shared by six steps
would let `backfill` spend the window `chain` needs, which is the priority
`DAILY_SEQUENCE` states exactly backwards: the three European chains are the only
series in this package that no amount of money recovers, and everything ahead of
and behind them is re-collectible — bars at any time, curves rung by rung, the US
chains re-bought, macro tomorrow for free. `chain` therefore gets the largest
slice, sized above the measured 57 minutes; `tests/test_deploy_units.py` asserts
it is the largest, so the ranking survives somebody retuning one number.

**The sum is the constraint, and it is what makes the two numbers one decision.**
The budgets add to 7020 against a `TimeoutStartSec` of 7200, so the sequence
always reaches its own deadline before systemd reaches its one. Without that the
budgets are decoration: whichever step is running when the unit gives up dies with
the process and takes the rest with it. A test pins the inequality rather than
either number, because what matters is the relationship.

**`interrupted`, not `error`.** This file already drew that distinction for a
reaped row — an error names what went wrong, an interruption names that nothing
got the chance to — and a step cancelled at its budget is the second. It does not
count toward `steps_completed`, because it wrote an unknown amount and claims
nothing about it. But it does move the sequence's verdict, and that half had to be
added: `INTERRUPTED` is not in `_COMPLETING`, so an abandoned `chain` beside five
healthy steps returned `ok` at exit 0 — the quietest possible way to lose the one
series that cannot be re-collected. It is `partial` now.

The rejected alternative was catching the SIGTERM. A handler would have to unwind
a collector mid-call and would still be racing systemd's `TimeoutStopSec`; more to
the point it treats the symptom, since the run that reaches the unit's timeout has
already spent the window the steps behind it needed. Bounding each step keeps the
process inside its budget rather than teaching it to die gracefully.

### An enabled route that cannot run is degraded; one told not to run is skipped

`vendor-chain` raised `NoMatchingTargets` for two different facts, and inside
`daily` that is recorded `skipped` at exit 0. `deploy/collect.env.example` ships
`MARKETDATA_API_TOKEN` empty while `config.yaml` ships `option_vendor.enabled`
true, so a **fresh install** collected zero US option chains every night and the
scheduler reported success. `docs/DEPLOY.md` §7b warned a human about it in
prose; nothing in the code did.

**The conflation being fixed was deliberate and its reason was good, so the fix is
a third outcome rather than a louder version of either.** What the skip was
avoiding is twelve targets failing every night, each after a full retry backoff,
for a condition known before the first call — and that property is kept exactly:
nothing is attempted, no retry is spent, no credit is spent. What changed is that
`option_vendor.enabled: false` ("told not to run") and an unset token ("could not
run") no longer read the same. The first stays a skip, because honouring a flag
somebody set is not a fault. The second records **one finding** and makes the
SEQUENCE degraded, so `obliskiq-collect status` and the exit code both say
something is wrong.

The precedent is `MacroPull`, which abandons a dead source with one finding and
continues rather than failing every series left in it: a condition that applies to
every remaining target is one fact about the route, not one fact per target.

### The cron file drifted on TIME, and the compute line had not

`deploy/crontab.example` fired `daily` at **22:30 UTC** while
`obliskiq-collect@daily.timer` fired at **20:15** — and that timer's own header
records why 22:30 is wrong: US option prices collapse between 21:08 and 23:04, so
a no-systemd deployment following the shipped crontab collected a largely unpriced
chain every night. `tests/test_deploy_units.py` compared which COMMANDS cron
scheduled and never their times, which is precisely how it drifted.

**The audit also said to shift the compute line behind it by the same amount. That
claim did not survive being measured, and the line was left alone.** It fires at
`30 23`, which already equals `obliskiq-compute@all.timer`. Shifting it would have
introduced a fresh divergence in the one file whose stated purpose is to be the
cron equivalent of the timers — the exact defect the new test exists to catch,
created while fixing it. Moving `daily` to 20:15 also removes a real race the old
pair had, since the collectors are bounded at two hours and used to start at 22:30
against a compute run at 23:30.

The guard is the deliverable as much as the edit: every cron line's hour and
minute are now compared against its timer's `OnCalendar`.

### `universe.yaml` had no orphan guard, and writing one found live drift

`tests/test_no_unread_config_keys.py` has enforced "a key nothing reads is a
decision that looks made and is not" for `config.yaml` since Phase 3.
`universe.yaml` had no equivalent — it appears in that file only as a *consumer*
of config series ids — so `tests/test_no_unread_universe_keys.py` asks the inverse
question about its own keys.

**It was written expecting to pass. It did not, and the audit's count was the
thing that was wrong.** Ten of the sixteen `futures_roots` rows are YAML *flow*
mappings, and an unquoted scalar inside `{}` ends at the first comma — so
`untradeable_reason: futures contract prohibited; no accessible retail wrapper,
macro signal only` stored the text up to the comma and parsed the tail as a key of
its own, `macro signal only: null`. Five such keys, and five reasons reaching
`compute/gates.py` missing half their text. Nothing could see it: the loader takes
the fields it recognises and ignores the rest, and a truncated sentence still
reads like a sentence. The scalars are quoted now.

Two shape differences from the config guard, both load-bearing. `markets:` keys
are **data** — requiring `us` or `eurozone` to appear in `src/` would fail the day
somebody adds a market — while the fields inside each market block are structural.
And a universe key is matched as a bare name rather than as a dotted path, because
it is addressed as `row.get("history_exchange")` and as a column of the same name;
requiring a dotted citation would fail every key in the file.

### The grid says which way the smile leans, and the leaf that did it was already written

`Smile.skew` was implemented and tested and called from nowhere in `src/`.
Meanwhile the collectors buy up to `collectors.chain.max_expiries_per_underlying`
expiries at `max_strikes_per_expiry` strikes on both rights, store every row with
its own `iv` and `delta`, and family 7 published the single leaf
`implied_vol_atm` off all of it. **ATM implied vol is symmetric**: two underlyings
at the same level can be pricing opposite risks and the board could not tell them
apart. `implied_vol_skew` is a risk reversal and says which tail is bid.

So `Smile.skew` ships rather than being deleted, and the audit's fifth dead-code
bullet is void. The other four went.

**Defined by delta, from config, and never substituted.** A moneyness band is a
different thing on a 15-vol name and a 90-vol one, so a skew defined that way is
not comparable across the board — which is the only reason a grid cell exists.
`smile.skew_delta` is the target and 0.25 is the conventional 25-delta risk
reversal. Each side must BRACKET it and the strike is interpolated between the two
points either side, which is the treatment `iv_at` already gives a target strike.

**The refusal is structural, which is why it needs no second threshold.** The
rejected alternative was a tolerance — take the nearest strike within so many
delta points — and it fails on the thing this repository refuses everywhere else:
it would publish a 45-delta reading under a 25-delta name, reading as an ordinary
number all the way to the output, and the tolerance itself would be a threshold
nothing measured. A band that does not reach the target reports `n/a` with the
|delta| span each side actually reached, so the refusal is checkable rather than
asserted. Measured on the fixture store, that is what every reachable cell does:
QQQ's collected band spans 0.40–0.50 and FCX's surviving band 0.28–0.48, so
neither reaches 25 delta on both sides and both say so in numbers.

**The leaf name carries no number, and that is not the convention it looks like it
should follow.** `realised_vol_{tenor}d` takes its tenor from a config LIST that
defines several leaves, so the tenor is part of each leaf's identity. Here there is
one leaf and one target, so baking the delta into the name would rename the leaf
whenever the threshold moved and break the day-to-day join
`_iv_percentile_leaves` exists to protect. The target travels in the cell's
`window` instead.

**Inside `FAMILY_IMPLIED_VOL` and not a family of its own**, so it inherits the
chain-or-snapshot freshness group and `MARKET_DATA_FAMILIES` still derives
correctly — both properties of `FAMILY_INPUTS` rather than of a hand-kept list. It
is read off the hygiene-filtered smile, because a skew computed off a strike the
spread, depth, delta-band or convexity filters dropped is not a skew. And both
leaves are emitted in every branch including the inapplicable one, on the rule a
grid whose leaf set changes with its status is a grid whose rows cannot be joined
across days.

The IV term structure is deliberately not attempted here. It needs a decision about
which expiry pair defines the front-versus-back slope, and `ROADMAP.md` carries it.


## A guard is verified by mutation, not by being green (Re-audit, 2026-09-16)

A read-only re-audit of the five pillars at `52a5bcc` found the suite green at
1,770 tests and the substance sound. What it also found is that three invariants
this repository states as load-bearing were guarded by tests that could not fail
for the reason they exist, which is a worse position than an untested one: an
untested rule makes somebody think, and a test that looks like it is checking
something stops them.

**The method is the decision.** Where a guard was suspected, the invariant was
broken on a throwaway copy of the tree and the suite re-run. Three mutations
passed all 1,770 tests:

- flipping the rate transform's sign in `stats.py`, which inverts every
  equity-against-rates reading in the correlation matrix;
- setting `runtime.sqlite.busy_timeout_ms` to 0, which is the `database is
  locked` crash the WAL/busy-timeout pair exists to prevent;
- deleting the exit-code append in `collect/cli.py` (below).

Each is now caught, and each fix was re-verified by re-running the mutation. A
test written to pin an invariant is not finished until the invariant has been
broken once and the test has failed.

**The process exit code and the sequence row are ONE fact.**
`docs/COLLECTORS.md` says so — "the thing systemd saw and the thing a human reads
afterwards are one row and can be checked against each other" — and
`docs/COLLECTORS.md` also says a step abandoned at its budget makes "the exit
code 1". Both were true of the STORE and false of the PROCESS: `_run_daily`
recorded a raised or abandoned step on the sequence and appended nothing to the
list `_exit_code` reads, so a night that lost its `chain` step stored
`exit_code = 1` and exited **0**. systemd saw success and `Restart=on-failure`
could not fire. The documentation needed no change; it had described the intended
behaviour correctly for as long as the code had not implemented it.

So `INTERRUPTED` is failure in both readings, and `SKIPPED` in neither. The
rejected alternative was to derive the process code from `SequenceRun.exit_code`
directly, which reads better and is wrong: `_exit_code` also serves the
single-collector path, which has no sequence at all, and one function with two
sources would be the drift this fixes wearing a different shape.

**Why the suite could not see it.** Every one of the eleven call sites awaited
`_run_daily` and discarded its return value, then asserted on the row read back
out of SQLite. The half that was broken was the half nothing looked at, which is
the general form of all three findings.

**An exemption is keyed to the sense, not to the number.**
`tests/test_skill_config_consistency.py` exempted `52[-\s]weeks?` wherever it
appeared, reasoning that "the 52-week lookback is a market convention, not a
tunable window". That is true of the range-position leaves, which read
`indicators.WEEKS_PER_YEAR`. It is false of the IV percentile, whose window is
`data.iv_percentile_window_weeks` — so two SKILL.md lines restated a config value
and the guard against restating config values waved them through, on the strength
of its own comment. The exemption now covers 52 weeks *the range is measured
over* and nothing else, and the two lines name the key.

The rejected alternative was to add the two lines to `ALLOWED_FRAGMENTS`, which
would have made the guard pass while leaving the restatement in place. That is
the failure mode the file's own header warns about: do not widen the allowlist to
make a restated threshold pass.

**A count in prose that no test reads has a shelf life.** `docs/COMPUTE.md`
quoted the leaf count twice and disagreed with itself — 44 in one paragraph and
47 in the verification block forty lines later, the first correct when it was
written and never revisited. The family count is pinned in four places and the
tool set in one; the leaves had nothing. The set is now derived from config and
asserted against a real build, and the figures are read back out of the document
and compared, so the next tenor-list change fails a test rather than ageing the
prose. A dated session record in `ROADMAP.md` keeps its original figure — it is
what that session measured — and loses only the present tense that made it read
as a current claim.


## Known, and not fixed here

- **The live-session-token has no renewal path.** A collector alive past the TTL
  fails permanently. It does not bite today — collectors are `Type=oneshot` with
  `TimeoutStartSec=7200` against an 82,800s TTL, and the server holds no IBKR
  session — but it is a trap for whoever writes a daemon. Recorded and pinned by a
  test rather than fixed blind against an API no sandbox session can reach.
- **Curve prices need a futures market-data subscription.** COMEX snapshots return
  `{}` outright on this account while the same contracts' daily bars serve
  delayed. The collector writes a complete ladder with no price on any rung and
  reports it as **degraded** rather than as success.
- **A census outside market hours can only be a floor test**, and the
  affordability gate's currency-awareness was a live finding, since closed.
- ~~**No market but the US has a CPI print newer than 2025-12, and there is no
  replacement source in this build.**~~ **CLOSED by Phase 9, 2026-09-15**, in the
  commit that merged this bullet unamended beside it. The measurement it records
  was real — the OECD `CPALTT01*` family stops between 2022-04 and 2025-04 and the
  ECB's own HICP flow stops at 2025-12 — but the conclusion drawn from it did not
  survive the phase: the euro area reads Eurostat's HICP **through FRED**, which
  serves it current, and the UK and Canada read their national statistics offices
  directly. `policy_rate_real` is now `ok` on **eight** of the fourteen markets
  that declare a policy rate and `stale` on **none**; the six that cannot be
  sourced point-in-time declare `cpi_series: null` with the route probed and the
  missing piece written into `universe.yaml`, which is a declared gap rather than
  a stale reading. See *What Phase 9 measured* in `ROADMAP.md` for the table, and
  `references/macro-playbook.md` for the series ids. This bullet stood as current
  fact for the life of the branch because nothing re-read it when the thing it
  described was fixed — which is the same failure, one document over, as a
  recorded diagnosis nobody re-tests.
- ~~**`south_africa/broad` has no measuring instrument.**~~ **CLOSED by Phase 8,
  2026-09-14, by removing the market.** Nothing on this route measures South
  African equities at any level, so the row was carrying four `ok` leaves of 516
  on the macro-blanket exception alone. `china/broad` was closed the other way —
  `510300` on SEHKNTL reaches the Shanghai listing at its own quote — and the
  exception itself stands and is what lets china and every unseeded market keep
  their macro row. See *The market axis, realigned for allocation* above.
- **A historical vendor chain carries no implied vol** — measured 2026-09-09, 0 of
  608 contracts — and that is now **solved rather than merely recorded**:
  `obliskiq-compute solve-iv` inverts the mid price, 61 of 61 contracts converging
  on the SPY and QQQ chains bought for 2026-09-02. What remains open is COVERAGE,
  not method: the historical tier serves SPY and QQQ and returned `no_data` for
  XLP, and only eleven of the twenty-four chain-collected instruments measure a
  grid cell at all. `widest_iv_hv` therefore still needs realised vol on the same
  names — bars this store did not have for QQQ — before it ranks anything.
- ~~**The European chains have never been collected from their own venues on
  `cp_api`.**~~ **CLOSED by Phase 6, 2026-09-10.** `secdef/search` serves an `OPT`
  section for each European conid and all three declared venues serve strikes;
  see *What the European chains looked like on CP* above. The follow-on question
  — whether being collected 4h45m after their own close leaves them unpriced — is
  **also closed**, by Phase 7 on 2026-09-14: 16 of 24 near-ATM contracts priced at
  the timer's hour, and no second timer declared.
- ~~**`SAN` and `TTE` still have no daily bars.**~~ **CLOSED by Phase 6,
  2026-09-10, and its premise was wrong twice over.** `"Chart data unavailable"`
  is a statement about the **exchange on the request**, not about the instrument:
  the same conids serve daily bars with the exchange omitted, so both rows now
  declare `history_exchange: null` on a probe rather than a guess. And "both
  already held the correct European conid" was measured on MCP — on CP with
  nothing pinned, all three resolve to their US line. See *`history_exchange:
  null` for SAN and TTE, probed before declared* above.
- **Every declared dividend yield is still `null`**, and the reason is now narrow
  and named. The convention is decided and built — `cp_api` owns the column, the
  connector cross-checks it, the collected figure is dated and carries its basis,
  and units and precision are both measured. What was missing was one **field
  id** — and Phase 4 measured the answer: **CP does not serve a dividend yield on
  this account at all**, so the column stays NULL by measurement rather than for
  want of credentials. See *Why the dividend yield stays null after a session that
  set out to fill it* above. (The count in the sentence this bullet used to carry
  was wrong too: **seven** `cp_api` credentials have a consumer, not nine.
  `CLAUDE.md` carries the corrected rule.)
- **A ranked view with nothing rankable writes no row at all**, rather than one
  row carrying its denominators and a `detail` saying why it is empty. That is
  how `widest_iv_hv` and `largest_correlation_shift` have always behaved on a
  young store, and `most_compressed` joined them when its percentile started
  refusing an overlapping sample. The rule that a thin view is labelled rather
  than suppressed is enforced at `grid.min_cells_for_ranked_view`, which only
  bites once there is at least one row to label. Left alone deliberately: it is
  a change to three dimensions of a table nothing in these corrections touches,
  and the grid's own reason histogram already names the refusal on every one of
  the 57 cells. `ROADMAP.md` carries it.

## Non-finite input at the wire boundary, and a crossed book that read as tight (Fault-tolerance audit, 2026-09-17)

A hostile-input audit of the four surfaces named in `CLAUDE.md`'s brief — ingress
deserialization, degenerate quantitative input, error masking, and the MCP tool
boundary. The five architectural pillars and the previous re-audit's findings
were not revisited. Twelve genuine defects, all fixed as guards; a clean
certification for everything else that was tried, listed at the end.

**The MCP wire genuinely admits `NaN` and the infinities.** JSON's own grammar
allows the literals `NaN` and `Infinity`, and an overflowing literal like
`-1e999` decodes to `-inf` rather than raising:

```
>>> mcp.types.JSONRPCMessage.model_validate_json(
...   '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
...   '{"name":"log_exit","arguments":{"realised_pnl":-1e999}}}'
... ).root.params["arguments"]
{'realised_pnl': -inf}
```

`mcp.shared.memory.create_connected_server_and_client_session` — the harness
every other tool test in this suite drives through — masks this: its in-memory
client rewrites `NaN`/`inf` to `null` on the way out, so a handler reached
through it never sees a non-finite float regardless of what the caller sent.
That is a property of the test double, not the protocol, and it is why none of
the twelve findings below had a test before this session. `test_hostile_input.py`
calls the registered handler through `FastMCP`'s own tool manager instead, one
layer below the client-side JSON round trip, which is the only way to actually
exercise this surface.

**One root cause reaches five places.** `safe_float` — the single numeric
coercion for every collector, fifty call sites across `collect/`, `macro/` and
`ibkr/` — parsed `"nan"`, `"Infinity"` and an overflowing `"1e999"` without
complaint, because Python's own `float()` does. Fixed once, at the coercion
point, rather than at each site: a non-finite parse now folds into the same
`default` path an absent or garbled value already takes. This also closed a
defect in `_bars_from_body`'s own stated guard — "a bar without a usable
timestamp is dropped," implemented as `if stamp is None: continue`, which a
`nan` timestamp defeated by raising `ValueError` out of `epoch_ms_to_stamp`
before the `None` check could ever see it.

**A ledger write is not reversible, and two writes proved it.** `log_exit`
compared `realised_pnl < 0` and `<= 0` — both are `False` against `nan`, so a
real loss fell through to `record_option_gain` and was booked as a gain of
unknown size with no penalty. Against `-inf`, `< 0` is `True`, so the loss path
ran, multiplied by the penalty multiple, and wrote `penalty_balance_after = inf`
into `option_ledger`, which is append-only and — this file already records —
deliberately never recomputed. `get_sleeve_status` would have reported an
available budget of zero forever, recoverable only by hand-editing an audit
table, and the response would have shown `penalty_balance: null` beside
`is_locked: true`, because `server/encode.py`'s non-finite-to-null rule turns
the `inf` into JSON `null` on the way out — hiding the very thing that broke it.
Both are now refused before `close_position` is reached. The same shape existed
on the equity side of `log_entry`: `entry_price <= 0` passed an infinite price
straight into `position_ledger`, from which every later stop, zombie test and
portfolio review would have priced off it. The option path was already safe —
the affordability gate refuses an unaffordable premium at any size — which is
why only the equity path was open.

**A crossed book read as tighter than the tightest real quote on the chain.**
`ChainRow.spread_pct_of_mid` is `(ask - bid) / mid`, and both the liquidity gate
and the smile's hygiene pass tested `spread > ceiling` — a comparison a
*negative* fraction always clears. `smile.py`'s own docstring names the
consequence: "one bad implied vol on an illiquid strike propagates into N(d2)
and out into an exit decision on a position that was still alive," and a
crossed book is the single most common bad-quote condition on an illiquid chain.

**The first fix considered was wrong, and the reason is worth keeping.** The
obvious repair — fold a crossed book into `None`, the same "unmeasurable" value
a missing bid or ask already produces — was drafted, reviewed with the account
holder, and approved on the stated understanding that it would make the gate
report *untested* and the entry would be *refused*. It does the first and not
the second: `GateResult.passed = not self.failures`, and an *untested* gate has
never blocked `allowed` in this design — deliberately, so a young store missing
a NAV or a chain stays usable. Folding a crossed book into the same channel
would have quietly reproduced the defect one layer down while looking fixed,
and it independently broke the smile's own hygiene check, which treats an
absent field the same permissive way for the same reason `_hygiene_failure`'s
own docstring gives ("a missing open interest or delta is the route not having
served the field... rejecting on it would empty the smile on any route that
does not carry greeks"). Caught before merging by mutation-testing the fix
itself, the same discipline the previous re-audit used.

The corrected design keeps `spread_pct_of_mid` signed — a crossed book is a
*measured* fact, not an absent one, and collapsing it to `None` erases the one
signal a consumer needs to tell "broken" from "unmeasurable" — and makes every
consumer test the sign explicitly as an outright failure: `CandidateGates.liquidity`
gains an unconditional check ahead of the depth waiver (a crossed book is not a
wide spread on a deep book; no resting size makes a broken quote tradeable), and
`smile._hygiene_failure` gains the same check against the raw `bid`/`ask` rather
than the derived fraction, with its own drop reason (`DROP_CROSSED`) rather than
overloading `DROP_SPREAD`'s "too wide" label for the opposite condition.

**`compute/stats.py`'s own contract — `None` rather than a number nobody could
defend — had three open cases**, all sharing one shape: a guard written for a
*zero* variance (`var <= 0`) does not catch an *infinite* one, because
`inf <= 0` is `False`. `correlation` and `ols` both returned `nan` on an
infinite reading in either series, and `ols`'s `r2 = max(0.0, 1.0 - residual /
total)` collapsed a `nan` residual to a confident-looking `0.0` — "measured, no
relationship" — which is the wrong side of the fact for a commodity proxy's
wrapper recommendation, which reads `r2` as whether the proxy has any
explanatory power at all. `log_returns` had a sharper failure: `math.log`
raises `ValueError` outright when a ratio underflows to exactly `0.0` (a finite
price following an infinite one), so one poisoned bar could abort the grid
build for every cell in the run rather than costing the one series it belonged
to — a fact `compute/cli.py`'s own per-step guard already reports honestly as
`RunStatus.ERROR`, which is what kept this from being silent, but not from
being disproportionate.

**A non-finite computed value could reach the store labelled `ok`.**
`grid`'s own `CHECK (status = 'ok' OR reason IS NOT NULL)` constrains the wrong
direction — it says a non-ok cell needs a reason, and says nothing about an
`ok` cell needing a value. SQLite converts a Python `nan` to `NULL` on write and
stores `inf` faithfully as an ordinary-looking `REAL`, so a caller reading
`get_grid_cell` could receive `{"value": null, "status": "ok", "reason": null,
"n_obs": 30}` — a cell claiming thirty observations behind a reading it does
not have, indistinguishable from a genuine measurement that happened to be
null. Fixed at the one physical write site, `GridBuilder._write_cells`, which
every one of the fifteen families passes through regardless of which one
produced the bad value: a non-finite `ok` cell is demoted to `n/a` with a new
reason code (`non_finite_value`) and the sample count it still carries. The
schema's own inverse `CHECK` is the natural companion and is **deliberately not
added here** — `grid` is `WITHOUT ROWID`, so tightening the constraint needs a
migration that rebuilds the table, as 010 did for the four-status version, and
that is real risk bought for defence in depth behind a guard that already
closes the reachable case. It is `ROADMAP.md`'s housekeeping list.

**Everything else tried against the same four surfaces held.** Every existing
row cap (`context.bounded()`, `history`, `limit`, `top_n`) already clamps
rather than truncating silently; `record_iv_percentile`'s own percentile range
check already refused `nan`, `inf` and `42.0` correctly, if by the accident that
`not (0.0 <= nan <= 1.0)` is `True`; the collector's own `cp_client.py` decoder
already drops `nan`/`inf`/the `-1.0` sentinel exactly as documented; `safe_int`
already rejects every non-finite string via the underlying `int()` exception;
`RankingRow.meets_full_definition` does not fail open on a corrupted
`conditions` blob (`all({})` would be `True`; the property returns `None`
instead); `store/db.py`'s `except BaseException` savepoint rollback is correct
and untouched; the six macro fetchers handle null values, missing keys, wrong
types and sentinel placeholders without a single unguarded exception reaching
`macro_pull`'s per-source guard. `record_iv_percentile`'s two companion
volatility fields (`implied_vol`, `historical_vol_30d`) had no check at all —
reusing the collector's own two plausibility keys, `ibkr.cp_api.marketdata.
implied_vol_min_plausible` / `_max_plausible`, rather than a new pair, closes
the same door this tool's separate write path had reopened next to it.

**One caller-supplied date needed a floor it did not have.** `log_entry`
accepted `opened_at` after `as_of` — a session that has not happened — from
which days-held, the zombie test's two-session rule, the time window and the
mandatory close are all measured. Refused outright, the same direction a future
session is already refused in `vendor-chain`'s `--as-of`.

**The database's own absolute path does not belong in a client-facing error.**
`Store.transaction()` embedded `self._path` in every `StoreError` message, and
`server/tools/base.py`'s `call()` passes an `ObliskError` through to the MCP
caller verbatim. Stripped at the three raise sites; nothing reads the path out
of the message anywhere in the suite.

Every fix above was verified the way the previous re-audit verified its three:
reverted on a throwaway copy of the tree, the new test shown to fail for its
own stated reason, then restored. `tests/test_hostile_input.py`,
`tests/test_util.py`, `tests/test_compute_stats.py`, `tests/test_compute_gates.py`,
`tests/test_compute_smile.py`, `tests/test_compute_screen.py` and
`tests/test_compute_grid.py` carry the twelve. 1,798 baseline tests plus 38 new
ones, all green; the offline fixture pipeline's exit codes and grid status
counts are unchanged from before the change set.

## Three documents, a licence that must not be edited, and the env file's missing guard (2026-09-18)

A post-merge review of the fault-tolerance audit above. The audit itself was
clean — the merge landed, the suite passed at 1,836, the offline pipeline's exit
codes and grid counts were byte-identical to their pre-change baseline, and
nothing was left behind. What the same sweep found was three pieces of drift in
the documentation around it, none of them introduced by that change set.

**`.env.template` asserted a surface this server does not have.** It described
the thing an auth token protects as "a position ledger and an order-staging
surface". `BUILD-BRIEF.md` owns the tool surface and says the opposite in as
many words — *"There is no order-staging tool: staging runs through the IBKR MCP
connector"* — and this file already records the `execution.*` trio being deleted
in Phase 3 for "implying the server gated order staging when the server exposes
no order-staging tool at all". The template was reasserting in prose exactly
what nineteen deleted keys were deleted to stop asserting, on the one file an
operator reads while provisioning. Its own sibling, `deploy/server.env.example`,
had it right: "the token is the ONLY thing between the internet and the ledger".
This is the *one owner per fact* rule failing in the direction it exists to
catch, and it is the second recorded instance after the three documents that
contradicted a safety rule the fourth one owned.

**The copyright line named a username, and not even the right one.**
`Copyright (C) 2026 AlmoonD.` in the package docstring and `authors = [{ name =
"AlmoonD" }]` in `pyproject.toml` — a handle rather than a legal name, and
missing the hyphen the actual GitHub handle carries. Copyright attaches to a
person; both now read **Alvaro de Luna Luque**, and the handle stays reachable
through the `Homepage` and `Source` URLs that were already there. The README
gained the licence section it had never had, which for an AGPL project is not a
formality: section 13 is the clause that distinguishes this licence from the
GPL, and it binds precisely because this ships a server reached over a network.

**`LICENSE` itself is deliberately untouched, and that is the decision.** The
question that prompted this review was whether the licence needed updating. It
does not, and editing it would have been the error: the AGPL text is verbatim by
its own terms — "changing it is not allowed" — and its appendix directs the
copyright notice to the *program* instead, "attach the following notices to the
program ... each file should have at least the 'copyright' line". So the notice
belongs in the package docstring and the README, which is where it now is, and
`LICENSE` stays a byte-for-byte copy of what the FSF published. A licence file
with a project's own name spliced into it is not a stronger claim; it is a
modified licence.

**One file was made to match the claim rather than the claim softened.** The
new README section says every source file under `src/` carries an
`SPDX-License-Identifier` line. It was true of 82 of 83: `compute/__init__.py`
was zero bytes, the one package door with no docstring at all. Softening the
sentence to "almost every" was the alternative, and it is the worse one — a
licence statement that is 98.8% true is the shape of claim this repository
spends its tests refusing. The file gained the header, and with it the note
explaining why that package deliberately re-exports nothing: a convenience
re-export there would pull a sibling's imports in behind it, which is exactly
how `httpx` reached the compute import graph through `util/__init__` before
PEP 562 lazy loading closed it, and `tests/test_compute_isolation.py` fails an
import graph that so much as names an HTTP client.

**And the env file had no guard, which is why a hand sweep found all this.**
`config.yaml` has had `test_no_unread_config_keys.py` since Phase 3 and
`universe.yaml` gained `test_no_unread_universe_keys.py` in Session 9.5, both
enforcing that a declaration and its consumer agree. `.env.template` — the third
declaration file, and the one an operator provisions a VM from — had nothing.
`test_no_committed_secrets.py` scans it, but for the opposite property: that
every slot present is EMPTY. A variable missing altogether is empty in exactly
the way that test cannot see.

`tests/test_env_template_is_complete.py` asks both directions: every variable
`env.py` reads is named in the template, and every variable the template names
is read. The exemption list holds one entry, `OBLISK_ENV_FILE`, which names the
env file and therefore cannot live inside it.

**Its first draft was wrong in the way this repository keeps deleting config
keys over.** It matched lookups by ACCESSOR — `_get(...)`, `require(...)` and
their siblings, named one by one — and immediately reported three real variables
as orphans, because the key paths go through a `_path_or_none` closure the list
did not know about. A hand-kept list of function names goes stale the next time
somebody adds a helper. It matches variable NAMES instead: `env.py`'s whole job
is reading the environment, so an `UPPER_SNAKE` literal in it is a variable
unless it is one of two values — `INFO` and `UTC`, the defaults for
`OBLISK_LOG_LEVEL` and `OBLISK_TZ` — which are exempt by name and by reason.

A third test pins the count `CLAUDE.md` got wrong for three phases: seven
`cp_api` credentials, not nine, with `OBLISK_IBKR_REALM` and
`OBLISK_IBKR_ACCOUNT_ID` in neither the code nor the template. That was true
when this was written and was verified by hand; it is now verified by a test,
which is the whole difference.

Proven by mutation, as everything here now is: dropping a variable from the
template, adding one to `env.py` the template never got, and putting
`OBLISK_IBKR_REALM` back each fail the test that should catch them, naming the
variable and the remedy.
