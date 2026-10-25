# Decisions

Why ObliskIQ is built the way it is, and what was rejected. Each entry says what was decided,
on what evidence, and where it applies. A decision in `docs/MASTER_PLAN.md` changes only
through a new entry here that names its evidence, with the plan edited in the same PR.
Legacy evidence is cited by its path in the `legacy/` snapshot (see `legacy/MANIFEST.md`).

## The design (MASTER_PLAN, recorded in Session 1, 2026-10-08)

### D1 · Survive first; every limit is configuration (§2)

No leverage, no shorting, no written options, no futures positions, no negative cash in any
currency, limit-capped orders only; what may be bought is decided by the active jurisdiction
profile. Every value lives in `config/`, so a change of residence is a profile switch plus,
at most, a policy module. Evidence: neither legacy project ever placed an automated order
(§3.4), and a blow-up is the one outcome no later skill recovers from.

### D2 · Lite's limits are the defaults; the stricter outcome wins (§2)

Lite's `config.yaml` blocks `instruments`, `positions`, `equity_risk`,
`portfolio_circuit_breaker`, `option_sleeve`, `tenor`, `zombie_test`, `discipline`,
`fx_inflation` and `commodities` become `config/defaults/` values as their consumers are
built. Where a lite limit meets a mechanism lite never had, the resolution is recorded here
and the stricter risk outcome wins. Evidence: `legacy/lite/config.yaml`; the sleeve was
measured viable at the account's size against live chains (`legacy/lite/docs/DECISIONS.md:89-91`).

### D3 · Edge is calibrated probability × asymmetric payoff (§4, §5.11)

Stated probabilities shrink toward the base rate (market-implied, or the random-walk barrier
probability); until a layer has a pre-registered minimum of graded outcomes and Brier skill
above zero it sizes by a fixed risk budget. Rejected: shrinking toward 0.5, as the old clamp
did (`legacy/old/ObliskIQ/modules/execution/phase4_decision_engine.py:831-832,878`), which
overstates the edge of a 3:1 target; base-rate anchoring alone would freeze the satellite.

### D4 · Simons means statistical discipline, not microstructure (§4, §5.2)

Rejected: market making, intraday arbitrage, order micro-blocks, multi-asset option baskets.
Kept: validation before trust, cost-aware execution, evidence-based sizing, the risk engine
and the scoreboard. Evidence: none of it is achievable on a REST API at retail size, where
one option contract is the atomic unit of a position.

### D5 · No regime model; risk balance plus a slow, tax-aware trend tilt (§4, §5.4)

Rejected: a regime model setting the allocation, and trend as an on/off switch. The old
classifier never said "bull" in 125 windows (§3.1; `analyzer.py` is not ported, so not
snapshotted); its overlay de-risked 45 of 125 months (`legacy/old/ObliskIQ/docs/STRATEGY_V2_DESIGN.md:217`)
and v1.1 failed its gate, OOS Sharpe 0.801 vs SPY 0.858 (`…/STRATEGY_V1_1_VERDICT.md:13`).
Trend has the stronger evidence (Moskowitz, Ooi & Pedersen 2012); bands-only is the fallback.
Narrowed by D60 and D61: the risk balance and the tilt are candidates of Session 4c's test, and
the core runs the point the owner picks from its frontier.

### D6 · Options: one block when IV is cheap, on real-time quotes (§4, §5.6, §5.13)

Rejected: adding to long options after the move confirms, which pays a higher premium for the
same convexity, and delayed quotes for options, whose wide spreads make stale mids misprice
(MASTER_PLAN §4, the rows on adding to long options and on delayed quotes). Pyramiding is for
shares only; stocks, ETFs, funds and bonds accept delayed quotes within a maximum age.

### D7 · Bonds by a computed rule; swap partners on a different index (§4, §5.3, §5.16)

A direct bond qualifies only when its minimum increment and per-order minimum commission fit
the role's amount and it beats the best fund all-in — no account-size threshold. Swap
partners differ in issuer **and** index by default: RIRPF art. 8 makes securities homogeneous
by issuer ("un mismo emisor"), and a different index removes any anti-avoidance argument at
negligible tracking cost.

### D8 · Two IBKR transports behind one client (§4, §5.12)

OAuth 1.0a where IBKR grants it; otherwise the Client Portal Gateway with automated login,
unattended in paper (paper logins have no second factor). Narrowed by D42: paper uses the
Gateway, OAuth is the live account's. Evidence: IBKR told the owner on
2026-10-08 that OAuth 1.0a is for institutional clients, while lite kept the OAuth route as its
route of record (`legacy/lite/docs/DECISIONS.md:248-250`) and ran it live on 2026-09-10
(`…/DECISIONS.md:2124`): 3,832 chain rows, 21 underlyings (`legacy/lite/ROADMAP.legacy.md:127-129`).

### D9 · Standard CPython 3.14, not free-threaded (§4, §7)

Parallel computation runs through a process pool (`compute.workers`) and the code stays
thread-safe; Session 14 benchmarks 3.14t and switches only if it is clearly faster with every
dependency available. Evidence: numpy releases the GIL inside its kernels; 3.14t is slower
single-threaded and not every scientific wheel ships a free-threaded build. Narrowed by D61:
the benchmark is dropped (there is no Session 14), and 3.14t is reconsidered only if profiling
calls for it (MASTER_PLAN §7).

### D10 · One long-format measurement store, many views (§4, §5.7)

Rejected: separate grids by region or by sector. Different questions need different axes;
one table `(entity, indicator, as_of, value, status, reason, …)` keeps them consistent and
carries lite's explicit statuses, `inapplicable` included (`legacy/lite/docs/DECISIONS.md:531,622`).

### D11 · Patterns are pre-registered; the LLM nominates, data disposes (§4, §5.9, §5.10)

Rejected: mining many patterns, social-media sentiment, an LLM that picks sectors and
companies and decides. Evidence: the factor zoo (Harvey, Liu & Zhu 2016) and post-publication
decay (McLean & Pontiff 2016); the old library reported 30 of 31 patterns trusted on fabricated
sample sizes and 0 of 31 once they were purged (`legacy/old/docs/PROVENANCE_PURGE_REPORT.md:35,143`).

### D12 · The satellite must earn its capital (§4, §5.19)

Its budget rises one step when the lower confidence bound of its graded excess return is above
zero after the minimum sample, up to the owner's ceiling, and halves on an interval wholly
below zero. Evidence: 58% of US stocks returned less than one-month T-bills over their
lifetimes and 4.3% of firms created all net wealth 1926–2016 (Bessembinder 2018). Replaced by
D65: capital follows the research outcome, and live results are monitored against a twin.

### D13 · Four pillars, three exclusions (§5.2)

Risk balance (All Weather), trend following (the CTA tradition), reflexivity (the satellite),
and statistical discipline as the reason the other three can be trusted. Not pillars: value
(a selection factor), permanent tail hedging (it pays away the variance risk premium,
Bollerslev, Tauchen & Zhou 2009; trend protects more cheaply, Hurst, Ooi & Pedersen 2017),
FX carry (it needs forwards, which the mandate forbids).

### D14 · Legacy pattern triage (§6.4)

The 31 patterns in `legacy/old/ObliskIQ/modules/analytics/pattern_library.py`: 6 built into
components, 6 re-tested in Session 21, 5 too rare to validate (≈ 7–22 events; low-weight
regime-fragility inputs only), 14 dropped and recorded as `retired`. Their stated IC, n and
hit rates are discarded — unverified, not false: none clears the precision gate, CI width
≤ 0.20, which needs n ≈ 250 (`legacy/old/docs/PROVENANCE_PURGE_REPORT.md:29-30`).

### D15 · Technology (§7)

Python 3.14 (`>=3.14,<3.15`; every planned library ships 3.14 wheels, checked 2026-10-07),
Pydantic v2, uv, ruff, `mypy --strict`, a coverage floor that only ratchets up. Rejected:
NautilusTrader and ib_async (they need TWS or IB Gateway and an interactive login) and
pandas_market_calendars (it wraps exchange_calendars, used directly). ibind is a reference,
attributed where copied. A further dependency needs an entry here. Narrowed by D96: the Python
cap is an exception with a review date, and 3.15 is tried in CI before it moves.

### D16 · Owner decisions (§11)

The §11 table, rows 1–13, as the owner decided: any account size with a configurable minimum
trade; budgets per row 2, moved by the allocation rule; autonomous paper; OPRA and Eurex Core
L1 shared to paper, everything else delayed; a global universe under lite's limits; holdings
`unmanaged`; EUR base; FIFO and the two-month rule; withdrawals planned at once, asking first
before cutting protected positions. Open: the LLM provider and budget, the machine, holdings to
release, the paper capital cap, the satellite ceiling, fund-switch support.

## Session 1 — Foundation (2026-10-08)

### D17 · The first dependency set

Runtime: httpx, pydantic, pydantic-settings and PyYAML, each with a consumer now; numpy,
scipy, pandas, exchange_calendars, cryptography and mcp arrive with theirs. Development:
pytest, pytest-asyncio, pytest-cov, respx, hypothesis, ruff, mypy, types-PyYAML,
import-linter, pre-commit, and hatchling (the build backend, used in-process by the packaging
test). The command line uses argparse, so no CLI library is added.

### D18 · The environment overrides configuration only for locations and logging

Credentials and deployment locations (`OBLISK_CONFIG_DIR`, `OBLISK_DATA_DIR`) come from the
environment; decisions come from `config/`. The one overlap is logging: `OBLISK_LOG_LEVEL`
and `OBLISK_LOG_FORMAT` override the `logging` block for one process, and `settings show`
names the variable as the value's source. Every credential and personal identifier —
including the account id, the Gateway username, the Telegram chat id and the SEC contact
string, which carries an email address — is a `SecretStr`.

### D19 · How the configuration loads

Owner facts with no expert default (`account.base_currency`, `jurisdiction`) live only in
`config/owner.yaml`. Each defaults section lives in exactly one file; owner mappings merge
key by key, anything else replaces, and an empty section (a bare header with its keys
commented out) overrides nothing. Schema fields carry no default values, so a key missing
from every file fails. The only optional field is a profile's `policies` map: a profile that
declares none resolves nothing, and asking it for a policy fails loudly.

### D20 · The foundation port from lite: what changed

`errors.py` keeps the classes something raises today (the others arrive with their raisers);
the text log format is stamped in UTC like JSON, with no local-time option;
`util/http.RetryPolicy` is built by its caller (`from_config` and lite's `http` keys come
with the IBKR client, Session 3); `env.py` is pydantic-settings over the names the plan
needs, without lite's MCP, timezone and connector variables; the typed loader replaces
lite's dotted-path `config.py`.

### D21 · CI and releases

CI cancels a superseded run except on `main`, where cancelling could leave a merged commit
with no completed result (lite's rule, `legacy/lite/tests/test_packaging.py`). The release
reads version and notes through `.github/scripts/release_notes.py`, which CI tests and ruff
holds to Python 3.12 (the runner's), and pins its actions by commit. The sdist ships an
explicit file list, so `legacy/` cannot leak into it.

### D22 · Subagents name model families

`.claude/agents/*.md` set `model:` to `opus`, `sonnet` or `haiku` with the `effort:` of
SESSION_PROMPTS §C.1. The aliases resolve to the newest model of each family — today the ones
§C.1 names — so the files carry no version identifiers and follow a family forward; pinning a
version would be a new entry here. Withdrawn: the owner removed `.claude/agents/` on
2026-10-16, and the end-of-session review is the session agent's own (AGENTS.md).

### D23 · The LLM contract also keeps out the broker

`obliskiq.llm` may import neither `decision`, `execution` nor `ibkr`: the order endpoints live
in `ibkr`, and the LLM never builds or sends orders (AGENTS.md). This adds a contract and
leaves the plan's rule (§5.10) as written. Each import contract was shown to break on a probe.

## Session 2 — Store, calendars and numerics (2026-10-09)

### D24 · Dependencies: numpy and exchange-calendars, no scipy

Runtime adds `numpy>=2.3,<3` and `exchange-calendars>=4.13.2,<5`. pandas arrives only as
`exchange_calendars`' dependency and is never imported by our code; the library and its pandas
objects stay inside `calendars/schedule.py`, which hands out standard-library types in UTC.
scipy is not added: the one function lite took from it, `ndtr`, is
`0.5 * erfc(-x / sqrt(2))` through `math.erfc` (exact to double precision, checked against
`statistics.NormalDist` and lite's test constants). This departs from MASTER_PLAN §7 and D17,
which listed scipy; a dependency with one consumer that the standard library serves is not
worth its weight. Dependabot keeps `exchange-calendars` current on purpose: its precomputed
calendars (XSHG, XBOM, XSES here) end on 31 December of their last recorded year, and
`Calendars.coverage()` reports each venue's window. exchange_calendars 4.13.2 builds XHKG's
Easter Monday with a bare-integer numpy timedelta that numpy 2.5 deprecates; one narrow
`filterwarnings` ignore names that module and message so nothing else is hidden.

### D25 · The store's shape

A fresh `001_core.sql` replaces lite's nineteen migrations. Tables are renamed for the plan's
vocabulary: `universe` → `instruments`, `underlying_snapshots` → `snapshots`,
`curve_snapshots` → `curves`, `collector_sequences` → `runs`, `collector_runs` → `run_steps`
(`run_id` is nullable: a step invoked on its own is not half of a run). `instruments` are
never deleted; a correction supersedes (`superseded_by`, `superseded_at`), and a read as of an
earlier date sees whether an instrument existed and whether it was superseded by then. Its other
attributes (`kid_status`, `ucits`, `lot_size`, `roles`, `history_exchange`) are overwritten in
place, so a past `as_of` read returns today's values for them; an attribute history, if a
later session needs one, is a new table. `bars` are **daily only and keyed by `session_date`**,
the listing venue's local date: lite keyed them by a UTC timestamp and a frequency, where a
weekly bar stamped at its period start would leak the days after it and an Asian session's
UTC date is the previous day. Weekly and monthly series are derived when needed. Dates and
stamps have one fixed shape each (`YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`), enforced by a GLOB
`CHECK`; the mappers raise `StoreError` on a stored value that is not a real date or time.

`market_data_status` is the **decoded** kind of quote (`realtime | delayed | frozen | none`,
`NULL` when the route said nothing, never read as realtime) and carries a `CHECK`; the route's
own flag is kept verbatim beside it in `market_data_raw`, and `quote_delay_seconds` beside that.
Lite deliberately put no `CHECK` on the raw flag so an unforeseen value could not crash a
collector on a day that cannot be recollected; the same intent holds here because the decoder
never emits a value outside the enum (an unrecognised flag becomes a `NULL` status with the raw
text kept). There is no `CHECK` on route names (`source`): SQLite cannot alter a `CHECK`, and no route
vocabulary exists yet, so the domain models accept any non-empty text; lite refused unknown
routes, and the enum that restores that arrives with the routes (Session 3). Not carried
because nothing serves them yet: vendor IV percentiles, bar watermarks, the grid, ledger and
census tables, and lite's `macro_fetch_log`, `collector_state`, `futures_roots`,
`vendor_fetch_log`, `universe_rejections`, `nav_history` and `commodity_proxy_map`; later
sessions add what a live route supplies by migration. The `universe` columns that describe the
grid and eligibility (`market`, `sector`, `tradable` with its `untradeable_reason` invariant,
the unique grid-cell index, `collect_chain`, `collect_bars` and the rest) are not carried: the
universe is configuration from Session 8, and that session decides where those invariants live.
The shape was derived by reading lite's chain, not by transcribing it.

Three write and read behaviours differ from lite and are chosen. A snapshot, chain or curve
upsert replaces the whole row (lite's writers each owned disjoint columns, such as the solved
implied volatility); a second route or a re-run therefore overwrites the first, and Session 11,
which adds the solver, adds a column-targeted writer with it. Content `CHECK`s that lite avoided
(`delta` range, `expiry >= snapshot_date`, `bars.close` and `macro.value` NOT NULL) are kept
because a row that violates them is wrong, not merely unusual, and a batch containing one rolls
back whole: a collector reports the day as failed instead of storing it. `limit <= 0` on a read
raises where lite ignored it.

### D26 · The migration runner and the connection

Python 3.14 `autocommit=True`: the runner and `Store.transaction()` begin and end every
transaction themselves. Each migration runs inside one `BEGIN IMMEDIATE` together with its
`schema_migrations` row, with the applied set read again inside the write lock, so two
processes starting on an empty database apply the first migration once. It refuses an edited
migration (SHA-256 of the LF-normalised text; `.gitattributes` fixes `*.sql` to LF), a missing
file, an unapplied one below an applied one, a gap or repeat in the numbering, and a script
that carries transaction control or sets a connection pragma. Foreign keys are switched off
for the duration of a migration, as SQLite's table-rebuild procedure requires, and
`PRAGMA foreign_key_check` runs before the commit, rolling back on any violation; a rebuild of
a referenced table is therefore possible and cannot lose a referenced row. Write-ahead logging
(a file database) and foreign keys are fixed in code and checked after being set; only
`store.busy_timeout_ms` and `store.synchronous` (`NORMAL`, `FULL` or `EXTRA`; `OFF` is not
offered, since a crash could corrupt the database) are configuration. `transaction()` takes
the write lock at the outermost level and savepoints inside; there is no `commit()`. A `Store`
belongs to the thread that opened it; async callers use `asyncio.to_thread`.

### D27 · Point-in-time reads

Every repository read takes a keyword-only `as_of` with no default, `COUNT(*)` included.
Bars are read up to the session `as_of` (callers pass the calendars' `last_completed_session`,
never an open session); snapshots, chains and curves up to `snapshot_date <= as_of`; macro by
`release_date <= as_of` in SQL; instruments by `created_at` and `superseded_at`, so a
supersession is invisible before the `superseded_at` its writer states (the store does not
stamp it, and `upsert_instrument` never changes a supersession). A snapshot date after `as_of` raises. A macro
observation with no release date cannot be placed in time: it is excluded and counted in
`MacroSeries.unaligned` (bounded by `collected_at`, so the count too is what the store held on
`as_of`), and a series from several sources must be asked for by source. One macro value is
kept per period (a re-pull overwrites; Session 10 adds the vintage). `curve_front_history`
takes the earliest expiry listed each session and drops a session whose front has no price;
lite took the earliest *priced* expiry, which silently spliced two contracts into one series.
Two structural tests keep this true: every public read in `store/repos.py` has a required
keyword-only `as_of`, and no package outside `store/` holds a SQL string.

### D28 · Calendar semantics

Two views of a day. `venue_status(venue, day)` is the planning view: any incident overlapping
the session blocks it (`partial` says whether tradable time remains), which is what an exit
scheduled against an expiry needs. `tradability(venue, at)` is the act-now view: incident
intervals are subtracted exactly, so trading resumes after a halt; a pre-trade check calls it.
The prompt's `tradable_window` is `tradable_windows`, plural, since a lunch break or an incident
splits it. An incident blocks `[start, expected_end + grace]`, or `[start, ∞)` without an end,
a function of the record alone; the clock only drives a once-per-incident "stale, remove it"
warning. Manual incidents need an explicit UTC offset, a known venue, an end after the start, a
reason, and for instrument scope an int `conid`; a file that cannot be read makes the whole book
invalid and blocks every venue (`incident_file_invalid`), because one typo must never silently
drop the entries beside it (pulse's loader failed open). A feed may raise only what its venue
declares. A date the library or configuration does not cover is `calendar_not_covered`, never
open; `coverage()` reports each venue's window.

Cross-check sources (`ScheduleSource`, registered by Session 3) are synchronous and
cache-backed, return `None` for "no information" (distinct from closed), compare the regular
session only (product hours differ and would read as a permanent disagreement), alert once per
(venue, day, kind, source) through an injected sink, and block only `date_critical` queries.
`last_tradable_session` walks back to the last day both venues are `ok` date-critically, lists
every day it skipped with the venue and reason, and raises when it meets a day the calendars
cannot judge. XEUR is a 27th venue (the library carries it since 4.13.2): its hours are the
whole venue's and wider than equity options trade, so an option order uses `common_windows` of
the option venue and the underlying venue. An unmapped IBKR code (`SMART`, anything unlisted)
raises. The library's sessions are sanity-checked (`open < break < close`; a break at or after
the close is dropped, as the library already does on special-close dates, anything else out of
order is `schedule_inconsistent`). All durations are computed on UTC instants: pulse subtracted
two wall-clock datetimes of one zone, which is an hour wrong across a daylight-saving change.

Findings of the adversarial test round on the facade: a date-critical order is blocked by a source
disagreement even when an incident also touches the day and the instant lies outside it; a manual
file that is not valid UTF-8, or a symlink whose target is gone, makes the book invalid instead
of leaving the previous book in force; a reload replaces only the entries that came from the
manual file's own origin, and `accept_incidents` refuses that origin, so neither path can erase
the other's halts; an expected end so far out that adding the grace overflows is an open-ended
block; a `conid` or time written as a boolean or a number (`yes`, a Unix time) is refused. A
raising or silent cross-check source still never blocks, even a date-critical query (D28): the
cross-check is advisory evidence, and a source that fails must not close a market.

Findings of the port check against the pulse bot, chosen or deferred. `phase_at` and `is_open`
describe the schedule and ignore incidents (pulse's phase state returned a halt phase); only
`tradability` and `venue_status` apply incidents, and an order path must call them. A naive
datetime is refused. Manual and feed incidents are a union, several per venue, so an operator
cannot override a wrong feed halt (pulse let a manual record replace a feed record); Session 10
decides whether an override is wanted, and until then the block errs towards no trade. Incidents
carry a free-text reason, not pulse's four types, and an incident lapses at `expected_end` plus
grace where pulse stayed in a reopening phase until removed. The calendar window is two years
back and three forward (`calendars.history_years`, `horizon_years`), narrower than the
library's default, so an older date is `calendar_not_covered`. The IBKR map omits lite's
`ICEEU` → `XLON` and maps `IBIS` to `XETR` and `NASDAQ` to `XNAS` (lite used `XEUR` and `XNYS`):
Session 8 pins each code with a contract. Pulse's `build_upcoming_events`, pre-notice
thresholds, `loop_mode` and `scaffold_config` are not carried; a digest rebuilds the holiday and
early-close forecast from `SessionBounds.early_close`. The XNAS halt-feed endpoint and pulse's
rule that a structured feed needs a URL are not carried; Session 10 re-adds them with the feed.

### D29 · Venue configuration

`venues.<MIC>` holds only what every venue has (name, region, time zone, phase windows,
incident source, provenance); what applies to a few venues lives in sparse MIC-keyed sections
(`synthetic_calendars`, `lunch_breaks`, `seasonal_closes`), and
`calendars.venue_order_offsets` ships empty. A venue is synthetic exactly when its MIC is in
`synthetic_calendars`, otherwise its MIC is the `exchange_calendars` name. The four venues the
library lacks (CHIA, XNSE, XSHE, XSPX) ship with no holiday list and are therefore
`calendar_not_covered` until one is sourced; pulse treated them as open every weekday. The
Singapore midday break (the library skips it; pulse's test for it was an expected failure) and
B3's earlier close while New York observes daylight saving time are configured overrides, and
an override over a break or a seasonal close the library later gains is a build error. Orders
go 30 minutes after the open to 15 before the close, a venue override available, until
transaction-cost analysis calibrates them. Times in YAML must be quoted (`"10:00"`; PyYAML
reads an unquoted 10:00 as the number 600), and the schema says so. Every value carries a source
and `verify_live`. `ibkr_exchanges` is a keyed mapping from IBKR code to venue; the codes the
prompt names plus those lite evidenced (`FTA`, `CDE`, `AMEX`, `BATS`, `LSEETF`) are mapped, all
awaiting a live reading. Pulse's per-venue currency is dropped: the instrument carries it.

### D30 · Numerical configuration

Lite's `numerics.bsm`, `numerics.implied_vol` and `numerics.implied_vol.solve`
(`min_dte`, `max_dte`, `moneyness_band`, `max_non_converged_fraction`), `smile`
(`min_abs_delta`, `max_abs_delta`, `butterfly_max_iv_deviation`, `min_points`) and `liquidity`
(`max_spread_pct`, `min_open_interest`, `require_session_volume`) keys carry over unchanged in
value, as `config/defaults/{numerics,smile,liquidity}.yaml`. Left out because nothing consumes
them yet (Sessions 11 and 12 extend the same files): `solve.enabled`, `smile.skew_delta`,
`smile.term_structure.*`, `liquidity.spread_waiver_min_open_interest` and `data.*`.
`market_data_status_live` disappears: the decoded enum defines what is live, and the decoder
matches the whole raw flag against known spellings, so `NOT_REALTIME` is never live
(`VERIFY-LIVE(IBKR market-data flag spellings)`).

Inference and cost-model findings of the port check. Legacy fixed its floors and defaults (a
Newey–West minimum of 3, Spearman and bootstrap minimums of 5, 2000 resamples, one seed, 0.05
levels) inside the functions; every one is now a required argument that the caller reads from its
own configuration (Sessions 11, 14 and 18 add those keys with their consumers), and the functions
refuse a minimum below two observations for a mean and three pairs for a rank correlation, and an
`alpha` outside (0, 1) or `n_boot` below one whatever the data. The result is a dataclass and
`ci95` is `ci`. In `costs`, an order with a non-positive or non-finite quantity or price is
refused (legacy returned a zero commission), and an option leg must be given its contract
multiplier (a premium per unit costed as one unit understates the money at risk a hundredfold).
Money is returned unrounded: quantisation to the instrument's tick and the currency's minor unit
belongs to the order boundary (Session 5), not to a cost estimate. Before a tax rule's
`effective_from` the leg has no rule and is labelled `tax_not_modelled`, so a backtest of an
earlier date understates cost and must read that label; `breakeven_move` doubles one leg's
fraction and so omits a side-specific tax of the other leg (Session 14 prices both legs). The
USD 1.00 option order minimum is new. The cost functions refuse malformed
input instead of pricing it: a side other than `buy`/`sell`, an issuer country that is not two
upper-case letters, a negative or non-finite notional, a non-positive or non-finite multiplier
or entry price. An unknown tax has no amount (`None`), as an unknown commission has none. A share
whose issuer country is not given has `tax_unknown` and no total, because a share's tax follows
its issuer; for other asset classes a missing country is labelled `tax_not_modelled`. A
`maximum_fraction_of_value` of zero is refused at load. Open questions, recorded not decided: any
USD bond is priced on the US Treasury row because order terms cannot tell a Treasury from other
bonds (Session 9's bond rule supplies the distinction); a missing average daily value gives zero
market impact without a label. Lite's IV-solve tests that need the store (vendor column
untouched, rate as-of refusals, vendor-served skip, source ordering) stay with Session 11.

### D31 · Costs

`quant/costs.py` is rebuilt on configuration, not copied. Commission schedules are rows chosen
by (venue, asset class, currency); transaction taxes are rows chosen by the issuer's country,
asset class, side and date, each with its provision, a `confirmation` (`official`,
`secondary` or `none`), an `effective_from` and `verify_live`. Money and rates are `Decimal`
written as quoted strings; a float in the file is refused with a hint. **An unknown cost is a
status, not a number:** `no_schedule`, `beyond_tier` (the EUR bond tier past EUR 8,500 is not
held), `tax_unknown` (a rate that could not be stated is `null`), `no_friction_model`; a leg with
any gap has no total, and a position cannot be adjusted by a cost nobody knows. Taxes are applied
as a conservative upper bound: a tax that depends on a published issuer list is assumed to apply
when the caller does not say, labelled `scope_assumed`, and an issuer with no rule is "not
modelled", which is not the same as untaxed. The 2026-10-09 reading: UK stamp duty reserve tax
0.5 % (gov.uk, official) and Hong Kong stamp duty 0.1 % each side (IRD and HKEX, official), Spain
0.2 % and France 0.4 % (secondary sources citing the provisions; a further French rise for 2026
was tabled and not settled), Italy **no rate entered** (sources disagree: 0.1 % and 0.2 % until
2025, reportedly 0.2 % and 0.4 % from 2026-01-01). Commission rows are the figures this
repository holds (US shares and ETFs $0.005 per share, $1 minimum, 1 % cap; US options $0.65 per
contract, $1 minimum; Eurex options EUR 1.50, minimum unestablished; US Treasuries 0.002 % of
face, USD 5 minimum; EUR bonds 0.1 % up to EUR 8,500, EUR 1 minimum), all awaiting a live
reading. The legacy spread, slippage and impact tiers are kept as labelled assumptions. Two
behaviours of the legacy model change: its equity path booked a flat `commission_per_trade` of
0.0 instead of the per-share schedule it had just defined (fixed), and its open-interest gate is
dropped (the option gates own liquidity, and `None → acceptable` contradicted honest
degradation). Cost fractions are decimals, not percents.

### D32 · Guards and tooling

The unread-key scan learns `KEYED_MAPPINGS`: for a section keyed by data (venues, IBKR codes) the
section and key are stripped and the remainder must be read as an attribute chain inside
`calendars/`; controls show an orphan field is caught, a chain in another package does not count
and every keyed section is non-empty. A cost record's fields inside a list are checked by a test
that each is read in `quant/costs.py`. `test_no_hardcoded_trading_times` refuses clock times,
`time(h, m)`, `hour=`/`minute=` literals, IANA zone names and configured venue codes in source
files, with a positive control for each. The exact-leaf assertions in the loader and CLI tests
become "the foundation rows are present and every value has a source", and `settings show` cuts
long values. Import contracts: `domain` is a leaf; `store`, `calendars` and `quant` are mutually
independent; `calendars` reaches neither the broker nor httpx nor the store. Hypothesis runs
with `deadline=None` and, in CI, derandomised; the property tests share stateless factory
fixtures through an explicit health-check suppression. The golden snapshot of 207 sessions
(`tests/fixtures/calendars/`) pins what the library says on nine dates; a library upgrade that
moves one fails the test and is reviewed, not absorbed.

## Session 3 — IBKR read path (2026-10-12)

### D33 · Two transports, one session layer

`ibkr/transport/` holds one `Transport` protocol and two implementations chosen by
`OBLISK_IBKR_TRANSPORT`: `oauth` (lite's OAuth 1.0a, ported in substance, plus renegotiation
inside the refresh margin and on `recover()`) and `gateway` (IBKR's Client Portal Gateway,
installed from a pinned archive and logged in by a headless browser). `ibkr/session.py` sits above
both: one request pipeline (limiter → transport headers → HTTP → `classify`) whose repairs come
from the error class, so a 401 is repaired the same way whatever the transport. Rejected: a
transport-specific client (two places to fix the next lite-style finding), and IBeam as a
dependency (a separate service with its own configuration; it was read as a reference only).

### D34 · The Gateway's trust, runtime and login

The shipped Gateway certificate expired in 2019 and names no host, so a client could only use it
with verification disabled, which AGENTS.md forbids. Each install generates its own key pair for
`localhost` with `keytool`; the HTTP client verifies against exactly that certificate with
hostname checks on, and the browser accepts only its public key
(`--ignore-certificate-errors-spki-list`). The archive is accepted only at the pinned size and
SHA-256 (IBKR publishes it at an unversioned URL). The Gateway refuses profile `live`: a live login
needs the owner's second factor. Its Java runtime is selectable (`OBLISK_IBKR_GATEWAY_JAVA_HOME`)
and gated by `ibkr.gateway.java_major_versions: [11]` — the version the Gateway's guide names and
the one on which the login reached "Client login succeeds" once. Playwright is a runtime
dependency pinned to `>=1.56,<1.57`, the build whose Chromium (1194) the cloud image carries.
The login types key by key and waits before submitting (`keystroke_delay_ms`,
`presubmit_wait_seconds`): submitted at once, IBKR answered "Authentication failed". A refused
password is never retried (repeats can lock the username); an accepted password without a session
is retried up to `max_attempts`. Narrowed by D96: Playwright follows its newest release (the
cloud image's Chromium no longer matters, since cloud sessions do not log in), and the Java list
is an exception tested on the deployment machine, because the one observation behind it does
not separate Java 11 from 21.

**The login did not establish a session from the cloud container** (docs/knowledge/ibkr.md,
"Session 3"). The transport is kept as built, because the failure is after IBKR accepted the
credentials and IBKR documents that it does not support automating this login; whether it holds
on the owner's machine is the first live check of the deployment. If it does not, the remaining
routes are OAuth (if IBKR grants it) or a login the owner performs daily — a design question for
the owner, not something to improvise. For paper, OAuth is no longer one of them (D42): the
fallback is the daily login.

### D35 · Pacing below the published caps

IBKR penalty-boxes an IP for 15 minutes on any violation, so the limits are enforced before a
request is sent, never learnt from a 429. Every window is set below the published cap (9/s for
10/s, 45/min for 50/min, one per 6 s for one per 5 s, 16 minutes for 15) because the server
measures arrival, not departure. Windows are sliding and reserved atomically after a concurrency
permit is held, so the reserved instant is the sending instant. A `Retry-After` pauses the whole
endpoint group. A group that fails five times in a row opens its circuit for 60 s, then lets one
probe through. Groups are a list of records (paths and windows) rather than a mapping, so an owner
override replaces them whole and the unread-key scan reads the list as one key.

### D36 · Errors are classified by body, and each class says what may follow

`classify` keeps lite's body rules (the `/accounts` precondition, the two no-data bodies) and adds
one: `429 {"error":"Exceed number of conids"}` is a malformed request, never retried. A 401 or a
lost precondition re-arms once (`ssodh/init` first, then both accounts endpoints, as lite measured)
and, if that fails, re-authenticates the transport once more before raising: a dropped session
that `ssodh/init` cannot revive (IBKR's daily reset) needs a new login, not a fourth re-arm. A 403
is never retried. A competing session or a session that stopped being authenticated (seen on the
tickle) is re-established and alerted through a hook; lite only logged them.

### D37 · Values changed from lite

- `secdef.accept_first_on_no_exact_match` is `false` (lite: `true`): the session prompt requires an
  ambiguous resolution to be refused, and lite's own reading shows the first row can be an
  unrelated instrument. Contract identity (`ibkr/identity.py`) also confirms the currency and the
  listing venue through `/iserver/contract/{conid}/info` before a conid is accepted.
- `secdef.send_sec_type: false` (new): IBKR removed `secType` from the search on 2026-09-09; rows
  are filtered by their sections instead. VERIFY-LIVE.
- Snapshot field ids, from IBKR's field table: `open_interest` `7638` (lite `7089`, which the
  table names option volume); per-contract `implied_vol` `7633` (lite `7283`, the underlying's
  30-day at-the-money vol — kept for the underlying set, where lite measured it); `vega` `7311` and
  `theta` `7310` (lite had them swapped); `historical_vol_30d` `7087` (lite `7284`, now
  deprecated). Each is VERIFY-LIVE.
- `ibkr.history.max_bars_per_request` is dropped: lite declared it and nothing read it; the probe
  replaces it. `sessions_per_year: 252` joins `history` (lite's `data.trading_days_per_year`, which
  the chunk probe consumes).
- `http` keys are lite's values; `ibkr.session.tickle_interval_seconds` stays 45 although IBKR asks
  for ~60.

### D38 · Market-data availability and delay

Field 6509's first character decides the status (IBKR's table); MCP-style spellings still decode
through the domain enum. A snapshot does not say how late a delayed quote is, so a delayed quote
carries IBKR's documented upper bound (1200 s) with the basis `documented_bound`, and a real-time
quote 0 with basis `realtime`; frozen and none carry no delay. A labelled bound is used rather than
none because Session 4's collar widens with the delay and an absent delay would have to refuse
every delayed order.

### D39 · The trading-schedule cross-check

IBKR's `GET /contract/trading-schedule` is the third schedule source: `liquid_hours` per day
becomes a regular-session observation; `observe()` reads a cache filled by `refresh()` from one
reference contract per venue. XEUR is skipped (`trading_schedule.skip_venues`): Eurex product
hours differ from the library's venue hours and would disagree daily (the Session 2 follow-up).
A weekday missing inside the returned span reads as closed, because IBKR documents that it omits
non-trading days; a disagreement only blocks date-critical orders, so this errs towards caution.

### D40 · The read client's surface

Every method is a read. Results are frozen pydantic models in `ibkr/models.py`, distinct from the
domain models Session 4 adds; money (cash, NAV, positions) is `Decimal`, and an amount a float
cannot hold is absent. The NAV is read only in the configured base currency and refused, never
converted, otherwise. Positions, cash and NAV are never printed by the preflight. Configuration is
read through `self._ibkr.<section>.<key>` chains so the unread-key scan sees each key.

### D41 · Fail-closed reads, session repair and pacing (Session 3 review)

- **Incomplete portfolio reads raise.** A positions page with a row that has no conid or quantity,
  a page that is not a list, and a pager that reaches `portfolio.max_position_pages` raise
  `DecodeError`; `cash_balances()` raises when a currency's balance is unreadable (the ledger
  names them in `unreadable_currencies`). lite skipped such rows with a warning; a short read
  would look complete to reconciliation and the no-negative-cash rule.
- **Endpoint groups open only after the account guard.** `IbkrClient` raises
  `AccountGuardError` for any group read before `start()` (or `accounts()` then `run_guard()`)
  has passed, and a failed start releases the session.
- **Repairs.** Any failure inside re-authentication counts as one failed attempt (not only
  `IbkrError`), so the attempts, the alert and the lost-session state always follow; the
  keepalive survives any failure. One repair runs at a time: concurrent requests that failed
  before it finished retry instead of repairing again or failing. The OAuth token request signs
  each retry afresh (a resent nonce is a replay) and honours `Retry-After`; a token endpoint
  still unreachable or busy after them is an upstream fault, counted by the group's breaker and
  not retried again by the request (a signature refusal stays a refusal). The Gateway logs in
  again at most once per `gateway.login.min_relogin_interval_seconds`, so a repair loop cannot
  lock the username; inside the interval a repair stops at once and its alert says when the next
  attempt is allowed. The interval's default is a judgement, not a measurement: IBKR publishes no
  login rate or lock-out count. The Gateway's JVM, the JDK tools and the login browser inherit
  only `gateway.inherited_environment` (never an `OBLISK_*` variable); the Playwright driver
  process itself still inherits this process's environment.
- **Pacing.** A request waiting for its window holds no concurrency permit; a slot and a permit
  are taken together, so the recorded instant stays the sending instant.
- **History.** `daily_between` anchors its first request at `latest` (lite's was anchored at the
  request time) and returns only bars inside `[earliest, latest]`.
- **Availability codes.** The 6509 first-letter rule applies only to 6509-shaped codes; spelled
  flags go through the domain enum as whole words (`DELAYED_FROZEN` is frozen). The `L` price
  prefix reads as halted, as lite read it.
- **Configuration bounds.** Snapshot batches stay inside IBKR's ceilings (100 conids, 50 fields);
  every endpoint this client calls that IBKR paces on its own must sit in a rate group (the order
  path adds its endpoints in Session 4); a paper prefix starts with `D`.

### D42 · OAuth is the live account's; paper runs use the Gateway (owner, 2026-10-12)

The owner decided that OAuth keys (`.pem` files) are reserved for the live account. The `oauth`
transport now refuses any profile but `live`, an unset one included (and the Gateway keeps
refusing profile `live`): one
check, `transport_refusal`, runs in `build_transport`, the preflight and the CLI **before** any
credential is read, so a paper run never looks for, asks for or opens an OAuth key. For the same
reason `Environment` no longer checks at load time that the key paths exist; the live OAuth
transport looks for them when it loads its keys and refuses a missing file by name (a missing
key stays an error, never a silent fallback). AGENTS.md, MASTER_PLAN §5.12, `.env.example` and
SESSION_PROMPTS §A say so: cloud environments hold the paper Gateway login only, never OAuth
keys. lite ran OAuth on the live username; nothing in ObliskIQ used paper OAuth keys.

## Reading budget and the Session 4 split (owner request, 2026-10-12)

### D43 · Sessions read their slice, when they need it

Sessions 1–3 used 111M, 219M and 260M cache-read tokens against about 1M output tokens each,
and kept reaching usage limits; cache reads are the context size times the number of turns. A
profile of every transcript event of Sessions 2 and 3 traced the context to front-loaded
reading (Session 3 grew from 74k to 421k tokens in 73 calls before any code:
`legacy/lite/references/ibkr-playbook.md`, 124k characters, read whole, and whole legacy
modules), subagents reading `legacy/MANIFEST.md` whole (seven times in Session 2), polling
(42 `ListAgents` calls at 200k–500k of context), plan-mode echoes of the plan (78k characters
in Session 2) and an owner follow-up run at 525k–678k of context. Test output
and diffs were not drivers. AGENTS.md therefore reads by section and per deliverable ("Read
first", "Context budget"): prompts pin line ranges into the frozen `legacy/` tree for prose
sections and YAML keys; code is named by symbol and read with its helpers, a ported module
whole; money-path mechanics are read at the source by the session model; nothing is polled;
`make prompt S=<id>` prints one session's prompt. Rejected: an AGENTS.md line telling sessions
to disregard the prompts' reading directives and read nothing upfront. It contradicts the
prompts instead of fixing them, leaves the subagent, polling and follow-up costs in place, and
lets a money-path session write code before reading its plan sections. Rejected too: a line cap
on `explorer` answers; the risk is a summary standing in for source, so money-path source is
never delegated.

### D44 · Session 4 is split at the broker-write boundary

Session 4 carried more work than Session 3, which ended at 678k tokens of context; cost grows
with the square of a session's length, and the riskiest code is reviewed better as two smaller
diffs (owner decision). Session 4 builds everything at and below the broker-write boundary:
intents and their tables, the order client and its idempotency, the reply and quote policy, the
state machine, modes, the kill switch and the paper guard. Session 4a builds the pre-trade
checks above it, the approval queue, `flatten` and the paper order test. So that no write ever
exists without a gate, `place` and `modify` take a `PretradeClearance` that only the pre-trade
gate issues, bound to the intent id and the order's terms, single use and short-lived; Session 4
ships that gate refusing every intent (`pretrade_unavailable`) and Session 4a replaces it. The
lock is stated as five invariants with their tests, once, in Session 4's item 4 of
SESSION_PROMPTS (the gate; no short against every sell that could still reach the broker;
class by effect, never by role, with a fixed list of waivers for orders that reduce risk; a
kill switch that works when everything else fails; no exit that leaves a position
unprotected); the mechanism is the implementing session's design. A role is never trusted:
two sells that are each within the position could together go short, and a "protective" buy
is still a buy. Four review rounds of this PR found a new mechanism gap each time the prompt
described a mechanism; invariants with adversarial tests and `test-breaker` are the safer
contract. `cancel` takes no clearance and is allowed in every mode, so the kill switch
works when everything else fails. The paper guard, the kill switch and the mode are checked both
at Session 4's write boundary and as Session 4a reason codes, deliberately: defence in depth.
Rejected: shipping the write path unlocked for one session (nothing could call it yet, but the
lock is cheap and stays as an invariant); building the pre-trade checks first (the kill switch's
cancels and the no-margin whatif need the order client); clearing by declared role (the PR #6
review showed a protective stop plus a flatten could oversell).

## Session 4 — Order path, kill switch and modes (2026-10-15)

### D45 · How the clearance lock holds (I1)

A `PretradeClearance` (`domain/orders.py`) names one intent, one action (place or modify, a modify
also the broker order) and the exact terms, by the SHA-256 of their canonical form (`1.50` and `1.5`
hash alike; any other change, the venue, the time in force or an algo parameter included, makes a
new digest). Only `execution/pretrade.py` constructs one and only it calls
`store.orders.record_clearance`; an AST guard test refuses a call of the class or of its model
constructors, an alias or the name as a string anywhere else under `src/` (the store's read-back of
a recorded clearance is the one exception). The gate records each clearance in the `clearances`
table. The order client spends it in `StoreClearanceLedger`: the clearance presented must equal the
stored one field for field, so a `model_copy` with another effect, quote or expiry is refused, and
one `UPDATE … WHERE used_at IS NULL AND expires_at > now AND` every binding matches spends it, so
two processes cannot both use it; triggers make "spent once, never changed, never deleted" a schema
rule, and fix an intent's identity (only its state and terms move). The cOID ties the write to the
clearance's intent. A clearance is spent after the cOID lookup and the caller's last check,
immediately before the send, and the last check (expiry, mode, paper guard, kill switch) runs again
before each warning is confirmed, because the confirmation is the write that makes the order live; a
placement the lookup finds is reported, not sent, and keeps its clearance. Session 4's gate refuses
every intent (`pretrade_unavailable`), so no write can happen until Session 4a's checks exist.
Rejected: an HMAC-signed token checked in memory (it cannot be spent once across processes), and
trusting the object the caller presents.

### D46 · The system's own state is read whole, not as of a date

D27's point-in-time rule keeps future information out of decisions. The order path's tables hold the
system's own state — an intent's state, whether a clearance was spent, the mode, the kill switch —
and a time bound on them can only hide a row: a kill switch or a downgrade stamped a second ahead of
a reader's clock would read as absent, which fails open. So `store/orders.py` reads that state
whole, and the mode and the kill switch by their latest row **by insertion**, not by stamp. Account
snapshots (positions and cash) are measurements and keep `as_of`; a read header (`account_reads`)
separates an empty account from no read at all. A structural test lists the state reads exempt from
`as_of` and holds every other read in `store/orders.py` to it. The fill and broker-order reads serve
the live order path; Session 7's tax lots and returns read fills through a bounded read of their
own.

### D47 · A write is sent once; an unknown outcome is looked up, never repeated

`BrokerSession.request(..., retry=False)` sends a write at most once: no backoff, no re-arm, no
re-authentication behind the caller's back. The order client sorts every answer into: placed;
already placed (the cOID lookup found it); a duplicate cOID refusal (the order exists); refused (an
error body, HTTP 400 or 403); not sent (an open circuit, a transport that could not sign, HTTP 401
or 429); or **unknown** (any 5xx, a no-data body included, a transport failure mid-flight, a body
that is not JSON). An unknown or not-sent intent stays `submitted` and the owner is alerted; the
next attempt needs a fresh clearance and looks the cOID up first. Order ids are digits: `-1`, which
cancels every open order, is never sent. The lookup needs a live-orders list IBKR marks as a
complete snapshot, and the trades of `execution.orders.trades_lookup_days` (IBKR serves seven); an
incomplete list refuses the placement. IBKR invalidates an open warning as soon as any other request
is sent, so one placement runs at a time and the reply decider uses only what the clearance
recorded. A declined warning on a **placement** is followed by a lookup and a cancel of anything
listed under the cOID; the intent is rejected only when that cleanup is seen to succeed, and
otherwise stays `submitted`. A declined **modify** leaves the order as it was, because cancelling it
could strip a position of its protective stop. Both alert. One process places a given intent: two
processes holding two clearances for the same `submitted` intent could both pass the lookup, and
only IBKR's duplicate-cOID refusal (VERIFY-LIVE) would stop the second; Session 5's executor owns
retries, and `flatten` (4a) makes its own intents.

### D48 · The kill switch fails closed and needs nothing but a data directory

Two flags, either one turns it on: the file `KILL_SWITCH` in the data directory and the latest row
of `killswitch`. The file's entry is `lstat`-ed: `Path.exists` reads a permission refusal or a
symlink loop as absent, and `stat` a dangling symlink. A file that cannot be checked, a database
that cannot be read within `execution.kill_switch.store_timeout_seconds` or any other failure reads
as ON — the inverse of the legacy `MonitoringSuite.should_halt_reflexivity`, which allowed trading
when its check failed (legacy/old/ObliskIQ/modules/execution/monitoring_suite.py). The file's name
is fixed in code, not configuration, so `obliskiq kill on` still works when the configuration does
not load (it then sets the file alone and says so). The command never opens a brokerage session —
IBKR allows one per username and `compete` would take it from the running bot — so the running
service's sweep does the cancelling: every `sweep_interval_seconds` while ON it reads the live
orders, cancels every buy, conversion (`secType CASH`) or order of unreadable side, keeps a sell
unless the store knows it as a conversion, a buy or an order the gate cleared as adding risk, and
classes from the broker alone what the store cannot (the store unreadable, an order the bot did not
place). A spent modify may never have reached the broker, so the store's word on a sell is the most
cautious of the intent's spent clearances: any one cleared as adding risk cancels it. What the
broker alone shows adds risk is cancelled before the store is read, and after one failed store read
a pass classes the remaining sells from the broker, so a hung store delays no cancel of a buy. The
sweep reads the live orders row by row: a row that cannot be read is cancelled by its id, one
without an id is reported, and one bad row never stops the others (the cOID lookup keeps the
all-or-nothing read). No failure ends the loop: a failing pass or report hook is logged and the next
pass runs on schedule; a cancel that raises anything is a failed cancel, retried next pass. A cancel
needs only the paper guard, whatever the mode or profile pairing. The write boundary checks the
switch again immediately before each send of an order that adds risk. Turning it off clears both
flags and fails while either stays set.

### D49 · The reply policy, the quote policy and the collar

- **Whitelist by condition.** `o10153` (mandatory cap price) for any order; `o10331` (stop order
  risks) for stop-limit orders; `o10164` and `o10223` for cash-quantity orders. Every other id is
  declined: a percentage, tick, size or value limit (`o163`, `o382`, `o383`, `o451`), a stop that
  would trigger at once (`o403`), market-order notices, limited liquidity, `p12` (whose "yes" would
  switch on IBKR's price management algo), and anything unlisted. A warning without an id is
  declined. The owner may widen the list in configuration.
- **`o354`** (no market data) is confirmed only when the asset class's rule is `realtime_preferred`,
  the clearance's quote is `delayed`, read within `max_quote_age_seconds` and no later than
  `max_delay_seconds`, and the order is inside the collar. A delayed quote needs a sigma even when
  it claims no delay. A real-time quote under `o354` contradicts the broker and is declined.
  `max_delay_seconds` is 1200, IBKR's documented bound that every delayed quote carries (D38).
- **The collar is one-sided:** a buy's limit at most `collar` above the reference (the mid, else the
  last), a sell's at most `collar` below; a passive limit is never refused by it. A sell stop-limit
  must rest its stop below the band, so a stale quote cannot hide a stop already through the market;
  a buy stop-limit has no collar rule and is refused. `collar = base + k·sigma·√(delay)`, the delay
  in years of trading time (252 sessions of 6.5 h); a delayed quote without a sigma is refused, and
  a collar past `max_fraction` (5 %) means an implausible sigma and refuses. The defaults (0.5 %
  base, k = 2, the 5 % ceiling, quotes at most 60 s old) are judgements, not measurements; Session
  18's transaction-cost analysis calibrates them.
- Nothing is suppressed; `OrderClient.reset_suppressed()` re-enables what a user suppressed, so no
  warning bypasses the policy (calling it at service start is Session 6's).

### D50 · Money-decision packages take only listed protocol constants

Closes the Session 1 follow-up on the magic-number guard. In `execution` and `decision` a
module-level numeric constant passes only when `PROTOCOL_CONSTANTS` in
`tests/unit/test_no_magic_numbers.py` lists it with the unit or protocol fact it names (today
`SECONDS_PER_HOUR`); elsewhere an UPPER_CASE name still passes on its own. A tunable in a money
decision cannot hide behind a capitalised name.

### D51 · Modes

A deployment starts in `execution.modes.initial` (`shadow`; configuration cannot start live).
Upgrades are owner commands (`obliskiq mode set`), audited in `mode_audit`; `shadow → paper` needs
profile `paper`, and its audit row records the promotion criteria as untested (Session 18 builds the
promotion gate). Upgrades to `live-small` and `live` are refused (`promotion_gate_unavailable`), and
the write boundary refuses those modes too, whatever the audit holds — the port of the legacy
`assert_live_trading_allowed` (legacy/old/ObliskIQ/modules/execution/paper_gate.py), whose PASS
criteria belong to Session 18. Downgrades are always allowed and audited. A writing mode needs its
profile (`paper` with profile paper; the live levels will need profile live) and the account guard's
profile must be the modes' profile; the boundary reads the mode from the store before each write and
again immediately before the send, so a downgrade by another process stops the next write. An intent
is written only in the mode it was made for, never after its `expires_at`, checked again immediately
before the send and before each warning is confirmed (one that expires before it is sent becomes
`expired`), and a modify must name an order placed for its intent.

### D52 · Smaller choices of the order path

- **An entry buys.** A sell that opens would be a short or a written option: an `entry` sell is
  refused when the intent is made (I2 in 4a's gate is the real guard).
- **A sell is sized in units**, but for a conversion, so the no-short check (I2) can count every
  sell that could reach the broker; a modify keeps the order's kind of sizing.
- **Discipline.** lite's `entry_order_type: limit_only` (an entry may be a limit or an algo with a
  limit cap, never a stop-limit) and `exit_reasons_allowed`, applied to exits **and** protective
  stops (a protective stop is an exit order). The other `discipline` keys are Session 4a's or later.
- **Vocabulary.** Sleeves `core`, `satellite`, `options`; a holding is a sleeve's or `unmanaged`, in
  an append-only registry (the latest row is the assignment). Time in force `DAY`, `GTC`, `IOC`;
  `OPG` is not offered (orders avoid the auctions). Roles are labels: the domain lets a
  protective-role buy exist so the effect rules can be tested against it (D44).
- **cOID.** The uuid7's 36 characters; below `coid_max_length` the id's bytes in lower-case base 32
  (26 characters), cut to the length, never under 16 (80 bits); the store's `UNIQUE` refuses a
  collision.
- **Routing.** `conid` for `SMART`, `conidex` (`conid@venue`) otherwise; ibind refuses both in one
  order.
- **Intent states.** Facts within a state (a modify accepted, a partial fill, a warning answered)
  are events with `from_state = to_state`, allowed only while an order can be at the broker; a
  broker report the intent cannot follow (a stale read) changes nothing and is logged.
- **The store from async code.** `StoreWorker` opens the store on one dedicated thread and runs
  every call there, so the event loop never waits on SQLite's lock.
- **Shadow fills** are priced at the quote's reference moved against the order by the cost model's
  spread and slippage per unit, with its commission, and fill only inside the limit; a leg the cost
  model cannot price, a buy stop-limit or a conversion is not filled.
- **Test helpers** live in `tests/support/` (on pytest's `pythonpath`): the fake broker, the order
  rig and the read path the preflight walks.

### D53 · The transport is readied outside the pacing slot (Session 3 follow-up)

`Transport.refresh()` runs before a request takes its pacing slot and concurrency permit: OAuth
renews a token inside its refresh margin there, so a slow or failing exchange holds no permit and
cannot stall the kill switch's cancels; the Gateway has nothing to renew. `headers()` still renews a
token that expired in between. A re-arm stops at the first request the transport cannot sign instead
of renegotiating on each of its three steps. `run_read_only` takes a login driver and is tested end
to end through the transport it builds: live OAuth with a mocked handshake, and the Gateway with a
data directory and an injected driver.

## Session 4a — Pre-trade rails (2026-10-16)

### D54 · The gate's shape: stored intent, three stages, every failure named

`execution/pretrade.py` replaces Session 4's refuse-everything gate and stays the only issuer of a
clearance (D45's guard tests unchanged). It reads the intent from the store and never trusts the
object a caller holds. It refuses the shape of the request at once (state, expiry, terms, a modify
of another intent's order, a conversion); then stage 1 decides everything it can without the broker
(paper guard, mode, kill switch, instrument, prohibited products, eligibility, approval, calendars)
and stops there on any failure, so a refused paper guard or kill switch touches nothing; stage 2
reads the broker (positions, a complete live-orders snapshot, the trades, the quotes, and for an
order that adds risk the ledger and a whatif) and applies the quote policy and the collar; stage 3
runs in one store transaction (`BEGIN IMMEDIATE`) that records the trades and the positions read,
counts what the store holds (holdings, I2, cash commitments, caps) and records the clearance, so two
processes clearing at once see each other. Each stage reports every failure it finds, each with one
of 42 reason codes (`Reason`); a refusal's `code` is the first. The clearance carries the order's
notional (in its currency and the base currency) so later checks can add up open commitments, and
expires at the earliest of the intent's expiry and each quote's `observed_at + max_quote_age_seconds`
(the Session 4 follow-up); a quote stamped by IBKR earlier than it was read is dated by IBKR's stamp.
`PretradeGate.check` runs the same checks without issuing, for I5. Rejected: stopping at the first
failure (the owner would fix one thing at a time), and a gate that trusts the caller's intent.

### D55 · No short (I2): the position and every sell that could reach the broker

The position is the lower of the broker's fresh read and the store's. The store's is its latest
positions read when the instrument has no fill in the window the trades cover
(`trades_lookup_days − 1` days); with fills, every read from the last one before the first of them
onwards, each plus the fills reported after it, and the lowest wins. A sell in the same second as a
read counts against it and a buy does not, since second-resolution stamps cannot order them. So a
stale broker read after a fill cannot re-arm a stop, and a position that arrives without a fill (a
transfer) is not understated for days. The gate records every positions read and the trades it reads,
so a read always precedes the next fill. Counted against the position, for the instrument: every
working sell the broker lists at its remaining size, or at the larger size an unspent modify or the
latest spent modify could leave it at; every sell cleared but not listed, unspent until it expires
and spent until the broker lists its cOID or its intent ends, once per intent, less its fills; and
the request itself once (a modify at its new quantity less what the order filled). A live order of
unreadable side, instrument or size refuses (`account_unreadable`), and a working sell whose
reported fills exceed the fills recorded under its cOID refuses (`fills_unreconciled`): the position
is not accounted. An option sold beyond the position is a written option (`written_option`). Rejected:
the lowest of every read in the window (it kept a transferred position at zero for days), and
trusting the latest read (it is the stale one).

### D56 · Class by effect (I3), the cash check and the caps

A non-conversion sell is classed as reducing risk and cleared only if it passes I2 and the holdings
registry (the intent's sleeve owns the holding); every buy and conversion adds risk. A reducing order
waives exactly the kill switch, the caps, the order count, buy-side eligibility, the cash and
no-margin check and approval; prohibited products, the quote policy, the collar (a protective stop:
its spec, D58), calendars, holdings and I2 still apply. **No leverage** is read as "the order would
borrow": `margin_use` when the whatif's own cost (total, else amount) exceeds the cash in the order's
currency, and `insufficient_cash` when that cash, less every other open buy in the currency (one
figure per intent, the largest of its live clearances, less its fills; a clearance that names no
currency counts against every one) and less `execution.pretrade.cash_floor` (base currency, at the
ledger's rate), does not cover the cost. The whatif's margin fields are recorded, not gated on: a
cash-funded purchase in a margin account reports an initial-margin change without borrowing
(VERIFY-LIVE). A currency the account does not hold is refused (`currency_not_held`) until Session 5
funds it, and every conversion is refused (`conversion_unavailable`) for the same reason. A working
buy the bot did not place has an unknown cost and refuses buys (`cash_unaccounted`). The caps are
fractions of the NAV in the base currency (`max_order_fraction_of_nav` 0.25, lite's largest initial
position; `max_daily_fraction_of_nav` 0.50; `max_orders_per_day` 20, judgements) over a rolling 24
hours, so no time zone decides where a day ends; a clearance without a notional cannot be added up
and refuses. The whatif uses a cOID of its own, so a preview never makes the order's cOID read as
used (VERIFY-LIVE).

### D57 · Eligibility: the skeleton policy, the instrument directory and the mandate

Policy factories take the configuration with the profile (`PolicyRegistry.resolve(profile,
settings)`), because the skeleton's allowlist is configuration, not a jurisdiction rule.
`SkeletonEligibility` allows exactly `eligibility.skeleton_allowlist` and says "skeleton policy" in
every verdict; an option is eligible when it and its underlying are listed, or, when its underlying
is an ETF or ETC the list does not hold, under the mandatory close (MASTER_PLAN §2). Nothing in this
build describes instruments, so each allowlist record also carries the facts the gate needs (asset
class, currency, listing exchange, the resolved description the markers read, the multiplier, an
option's underlying and expiry), and `ConfiguredDirectory` serves them; an instrument nobody
describes is never traded. Session 8 replaces both with the universe. The conids are lite's
(EXV1 on Xetra, read live by lite; FCX from lite's conid fixture); the paper order test resolves each
through IBKR's contract search before it orders. The **mandatory close** uses lite's
`tenor.mandatory_close_trading_days` (10), counted back from the last tradable session
(`session_before(..., sessions=10)`): an option on an ineligible ETP is not opened on or after that
session, and from it on any order on an option is date-critical, so an unresolved calendar
disagreement blocks it (the earlier date wins); a stock order never is. The prohibited categories are
lite's; the schema refuses a configuration that drops one of the mandate's five, and
`allow_futures_options` stays false until the never-exercise rule exists (Session 12). The markers are
matched against every instrument's description, an option's underlying's included, never a ticker.

### D58 · No exit leaves a position unprotected (I5), flatten and the protective spec

`ExitCoordinator.place_exit` cuts the instrument's working protective stops by the exit's size,
largest first (a stop cut to nothing is cancelled), but only after `PretradeGate.check` has passed
every check of the exit with the stops counted at their cut size; then it clears and places the exit.
Each cut is recorded in `stop_resizes` with the stop's terms before it. `settle` restores what an exit
that ended without selling (refused, cancelled, expired, or refused before it was sent) left cut: a
modify back up while the stop works, else a new protective intent; either passes the gate, and the
restore covers the original size less what the exit sold (the larger of its recorded fills and its
orders' last report). A downgrade can always come between a cut and its restore — downgrades are
automatic and never wait — and the boundary then refuses the restore: the resize turns
`restore_failed`, the owner is alerted at once, and every later `settle` tries again (Session 5's
executor calls it each cycle). Rejected: blocking a downgrade while a stop is cut, and IBKR's
one-cancels-all groups (their Web API behaviour is unverified). `flatten` sells each managed long
position at the bid as an exit with reason `override`, through the coordinator, and reports what it
leaves alone (`unmanaged` and unassigned holdings, a short position, an undescribed instrument, no
bid); one position's failure never stops the others; it does not cancel working buys (the kill
switch's job). A sell stop-limit that reduces risk is checked against its **protective spec** instead
of the collar: its stop rests below the collar's band (so a stale quote cannot hide a stop already
through the market) and its limit sits at or below the stop, at most
`execution.pretrade.protective.max_limit_offset_fraction` (5 %, a judgement Session 5 refines) below.

### D59 · The approval queue and the paper order test

The approval queue holds every intent that adds risk of a sleeve listed in
`execution.approval.sleeves` (empty: off, MASTER_PLAN §2) or made with `approval_required`; the
owner's decisions are an append-only `approvals` table, an approval moves the intent from proposed to
approved and a rejection to rejected, and the gate refuses an intent that needs one and has none.
`obliskiq preflight --paper-order-test` needs mode `paper` (it writes nothing otherwise), buys the
contract's size increment at `limit_offset_fraction` (5 %) below the bid rounded down to the
contract's tick, and counts a US stock refused by the cash check (`currency_not_held`) when the paper
account holds no dollars as the expected outcome. Its report masks the account and prints no balance
or position.

## Design review (owner request, 2026-10-16)

A red-team of the plan against the code and the legacy evidence, challenged twice by two other
reviewers. Nothing in the repository measures an edge yet: the inherited verdicts concern other
specifications (v1 and v1.1: momentum on 102 US large caps with a regime overlay, 2016–2026,
`legacy/old/ObliskIQ/docs/STRATEGY_V1_1_VERDICT.md`), the Hunter's zero firings were a data gap
by the legacy's own account (`legacy/old/ObliskIQ/docs/ASYMMETRY_HUNTER.md` §9), and the purged
pattern library is unverified, not false (D14). The entries below therefore separate defects
found in the repository from hypotheses that need evidence, and keep the system's ambition.

### D60 · The objective: after-tax log growth under a drawdown limit (§1)

The system maximises the long-run growth of after-tax wealth. The primary statistic is the
annualised log growth of the account's **liquidation value**, net of costs, subject to a limit
on the peak-to-trough drawdown of mark-to-market NAV, expressed as a largest acceptable
probability of breaching it. **Tax is simulated as it arises**: every realised gain or loss goes
through the lot engine (FIFO, the two-month rule, offsets, carry-forward), each tax year's tax
is paid out of the portfolio at the profile's payment date in the following year, and
withholding on income is taken when the income is received; at the end of the horizon the
remaining positions are sold and the tax on that sale is paid. Rotation therefore pays its tax
when it happens, and deferral keeps only the benefit it really has. Distributions are compared
by their median log growth under a block bootstrap, with the 5th percentile of terminal wealth
reported beside it; the drawdown limit filters candidates rather than entering the score. Two
references are scored under the same rule: a global accumulating UCITS equity ETF bought and
held, and a global 60/40. They are yardsticks and a possible outcome, never the destination: the
system keeps its aim of finding asymmetric opportunities. Tax is a cost inside the simulation,
not a principle; the test decides when realising a gain pays.

**Research is separate from authorisation.** No research session waits for the owner's numbers:
Session 4c reports a frontier — for each drawdown limit and probability on a pre-registered
grid, the variant with the highest median after-tax log growth that respects it, and its outcome
against the equity reference. Choosing the variant that receives capital is the owner's
authorisation: the owner's drawdown tolerance picks a point on the frontier, recorded here
before Session 13 builds it. The owner's stated aspiration, relayed on 2026-10-16, is 30–50 % a
year or more — an aspiration, not a promise and not a gate; reports show each variant's distance
from it. For scale: with 40 % volatility, 30 % compound growth needs an arithmetic mean near
38 % a year, which no documented premium approaches; reaching it depends on a selection edge
still to be found and on concentration the drawdown tolerance must allow.

Evidence: Session 13's gate compared the core with a 60/40 on Sharpe and drawdown, which an
unlevered variant can pass while compounding less than the equity reference; an unlevered risk
balance weights by risk, not by growth (two assets of 16 % and 5 % volatility at equal risk hold
about 24 % equity). Rejected: mean terminal wealth (dominated by its tails); Sharpe against the
60/40 as the gate; after-tax wealth without liquidation (it counts a deferred tax as a gain);
deferring all tax to the horizon (it would hide rotation's tax cost); minimising realised gains
as a rule of its own; blocking research on the owner's risk numbers.

### D61 · Evidence first: the research track and the session sequence (§10)

Sessions 4b (research harness), 4c (the core verdict) and 4d (one selection hypothesis) run
before Session 5, and the owner runs T1 on the deployment machine in parallel (D68). A historical
test needs none of Sessions 5–12, so waiting for them delayed the result that decides what to
build. Session 14 is absorbed into 4c: work an earlier entry gives to Session 14 (D30's inference
keys and its pricing of both legs) belongs to 4c, and D9's benchmark of 3.14t is dropped because
no decision depends on it (standard CPython stays). Session 13 builds the variant the owner picks from 4c's frontier.
Session 19a builds what pays only at larger capital (D66). Session 21 is the second research
batch. Session 5 runs as written plus D67's additions: it is independent of the strategy, and
any outcome that trades US instruments or holds shares with stops needs its currency manager and
protective exits. No engine is deleted and no capital budget (MASTER_PLAN §11 row 2) moves before
evidence exists; Sessions 8–18 build what a registered hypothesis or a safety rule consumes and
put the rest behind activation conditions (D66). Rejected: cutting engines on the strength of
the legacy record, which is absence of evidence about them, not evidence against them.

### D62 · The decision rule for a pre-registered test (§5.9)

A test estimates one effect β, its pre-registered primary metric (net of costs and tax), with a
two-sided interval [L, U] at level 1 − 2α, α = 0.05 by default, Bonferroni-adjusted across the
tests of its confirmation run. θ > 0 is the smallest net effect worth the layer's added risk and
complexity, stated with its justification in the pre-registration, which the owner can amend
before it merges; the merge fixes it. Before inference the data must pass the pre-registered
adequacy checks (point in time, coverage, survivorship measured); if they fail, the outcome is
**not evaluable** and nothing is concluded. Otherwise exactly one of:

- **sufficient** — L ≥ θ: an effect worth having is shown;
- **insufficient** — U < θ: an effect worth having is ruled out (labelled *negative* when U < 0);
- **inconclusive** — L < θ ≤ U (labelled *positive* when L > 0, *sign uncertain* otherwise).

The three are exhaustive and mutually exclusive for any L ≤ U. Power is a label, not an outcome:
the pre-registration states the power to detect θ (target 0.8) and the effect detectable at that
power; an inconclusive result under low power is reported as such and never read as evidence
against. Actions: sufficient → shadow, then an exploration budget (D65); inconclusive positive →
shadow at zero weight, forward evidence pooled by the pre-registered method; other inconclusive →
archived, or collected forward when that is cheap; insufficient → retired with its record; not
evaluable → data work or archived. Rejected: categories that overlapped or left gaps (an interval
above zero whose estimate is below θ fitted none or two of the earlier ones), a separate
"anti-predictive" outcome (it is a label of insufficient), and reading "detectable effect above θ"
as untestable (it means low power for θ; a larger effect can still show).

### D63 · How a test is run: protocol, combinations, dependence and robustness (§5.9)

Each batch is pre-registered in its own PR (AGENTS.md session workflow) from
`research/preregistrations/TEMPLATE.md`: mechanism, universe, data and their point-in-time status,
primary metric, horizon, θ, power, the adjustment, robustness checks and the trial budget.
Exploration runs on a development sample and records every trial in the trial registry; at most
the pre-registered number of candidates goes to one confirmation run on a locked sample (a later
period and, where data allow, another region), and every trial counts in the deflated Sharpe.

**Combinations.** A combination is tested when it is pre-registered with the mechanism that makes
the interaction matter, beside its components on the same sample, and counted as a trial; it is
never found by search. The Hunter's k-of-n agreement is such a combination and is tested as one,
beside its components. Rejected: requiring each component to pass before its combination is
tested, which could discard an edge that exists only conditionally.

**Dependence.** Cross-sectional effects are a monthly series (Fama–MacBeth) with Newey–West lags
at least the horizon; events are clustered by date; intervals come from a block bootstrap. A
minimum sample is stated in effective observations, not counts: n predictions per period whose
outcomes correlate on average by ρ carry about n_eff = n / (1 + (n − 1)ρ) per non-overlapping
period (188 weekly names at ρ = 0.2 carry about five; at ρ = 0.05, about eighteen), with ρ
estimated from history by the harness before Session 18 fixes any minimum. The earlier claim
that a weekly 188-name panel would calibrate quickly is withdrawn: it counted predictions.

**Robustness.** The sign holds in every pre-registered subperiod; a placebo (the signal lagged past
its horizon, or random event dates) fails; the break-even cost multiplier — the factor on costs at
which the estimate falls to θ — is reported and must exceed the cost uncertainty the
pre-registration declares (the friction tiers in `costs.yaml` are labelled assumptions, so by
default the next tier up; measured costs use their transaction-cost interval). Rejected: a fixed
"costs doubled" check, whose factor stood for no stated uncertainty.

### D64 · Research code: one harness under the full rules; exploration outside (AGENTS.md)

Code whose output can move capital lives in `src/obliskiq/research/` under every rule of `src/`
(strict types, import contracts, the coverage floor), with tests of data correctness (units, dates,
release dates, duplicates), point in time (a test perturbs every observation after `as_of` and finds
every output unchanged), computation (hand-computed fixtures for the metrics, tax, costs,
intervals, power and n_eff) and reproducibility (input hashes and seeds in every report).
Exploratory scripts live in `research/explore/`, outside the coverage floor, and are never
evidence: only a run of the harness on a merged, locked pre-registration counts. Rejected:
excluding research from the coverage floor wholesale, which would also have dropped the tests that
keep its numbers right.

### D65 · Capital follows evidence; live results are monitored, not used as proof (§5.19)

A live record confirms a moderate edge only slowly (t ≈ IR·√years: about 16 years at an
information ratio of 0.5 for t = 2), so a layer's capital follows its research outcome (D62) and
live results serve as a monitor. A layer earns an exploration budget only with a sufficient
outcome, within the owner's ceiling; its size is the owner's tolerance for a bet not yet confirmed
live, a risk decision recorded as such. An increase needs the pre-registered pooled re-test
(history plus the live twin) to remain sufficient, and an owner command.

Each active layer has a **twin**: the same signals on the same dates, simulated with the cost
model. The live-minus-twin gap measures implementation and cost; the twin against the backtest's
predictive distribution for the elapsed time measures decay or chance. Both are reviewed monthly
from activation against one-sided boundaries whose false-alarm probability over the planned looks
is held at the pre-registered level by an alpha-spending function. A gap beyond its boundary
freezes the layer's new entries and opens an operational investigation; a twin below its boundary
reduces the layer to shadow at zero weight and opens a research review; a layer is retired only by
a new pre-registered test. Monitoring cannot tell decay from bad luck in a short sample, so it
reduces risk and never declares a verdict. Replaces §5.19's step rule on the live lower confidence
bound. Rejected: changing the statistical gate as if that created evidence; a sequential test on
live P&L as the proof of skill.

### D66 · Activation conditions and scale (§2, §5.3, §5.11)

A capability runs when a condition computed from configuration holds, never above a NAV threshold
key, as the bond rule already does: a satellite sleeve when its hypothesis is sufficient and its
budget over the absolute position floor gives the pre-registered minimum of concurrent positions;
the options sleeve when H8 (an option against a share with a stop, for a sufficient signal) is
sufficient and the sleeve buys the pre-registered number of median premiums within the friction
gate; an execution venue when a sufficient hypothesis selects instruments there, the permission is
verified and its market-data cost stays within the pre-registered share of the expected benefit; a
data source when a registered hypothesis, a component or a safety rule consumes it; an LLM role in
shadow within its cost share of NAV, and live only with a sufficient H7. lite's
`positions.min_size_pct_nav` becomes an absolute floor derived from commissions, as lite's own
`option_sleeve.min_premium_base_currency` is (Session 17): a floor in percent of NAV caps a 20 %
satellite at four positions at any capital. Capabilities that pay only at larger capital — direct
bonds and bills, stock baskets, mutual funds, further venues and working currencies, capacity
limits from average daily value, the upper tax brackets and foreign-asset reporting — are built in
Session 19a, before the red-team, so 1.0.0 carries them (owner, 2026-10-16). The owner chose this
over building each one when its condition first holds, which two reviewers proposed: an account
that grows past a threshold should not wait for a build session. The cost is kept down by order —
nothing at larger capital is built before the evidence-driven sessions — and by form: each
capability sits behind its activation condition, stays off until it holds, and is held working by
the NAV-ladder tests that show it turning on exactly when its condition holds. Within 19a the order
is bonds and bills, mutual funds, capacity limits, tax at scale, venues and currencies, then
baskets; a session that cannot finish them with quality splits under AGENTS.md.

### D67 · Protective exits and sizing under gaps (§5.11, §5.12)

A stop-limit becomes a limit order when it triggers; in a gap through its limit it does not fill, so
the loss at the stop is not a maximum loss. Shares are sized by a **gap-adjusted loss**: the stop
distance plus a pre-registered quantile of the instrument's overnight-gap distribution, wider across
earnings dates (Sessions 11 and 17). The protective spec's limit offset follows the instrument's
volatility and gap distribution rather than one fixed fraction (Session 5). A triggered stop that
does not fill is repriced toward the bid in steps, each through the gate, which clears it as
reducing risk. A limit deep enough to act as a market order is the owner's decision, off by
default, and recorded here if taken. Session 5 probes IBKR's attached orders (a child stop-limit
linked by `parentId`) on paper; if the Web API supports them, the clearance covers the entry and
its stop as one package (an I1 and I5 decision for that session), closing the window between a fill
and its stop. Reconciliation waits a configured grace period for IBKR's reporting lag before it
calls drift, and the executor gives the cost model the listing venue, never `SMART` (Session 5).
Rejected: escalating an exit to a market order (AGENTS.md's hard rule; the kill switch never
liquidates, D48, and `flatten` is a separate command).

### D68 · The deployment machine first (T1), and the transport's fallback (§5.12, §7)

The owner runs T1 (SESSION_PROMPTS §A.5) on the deployment machine before Session 6: the Gateway
installed, an unattended login on two different days (the second a fresh login after IBKR's daily
reset), `obliskiq preflight --read-only`, then `--paper-order-test`, and the masked reports pasted
into a short session that records them. Two days are the least evidence that the login works
unattended and works again after a reset; the in-process re-login is Session 6's keepalive.
Reliability is measured afterwards, over L1's ten sessions and L2's eight weeks, where no failure
bounds the daily failure rate at about 30 % and 7.5 % (rule of three, 95 %), and every failed
re-login alerts within one heartbeat: protective stops rest at the broker, so a lost session costs
cycles, not protection. If the unattended login cannot complete on the machine, the owner chooses
between a TWS API with IBC spike and a daily manual login. D15 rejected ib_async and TWS because
they need an interactive login; the Client Portal Gateway needs one too (IBKR does not support
automating it, `docs/knowledge/ibkr.md`), so that rejection rests on the REST surface and lite's
verified port, and the alternative stays open. Rejected: a fixed six-of-seven-days pass mark,
which tested no stated failure rate.

### D69 · Gaps in configuration and tax (§5.3, §5.4, §5.16)

- No commission schedule covers a European venue (`costs.yaml` has US shares and options, Eurex
  options and bonds), so the cost model returns `no_schedule` for every UCITS core order and a
  shadow fill refuses it (`execution/shadow.py`). Session 4b adds IBKR's schedules for the venues
  the core's vehicles trade on, each with a source and `verify_live`.
- Gains and losses on foreign-currency balances are not modelled. Session 7 keeps currency lots
  under the TaxPolicy, with the Spanish treatment confirmed from an official source.
- Currency-hedged share classes and look-through exposure (country, sector and currency of an
  ETF's holdings) are undefined: Session 8 describes both, Session 9 ranks hedged classes as
  distinct vehicles, Session 17's caps read look-through exposure. Whether a hedge inside a UCITS
  share class fits the mandate is the owner's call (MASTER_PLAN §11).
- lite's position caps were set for a mandate of 95 % stock selection and 5 % options; in a 70/20/5
  book their meaning changes (a 25 % initial position exceeds a 20 % satellite). Session 17 restates
  each against the sleeve it governs.

### D70 · Evidence from an LLM-derived signal is forward only (§5.10)

A model trained after a news item can carry what followed it, so an LLM-derived signal backtested
on history before the model's training cutoff is contaminated (Glasserman & Lin 2023 measure this
look-ahead bias in GPT sentiment). Only outputs produced live and stamped when produced count as
evidence, so the clock that matters is calendar time. A minimal EventExtractor therefore starts in
Session 10, with the first news collectors: one configured provider, the `ExtractedEvent` schema,
outputs stamped and stored with the model id and prompt hash, a budget cap, and nothing that
consumes them. H7's pre-registration (do extracted event features add to the price signal) merges
before its first live run. Session 16 builds the rest of the layer on that stored series without
breaking it; a change of model or prompt starts a new labelled segment. Without a configured
provider key the extractor is queued and the clock starts when one exists. Every role is
measurable for extraction quality by its evaluation set; none has a measured link to returns.
Rejected: waiting for the whole LLM layer before the first stamped output.

### D71 · The first research batches (§5.9)

Batch 1 is the core verdict (4c) and one selection hypothesis (4d), chosen by a data audit among H1
(cross-asset and country momentum through UCITS), H2 (insider-purchase clusters, SEC Form 4) and H4
(a stock-level composite: nearness to the 52-week high, 12-1 momentum with a crash guard,
profitability). The audit measures point-in-time coverage and the share of names lost to
survivorship. Among candidates with adequate data the preference is H2, then H4, then H1, because
the satellite's purpose is single-name asymmetry and H1 overlaps the core. H5 (whether a stop adds
value to the chosen signal, Kaminski & Lo 2014) is that batch's registered secondary test. Batch 2
(Session 21): H3 forced selling (spin-offs, index deletions), H6 conditioning the core's risk on the
variance risk premium, the excess bond premium and trend, H9 hedging pressure in COT for commodity
ETCs, the Hunter's components and their agreement rule, and the candidates batch 1 did not choose.
H8 (an option against a share with a stop, for a sufficient signal) needs option-surface history:
the chain collector stores it from Session 8, and buying history is the owner's decision. A
VIX-based simulation is exploration only: VIX is a 30-day S&P 500 measure and represents neither
three-to-six-month nor single-name options.

## Session 4b — Research harness (2026-10-17)

### D72 · Dependencies: yfinance for prices; no workbook library, no pandas in our code

Runtime adds `yfinance>=1.7,<2` (1.7.0, the latest release at least two weeks old; 0.2.x is
superseded), the loader of Yahoo end-of-day prices the session names. It brings pandas,
`curl_cffi` and lxml with it. As with `exchange_calendars` (D24), no pandas type leaves the one
module that touches it: `research/data.py` reads yfinance's frame through `iterrows()` and hands
out `YahooBar`s; the call itself (`yfinance_history`) is the only code that reaches Yahoo and is
left out of the coverage floor as network code, its frame shape pinned by a recorded fixture.
A mypy override confines yfinance's missing types to that module. AQR's data sets are `.xlsx`
workbooks; they are read by a minimal stdlib reader (`research/xlsx.py`: shared and inline
strings, numbers, booleans, Excel serial dates) rather than openpyxl, because the harness needs
the values of one sheet and a reader that refuses anything else is smaller than a dependency.
Rejected: calling Yahoo's chart endpoint with httpx (it answered HTTP 429 to a plain client on
2026-10-16; yfinance's browser impersonation got through), and openpyxl.

### D73 · The research store and point-in-time research history

Research history lives in its own database file (`paths.research_database`), opened with the
same migrations: the live store and its backups do not carry downloads, and an import contract
keeps `obliskiq.research` and `obliskiq.store.research` away from decision, execution, engines
and the broker ("live decision code never reads research data"). Migration 004 adds
`research_series` (vendor, URL, download stamp, payload SHA-256, how availability was derived,
bias notes) and `research_history` (one row per period and vintage, `observed_on` and
`available_on`; every read is bounded by `available_on <= as_of` and returns the latest vintage
of each period). A reload replaces a series whole, so rows a vendor dropped do not linger.
Availability is one of three kinds, each named on the series: `as_observed` (an exchange close,
an ECB rate), `vintages` (ALFRED's `realtime_start`) and `release_lag` (a declared lag after the
period for libraries revised without vintages: Ken French, AQR, FRED's CSV), the last labelled
in the bias notes. Series metadata and trials are the harness's own records and are read whole
(D46's rule). Conversion to the base currency uses ECB rates, which go stale after
`research.data.max_fx_age_days`; a value without a fresh rate is left out, never interpolated.

### D74 · The tax engine (§5.16)

`TaxPolicy` sits beside `EligibilityPolicy` in the registry; `es.yaml` selects `es_irpf` and
carries every value under `tax:` with its source, and `generic` (FIFO, no wash-sale rule) is
the template. The lot engine applies a policy; the ledger settles each year once, in order, and
a settled year refuses any later event (a deferral found after its year settled fails loudly
rather than landing in another year). Choices, each conservative where the law leaves room:

- Homogeneity (RIRPF art. 8): the same ISIN, or the same issuer and tracked index; ETFs and
  funds count together as collective investments.
- The two-month rule (LIRPF art. 33.5.f): matched unit by unit, oldest purchase first, against
  homogeneous units bought in the window and still held, then against purchases in the window
  after the sale; a unit takes one loss; a deferred loss rides on its units and is released in
  proportion as they are sold, as part of that sale's result, so a release into another loss
  sale inside a window is deferred again.
- Offsets (art. 49): one 25 % allowance per year and direction, shared by the year's own and the
  carried negatives.
- The savings scale records the tax years it was verified for (2025–2026, Ley 7/2024); the
  personal and family minimum is not applied; a year outside the table is labelled.
- A year's tax is paid in full on 30 June of the next year (Orden HAC/277/2026 art. 7.1); the
  60/40 split, which would defer 40 % to November, is not used.
- Costs enter the basis and the proceeds (art. 35), spread and slippage included.
- Withholding at source: one row, the US treaty rate of 15 %, confirmed from secondary sources
  only; income from a country without a row cannot be simulated.

The hooks a decision asks — `tax_cost`, `wash_sale_conflict`, `harvestable_losses` — run the
trade on a copy of the engine. Lots are saved whole (`tax_lots`); open loss windows and the
ledger are not yet persisted, which Session 7 does when it wires the engine to fills. A test
holds `tax/` and `jurisdiction/tax*.py` to structural numbers and two listed unit facts.

### D75 · The simulator (§1, D60)

Monthly walk-forward on the month ends of a calendar instrument. At each date: income since the
last date (withheld at source, credited at this date, converted at its ex-date's rate); the tax
of every year whose payment date has come; the strategy's weights of the value after that tax,
from a view of what was public on the date; delta execution with the legacy backtester's 0.5 % no-trade band
(legacy `delta_no_trade_band_pct`), sells first, the tax paid, then buys scaled by one common
factor found by bisection so the cash covers notional and every leg's cost (a minimum
commission makes proportional scaling never converge); whole units by default. Weights above 1
or below 0 are refused (no leverage, no shorting). Every leg is priced by `quant/costs.py` on the
listing venue, scaled by the cost multiplier; an unpriced leg, a missing or stale price or rate,
a sale costing more than it raises, or tax the account cannot pay stops the run, and the study
reports not evaluable. At the horizon everything is sold, every open year is settled and its
tax paid; losses still carried are worth nothing. Currency conversions are instant and free and
no currency balance is held (Session 5 adds the currency manager; D69's currency lots follow).
The mark-to-market series is taken after each date's trades and tax, and before the terminal
sale at the horizon. The legacy's √252-on-monthly bug cannot recur: growth is `ln(V_T/V_0)` over
calendar years (`research.simulation.days_per_year`, the legacy 365.25), and the bootstrap
works on months.

### D76 · The study, the lock and the registry in code (D62, D63)

A pre-registration is Markdown whose front matter carries every field of D63; `PreregSpec`
validates it. This build runs one study kind, a portfolio comparison: a candidate against a
reference, the primary effect their difference in annualised after-tax log growth of
liquidation value. Its interval comes from a paired moving-block bootstrap of whole months
(every instrument's return, its income yield and every rate together), both strategies
simulated on each path so each path pays its own tax; the interval has level 1 − 2α/m. Power is
the probability that the interval's lower end exceeds zero when the effect is θ,
`Φ(θ/se − z_{1−α/m})`, and the detectable effect `(z_{1−α/m} + z_p)·se` is its exact inverse;
before data, `research power` takes se from the declared per-observation sd, periods and n_eff.
The adequacy checks are the data being loaded, each series' availability among the
pre-registered kinds, coverage of the rebalance dates and the effective observations; the
survivorship report is always computed and printed with what a universe listed today cannot
show. Robustness: the sign in each subperiod, and the break-even multiplier by re-running the
historical path at scaled costs (a cost at which a strategy cannot trade is no effect), against
the declared cost uncertainty; a portfolio comparison has no placebo and says so. The deflated
Sharpe is taken on the monthly excess log returns across every trial the registry holds for the
id. The lock (`<id>.lock`: file, SHA-256, merge commit) is verified against git before any data
is read: the file unchanged, the commit an ancestor of HEAD and holding that exact text.
`obliskiq research lock` writes it (a command beyond the prompt's list: Session 4c needs it,
and a lock written by hand would be the weakest link). A confirmation run is single-shot once
it has an outcome (only a not-evaluable run may be repeated after the data are fixed) and writes
`research/reports/<id>.md`; `--explore` runs without a lock, is counted in the registry, stops at
the pre-registered trial budget and is labelled never evidence. Rejected: deciding power as the
probability of a sufficient outcome at θ (about α by construction, so it says nothing), and
treating a robustness failure as not evaluable (it is reported beside the outcome).

### D77 · European commissions (D69)

IBKR's fixed schedule for shares, ETFs and ETCs on Xetra, the LSE, Euronext Amsterdam and
Paris, Borsa Italiana and SIX, read on its published page on 2026-10-16, priced as **direct
routing** to the listing venue: 0.10 % of value with a minimum of EUR 4.00, USD 6.00, GBP 4.00
(LSE) or 6.00 (SIX) and CHF 6.00, no maximum, third-party fees included. The page's
SmartRouting column is half that (0.05 %, lower minimums); the order path's routing is Session
5's choice, so the higher figure stands until then and `docs/knowledge/costs.md` records both.
A new schedule kind, `fraction_of_value`, prices on the trade value. Rows are grouped by
currency and minimum; every row keeps `verify_live: true`.

## Session 4c — Core verdict (2026-10-17)

### D78 · The data for an after-tax test from 2005 in EUR

Yahoo's European listings start in 2008–2009 whatever the fund's launch (EUNL, IWDA, IQQW and
the iShares and Lyxor bond lines were checked on 2026-10-17), so a test from 2005 needs
long-history proxies. Three public vendors are added, each read as observed: **MSCI**'s
end-of-day index levels (MSCI World and Emerging Markets, net total return in EUR, from
29 December 2000), the **ECB Data Portal** (EONIA from 1999 and the euro short-term rate) and the
**Bundesbank** (zero-coupon yields of German Federal securities by residual maturity, daily from
August 1997). Two **derived** series are computed from stored ones, as research history of their
own (`research data load` computes them; the content hash names the inputs' hashes): an index
accruing the overnight rate day by day (EONIA through its last fixing, then the euro short-term
rate) and the index of a zero-coupon bond held at a constant maturity, rolled daily at the
curve's yield for the maturity left. The Bundesbank's rates are continuously compounded: its
published 1-, 5- and 10-year rates of 5 October 2026 equal the Svensson formula of that day's
parameters. A study deducts a vehicle's expense ratio from an index proxy day by day
(`expense_ratio`), so an index does not pass for a fund. Migration 005 widens the vendor list.
Rejected: building euro-bond returns from monthly average yields (FRED), which misstate a month
end; the ECB's AAA curve, which starts in September 2004 and leaves no lookback before 2005;
stooq and LBMA, which refused the container's requests.

### D79 · Several candidates against one reference; the frontier and its resolution (D60, D76)

A pre-registration names a `reference` and a list of `candidates`; each candidate is one test of
its log-growth difference against the reference, with `tests_in_confirmation` equal to their
number and every interval at level `1 - 2α/m` (Bonferroni). Every portfolio is simulated on the
same bootstrap paths, so pairs can be compared path by path. The **frontier** (`frontier`) takes
each drawdown limit and breach probability of its grid, keeps the portfolios whose share of paths
breaching the limit is at most the probability, and names the one with the highest median
after-tax log growth; portfolios whose paired growth difference from it has an interval holding
zero (same level) cannot be told apart from it, and the point resolves to the simplest of them
by the pre-registered `simplicity` rank, then the lowest turnover. References are on the frontier
(D60: a possible outcome). Each candidate is one trial in the registry (one row per candidate),
so the deflated Sharpe counts every candidate of the run. Every portfolio also runs **before
tax** (`SimulationSettings.taxed` false: the same trades, nothing withheld, settled or paid), and
the owner's aspiration (`aspiration`) is reported as the distance of each median from
`ln(1 + a)`. Power before data takes a candidate's own standard deviation
(`power.by_candidate`). A pre-registered **structure check** (`structure`) runs the tilt before
tax on the longest history (the Ken French US market and bill from 1926, `ff3_market`, public at
the month's close): descriptive, never an outcome and outside the Bonferroni count, its interval
from a block bootstrap of the two arms' realised returns.

### D80 · The core's variants in the harness (MASTER_PLAN §5.4)

One mechanism, `CoreStrategy`, with static weights by role: `banded_weights` states them,
`risk_balance` computes them once from risk budgets by environment (each environment's budget
split equally among its roles, a role in two environments taking a share of each) on a
**declared** long-run covariance, and `equity_overlay` holds one equity role at a fraction and the
rest in cash. Wide bands: nothing trades while every role is inside its outer band (the smaller
of an absolute width and a share of its target); when one leaves it, every role goes back to
target. The tilt scales each risky role by `quant.trend` (momentum over cash, a moving average,
hysteresis, a floor above zero) and gives the freed weight to the cash role. Tax-aware execution
holds a trend reduction whose tax (the lot engine's own answer) exceeds a pre-registered share of
the value sold, lets losses through, buys a role's swap partner when the primary would defer a
loss, and sells first from the vehicle with the smaller gain; a held reduction alone trades
nothing else. The static risk balance does not estimate covariance: the proxies for gold and
commodities start in late 2004, so no multi-year estimate exists before a 2005 start, and a
re-estimated risk balance could not be tested from 2005; MASTER_PLAN §5.4's covariance line now
says so. `quant/risk_budget.py` (cyclical coordinate descent on the convex risk-budgeting problem,
the legacy `_risk_parity` fixed) and `quant/trend.py` are in `quant/` so that Session 13's engine
uses the same code. Rejected: a risk balance re-estimated monthly from a short window, and
placeholder vehicles priced as if they were funds. The data limitation is narrowed by D90.

### D81 · The simulator at bootstrap scale

A strategy sees a `Book` (units, base-currency values, the investable value after tax owed, and
the lot engine's answers through read-only queries or a copy), not just units. The lot engine's
trial copy is built field by field instead of `deepcopy` (a twentyfold saving), and
`LotEngine.purchase_defers_loss` answers the purchase side of the wash-sale question without a
trial. Prices, rates, completed month-end closes and trend states are kept per panel; on a
bootstrap path the history before the first date keeps only the month-end rows a lookback
reads, and a role's two vehicles share one resampled series. The paths are drawn from the seed
first and shared among `research.workers` processes in contiguous chunks put back in order, so a
result does not depend on the number of workers (a test runs both). Found while benchmarking: a
part sale of a lot whose units all carry a deferred loss could leave `replacement_units` a
rounding hair above the units left (5/14 at Decimal's precision), which the lot's invariant then
refused; the engine now caps them at the units left (a test reproduces it).

### D82 · Session 13's quant modules exist already (SESSION_PROMPTS)

Session 13's prompt asked for `quant/risk_budget.py` and, if a registered variant uses risk
budgeting, `quant/covariance.py`. The first exists from this session, tested against closed forms
and an independent Newton solve; the registered variants hold static weights from declared risk,
so Session 13 builds no covariance estimator unless a later pre-registration re-estimates the
weights, and its prompt now says so (a fact this session changed, AGENTS.md session workflow).

### D83 · A confirmation run the harness made not evaluable is repeated after the fix (D76)

The first confirmation run of `core-allocation` (2026-10-17, lock at 7537c6c) stopped on a
bootstrap path. A sale of one unit of the commodity vehicle would have cost more than it raised
(the USD 6 minimum commission on a resampled price below it), and the simulator treated that as
an error, so every candidate came out not evaluable. Nothing in the outcome was computed: no
estimate, interval, path statistic or frontier. The historical runs had completed without error
and none of their figures were printed. The only figures produced were the structure check's: it
is descriptive, before tax, on the US market, and never an outcome. D76 lets a run that is not
evaluable be repeated once its data are fixed, and the same holds once the harness is fixed,
provided that:

- the fix changes no pre-registered constant;
- it follows from the mechanics alone, not from any outcome;
- it is merged before the repeat;
- the first run is recorded (here, ROADMAP).

The fix: a sale whose costs would exceed what it raises is not made, as no executor would place
it. Its units stay held and count as nothing at the horizon, and every report counts such sales
on the historical path and on the bootstrap paths.

Rejected:

- keeping the refusal: one rare path makes the whole test not evaluable;
- negative proceeds: cash would fall below zero, which the mandate forbids;
- a new pre-registration: a new id for a change no constant needs.

## Session 4d — First selection hypothesis (2026-10-17)

### D84 · The data audit and the choice of H1 (D62, D71)

`research/reports/selection-data-audit.md` measured, before any pre-registration, the data each
batch-1 selection hypothesis would stand on. A hypothesis has adequate data when every input is
point in time, the share of its point-in-time universe without usable outcome data is at most
5 % in every year of the sample (the bias a lost share m can cause is about m times the lost
names' outcome gap, and performance delistings lose about 30 % in their last month), the
signal's inputs exist, and the sample spans ten years. Power is reported beside these, never as
a criterion (D62).

- **H2** (insider-purchase clusters): the SEC's data sets are point in time from 2006, and 17,257
  clusters would carry about 1,030 effective observations; but free prices (Yahoo, under the
  reported symbol and the current ticker, with the reported price as an identity check) miss
  74.5 % of the 2006 events and still 11.6 % of the 2025 ones. Not adequate.
- **H4** (the stock-level composite): the same prices miss 76.5 % of a seeded sample of 2006
  issuers and 12.5 % in 2025; point-in-time fundamentals start in 2009. Not adequate.
- **H1** (country momentum through UCITS): MSCI's endpoint serves 51 of the 53 countries in MSCI
  ACWI since 2001; Venezuela and Russia are not served, at most 4.1 % of the members on a date.
  Adequate.

H1 is tested, with H5 beside it. Its cross-asset half is left out: through UCITS it has four
government bond markets and a handful of commodities with short or spot-only histories, too few
for a cross-section, and ranking asset classes is the core's trend tilt (4c). H2 and H4 are
`idea` entries in the pattern registry with the data that would make them evaluable (a
survivorship-free US price history with delisting returns: CRSP, or Norgate at US$630 a year,
read 2026-10-17), the owner's purchase decision. Rejected: testing H2 or H4 on the survivors
(a bias of unknown sign as large as the effect), restricting them to the last two years
(too few observations for a development and a confirmation sample), and testing their
components on published portfolios (a different hypothesis from the registered composite).

### D85 · The cross-section study kind (D63, MASTER_PLAN §5.9)

A selection signal is tested as a **cross-section** (`kind: cross_section`), beside the portfolio
comparison. The pre-registration names the members of a universe, each with the spans it belongs
and its series; a member the vendor does not serve is named with no series, so the share of the
universe lost to survivorship is measured on every rebalance date and checked against a
pre-registered maximum (an adequacy check: above it the outcome is not evaluable). Every member
is held through one kind of vehicle (venue, currency, yearly cost deducted from an index proxy,
the friction tier's size). Two rules build portfolios: the whole **universe**, equally weighted,
and the signal's **top group** (momentum from `lookback` to `skip` month ends back; a name enters
within the top `enter_fraction` and is kept while within the top `keep_fraction`); a lagged
signal is the placebo. Both trade only what changes (a leaver is sold, the entrants share what
the continuing holdings leave) and are reset to equal weights once a year, because a monthly
equal-weight rebalance of dozens of index vehicles pays a minimum commission on every drift.

Each portfolio runs through the simulator with costs and tax as they arise and is liquidated at
the horizon; its monthly series is the log change of the account's value, from the initial
capital to the liquidation value, so the series of the candidate minus the other is the
Fama-MacBeth series of the top group's excess return after costs and tax, and its sum is their
difference in log growth. The primary effect is its mean times the periods in a year; the
interval is a moving-block bootstrap of the series at the confirmation level (Bonferroni over the
run's tests), with the Newey-West interval beside it (lags at least the horizon). Each test names
its own theta and the standard deviation per month its power before data assumes. Exploration
(`--explore`) runs on the development sample; the confirmation run on the later confirmation
sample, read as of its end; each has its own minimum of months. Robustness: the sign in each
subperiod (the series' months), the break-even cost multiplier (both portfolios re-run at scaled
costs) and the placebo, which fails as it should while its interval's lower end stays below its
test's theta. Rejected: resimulating bootstrap paths as the portfolio comparison does (members
join and leave and some series start late, so a resampled month has no return for a member that
is in the universe at the path's date); keeping the tax path dependence out of the interval is
stated in every report.

### D86 · Protective stops in the simulator (H5; D67)

H5 needs a stop between monthly rebalance dates. `Simulator.run(..., stop=StopRule)` watches every
position on the daily closes until the next date: the stop trails the highest close since the
position was opened by `multiple` standard deviations of the daily log returns of the last
`volatility_sessions` sessions, scaled by the square root of `horizon_sessions`, fixed at the last
rebalance date. The whole position is sold at the first close at or below it, so a gap through
the stop is borne in full (D67: a stop-limit does not fill through a gap, and a close is the
worst a daily series can show); a touch inside a session that recovers by the close is not seen,
and a close published after its day cannot trigger it. The cash waits for the next rebalance date,
where the strategy may buy the name again. Rejected: filling at the stop level (it assumes the
fill a gap denies) and an intraday model (the index proxies have closes only).

### D87 · The first selection verdict: H1 and H5 insufficient (D62, D65, D71)

The one confirmation run of `selection-country-momentum` (lock at 89ea75e; report
`research/reports/selection-country-momentum.md`), 2008-01 to 2026-09 after costs and Spanish tax:

- **H1** (the top third of MSCI ACWI countries by 12-1 momentum against every member equally):
  −1.04 % a year, interval [−3.76 %, 1.39 %] at level 0.95. **Insufficient**: an effect of
  2 % a year is ruled out. The effect is below theta even without costs, and its sign differs
  between 2008–2016 and 2017–2026. The lagged placebo shows nothing.
- **H5** (the same top group with a trailing volatility stop against it without one):
  −5.46 % a year, [−7.92 %, −3.92 %]. **Insufficient, negative.**

What follows (D62, D65):

- Both are `retired` in `research/pattern_registry.yaml`, and only a new pre-registration
  revives them.
- No batch-1 selection hypothesis earns capital or a shadow run. The satellite starts with
  the Hunter's components in shadow (Session 15) and selection inside a thesis by the plan's
  rules (§5.5 item 8). It has no tested selection signal.
- A stop is a risk control for satellite shares (D67, §5.11), not an edge for this signal. Its
  cost in growth is measured: on country ETFs at the illiquid tier it cost more than five
  points a year.
- Session 8 no longer builds a universe for this hypothesis first (SESSION_PROMPTS edit).
- H2 and H4 wait for survivorship-free data, the owner's purchase decision (D84).

Rejected:

- Re-running H1 with other constants on the same sample: a search over the confirmation
  sample, which D63 forbids.
- Reading the inconclusive development sample (+4.19 % a year, exploration) as evidence: D64.

## Owner decisions (2026-10-17 and 2026-10-18)

### D88 · The drawdown tolerance, exposure caps and the option budget (D60; MASTER_PLAN §1, §11)

The owner set the drawdown tolerance at **30 % from the previous peak of the portfolio's
value**: D60's mark-to-market NAV of everything the bot manages. Holdings the bot does not
manage do not count toward it (owner). The horizon is very long: the owner does not need the
money now. The objective stays D60's: compound growth of wealth net of costs and tax.

**Meaning.** The tolerance is the limit D60's research uses to filter candidates. A candidate
respects it when the share of simulated paths whose drawdown reaches 30 % is at most the
acceptable probability, and the core's variant is chosen by the same rule. It is **not** a
guaranteed maximum loss, and **not** an order to sell when it is reached. The breakers of
§5.11 keep thresholds and keys of their own, and this entry sets none of them to 30 %.

**Measurement** (owner). The peak is the highest managed NAV taken at the day's last decision
cycle (US-late, §5.14). It is updated once a day, when that NAV exceeds it. The drawdown at any
moment is one minus the managed NAV over the peak. A value read inside the day is measured
against the peak but never raises it, so an intraday or delayed quote cannot lift the peak.
The owner allowed an update at every cycle if that were simpler. It is not: the daily rule is
one update in one cycle, and it matches the daily closes research measures on. Session 4c
measured month-end marks, which understate a fall inside a month; later studies measure daily
closes.

**Open** when this entry was made; D91 sets both. Reports show their implications without
assuming a value (`research/reports/drawdown-tolerance-implications.md`).

- the acceptable probability of breaching the tolerance;
- how that probability is stated: over a number of years, or as a rate that needs none
  (breaches per decade, the share of time beyond the limit). "Very long" alone does not define
  it, because the probability of at least one breach rises toward one as the horizon grows.

**Exposure caps by asset class.** None is set. They are configuration keys that are absent by
default, so no limit applies until the owner writes one. When present, they are read on
look-through exposure (D69) over the whole book (Session 17). The mandate's own constraints
(§2) are unchanged.

**Options.** The sleeve's premium budget stays at most 5 % (§11 row 2; lite's
`option_sleeve.budget_pct`), with lite's penalty balance as documented (§5.6). Neither is
reinterpreted.

Rejected:

- reading the tolerance as a stop or a liquidation trigger (the owner's ruling);
- counting unmanaged holdings toward it (the owner's ruling). Whole-book risk (§5.11) still
  reads them for concentration and exposure, which this entry does not change;
- setting caps by asset class now;
- assuming a probability or horizon on the owner's behalf.

### D89 · Adaptation is a requirement; it earns capital by test (D62, D65; MASTER_PLAN §5.4)

The owner requires the allocation to adapt to market conditions and opportunities, within the
owner's limits, through validated rules. A fixed portfolio does not replace that requirement
because it is easier to test.

**How the requirement is met.**

- **The target.** It is the neutral allocation for the owner's risk level and limits, plus every
  adaptation that is sufficient under D62.
- **Weight and execution.** Each adaptation is weighted by its evidence (D65), capped by the
  limits, and traded through the bands and the tax-aware rules.
- **Before it is sufficient.** An adaptation that is not yet sufficient runs in shadow, graded on
  outcomes after its signals (§5.19).
- **The static variants.** Session 4c's static variants are the fallback while no adaptation is
  sufficient, and the benchmark each adaptation must beat after tax.
- **Configuration.** Every adaptive parameter is configuration, pre-registered, and off until its
  test is sufficient.

**One mechanism at a time.** The mechanisms are tested separately, so that an outcome can be
attributed:

- risk estimation (the covariance behind the weights);
- scaling total risk;
- timing returns (trend, value, carry, macro);
- opportunities (the satellite).

Risk estimation comes first (Session 4e). Its forecasts can be scored against realised risk on
daily data, which is expected to give more power than a growth test; the power is computed
before any data are read, not assumed. D92 puts scaling total risk first and makes a forecast's
accuracy a check rather than the outcome.

Rejected:

- one combined "adaptive portfolio" tested at once: a failure cannot be attributed, and its
  power is the lowest;
- adopting an adaptation without a test.

### D90 · D80's data limitation is narrower than stated (D78, D80)

D80 said that no multi-year covariance estimate existed before 2005 and that a re-estimated
risk balance could not be tested from 2005. That holds for the proxies Session 4c chose:

| Proxy | From |
|---|---|
| GLD | 2004-11 |
| CRSOX | 2004-12 |
| TIP | 2003-12 |
| IEF | 2002-07 |

It does not hold in general, for two reasons:

- **Longer candidates exist**, each with a bias of its own (`docs/knowledge/research-sources.md`,
  `research/reports/global-data-audit.md`).
- **A history is not required.** An estimator that shrinks toward a declared matrix and adds data
  as they arrive needs no history before its first date.

Session 4c's result stands as run. A re-estimated variant is a new pre-registration (Session
4e), whose estimator is specified in full:

- the prior's weight in equivalent observations;
- the decay;
- the blend of short and long windows;
- the shrinkage;
- the largest weight change a month.

The same audit found that 4c's IL role holds US inflation through TIP, where a euro investor's
role calls for euro HICP-linked bonds. A euro proxy exists only from 2008 (the audit), so a test
from 2005 states that bias or starts later.

### D91 · The owner's point on the frontier: 30 % at most 5 % of the time over 20 years (D60, D79, D88)

The owner accepted the proposal of 2026-10-18. The drawdown tolerance of 30 % (D88) may be
reached on at most 5 % of simulated 20-year paths, measured as the drawdown from the running
peak. That is 95 % confidence of not reaching it over that span. The investment horizon stays
very long: 20 years is the span over which the probability is evaluated, close to the
20.75-year paths of Session 4c.

**What it selects today.** On Session 4c's frontier, the point (30 %, 5 %) resolves to **A**,
the static risk balance with wide bands.

- Five portfolios respect the point: A, B, C, D10 and D20. A has the best median among them,
  and the bootstrap tells none of the others apart from it.
- A reaches 30 % on 0.1 % of paths. Its median after-tax log growth is 3.22 % a year.
- A's comparison with the equity reference is inconclusive, like every candidate's.
- The 60/40 (10.7 %) and D30 (5.7 %) do not respect the point.

**What follows.**

- **Session 13's precondition is met.** `core.variant` defaults to A when Session 13 adds the
  key with its consumer.
- **The point sets the neutral allocation of D89.** A specification Session 4e finds sufficient
  is built beside it and earns weight by its evidence (D65). It must respect the same point.
- **Studies measure this point.** Later studies report it beside their grid, with the drawdown
  on daily closes (D88). Session 4c's month-end marks err low. A's margin (0.1 % against 5 %) is
  wide; D30's (5.7 %) is not.
- **The aspiration (D60) is far from A's median.** Closing that gap within this point is the
  task of the satellite and of sufficient adaptations.

Rejected:

- a rate form such as breaches per decade: the owner accepted a fixed span;
- a looser probability on the strength of the aspiration.

### D92 · Session 4e: total-risk scaling first, decided on the objective; bootstraps that respect lookbacks (D60, D63, D89, D91)

The owner accepted a challenge of D89–D91 (2026-10-18). At the owner's point the binding limit is
growth, not risk: A reaches 30 % on 0.1 % of paths against the 5 % allowed, and its median
after-tax log growth is 3.22 % a year, with about 15 % in equities and 68 % in bonds. Session 4e
as written would have tested the side that is already slack, on a proxy. Four rules follow.

**1. An adaptation is sufficient on the objective.** The primary outcome of a test of adaptation
is D60's: annualised log growth of liquidation value after costs and Spanish tax, minus static
A's, paired. That is the benchmark "each adaptation must beat after tax" (D89).

- A candidate that does not respect the owner's point (D91), measured on daily closes, is reported
  but cannot be sufficient.
- A forecast's accuracy is a check that an estimator works, reported beside the outcome and never
  the outcome. A forecast that follows recent volatility beats a constant one almost by
  construction (volatility clusters), so a test on it would be sufficient whatever it did to
  growth. This is D60's rejection of Sharpe against the 60/40 as the gate, applied to a forecast
  loss.
- An inconclusive-positive candidate that respects the point runs in shadow (§5.9); none is built
  on its forecast alone.
- The report places every candidate on the frontier at the owner's point under D79's rule. A new
  variant chosen from it is the owner's authorisation (D60), recorded here.

**2. Scaling total risk comes first.** D89's order changes.

- Re-estimating the risk balance's weights moves weight among roles of a book that stays fully
  invested and about two-thirds bonds. In a fall of stocks and bonds together (2022) it has
  nowhere to go, and A's risk is already fifty times below the limit.
- Scaling total risk acts on the binding limit. The share in the risky roles is a target
  volatility over the forecast volatility, capped at one (no leverage); the rest is in the cash
  role. It runs on bases with more equity than A, sized by a rule fixed before data so that they
  approach the point instead of staying far inside it.
- It is one mechanism. The relative weights among the risky roles stay static, there is no trend
  tilt (return timing is another mechanism), and each specification is paired with its declared
  twin: the same rule on the declared matrix, a static portfolio. The pair attributes what
  estimation adds.
- The estimator is the one D90 lists (the prior's weight, the decay, the blend, the shrinkage, the
  largest change), in `quant/covariance.py`. Re-estimated risk-balance weights stay a later test,
  under rule 1.

**3. A bootstrap respects the lookbacks it resamples.** A moving-block bootstrap cuts every signal
whose window spans a block join: the window then reads months from an unrelated period. For a
lookback of L months and blocks of b ≥ L months, the share of the window from another block is
(L + 1) / (2b) on average (checked by simulation). With 4c's 12-month blocks and the tilt's
12-month momentum it is about 54 %, so a tilted variant's breach and growth on the paths err
toward those of a randomly timed tilt.

- The symptom is in 4c. D50 fell at most 19.79 % on the real path but reached 30 % on 58.5 % of
  the paths, against 72.3 % for the equity reference held. On 1927–2026 the tilt cut the US
  market's largest drawdown from 83.65 % to 49.19 % (the structure check: before tax, US only,
  descriptive).
- A pre-registration whose candidates read a lookback sets blocks of at least twice the longest
  one (24 months or more for the tilt, about a quarter of the window from another block) and
  reports a sensitivity over at least three block lengths. Longer blocks leave fewer independent
  blocks and wider intervals; the sensitivity shows by how much.
- An EWMA's lookback counts as three half-lives, a window's as its length. Where twice the
  lookback exceeds a third of the sample, the bootstrap cannot respect it, and the report says so.

**4. The breach is also measured on a longer history.** A 20.75-year sample holds few crises, and
Session 4c has already read 2005–2026.

- A test at the owner's point measures the breach, as a pre-registered robustness check, on the
  longest history each candidate's roles allow (the global audit: MSCI from 2000, Ken French's
  developed regions from 1990, AQR's *Century of Factor Premia* from 1926). It uses the base
  currency where a series exists in it, and states the currency, vendor and index biases.
  It is the only part of the evidence that 4c did not read.
- The pre-registration states what of 2005–2026 its author had seen in 4c's report, and sizes its
  candidates from design falls and the declared matrix, not from 4c's breach table.
- Static A cannot be simulated before 2004 on its proxies. Its breach rests on 2005–2026 alone,
  a span in which bonds gained from falling yields, and every report says so.

**What does not change.** 4c's result stands as run, with its 12-month blocks. D91 stands: A is
the neutral allocation and the benchmark, and `core.variant` defaults to A. A's margin over D30
at the point (D30 reached 30 % on 5.7 % of paths, with a median 1.14 points a year higher and a
real-path drawdown of 11.99 % against A's 10.95 %) is not evidence that A grows more or risks
less: rule 3's bias counts against D30's tilt, and month-end marks count the other way. A later
study that compares tilted variants at the point applies rules 3 and 4.

Rejected:

- running Session 4e as written: its primary would be sufficient almost by construction, its
  growth outcome underpowered, and a variant would be built on a forecast;
- scaling total risk with the trend tilt on: two mechanisms in one outcome (D89);
- re-running 4c with longer blocks or more candidates on 2005–2026: a search over a confirmation
  sample already read (D63);
- choosing the target volatility from 4c's breach table: a constant fitted to an outcome already
  seen.

## Session 4e — Total-risk scaling (2026-10-18)

### D93 · The data of Session 4e, chosen on the checks alone (D78, D88, D92)

Step 1 checked every series the study and its long-history check read
(`research/explore/risk_scaling_data/`, recorded in `docs/knowledge/research-sources.md`):
metadata, span, duplicates, impossible values, continuity on the weekday calendar and alignment
with MSCI's calculation days. No return or statistic was computed, and nothing below was chosen
on one.

**The main sample keeps Session 4c's proxies.** Static A is the benchmark (D91, D92), and a base
is a fixed mix of 4c's roles, so each role keeps its one proxy in every portfolio:

- MSCI World and Emerging Markets, net, in EUR;
- the 5- and 17-year constant-maturity Bund zeros;
- the euro cash index (EONIA, then the euro short-term rate);
- TIP, IEF, GLD and CRSOX at ECB reference rates.

The bases will hold only WORLD, EM, EGB_INT, EGB_LONG and CASH, the roles that have a long
history. So no euro inflation-linked proxy is loaded (none holds IL), and the ECB's all-issuer
curve is not added: it would change A's euro bond proxy, the benchmark D91 fixed. The Bund-only
bias (no periphery spread in 2010–12) is stated instead.

**Daily data.** D88's drawdown on daily closes and an estimator of daily risk need a daily grid.

- The grid is MSCI's calculation days: a level on every weekday from 2000-12-29, holidays
  included, without a gap, a repeated value or a splice.
- Every other series carries its latest value public on the day, within the existing age limit
  of seven days. The checks bound how often that happens: TARGET, German and US holidays, at most
  three weekdays in a row.
- CRSOX repeats one net asset value on five sessions in May 2014, which a daily grid reads as
  four days without a move. It is stated, not repaired.

**The span.**

- The evaluation starts on 2004-12-31, the first month end on which every proxy of A has a price
  (CRSOX's first row), and ends on 2026-09-30, the last month end before the session: 21.75
  years, enough for D91's 20-year horizon.
- The estimators read daily data from 2000-12-29, MSCI's first level. That is the warm-up; the
  bootstrap keeps the real days before its first date.

**The long histories** (D92 rule 4), each with its own cash rate:

| History | Roles it covers | Frequency, currency | Span | Cash |
|---|---|---|---|---|
| MSCI | WORLD, EM, EGB_INT, EGB_LONG | daily, EUR | from 2000-12-29 | the euro cash index |
| Ken French developed | WORLD; EM mapped to it | daily, USD | 1990-07-02 to 2026-08-31 | its US one-month bill |
| AQR *Century of Factor Premia* | equity indices for WORLD and EM, fixed income for the bond roles | monthly, local excess returns | 1926-07 to 2026-02 | the US one-month bill (`ff3_market.RF`) |

- Ken French's emerging markets are monthly only (from 1989-07). An EM role is therefore mapped
  to the developed market there, and the report states it.
- AQR stamps each month with its last business day and Ken French with the calendar month end.
  The two are joined by month.
- AQR's market columns are excess returns over local cash, so a total return built on the US
  bill is a currency-hedged one; a EUR investor's currency risk is absent. The workbook does not
  say how the columns weight their countries.
- AQR's history is monthly, so a daily estimator cannot run on it as specified. The
  pre-registration states how its rule reads monthly returns there, and the report labels it.

Rejected:

- Ken French's daily US market from 1926: the US alone, the convenience bias D92 names.
- The euro all-issuer curve and IBCI, for the reasons above.
- Session 4c's start (2005-12): it waited for the tilt's 12-month lookback, and A reads none.

### D94 · The harness for scaling total risk (D88, D90, D92)

**The estimator** (`quant/covariance.py`), each step a constant a pre-registration fixes:

- weights over the daily observations: an EWMA by its half-life, an equal weight on a window,
  or their blend, each part summing to 1 over the observations it has;
- the second moment about zero (RiskMetrics: a daily mean is small beside its spread and is not
  estimated), with Kish's effective number of observations;
- Ledoit–Wolf shrinkage toward the declared matrix. For a target not estimated from the sample
  the covariance term of their optimal intensity is zero; the intensity is written for weighted
  observations and equals theirs with equal weights;
- a prior worth a stated number of observations of the declared matrix, which is the estimate
  before any data (D90).

Tests hold it to closed forms (equal weights, the EWMA's limit of effective observations,
Ledoit and Wolf's equal-weight formula) and to an independent implementation written as loops. A
lookback is three half-lives of an EWMA and the length of a window (D92 rule 3).

**The strategy.** `total_risk` is the core's own mechanism, `CoreStrategy`: its bands, the
no-trade band, whole units, the sale order and the swap partners. Only the targets differ: a
risky share of the target volatility over the base's forecast volatility, capped at one, at
least the floor and at most the largest change from the share held, the rest in the cash role.
The declared twin forecasts with the declared matrix, so its share is constant and it is a
static portfolio.

Tax-aware execution has two parts:

- buying through a swap partner when the primary would defer a loss;
- holding a reduction whose tax exceeds a benefit fraction (`hold_costly_reductions`).

The second is a separate switch. Held reductions would switch the mechanism off when embedded
gains are largest, which is after a long rise and before a fall; the pre-registration chooses.

**Daily data.**

- A study that reads daily risk or marks daily builds a daily history on its calendar
  instrument's days, each instrument in the base currency at the latest price and rate public
  that day.
- A month's daily returns sum to the change between its month-end prices, so the daily and
  monthly views of a path agree. A bootstrap month carries its days.
- The account is marked on each day from the holdings after the last rebalance (D88). A day
  without a price inside a month makes the run not evaluable.
- The drawdown statistics are the deepest fall, when each limit is first reached, the share of
  marks beyond it and the episodes reaching it.

**Tests and decisions.**

- Twins are simulated beside the candidates and tested only through registered comparisons.
- Every test counts in Bonferroni and is one trial.
- A candidate outside the owner's point is labelled so and cannot be sufficient: an interval that
  would make it so makes it inconclusive (positive).
- D92 rule 3 is refused at validation for a study that scales total risk, and every report
  states the lookback, the blocks and whether they respect it. A registered sensitivity repeats
  the bootstrap with other block lengths.

**The forecast check** scores each estimator's forecast of its base's next-month variance with
QLIKE (Patton 2011). It is compared with the declared matrix's constant forecast, with a
block-bootstrap interval of the mean difference. It is reported apart from the outcomes.

**The long-history check.**

- Each total-risk strategy whose base a registered history covers is run before tax, at a flat
  cost a leg. The estimator's lengths are converted to the history's observations through its
  periods a year.
- The real days before the history's start are kept before every path.
- Paths of whole months are as long as the owner's horizon. Return series are joined by month.

**A development window.** An exploratory run there simulates the comparisons' portfolios on
their own instruments, before the study's start, and tests the comparisons only. There is no
frontier, owner's point, sensitivity or long history, and no minimum of effective observations:
it is never evidence.

**The weight quantum** of the simulator is 1e-24 rather than 1e-20. A property test found a
one-unit position worth about a millionth of a percent of a billion-euro account that lost its
round trip. No result of Session 4c or 4d depends on it.

Rejected:

- Estimating risk from monthly returns. The forecast check and D88's marks need days, and one
  observation a month cannot follow volatility.
- A separate simulator for total risk: it would drift from the core's execution, which Session
  13 builds on.
- Calendar dates synthesised for each resampled day: a path's rows and its rebalance dates are
  all a strategy and the marks read.

### D95 · The total-risk verdict: nothing sufficient, nothing within the owner's point; A stays (D62, D89, D91, D92)

The one confirmation run of `core-risk-scaling` (lock at 69c8cc8; report
`research/reports/core-risk-scaling.md`), 2004-12 to 2026-09 after costs and Spanish tax, daily
closes, each interval at level 0.9833 (Bonferroni over six tests):

- **Against A** (theta 1 % a year): E-fast +1.56 % [−2.07 %, 4.58 %], E-blend +1.34 %
  [−2.54 %, 4.82 %], B-fast +1.33 % [−1.79 %, 4.13 %]. Each is **inconclusive** (sign uncertain,
  low power: about 0.04 to detect theta, as the pre-registration expected).
- **Against its declared twin** (theta 0.5 %): E-fast +0.06 % [−1.06 %, 0.90 %] and E-blend
  −0.16 % [−1.42 %, 0.94 %], inconclusive. B-fast −0.51 % [−1.23 %, 0.21 %], **insufficient**:
  re-estimating volatility adds less than half a point a year to the 60/40 base at its declared
  share.
- **The owner's point** (30 % on at most 5 % of 20-year paths): E-fast 20.9 %, E-blend 22.0 %,
  B-fast 16.2 %, so each is **outside** it. The twins are at 46.8 % and 40.1 %, A at 0.0 %.

What it shows:

- Scaling to a forecast volatility lowered the breach of each base held statically. It fell to
  less than half on the main sample and on MSCI's history in EUR, and by a quarter to a third on
  Ken French's (both before tax). It did not bring any base near the owner's point.
- The bases were sized so that a design fall of 51 % (E) or 30 % (B) would take the twin to the
  owner's limit. On the one history the twins fell 33.12 % and 31.05 %. MSCI World in EUR fell
  53.4 % from June 2007 to March 2009, deeper than the design's 50 %.
- What the candidates gained over A on the historical path came from their bases' equity. Their
  twins grew about as much, and they turned over 64–116 % a year against the twins' 18 %.
- The breach depends on the block length. With 60-month blocks, whose paths are nearer the one
  history, E-fast's is 4.9 % and B-fast's 3.7 %. The registered blocks are 24 months (D92 rule 3),
  and at 12 and 36 months the candidates' breaches are 14–20 %.
- On AQR's monthly century the scaled E bases breached more than their twin (32 % against 21 %).
  Estimators that read monthly returns react a month late at best.
- Each estimator forecast its base's next-month variance better than the declared constant. This
  is the forecast check, never an outcome: it is the growth at the owner's point that decides
  (D92 rule 1).

What it does not show:

- **Adaptation in general.** It tested one mechanism: three fixed bases scaled against cash by two
  estimators. Re-estimated risk-balance weights (D92), a blend with a multi-year window and
  return timing are untested. D89 stands: A is the fallback and the benchmark while no
  adaptation is sufficient. That is not a finding that the core should stay static.
- **That estimation cannot add growth.** B-fast's interval against its twin rules out half a
  point a year, not every positive effect. E-fast's and E-blend's hold zero.
- **A breach probability free of its model.** Across the registered block lengths (12, 24, 36
  and 60 months) the candidates' breaches range from 3.7 % to 22.0 % and the twins' from 19.0 %
  to 56.3 %. A's range is 0.0 % to 0.2 %: its 0.0 % is no path in 5,000, not zero risk. The
  verdict holds at 12, 24 and 36 months. At 60 months a path holds four or five blocks and comes
  close to replaying the one history, in which no candidate fell 30 %.
- **The whole book.** The point was measured on the core alone. The owner's tolerance applies to
  everything the bot manages (D88), so the satellite and the option sleeve must be checked with
  the core against the same point.

What follows (D62, D89, D92):

- No specification is sufficient, and no inconclusive one respects the point. Session 13 builds
  no total-risk variant, and nothing runs in shadow. `quant/covariance.py` stays for research.
- `research/pattern_registry.yaml`:
  - E-fast and E-blend are `tested`, inconclusive, outside the point.
  - B-fast is `retired`: its estimator is insufficient against its twin.
  - Only a new pre-registration revives any of them.
- **The owner's point resolves to A.** The frontier at 30 % and 5 % has only A. Every other
  point of the grid that any portfolio respects also resolves to A, since no candidate's growth
  can be told apart from A's (no portfolio respects a 10 % limit).
  There is no new point to offer the owner (D60), and D91 stands.
- Session 13: no total-risk scaling (SESSION_PROMPTS edit).

Rejected:

- Lower targets, other estimators or longer blocks tried on the same sample. The sample is spent:
  a new specification here would be a search over the confirmation sample, which D63 forbids.
- Reading the 60-month sensitivity as respecting the point. The registered blocks are 24 months,
  chosen before data by D92 rule 3.
- Reading the forecast check, or the development window's comparisons (−0.67 % to −0.08 % a
  year, exploration), as evidence (D64, D92).

## Dependency currency (owner request, 2026-10-19)

### D96 · The newest stable release by default; every pin is a registered exception (D15, D24, D34)

The owner asked for the newest stable version of every runtime and dependency, upgraded often in
small steps. Adopted with three changes, because the rule cannot hold literally: IBKR publishes
one Gateway build, dated data has no version, and a major release can need code first
(cryptography 50 deprecates the finite-field Diffie-Hellman whose PEM parameters `oauth.py`
loads; CI caught it because `filterwarnings = "error"` turns the deprecation into a failure). Versions and dates below are in
`docs/knowledge/runtimes.md` and `docs/knowledge/ibkr.md`, read on 2026-10-19.

- **The rule.** The newest stable release; for the OS and Java, the newest LTS. A `pyproject`
  range runs from the version tested to the next major, never capped at a minor.
- **Exceptions.** Each one is a row below with its reason, a review date and its exit. A comment
  at the pin points here rather than restating the evidence.
- **Merging.** The updates were piling up unmerged, not unseen: Dependabot opened them and none
  was merged for a week. Each session starts by merging the green Dependabot patch and minor pull
  requests; a red one is fixed or recorded in the ROADMAP's follow-ups (AGENTS.md). A major one is
  merged only after its changelog is read and each removed or deprecated API is checked against
  the paths CI fakes or cannot reach: the live OAuth transport, the Gateway and the browser (the
  login tests fake Playwright whole). An update of any size to Playwright, the Gateway archive or
  Java also needs `preflight --read-only` on the machine before a release carrying it is
  deployed: a Playwright minor changes the browser build.
- **Testing an upgrade.** CI runs `make check` on the pinned Python; a newer CPython is tried
  there before the pin moves. What CI cannot exercise (Java, Chromium, the Gateway archive, the OS)
  is checked by `obliskiq preflight --read-only` on the deployment machine before a release that
  changes it is deployed, with the bot paused once it runs continuously. No test reads today's
  date, so no review date can turn `main` red.

| Pin | Value | Reason | Review | Exit |
|---|---|---|---|---|
| Client Portal Gateway | the 2023-04-24 archive by SHA-256 and size | the only build IBKR publishes, at an unversioned URL (D34) | when the install refuses a changed archive | pin the new archive after a read-only preflight |
| Java for the Gateway | `java_major_versions: [11]` | the version the Gateway's guide names as tested; no version has opened a session yet | T1's record; 2027-07-01 at the latest | the newest LTS that passes T1's side test; failing that, D68's TWS API with IBC |
| Python | `>=3.14,<3.15` | 3.15.0 is ten days old; the suite passes on it, CI has not run it | 2026-11-30 | 3.15 after T1, once CI is green on it |
| uv in CI | `version: "0.12.23"` in `ci.yml` | none recorded; Dependabot does not read it | the next Dependabot round | one version for CI and machine in `[tool.uv] required-version` |
| cryptography | `>=46,<47` | 50 deprecates the API `oauth.py` parses DH parameters with | before the live account's OAuth runs | parse the parameters without it (the exchange is already `pow`) |

**Java.** The Gateway is compiled for Java 8 and bundles vert.x 3.5.0 and netty 4.1.15 (2017); its
guide asks for Java 8u192 or later, "tested successfully with OpenJDK 11", and names no upper
version. IBeam runs the same Gateway on OpenJDK 17. D34's reading was one attempt: the Java 11 run
that reached "Client login succeeds" still had no session, a later Java 11 run failed, and the
likely cause, the container's changing address, touched every attempt. So the record does not
separate 11 from 21, and no root cause is known. Temurin builds 11 to at least October 2027, so
11 is not yet a blocker: the review date is a quarter before that. T1 runs on 11 and tries 21
and 25 beside it (SESSION_PROMPTS §A.5); a version joins the list after it passes on both days,
and the newest that passes leads. If 21 fails where 11 passes, 17 and 21 with
`JDK_JAVA_OPTIONS="--add-opens=java.base/java.nio=ALL-UNNAMED --add-opens=java.base/sun.nio.ch=ALL-UNNAMED"`
narrow it down (old netty under JDK 17's strong encapsulation is a guess, not a finding).

**The deployment OS is Ubuntu 26.04 LTS on x86_64.** Java does not decide it (every LTS from 11 to
25 is packaged on 22.04, 24.04 and 26.04), nor Python (uv installs its own). Playwright does: it
supports 26.04 from 1.61, and 1.64 is locked. 22.04's standard support ends in May 2027, which
rules it out; 24.04 (to May 2029) is the fallback if Chromium's dependencies do not install on
26.04 (to May 2031). x86_64, because on arm64 the Gateway's native TLS library and one development
wheel are untried.

**Python 3.15.** Every locked package has a 3.15 wheel except PyYAML 6.0.3, which builds from its
source; the configuration loader uses its pure-Python `SafeLoader` anyway. On arm64, grimp (a
development tool) has no 3.15 wheel. On 2026-10-19 a scratch copy with `requires-python` widened
ran on CPython 3.15.0: ruff, mypy (also with `--python-version 3.15`) and the import contracts
pass, and pytest passes 3,569 of 3,572 tests with no new warning; the three failures are the tests
that pin 3.14. Python is not a property of the machine: uv reads `.python-version`, so moving to
3.15 is a change to this repository that the machine takes with `uv sync`. It moves after T1, so
T1 does not also test a new interpreter.

**The calendars past 2026.** exchange-calendars 4.13.2, the newest release, ends XSHG, XBOM and
XSES on 2026-12-31 (D24). No instrument trades there: the three venues are in the calendar layer
(`venues.yaml`: phase windows and incidents), and no eligibility list, allowlist or jurisdiction
names them. Past their coverage they read blocked, never open, and that is accepted. A venue the
universe admits (Session 8) must be covered through `calendars.horizon_years`, by the library or
by a `synthetic_calendars` entry as XSHE is.

**T1 is not held for any of this.** T1 tests an unattended login from a stable address and the
order shapes. The Java question needs T1's own evidence, Playwright 1.64 is already merged, and
Python, uv and cryptography do not touch the login. T1 changes the machine, the address, the OS
and Playwright at once, the confounding found in the Java evidence; with no working baseline there
is nothing to lose, so the failure path is fixed before the first login instead (SESSION_PROMPTS
§A.5, "If a step fails"). Its first step after a password accepted without a session is one manual
login through the Gateway on the machine: it tells the automation (Playwright, Java, the OS) from
the environment (the address, the account) in one login. A lock or a refused password stops every
side run until T1 has passed on Java 11. The side runs read a copy of `config/` through
`OBLISK_CONFIG_DIR`, since `config/owner.yaml` is tracked.

Rejected:

- Pinning to whatever the cloud image carries (D34's Playwright cap). Cloud sessions no longer log
  in (AGENTS.md) and the tests fake the browser.
- A test that fails when a review date passes. It would turn `main` red on a date, not on a
  change.
- Moving Java to 25 before T1. No version is known to work; T1 would no longer tell the machine
  from the runtime.

## Session 5 — Execution mechanics (2026-10-20)

### D97 · Every order routes SMART; the European rows follow it (D67, D77)

Every order is sent with `conid` and routed `SMART`, conversions included; `conidex` routing to
one venue stays expressible but nothing builds it. IB SmartRouting is IBKR's default, searches
every venue for the best price, and on IBKR's European fixed schedule costs half of direct
routing: 0.05 % of value with minimums of EUR 3.00, USD 4.00, GBP 3.00 (the LSE and SIX) and
CHF 5.00, against 0.10 % and EUR 4.00, USD 6.00, GBP 4.00 or 6.00 and CHF 6.00 direct
(`interactivebrokers.ie`, read 2026-10-20). Direct routing also rests on an unverified shape
(`VERIFY-LIVE(routing by conidex)`). So the European rows of `costs.yaml` price SmartRouting.
The cost model still chooses the schedule by the instrument's **listing venue** (D67,
`pricing.cost_venue`): fixed pricing is all-inclusive whichever venue SMART reaches, and the
listing venue is what the schedule is keyed on. A row `spot_fx` prices a conversion: 0.20 basis
point of its value in US dollars, at least USD 2.00, charged in the base currency (Tier I of
IBKR's spot-currency page, read the same day).

The verdicts of Sessions 4c to 4e were computed at the direct-routing rows, the dearer side. They
are not re-run: a verdict belongs to its locked pre-registration (D64), and a pre-registration
written after this one prices at these rows. The research simulator does not price conversions
yet (a follow-up). Rejected: direct routing for European listings so that the venue is known in
advance, which pays twice the commission for a venue choice nothing in the plan needs.

### D98 · Currency conversions: limit-capped, one per currency, cleared as risk (§5.12)

- **The order.** A conversion is an order on one of `execution.currency.pairs` (EUR.USD, conid
  12087792) with `isCcyConv` and `fxQty`, the amount of the currency it converts **from**:
  buying the pair sells the quote currency for the base, selling it sells the base for the quote
  (IBKR's tutorial on currency conversion orders, read 2026-10-20). IBKR's example sends `MKT`
  and both `fxQty` and `quantity` (the amount to); ObliskIQ sends `LMT` and `fxQty` alone,
  because no order is a plain market order. The limit sits `limit_fraction` (0.2 %) through the
  pair's reference against the conversion, on the pair's tick away from the reference, inside
  the collar; FX follows the `realtime_preferred` quote policy.
  VERIFY-LIVE(a limit conversion, its sides, fxQty without quantity, its trades).
- **The gate's path.** A conversion adds risk (Session 4's item 4): the paper guard, the mode,
  the kill switch and the reconciliation freeze apply. It is cleared against the currency it
  converts from (its cash less every open commitment and the floor, working conversions out of it
  included) and held to lite's single-foreign-currency cap (`positions.
  max_single_foreign_currency_pct`, 80 % of the NAV) on the currency it buys: that currency's
  cash, the conversion, conversions still working into it and the positions quoted in it at
  IBKR's marks. A position with no mark refuses it (`exposure_unreadable`); converting into the
  base currency skips the cap. A conversion funds a trade that passes its own checks, so it
  carries no eligibility, approval, calendar or notional cap of its own and no order count.
- **Funding.** For the buys about to be placed in one foreign currency: what they commit at
  their limits plus `buffer_base`, against the currency's cash less its open commitments. A
  shortfall is one conversion for the whole batch (IBKR's minimum commission), raised to
  `min_conversion_base` where the base cash and the cap leave room. The trades wait for its fill,
  at most `fill_timeout_seconds`; while a conversion into the currency works no second one is
  made; a refused, rejected or cancelled conversion leaves them unplaced and alerts
  (`conversion_failed`). Shadow mode converts nothing (D52).
- **Repatriation.** Cash in a currency no pending buy or conversion uses, beyond its buffer and
  open commitments, is converted home once the excess reaches `repatriate_above_base`. It is not
  waited on.
- **Rates.** The ledger's rate to the base currency, else the pair's quote. ECB reference rates
  serve reporting only.

The buffer, the minimum, the threshold and the limit fraction are judgements in
`execution.yaml`. Rejected: a market conversion as in IBKR's example (AGENTS.md), and one
conversion per trade (each pays the USD 2.00 minimum).

### D99 · Protective stops rest GTC; their spec is on the intent; attached orders wait (D67)

- **How long a stop rests.** `GTC`: until it fills or the bot cancels it. A `DAY` stop would
  leave every position unprotected from each close until the bot re-placed it after the next
  open, the moment gaps happen, and through any day the machine is down. The protective intent
  lives `intent_ttl_days` (365) and governs the stop's modifies; the executor never expires it,
  and renewing one before its end is a follow-up. VERIFY-LIVE(a GTC stop listed and its status
  readable in a later session).
- **The spec.** Each entry that wants protection carries a `ProtectiveSpec` (stop, limit offset,
  basis), stored on the intent (migration 006, fixed once written) and copied to its stops. The
  offset is the larger of `sigma_multiple` times the daily volatility and the overnight-gap
  quantile, never below `floor_fraction` (1 %), and a spec beyond `max_fraction` (10 %) is
  refused: a limit that deep acts as a market order. A missing input carries no weight and the
  basis says so (gap quantiles arrive with Session 11). This replaces
  `execution.pretrade.protective.max_limit_offset_fraction` (D58): the gate checks a reducing
  stop-limit against its own intent's spec, and a protective intent without one is refused.
- **Coverage.** After each fill and at every heartbeat, a position's working stops cover what it
  holds less the exits working beside it: a stop is placed, resized, cut (largest first; a stop
  beyond the position would sell short) or cancelled, each through the gate as an order that
  reduces risk, so with the kill switch on as well. A stop the broker does not list yet counts
  while it is new (`reconcile.grace_seconds`); past that its status is read, and one that cannot
  be read covers nothing: a new stop is tried, which the gate refuses while the old one may still
  work, and the owner hears of it. A stop the heartbeat re-places is alerted (`stop_missing`); one
  placed after a fill is not.
- **Gaps.** A triggered stop the bid no longer reaches moves toward the bid by `step_fraction`
  (0.5 %) of the reference every `step_interval_minutes` (2), at most `max_steps` (6) times, each a
  modify the gate holds to the sell collar. The emergency limit (`fraction_below_bid`, 5 %) is off
  by default; when the owner enables it, the last step may reach it. A gap past the collar with it
  off is alerted (`stop_gapped`) and the stop rests where the collar allows.
- **Attached orders are not adopted.** IBKR documents `parentId` (equal to the parent's cOID,
  on the child) and a whatif of a bracket (read 2026-10-20), not how a child stop-limit of a
  limit entry is listed, reported or cancelled. Adopting it needs one clearance for two orders
  (I1) whose child is protective (I5): a money-path design on unknown broker behaviour, and
  placing it even once on paper would be a write the gate does not clear. So the stop is placed
  on the first fill, the same cycle that sees it; the gap between a fill and its stop is one cycle
  plus IBKR's reporting lag. `--execution-test` reports item (c) as not probed. A later session
  can probe it read-only with a whatif of the package before designing its clearance.
- **Exits at the window's end.** An exit the executor gives up is cancelled while its venue's
  window is still open, so its cut stop is restored inside the window (I5); a restore outside
  it would be refused as not tradable.

### D100 · The executor and reconciliation (§5.12, D47)

- **Refusals.** A refusal no later moment lifts (an unknown or ineligible instrument, a prohibited
  product, the paper guard, an expired intent, a protective spec, a notional cap, and the others in
  `executor.PERMANENT_REFUSALS`) rejects the intent; any other leaves it staged to try again next
  cycle until it expires.
- **Pricing.** A buy or sell starts at a patient limit `start_ticks_inside_mid` (1) tick inside the
  mid, or as an IBKR algo with a limit cap at the collar's far edge where an `execution.pricing.
  algos` rule is enabled for the venue and asset class and the contract offers the algo (Adaptive,
  off until verified). It is repriced toward the touch at most `max_reprices` (3) times every
  `reprice_interval_minutes` (5), each a modify cleared for its new terms, refused for a buy with
  the kill switch on; then cancelled, also when the venue's window ends before the next reprice,
  and a successor priced afresh follows at the next tradable window, carrying the owner's
  approval. A protective stop is never repriced here.
- **After a restart** every intent that may be at the broker is looked up by its cOID: found, it is
  followed; a submitted one not found is placed again only after the grace period, its cOID looked
  up first (IBKR also refuses a cOID used in the last 24 hours, read 2026-10-20); a working one not
  found is alerted (`resume_unresolved`). One process runs the executor (D47).
- **Reconciliation** compares the broker with the store: every intent IBKR should list (after the
  grace period, its status read by id), orders and fills the bot did not place, fills the store
  lacks, and positions and cash per currency against the **anchor**, the latest green run that
  was settled, moved by the bot's fills since. A fill stamped in the anchor's own second counts
  either way (IBKR's stamps have whole seconds). Drift freezes new risk until a green run; a gap
  within the grace period of a fill or an order is reported as settling, not drift; more cash than
  expected (a dividend, interest) is noted and anchors the next run; less cash beyond
  `cash_tolerance_base`, a negative balance, a foreign order or fill freeze until
  `obliskiq reconcile accept --reason` records the owner's acceptance. Partial or missing data is
  `incomplete`, never green, and freezes too. A held conid no sleeve owns is assigned to the sleeve
  whose entry filled it, else `unmanaged`.
- **The grace period** (`reconcile.grace_seconds`, 120 s) is a labelled judgement: no fill has been
  observed through ObliskIQ. `--execution-test` measures the lag (item d) on the machine.
- **Shadow.** Intents of mode shadow are checked by the gate as if they were sent (`check` with
  `simulated`) and filled by `ShadowFiller` on the listing venue's schedule; nothing reaches the
  broker and nothing is converted.

Rejected: a reconciliation that compares each run with the previous one (a lag would read as
drift and heal by itself), and placing again any order not listed without the grace period.

### D101 · A conversion is previewed as the plain order on its pair (§5.12, D98)

Found by the owner's `--execution-test` on the paper account, 2026-10-20.

- **The whatif refuses the conversion flags.** `POST …/orders/whatif` answers HTTP 503
  `Service Unavailable` for an order carrying `isCcyConv` and `fxQty`, with or without a prior
  snapshot of the pair, as `conid` or `conidex …@IDEALPRO`, and with or without `outsideRTH`. The
  same pair as an ordinary limit order in units previews with its amount, commission and
  equity effect (EUR.USD, 100 units sold at 1.12126: 112.35 USD, commission 1.78 EUR).
- **Decision.** The gate and the execution test preview a conversion with
  `fx.preview_terms`: the pair, the side and the limit unchanged, sized in units of the pair's
  base currency (the amount sold, or the amount spent divided by the limit, rounded to whole
  units; below one unit the conversion is refused `whatif_refused`). The preview is never sent as
  an order and registers no cOID. The gate reads only the whatif's error from it; the conversion
  is still placed with `isCcyConv` and `fxQty`, and every other rail is unchanged.
- **Not decided.** IBKR raised `o10164` on the placed conversion; `execution.reply_policy`
  confirms it for cash-quantity orders that are not conversions, so the order was declined, as
  designed. Whether to confirm it for conversions needs the warning's text, which no log
  records; the next run must capture it (ROADMAP, Follow-ups).

Rejected: skipping the whatif for a conversion (the gate would lose IBKR's error for a
permission or size refusal), and confirming `o10164` for conversions unread.

## Session 6 — Walking skeleton (2026-10-20)

### D102 · The service: one process, jobs anchored to venue sessions, recovery from the run log (§5.14, D47)

- **One process talks to the broker.** `obliskiq serve`, `run-cycle` and every command that opens a
  brokerage session (`preflight`, `reconcile`, `flatten`) take an exclusive `flock` on
  `paths.service_lock`; a second one refuses and says who holds it. The kernel drops the lock when
  its process ends, however it ends. This is D47's "one process places a given intent" made a
  rule, and it keeps a command from taking the username's single brokerage session from a running
  service (`compete`). Commands that read only the store (`status`, `intents list`, `holdings`,
  `mode`, `kill`) need no lock; `kill on` never needs anything (D48).
- **The schedule.** A heartbeat every `runtime.heartbeat_seconds` (300) and four decision cycles a
  trading day, each an offset from one venue's regular session through the calendars: Asia an
  hour after Tokyo's open, Europe an hour after Xetra's, US an hour after the NYSE's, US-late an
  hour before its close; the digest half an hour after it. A venue with no session has no cycle
  that day; the others run. Daylight saving follows each venue (Europe moves an hour in UTC on
  25 October, New York on 1 November).
- **A cycle** is one run record of three guarded steps, `nav`, `rebalance`, `execute` (lite's
  sequence and run rows, legacy/lite collect/sequence.py and runlog.py, in 001_core's `runs` and
  `run_steps`): each step's row is written before it works, any exception is that step's `error`
  and the next step runs, and the run's verdict is the worst step's. **The heartbeat runs the
  executor's cycle every time**: reprices keep their pace, a staged intent goes out when its venue
  opens, a fill a restart's cOID lookup found is followed and its holding registered, the stops are
  checked and the broker reconciled every few minutes. With IBKR's pacing a heartbeat takes about
  half a minute of reads.
- **Recovery.** At start, under the lock, every run row still `running` belongs to a dead process
  and is closed `interrupted` at once (lite reaped by age, because nothing else proved the process
  dead; the lock does). Then `Executor.resume` looks every intent that may be at the broker up by
  its cOID, the skeleton's conids are confirmed, and the run log decides catch-up: a cycle or
  digest of the last day whose moment passed with no finished run runs now if it is at most
  `runtime.catch_up_minutes` (120) late, and is recorded `skipped` ("missed") otherwise.
- **Downgrade.** The plan makes downgrades automatic without naming a trigger this build can
  observe (breakers are Session 17's, monitoring Session 18's). The service downgrades a writing
  mode to shadow after `runtime.downgrade_after_failed_cycles` (3) consecutive cycles that end
  `error` or `partial`, audited in `mode_audit` and alerted; upgrading back stays an owner command.
- **Stop.** SIGTERM or SIGINT ends the loop after the job in hand; the keepalive and the kill
  switch's sweep stop, the health file says `stopped`, queued alerts go out.
- **lite's `runtime` keys** (legacy/lite/config.yaml:741-751): the SQLite ones are Session 2's
  `store.busy_timeout_ms` and `store.synchronous`, with WAL and foreign keys fixed in code; UTC
  timestamps are a rule (AGENTS.md), not a setting. No new key is consumed.

Rejected: reaping by age under the lock (a cycle legitimately running long would be mislabelled),
a PID file (reused, and meaningless after the reboot that most likely killed the process), and
running the executor only on the four cycles (a reprice every five minutes would happen four times
a day, and a restart's fills would wait for the next cycle).

### D103 · A shadow limit order fills at the touch (§5.12, D52)

D52 priced a shadow fill at the quote's reference moved by the cost model's spread and slippage,
filled only inside the limit. For the skeleton's vehicles that fills nothing: an ETF has no
market capitalisation, so the illiquid tier is assumed (0.5 % spread plus 0.5 % slippage), beyond
the 0.5 % collar a limit may reach; even the liquid tier (0.2 %) is beyond the quoted half-spread
of a liquid UCITS line, where every reprice ends. Shadow mode would never fill, and the follow-up
Session 5 left (does the friction tier bind?) is answered by construction.

- With the touch on the order's side (the ask for a buy, the bid for a sell), a shadow order fills
  there once its limit reaches it, and rests while it does not: a patient limit inside the spread
  fills in the market only if the market comes to it, which a snapshot cannot tell. Without that
  side of the quote, D52's rule applies. The commission is always the cost model's.
- An unfilled shadow order is repriced toward the touch on the real clock (its terms change;
  nothing is sent), then given up and retried at the next window, as a real one is. The last
  reprice reaches the touch, so a quote that holds fills in shadow within
  `max_reprices × reprice_interval_minutes`.
- Not simulated: queue position, size beyond the displayed quote, market impact. Session 18's
  transaction-cost analysis compares paper and live fills with this rule.
- Shadow converts nothing (D52): a buy in a currency the account holds no cash in waits.

Rejected: starting shadow orders at the touch (shadow would stop testing the pricing that paper
and live use) and keeping D52 alone (no skeleton order would ever fill in shadow).

### D104 · Managed capital and the skeleton book (§5.4, §5.15)

- **Managed capital** (`capital/`, Session 7 extends it). `capital.mode: whole_account` manages
  the ledger's NAV; `cap` manages `min(cap_base, NAV)`. The default is `cap` at 1,000 euros: the
  bot manages a small book until the owner sizes it in config/owner.yaml. An unreadable NAV
  manages nothing. The cap is the book's size, not its starting value: gains above it are not
  reinvested, and beyond the band a role is trimmed back. Session 7's managed capital follows
  flows and returns.
- **A capped book converts its shortfall alone.** The currency manager raises a conversion to
  `min_conversion_base` (1,000 euros) where the account's cash allows (D98); under a cap of a few
  hundred euros that would convert more than the whole book. In mode `cap` the conversion is the
  shortfall at the limit, as a small account with that much cash would convert.
- **The skeleton** (`core.skeleton_targets`, removed in Session 13): three of variant A's roles
  (D91), weighted as A weights them in Session 4c's pre-registration (WORLD 9 %, EM 7 %,
  EGB_INT 34 %) scaled to fill the book (0.18, 0.14, 0.68). The vehicles are UCITS funds:
  iShares Core MSCI World on the LSE in US dollars (so a cycle exercises the conversion),
  iShares Core MSCI EM IMI and iShares € Govt Bond 7-10yr on Euronext Amsterdam in euros. Their
  conids were read from IBKR's contract search on the MCP route, with a snapshot that priced each
  in its currency [MCP ✓ 2026-10-20]; the euro government line IBKR's search found for 3-7 years
  trades on SIX, so the 7-10 year line keeps both euro roles on one venue. Every start (`serve`,
  `run-cycle`) confirms each through `/iserver/secdef/search` and `/iserver/contract/{conid}/info`;
  a role IBKR does not confirm is not traded and is alerted. Like the core, no stops.
- **The basic rebalancer** (`decision/rebalance_basic.py`). A role trades back to its target when
  its weight leaves the inner band, the smaller of `trading.inner_band.absolute` (2 points) and
  `relative` (10 % of its target); in whole units, toward zero, a sale never beyond the holding;
  above `trading.min_trade` (the larger of 50 euros and 2 % of managed capital) and never below the
  order's own commission (an order the cost model cannot price is not made, since the floor cannot
  be checked); buys within the smaller of the account's and the book's cash, less every open buy
  and `execution.pretrade.cash_floor`, the largest shortfall first. A rebalancing sale is an exit
  with the reason `rebalance`, added to lite's `discipline.exit_reasons_allowed`: lite had no core
  to rebalance. Every value is a judgement; Session 13's engine brings the core's bands.

Rejected: A's eight roles at their weights (a few hundred euros buys none of most of them), equal
weights (nothing ties them to the chosen variant), and the whole account by default (a paper
balance of a million would make the skeleton trade a million).

### D105 · The owner's channel: alerts once per condition, the digest, health (§5.14, D68)

- **Alerts** come from the order path's `OrderAlert`, the session's `SessionAlert`, calendar
  disagreements, the kill switch's sweep and the service's own findings (a failed cycle or
  heartbeat, a stuck intent, a venue blocked when it should trade, an unconfirmed target, a
  downgrade). A **condition** is sent once and held under its key (its intent, or its words with the
  numbers masked) while it holds: it clears when a round of the job that raised it ends without it,
  or when an event resolves it (a re-login clears the session's alerts). An **event** is sent each
  time. The book is a JSON file written atomically (market-pulse-bot's state file); one that cannot
  be read is set aside. This answers Session 5's follow-ups: a stop the heartbeat cannot place, or
  a conversion that fails at each funding attempt, reaches the owner once.
- **A stuck intent** is one submitted with no answer, or open past its expiry, for
  `runtime.stuck_after_minutes` (30): L1's "no stuck intent" (§5.19). A protective intent is never
  stuck: it governs a resting stop.
- **A failed re-login reaches the owner within one heartbeat.** Session alerts are sent the moment
  the keepalive raises them, and configuration refuses a heartbeat shorter than the keepalive's
  slowest path to its alert (failed tickles, then every re-login attempt and pause: 150 s against
  300). A repair the Gateway defers (its re-login interval) marks the session lost until the
  deadline: reads fail at once with no repair and no repeated alert, and the keepalive logs in
  again when it passes (Session 3's follow-up).
- **Telegram** through the Bot API in HTML mode, split under the message cap (the legacy notifier
  and run summary); a send never raises and the token, part of the URL, is never logged. Without
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` every message goes to the log. lite rejected an
  outbound notifier for its collectors; the plan's service needs one, and the exit code, the run
  log and `obliskiq status` stay the channel that works when Telegram does not.
- **Health**: the heartbeat rewrites `paths.heartbeat_file`; when `OBLISK_HEALTH_PING_URL` is set it
  is fetched after each heartbeat. The prompt put the URL in configuration; it is an environment
  secret instead, because whoever holds it can report the bot alive, and config/owner.yaml is
  committed.
- **IBKR's warning text** is kept with each reply (masked, at most 500 characters) in the intent's
  history and in a declined warning's alert, so the next live conversion answers D101's open
  question about `o10164`.

## Session 7 — Capital, cash flows and tax (2026-10-21)

### D106 · Managed capital follows flows and returns; one budget reader (§5.15, §11, D104)

- **A capped book is its own account.** In mode `cap` the book's cash is `capital.cap_base` (the
  start, 1,000 euros), plus the owner's recorded flows that count toward the book, plus the cash
  each of the book's fills moved, per currency and valued at today's rate; its value is that cash
  plus its holdings at IBKR's marks. Managed capital is that value, never more than the account's
  NAV less its unmanaged holdings. So the book follows its returns and the owner's flows, as D104
  said Session 7 would: the cap is where the book starts, not a ceiling. A paper account's
  balance never moves with an owner's money, so on paper a flow adjusts this virtual book only;
  in shadow the book is its simulated fills.
- **The whole account** manages the NAV; its cash is the NAV less the book's holdings and the
  unmanaged ones.
- **Investable capital** = `max(0, min(managed, NAV − unmanaged) − reserved)`: unmanaged
  holdings are counted and never traded, and an open withdrawal reservation comes off at once. A
  NAV, a holding, a currency or a reservation that cannot be read or valued manages nothing, with
  the reason (honest degradation).
- **Budgets** (`capital.budgets`): core 0.70, equity satellite 0.20, option premium 0.05, cash
  floor 0.05 (MASTER_PLAN §11 row 2). A sleeve with no active layer leaves its budget in the core
  (Session 17's rule), so the skeleton's core takes 0.95 and nothing sits idle for want of a
  satellite; the cash floor stays cash. `capital.satellite_ceiling` (0.20) bounds Session 18's
  move of the satellite's share; configuration refuses budgets above 1, and a ceiling below the
  satellite's budget or above what the other budgets leave. One reader (`CapitalReader`,
  `SleeveBudgets`) serves the planner, the rebalancer and later the options sleeve and the
  decision layer.
- **External holdings** (lite's `external_positions`): entered by hand with the day they were
  last confirmed, updated in place on (broker, symbol, ISIN), stale after
  `positions.external_data_stale_days` (30, lite's value); anything computed over a stale one is
  reported unreliable. They are kept beside managed capital, not in it: IBKR's NAV does not hold
  them, and Session 17's whole-book risk reads them.

Rejected: the paper account's cash as the book's (it never moves with a recorded deposit); an
inactive sleeve's budget kept as cash (a quarter of the book idle until Session 15).

### D107 · The cash-flow ledger and unexplained cash (§5.15, D100)

- **Records** (migration `008_capital.sql`): a `Deposit`, a `WithdrawalReservation` (amount,
  currency, due date, reason, state open → withdrawn | cancelled, with its events), a
  `Withdrawal` that closes a reservation in the same transaction, and an `Adjustment` (dividend,
  interest, fee, withholding tax, correction, other). Nothing is deleted: an open reservation or a
  flow the broker has not shown may be cancelled, audited. A flow states whether it counts toward
  the book (`in_book`), so an unmanaged holding's dividend never moves a capped book.
- **The CLI echoes and needs `--confirm`.** `obliskiq cash deposit|reserve|withdrawn|cancel|
  classify|list`; ids carry their kind (`R`, `F`, `E`) because the kinds share numbers. A deposit
  dated in the future and a reservation already past due are refused.
- **Reconciliation explains cash** by the last reading plus the bot's fills plus the flows
  recorded since (`execution/fill_cash.py`). A gap beyond
  `execution.reconcile.cash_tolerance_base` that a recorded, unmatched deposit or withdrawal of
  that amount explains is matched to it (stamped with the reading's time, so it is never counted
  twice); any other gap is a **cash event**, one pending per currency and refreshed while it
  lasts, a freezing finding: new buys stop, the owner is alerted, and the freeze lifts at the
  next reconciliation after
  `obliskiq cash classify E<n> <kind> --confirm`. Classifying records the flow that explains it.
  While unexplained cash is the only freeze, the positions reconcile, so the reservation
  planner still sells for a withdrawal (a sale adds no risk, so the gate lets it through); any
  other drift stops the planner too. A fill whose cash cannot be computed is `cash_unpriced`.
  How IBKR words dividends, interest and fees in its activity was not observed (no paper
  credentials in a cloud session): VERIFY-LIVE, and until then the owner classifies.

Rejected: classifying from the size of a change (a fee and a correction can be the same amount);
a freeze on sales too (a reservation must still be met while the owner is away).

### D108 · The reservation planner (§5.15)

- **Order of sources.** The cash line first (the book's cash and the proceeds of sales already
  working), then sales ranked by `tax + trading + strategy` cost per euro raised, each weighed by
  `capital.planner.weights` (1, 1, 1). Tax is the lot engine's on the live lots, so losses go
  before gains and a loss the two-month rule would defer saves nothing; trading is the cost
  model's; strategy is a share of the proceeds by `StrategyValue`'s category (an exit the
  strategy makes anyway 0; `capital.planner.strategy_cost`: the part of a core role above its
  target 0, an ordinary holding 0.01, a protected one 0.05), or the position's usable expected
  return when it has one (never below the overweight's share). A sale whose tax or trading cost
  is unknown comes after every one whose cost is known. "Above its target" is measured against
  the targets before the reservation: measured after, every holding would be overweight the
  moment a large reservation exists.
- **StrategyValue's default** protects a satellite or options position for
  `capital.planner.entry_window_days` (20) after it opened and gives no expected return; probes and
  catalysts arrive with the satellite (Session 15). An expected return counts only from a layer
  whose outcome is sufficient (§5.9).
- **Settlement.** Each venue carries `settlement_sessions` in `venues.yaml` with its source (T+1
  in the US, Canada, Mexico, India, China and on Eurex from their T+1 dates; T+2 elsewhere; the
  rows read only in secondary sources are `verify_live`). A sale's last safe session is the last
  session of its venue whose settlement, counted on that venue's calendar, meets the due date
  less `capital.reservations.buffer_days` (2); a holiday moves it earlier, and a sale that cannot
  settle in time is not planned: the reservation is short, and that is alerted.
- **Pacing** (`capital.pacing`). An exit the strategy makes anyway and an overweight are sold at
  once. With a usable expected return, one below `weak_below` (2 % a year) is sold at once, one at
  or above `strong_above` (10 %) is held to the last `hold_sessions` (2) safe sessions, and one
  between is sold in `tranches` sessions (1); without one, a protected position is held and any
  other sold in tranches. Nothing is held past the last safe session. A strong position held is
  offered to the owner as a later date (`capital.questions.postpone_days`, 30) whose default is the
  date as it is.
- **While a reservation is short of cash, the rebalancer sells nothing** (the planner's sales
  are the ones that raise it) and spends nothing reserved; sales are exits with the reason
  `withdrawal`, added to `execution.exit_reasons_allowed`. The plan is surveyed afresh at every
  cycle's `reserve` step, between the NAV and the rebalance, and goes into the digest.

Rejected: a plan stored and executed later (what sold, what arrived and what the owner answered
are always read afresh); selling a protected position silently when it is the only source.

### D109 · Owner questions (§5.15)

- **When.** Meeting a date would cut a protected position (one whose protection lasts past the
  last safe session): the planner asks once per reservation, with options each carrying their
  tax and strategy cost: *proceed*, *postpone* (the first date at which the protection has
  ended, settled, plus the buffer), *reduce* (to what the cash line and unprotected sales cover),
  and, when the plan pays tax this year and the next year's first session still settles in time,
  *after year-end*. A strong position held to the last safe days raises the *later date*
  question.
- **The deadline** is the opening of the first of the last `hold_sessions` safe sessions, so an
  unanswered question still leaves time to sell. **The default is proceed: without an answer the
  withdrawal wins** and the plan executes. An answer takes effect at once (postpone moves the due
  date, reduce lowers the amount, both audited on the reservation); every question, answer and
  expiry is audited, and an open question of a kind is unique per reservation.
- **Channels.** `obliskiq questions list|answer <id> <option> --confirm`, a critical alert when a
  question is asked and a warning when one expires; Telegram and MCP answers are Session 19's.

### D110 · Tax on the live path; currency lots (§5.16, D69, D74)

- **The tax book** (`tax/book.py`) takes every non-simulated fill once, oldest first, at the
  heartbeat, applying it to Session 4b's lot engine under the profile's policy, and saves the
  lots, the open loss windows, the ledger, the lot numbering, the fill and its realisations in
  one transaction (D74), so a fill is never taken twice or half. Shadow fills never reach it.
- **Values.** A fill is valued in the base currency at the rate of its trade date: the ledger's
  per-currency rate, recorded by the cycle's NAV step and the heartbeat (`fx_rates`). A fill of
  today waits for today's rate; one whose day passed without a recorded rate, a sale beyond the
  units the lots hold, or an instrument without tax facts (`eligibility.skeleton_tax_facts`, the
  ISIN, issuer and tracked index homogeneity needs, read from IBKR's contract search) is recorded
  **incomplete** and alerted, and the hooks answer nothing for that security until it is complete
  (honest degradation). Commissions enter the cost and the proceeds (art. 35).
- **Currency lots** (`tax.currency_gains` in `es.yaml`): each foreign currency is a security of
  its own. A conversion, a security sold in that currency or one bought with it opens or closes
  currency lots, first in, first out, at the euro value of the day (LIRPF arts. 14.2.e, 33.1 and
  46.b, read in the BOE's text). A currency loss is deferred when the same currency is bought
  within the twelve months after (art. 33.5.e, none before): the conservative reading, which
  defers most currency losses. The DGT's rulings on currency balances were read in secondary
  sources only (VERIFY-LIVE). The window a policy applies is `wash_sale_window`: two months each
  side for listed securities, none before and twelve after for currency.
- **Years settle** once over and no loss window opened in them can still close, so a late
  repurchase never reaches a settled year (D74). The hooks (`tax_cost`, `wash_sale_conflict`,
  `harvestable_losses`, `purchase_defers_loss`) read a copy of the live lots; the rebalancer
  never buys back inside an open window. `obliskiq tax report --year` writes the year's
  realisations, deferred losses and summary as CSV (not tax advice).

Rejected: the rate of the day the fill is taken (a heartbeat after midnight would value a fill at
the next day's rate); a currency gain on revaluation (art. 14.2.e imputes it on payment).

### D111 · The time-weighted return (§5.15)

The account's NAV readings split time into periods; each period's return neutralises the owner's
deposits and withdrawals that reached the account (matched to its cash, in the base currency at
their day's rate), and the periods chain. `capital.twr.flow_timing: in_reading` counts a flow
dated on a reading's day as already in that reading (`(V1 − F) / V0 − 1`); `after_reading` the
other way. Dividends, interest and fees are the book's own return. A period with no positive base
or a flow without a rate makes the whole return unknown, with why, never a partial number; the
digest shows it over `runtime.status_report_days`. Session 18 attributes it.

## Session 8 — Universe and market-data collectors (2026-10-22)

### D112 · The universe is configuration, one file, validated whole (§5.3, D69)

- **One file, one section.** `config/universe.yaml` holds the `universe` section, merged after
  `config/defaults/` and before `owner.yaml`; a `universe` key anywhere else, or another key in it,
  is refused. Lite's `universe.yaml` is ported whole, its comments with it, regrouped into roles
  (`data`, `tradable_candidate`, `option_underlying`), attributes (domicile, UCITS, KID, share
  class and its hedge, fee, lot, inception, ISIN, issuer, tracked index, replication, bond
  issuer, with the source of each instrument's facts and its date) and exposures (core roles, one
  grid cell, macro-board entities, the concentration country, the commodity bucket). Unknown is
  `null`, never a default.
- **Validated whole.** The grid matrix is complete (every cell measured by exactly one instrument
  or unmapped with a reason; a market's native index is never a wrapper of another currency), the
  macro board's every cell is claimed or declared unmapped, every reference resolves (buckets,
  roots, proxies, holdings markets), every exchange code is in `ibkr_exchanges` and every
  instrument lists in one of its venue's currencies. `data_only_reason` is set exactly when an
  instrument is not a tradable candidate (lite's `tradable`/`untradeable_reason` invariant) and
  an index is never tradable.
- **Added beyond lite:** IWDA, EMIM and IBGM (the skeleton's funds) with their iShares UK facts,
  and the sixteen iShares US country funds (data and option underlyings, and candidate sources).
  Pins were read through the owner's IBKR connector (contract search, read-only) [MCP ✓
  2026-10-22].
- **Look-through (D69):** the country, sector and currency weights live in the store, from the
  issuer's holdings files on their cadence (D117); the universe owns the source URL. One owner per
  fact: a weight in the config would be a snapshot that never refreshes.
- **Dropped:** lite's `dividend_yield` (null on every row; it returns with its consumer, Session
  12) and `top2_concentration` (computed from the stored holdings).
- **A correction:** lite recorded ISF as UK-domiciled and not UCITS; iShares' page says Ireland,
  UCITS, ISIN IE0005042456 [read 2026-10-21]. The universe carries the issuer's facts.

Rejected: the universe as store rows seeded by hand (an edit would leave no review trail); the
weights in the config (stale by design).

### D113 · Eligibility under `es`: the `eu_priips` policy (§2, §5.3, §5.16)

- **Computed from attributes.** A share is not a packaged product: eligible. A packaged product
  (`eligibility.kid_required`: ETF, ETC, fund) needs its KID available; an ETF or fund must be
  UCITS (`ucits_required`); a futures-tracking ETC is eligible only while lite's contango rule
  passes, through a `ContangoCheck` that fails closed until Session 12 builds it; government bonds
  on, corporate bonds off by default; an index is never bought. An option on an eligible
  underlying is eligible; on an ineligible ETP it is allowed with the mandatory close
  (`options_on_ineligible_etps`), on anything else refused. A verdict says why in the attributes'
  words ("SPY is an etf domiciled in US, UCITS False, with its PRIIPs KID unavailable: …"), which
  is how the acceptance's "eligibility explains why US-domiciled ETFs are rejected" is met.
- **Selected by the profile** (`es.yaml: policies.eligibility: eu_priips`, its `eligibility`
  section with the regulation's citation); `config validate` builds it, so a profile without its
  data fails at once. Wired into pre-trade unchanged: the gate asks the profile's policy.
- **Leveraged and inverse products are rejected at ingestion** by their resolved description
  (`instruments.leveraged_inverse_markers`): never seeded, never in the directory, recorded in
  `universe_rejections`.
- **The skeleton's allowlist is gone.** Its config key, `SkeletonEligibility` and
  `ConfiguredDirectory` are removed; `UniverseDirectory` describes every pinned tradable candidate
  and option underlying with a resolved description (a data-only instrument is measured, never
  named by an order). Tax facts (ISIN, issuer, tracked index) come from the universe's attributes.

Rejected: an allowlist beside the attributes (two owners of one fact that drift); reading domicile
or UCITS from IBKR's description (it does not carry them).

### D114 · Venue profiles and the market-data plan (§5.3, §5.13)

- `venues.yaml` gains, per venue, the currencies its listings trade in, the IBKR trading
  permission an order there needs and the board lot (Tokyo's 100), each with its source and
  whether it still awaits a live reading; settlement and transaction taxes stay where they were.
  `settlement_sessions` may be unknown (`null`, Tel Aviv): a settlement date there raises rather
  than guess. New venue XTAE and the IBKR codes the universe sends (B3, BVME.ETF, HKFE, KRX, KSE,
  NSE, OSE.JPN, SEHKNTL, TASE and the futures exchanges), all VERIFY-LIVE.
- `market_data.feeds` lists the subscriptions with price, waiver, venues and asset classes;
  `owner.yaml: market_data.plan` defaults to OPRA L1, Eurex Core L1 and the complimentary US feed.
  The snapshot collector judges each quote against the plan: delayed where a planned feed streams
  the venue is degraded (a subscription missing or not shared with the session); delayed elsewhere
  is what the plan buys.
- A share without a lot of its own is stored with its venue's board lot.

### D115 · The market-data collectors (§5.13, D71; lite `collect/`)

- **Ported:** the universe sync (seed by confirmed pin, supersede a corrected pin, report an
  unpinned instrument with the conid it resolves to, reject at ingestion), the daily-bar backfill
  (the window once, then the newest bar re-asked with an overlap and older history down to the
  window or the inception; Session 3's reader probes the cap, walks backwards and steps a refused
  span down; a bar is dated in its venue's time zone), underlying snapshots, the option-chain
  collector and the curve collector, with lite's per-target retry, pacing and verdicts (one dead
  target degrades a run; zero chain rows is a failure; worst step wins).
- **The chain collector stores surfaces forward** (H8's history, D71): every option underlying,
  daily, inside the tenor window and a volatility-scaled strike band (lite's sigma ladder, every
  fallback degraded and recorded on the row), the declared option venue sent verbatim, the venue
  and the contract's multiplier on every row (migration 009), the underlying's snapshot and its
  rows in one transaction, and no snapshot row made of a bar close.
- **Not ported:** lite's paid option vendor (a paid service), its MCP capture route, the macro
  pull (Session 10), the NAV collector (Session 6 owns it), the portfolio union (Session 6's
  holdings registry) and the Parkinson and Garman-Klass estimators (with their consumers, the
  indicators of Session 11).

### D116 · Collection runs per venue after its close (§5.14, D102)

The service schedules one collection job per venue the universe collects on, at its regular close
plus `collectors.schedule.after_close_minutes`, through the calendars, as `collect:<MIC>` in the
run log; each step has a deadline (`collectors.step_deadline_seconds`, the chain step the largest)
and a step that raises or outruns it does not stop the next. A venue the calendars do not cover
that day (XSHG after 2026-12-31) and any venue outside the plan where a pinned candidate lists
collect in `fallback_venue`'s job, so no venue is silently dropped. `collectors.schedule.enabled`
turns the service's jobs off; `obliskiq collect` runs them by hand under the service lock.

### D117 · Stock candidates per grid cell (§5.3)

- **Sources:** the iShares holdings files the universe names (the US site's form and the UK
  site's) and the IBKR scanner runs it declares. Each file is stored with its own date ("Fund
  Holdings as of"), its download stamp and its hash, every line with the issuer's words beside what
  they map to (`collectors.candidates`: sector, location, exchange; unmapped stays unmapped).
- **Crediting:** a share line counts for the market of its location's country and for the market
  its file is a source for; for its own sector's cell and the broad cell. Listings of one company
  are one candidate (country and name less `name_noise`), each listing kept with its exchange and
  currency. A scanner row counts for its market's broad cell.
- **Point in time:** a read as of a date takes each fund's newest file of that date that was held
  by then; a stock enters a cell on the date its first such file showed it.
- **Every cell has candidates or a declared gap** (`universe.candidates.gaps`, with the reason),
  and `coverage_problems` reports a cell with neither and a gap that has candidates. Measured on the
  live files of 2026-10-20 and 2026-10-21: 182 of 192 cells hold candidates (3,954 companies), the
  other 10 are the declared gaps.
- **Resolved on demand, pinned for a thesis:** `obliskiq universe pin --candidate <key> --thesis
  <id>` resolves one listing (exact ticker on its venue, currency confirmed) and pins it; the next
  universe step seeds it and it is collected. Files are refreshed every `refresh_days`.

Rejected: resolving every candidate up front (IBKR's pacing, and most are never needed); matching
listings by ticker (a company's tickers differ by venue); crediting by the file alone (MSCI World's
Japanese lines are Japan's).

### D118 · Research history for the universe (§5.9, D64)

`obliskiq research data load-universe` reads every pinned tradable candidate from Yahoo through
`research/data.py` (`--candidates N`: and the N largest stock candidates of each cell, home listing
first), from `research.data.yahoo.universe_start` or the instrument's inception. Yahoo's symbol is
the IBKR symbol in Yahoo's form (the suffix by IBKR exchange, from legacy global_vehicles'
`EXCHANGE_SUFFIX_MAP`; a share class's dot as a dash; Hong Kong padded to four digits); a fund's
ongoing charge is in its bias notes, since its closes are net of it. Loaded 2026-10-22: 23 of 23
tradable candidates and 181 of 181 top candidates (ROADMAP, Session 8's log).

## Session 9 — Broker capabilities and vehicle selector (2026-10-22)

### D119 · Broker capabilities are configuration, verified only through the Web API (§5.3, §5.12)

- **Where they live.** `config/defaults/capabilities.yaml` (`broker_capabilities`) holds one check
  per asset class and venue, with a probe instrument from the universe and five readings: search,
  quote, order (a whatif preview), order types and algos. Each reading has a status (verified,
  refused, unverified), the route and date it was read on and its evidence. Only a CP reading
  verifies: what IBKR's MCP connector returns is evidence, kept unverified, because the connector
  is another API with its own pacing and answers. Validation refuses a venue that is not declared
  and a probe that is not a universe instrument of the check's class on that venue.
- **The gate.** The selector proposes a vehicle only when search, quote and order are verified
  for its class on its venue (`decision/capabilities.py: REQUIRED_CHECKS`). Order types and algos
  are recorded but do not gate: every order is a limit, and a preview of one is the order
  reading. A venue's trading permission is held where an order there has been previewed and
  accepted; no other reading of the account's permissions is trusted yet.
- **The probe.** `obliskiq universe capabilities --probe` runs on the deployment machine in paper.
  Per check it searches the probe, resolves an option for an option check, reads a snapshot with
  the availability (6509) and permission (7768) fields, the contract rules and the algos, and
  previews a limit buy of one size increment, `probe.limit_offset_fraction` below the bid. Nothing is
  placed (the preview endpoint, under the paper guard). It prints the readings to record and
  writes nothing: a capability widens what the bot may buy, so it enters the configuration by a
  reviewed change. The readings shipped are the owner's paper run of 2026-10-21 (T1) and lite's
  of 2026-09-10; the cloud container cannot log in to the Gateway (`docs/knowledge/ibkr.md`).

Rejected: the probe writing its readings into the store or the configuration (an unreviewed
reading would widen the mandate's reach); MCP readings counting as verified; inferring an ETF's
capability on a venue from a share's.

### D120 · The vehicle selector: gates, then a weighted ranking with reasons (§5.8)

- **Candidates and gates.** An exposure is a core role (`core.roles`) or a grid cell; its
  candidates are the universe's instruments whose exposures name it. Each passes, in order: a
  tradable candidate, pinned and described, not prohibited by the mandate, eligible under the
  jurisdiction's policy, a qualification rule for its type (shares, ETFs and ETCs qualify through their quote
  policy; bonds, funds and baskets join through rules Session 19a adds), a hedged class only
  where `vehicles.hedged_share_classes` allows it, and a verified capability (D119). A rejection
  names its gate and the reason in the policy's own words.
- **Ranking.** Seven criteria, each scored 0 to 1, weighted by `vehicles.ranking.weights` (they
  sum to 1): the all-in yearly cost at the reference amount (the fee, plus a round trip through
  the cost book — commission, spread, slippage, transaction tax — and its two conversions spread
  over `holding_years`, plus market data where the quote policy needs real time the plan lacks),
  against `cost_ceiling`; tracking, `(ρ − floor) / (1 − floor)`; liquidity, decades of average
  daily traded value over the reference amount; tax, whether the policy defers the income an
  accumulating class retains; currency; the share of the day's cycles inside the venue's order
  window; real-time data on the plan. An input that is missing scores 0 and says why; every
  ranked vehicle lists every criterion with its score, weight and reason.
- **Research is passed in.** Correlations are computed in research (`research/tracking.py`) and
  handed to the selector by its caller: the CLI now, the rebalancer later. The decision path
  never reads research (D64's import contract).
- **Known limits.** The cost book prices an ETF of unknown market capitalisation at its illiquid
  friction tier (2.12 % a round trip, an assumption), so today the cost criterion separates
  European ETFs mostly by fee; a friction tier for ETFs by traded value is a follow-up. The
  withholding a fund suffers by its domicile is not scored: every equity candidate is Irish, and
  a fund's closes are net of it, so tracking already measures it.

Rejected: ranking by fee alone (it ignores tracking and liquidity); weights in code; the selector
reading the research store.

### D121 · Core roles and their candidates (§5.3, §5.4)

- **Roles.** `core.roles` declares the nine roles of Session 4c's variants with the research
  series each is measured on (4c's pre-registration's series); the skeleton's `euro_govt_bonds` is
  renamed `euro_govt_intermediate` (4c's EGB_INT). The universe may name only declared roles.
- **Candidates.** 33 vehicles, two to five per role (`docs/knowledge/universe-sources.md`): other
  issuers and indexes where they exist, accumulating classes first, the Xetra line in EUR where
  IBKR's search returns one. Identifiers were confirmed on the issuer's page and justETF, conids
  read by ISIN through IBKR's connector [MCP ✓ 2026-10-22], VERIFY-LIVE on CP. Xetra's ETF segment
  is `IBIS2` at IBKR, added to `ibkr_exchanges` as XETR.
- **Gold is held through ETCs.** The prompt asks for UCITS vehicles, but a UCITS fund must
  diversify and cannot hold one metal; Session 4c registered GOLD as an ETC, and the candidates
  follow it (eligible under `es` with a KID).
- **Tracking is measured monthly.** Log returns of each calendar month in the base currency at
  the ECB's rates, over five years, at least 36 of them (`vehicles.correlation`). Weekly returns
  understate tracking where closes are hours apart (XMME against MSCI EM: 0.70 weekly over three
  years, 0.98 monthly over five) and amplify an isolated bad tick.
- **Not ported:** lite's ADR map (`global_vehicles.ADR_MAP`). It chooses a company's US
  depositary line, a stock-candidate concern; it moves to the satellite's first stock order
  (Session 15).

### D122 · Swap partners, hedged classes and ETCs under `es` (§4, §5.8, §5.16, D7, D69)

- **What the law requires and what the owner chooses are separate.** `es.yaml: swap_partner`
  states the law: a partner differs in issuer (RIRPF art. 8 makes securities homogeneous by
  issuer), not necessarily in index. `vehicles.swap_partners.same_index: false` keeps D7's
  default, a different index too; turning it on admits a same-index partner from another issuer.
  A partner is a ranked vehicle of the same exposure whose monthly returns correlate with the
  primary's at least `min_correlation`; each refusal says which rule refused it.
- **Hedged classes** are ranked as their own vehicles and refused while
  `vehicles.hedged_share_classes` is `undecided`, the owner's open question (MASTER_PLAN §11).
- **An ETC is a debt security for tax** (`SecurityKind.BOND`, as 4c's pre-registration taxed
  GOLD): homogeneous only with its own ISIN or its issuer's same index, never with a fund. Whether
  Spain reads its result as a gain or as capital income (LIRPF art. 25.2) is queued for an official
  source (`docs/knowledge/tax-es.md`).
- **Retained income.** `es.yaml: retained_income.deferred: true` (LIRPF art. 94): an accumulating
  class defers the tax on what it retains, the selector's tax criterion.

## Owner decision (2026-10-23)

### D123 · Swap partners on the same index (§4, §5.4, §11, D7, D122)

The owner turned on `vehicles.swap_partners.same_index` in `config/owner.yaml`. A swap partner may
now track the same index as the vehicle it replaces, provided it comes from another issuer: that
is the rule Spain's law sets (RIRPF art. 8, `es.yaml: swap_partner`), and every other condition
stands (not homogeneous, a ranked vehicle of the same exposure, correlation at least
`min_correlation`). The expert default in `config/defaults/vehicles.yaml` stays off.

- **What it changes today.** World equity gains a partner: SPPW and XDWD both track MSCI World
  and read 1.000 monthly over five years (State Street and Xtrackers). The other roles are
  unchanged: no other proposable pair from two issuers tracks one index. IGLN and SGLD (LBMA
  Gold) and CMOD and ICOM (Bloomberg Commodity) will be, once the probe verifies the LSE and Milan.
- **The risk the owner accepts.** D7 kept a different index to remove any argument under the
  general anti-avoidance rule; a same-index partner from another issuer is not homogeneous under
  art. 8, but it leaves that argument open. With the switch on, an unknown tracked index no longer
  refuses a partner, since the index no longer decides.

## Session 10 — Macro, positioning, insiders and events (2026-10-23)

### D124 · Sources besides IBKR: declared with a consumer, dated by a release-date policy (§5.18, D66)

- **One file.** `config/sources.yaml` declares every source with its area, its consumers
  (`hypothesis:<id>` resolved against `research/pattern_registry.yaml` by a test,
  `component:<name>`, `view:<name>` or `safety:<name>`), its credential's variable name, its
  schedule and its bias notes. A `built` source names a consumer, a release-date policy and a
  staleness window. A `declared` one names only the condition that would build it: Eurostat, the
  BoE, the SNB, the Bundesbank, BoC Valet, the ABS, the RBA, BCB SGS, Norges Bank, MAS, Banxico,
  CBRT EVDS, DBnomics and the MOF's JGB yields. Every macro series, CFTC contract, curated event
  and feed is declared there too, with its consumers.
- **Release-date policies.** Each series is dated by a release-date policy: `from_api` (ALFRED,
  ONS, StatCan, EDGAR), `reference_plus_lag` (a published schedule), `same_as_reference` (only a
  value public on the day it describes), `as_collected` (a reading taken when collected) or
  `unknown`. A row of an `unknown` series is stored without a release date. A backtest read
  refuses a series that is undeclared or `unknown` (`data/macro/catalog.py: backtest_series`):
  no source without a defensible release date feeds a backtest.
- **Lags changed from lite.** EIA's weekly stocks are dated 10 days after the week's Friday, not
  lite's 3. They are published the Wednesday after, the Thursday after a Monday holiday, and over
  Christmas 2025 the Monday ten days after (EIA's schedule), so 3 days dated every week before
  its release, and 6 would have dated the Christmas week early. A lag dates late in an ordinary
  week, which can delay knowledge and never advance it.
- **Two vintages of FRED.** A FRED series reads its value from the initial release
  (`value_vintage: initial`) or from the latest one (`latest`); its release date always comes from
  the initial release. `latest` is for an index rebased mid-history: Eurostat's HICP, rebased to
  2025 = 100 during 2026.
- **The guards.** A series whose source reports units other than those declared fails
  permanently (`UnitsChangedError`); its rows are not stored. A source is abandoned for the run
  after `stop_after_consecutive_failures` failures in a row. A target succeeded within
  `refetch_after_seconds` is not asked again.
- **A prompt fact corrected.** The prompt names JGB yields among the BoJ series. The BoJ's
  Time-Series database holds none (2026-10-23): the Ministry of Finance publishes them. The
  macro board reads the monthly ten-year yield through FRED, and `mof_japan` is declared with its
  condition.

Rejected:

- Lite's lag of three days for EIA, which dated each week before its release, and six, which
  dated the Christmas week early.
- Dating `unknown` series by their reference period, which fabricates a release date.
- Building every listed regional source now, which adds collectors nothing consumes (D66).

### D125 · A single-stock halt blocks its instrument, never its venue (§5.12 "Calendars and phases")

- **The feed.** Nasdaq Trader's trade-halts feed is read every `refresh_minutes` by the service's
  heartbeat (`data/halts.py`) through market-pulse-bot's structured-feed reader
  (`calendars/halt_feeds.py`). Its `Market` code maps to a venue through `halts.markets`, and only
  a venue whose `incident_source` is a single-stock structured feed may be mapped (config
  validation). A halt of a universe instrument we hold a conid for becomes an instrument-scoped
  feed incident. It runs from the halt to the resumption the feed shows, and is open-ended
  without one. It reaches the calendars through `Calendars.accept_incidents`, which replaces the
  feed's origin wholesale. The instrument cannot trade; its venue and the venue's other
  instruments do.
- **Market-wide halts.** Market-wide halts and exceptional closures stay manual entries: no
  trustworthy free market-wide feed exists for most venues, and a feed is treated as market-wide
  only once verified and declared in config.
- **When the feed fails.** A failure keeps the last accepted state in force: a halt is never
  lifted because the feed went quiet. That state is kept in the store and restored at the
  service's start. `alert_after_failures` failures in a row raise an alert, and the next good read
  clears it. A halt whose resumption plus the incident grace has passed is dropped.

Rejected:

- Blocking the venue on a single-stock halt, which stops every other instrument for one name.
- Lifting halts on a failed read.
- Mapping NYSE American (`AMEX`), which no universe instrument lists on.

### D126 · Positioning and insiders, point in time (§5.5, §5.9)

- **CFTC.** A COT report (legacy futures-only, by contract code) is public on its Tuesday plus 3
  days, the Friday it is published. Positioning is the net non-commercial position as a share of
  open interest. Its percentile is inclusive over the trailing `percentile_window_weeks` of
  reports released by the day asked, after `percentile_min_weeks` of warm-up; its velocity is
  measured over `velocity_weeks`.
- **Collection and its gap.** History comes from the annual archives, each week from Socrata
  (re-reading `incremental_weeks`, so a revised report replaces the one held). A government
  shutdown delays releases (October–November 2025): the fixed lag then dates those reports early.
  This is a follow-up (an exceptions list from the CFTC's notices).
- **SEC Form 4.** Filings are collected forward from EDGAR's daily form index for the US stock
  candidates. One network-free parser (`data/insiders/form4.py`) reads the full submission and
  would be shared with any research loader. A filing is public on its filing date. Cluster inputs
  (open-market purchases by officers and directors over `cluster_window_days`) count filings by
  that date only. A day whose index is absent is a closed day, not a failure.

Rejected:

- Dating COT by its Tuesday, which is the position date and not the publication date.
- Loading Form 4 history here: survivorship needs the research loader of H2 (D84).

### D127 · Event sources: as collected, and only what a reading can stand on (§5.5)

- **Prediction markets.** Polymarket (curated events by slug, and tag discovery above a liquidity
  floor) and Kalshi (curated series) are stored as snapshots of the Yes probability, stamped when
  read. Kalshi's probability is the midpoint when the quote is no wider than `max_mid_spread`,
  else the last trade. A market without a Yes price is counted, never filled. Velocity is the
  change against the newest snapshot at least `velocity_hours` older.
- **GDELT.** Hourly volume and tone are summed into whole UTC days: after the span's first point
  and before today, a day held keeping its first reading. GDELT leaves out the same hours in every
  query (a gap in its data), and an omitted hour counts in neither total, so a day's share of
  coverage is unbiased. The pulse is the newest day's Welford z-score against every earlier day
  held, after `min_history_days`. Requests are spaced, a 429 waits the backoff, and after
  `give_up_after_failed_queries` failures in a row the rest are not asked.
- **News.** RSS 2.0, RSS 1.0 (RDF) and Atom feeds are refused unparsed when over
  `max_body_bytes` or declaring a DTD or entity. Items are deduplicated by a hash of guid, else
  link, else title and date, and stored raw. An item published more than `max_item_age_days`
  before it was first read is left out.
- **Issuers' feeds.** These are EDGAR company feeds: their entries carry no text, so each new
  filing's press release (its first EX-99 exhibit, else its main document) is read as the item's
  text. The issuers' investor-relations pages refused or lacked a feed (403 or 404, 2026-10-23).
- **Tone without the LLM.** VADER, with config's financial terms, is only the offline fallback
  tone.
- **Earnings dates.** They come from Yahoo through yfinance and are research-grade: each read is
  stored whole under its day. The catalyst calendar labels them `research` beside the official
  central-bank and release dates config holds with their source pages.

Rejected:

- Polymarket's first leg as a stand-in for a missing Yes.
- Requiring 24 hours for a GDELT day, which discarded half the days to GDELT's own gaps.
- Scraping investor-relations pages.

### D128 · The minimal EventExtractor (§5.10, D70)

- **One provider.** One provider (`gemini`, OpenAI-compatible) and one model come from
  `config/llm.yaml`. The key is read by the variable name config gives (`key_env`), from the
  process environment or the env file (`Environment.named_secret`), never as a field of
  `Environment`: adding a provider is a YAML entry. Without the key a run is skipped and the items
  wait.
- **The prompt.** The item enters through a template string whose interpolations are escaped
  inside `<item>` delimiters it cannot close. The prompt's hash covers the instructions, the
  template, the schema and the parameters; a change starts a new segment of H7.
- **The answer.** It must validate as `ExtractedEvent` and cite the item's own source id. It is
  asked once more with the validation error, then fails closed (`invalid`). A failed call stays
  queued up to `max_call_failures`. An item too long for the context window fails without a call.
- **The budget.** A call is made only if the month's spend plus its worst case (every attempt at
  the estimated input and the full output allowance) stays within `monthly_usd`.
- **The log.** Every call is appended (the table refuses updates and deletes) with its item, time
  produced, provider, model, prompt version and hash, tokens and cost.
- **Who runs it.** The service runs it after each news source's job, as a run of its own;
  `obliskiq llm extract` runs it by hand. Nothing consumes its output.
- **H7.** It is pre-registered as a `forward_test` (`research/preregistrations/llm-events.md`),
  merged on its own before any live run and then locked. It tests whether the tone of issuers'
  press releases adds to the price signal on outputs produced live.

Rejected:

- A field of `Environment` per provider key.
- Retrying an invalid answer more than once.
- Estimating a call's cost from its expected rather than its worst case.

### D129 · Scheduled sources and their health (§5.12, §5.14)

- **Scheduling.** Each built source runs at the moments its `schedule` names (a venue's open or
  close moved by an offset, through the calendars) as a job `source:<id>.<n>`, a run record of
  its own; the calendars of the service cover those venues. `obliskiq sources run` runs one by
  hand. A run records its verdict, rows and reason as the source's health.
- **Health.** A source is `failing` when its last run failed outright and `stale` when it has not
  succeeded within `stale_after_hours` (or never has); otherwise it is `ok`. `obliskiq status`,
  `obliskiq sources health` and the heartbeat file show it.
- **Alerts.** Only the halt feed alerts: a scheduled source failing is reported, not alerted,
  since nothing trades on it yet.
- **Inline jobs.** A source job runs in the service loop, as a market collection does, so a slow
  one delays the next heartbeat. The slowest is a throttled GDELT run, bounded by its spacing,
  backoffs and give-up rule (ROADMAP, Follow-ups). The heartbeat's halt-feed read never stops the
  heartbeat.

Rejected: an alert per failed source run, which GDELT's throttling alone would make noise.

## Session 11 — Measurement store and views (2026-10-23)

### D130 · One measurement store; every view a query (§5.7)

- **The table.** `measurements` holds one leaf of one entity as of one session:
  `(entity_kind, entity_id, indicator, leaf, as_of)` with its value, status (`ok | n/a | stale |
  inapplicable`), reason, `n_obs`, window, units, source and `inputs_hash`, as the prompt names
  them, plus a groupable `reason_code`, the `market_data_status` of the quotes behind the value and
  `computed_at`. An entity is an equity-grid cell (`us/broad`), a macro-board cell
  (`us.rate_10y`), an instrument (its conid) or a correlated pair of legs
  (`equity:us|rate:DGS10`); `indicator` is the family. A build upserts its own session, so each
  leaf's rows across sessions are its history and a rebuild replaces one session only. `CHECK`s
  refuse a measured leaf without a finite value, an absent one with a value, and any leaf that is
  not `ok` without a reason and a code.
- **Inputs.** `source` names the inputs read (`bars:<conid>`, `chain:<conid>`, `macro:<ids>`).
  `inputs_hash` is the SHA-256 of a fingerprint of those inputs (a digest of the rows read, by
  `as_of`) and of the grid's settings, so a rebuild on the same store reproduces every row and a
  changed input or setting changes the hash.
- **Views are queries.** The equity grid, the macro board, the four ranked views, family reach,
  the correlation flags and the census read `measurements` (and the universe); nothing they derive
  is stored again. Lite's `grid_coverage` disclosure (instrument, kind, hedged class, top-two
  concentration) is read at view time from the universe and the dated holdings files Session 8
  stores, so the concentration is a measured fact where a file exists and "not sourced" where it
  does not.
- **The solved implied vol** stays in the snapshot's own column (D25's column-targeted writer): a
  collector's re-upsert keeps the session's solve, and the solver writes only its two columns.
- **`obliskiq compute <solve-iv|measure|rankings|census|all>`** runs the steps under one run
  record. Exit codes are the collectors': a young store is degraded (2), a step that raises fails
  the run (1) and the next step still runs. The steps touch no network: the import contract covers
  `orchestration.compute`, and a test runs `all` with network sockets, name resolution and
  connections refused (a local socket pair stays allowed: asyncio's loop wakes itself through one,
  and a private network namespace keeps them).

Rejected:

- A table per view (lite's `grid`, `grid_coverage`, `grid_rankings`, `correlation_matrix`,
  `universe_census`): each a second owner of facts the measurements hold.
- Storing whether a correlation shift is flagged: derived from the shift and
  `grid.correlation_shift_sd` when read, so a changed threshold cannot leave stale flags.

### D131 · The macro board (§5.7)

The board's axes and each cell's inputs are Session 8's (`config/universe.yaml`, `macro_board`).
What each asset class measures, here first (lite had no board):

- **`equity_index`**: the grid's returns, distance-from-high and realised-vol families on the
  instrument that claims the cell, and positioning where the cell names a CFTC contract.
- **`rate_2y`, `rate_10y`, `credit`**: the level, its change over each horizon in
  `grid.macro_board.change_horizons_months` (3 and 12; both ends release-aligned) and the level's
  percentile over `percentile_window_years` (5) of released observations.
- **`vol_index`**: the same, and the term slope, short over long minus one (the statistic
  `rv_term_slope` computes, so a positive slope is an inverted curve).
- **`curve`**: the slope the label names and its changes; **`real_rate`**: the policy rate, CPI year
  on year and their difference, stale when the CPI print is older than the macro window (lite's
  family 12); **`fx_vs_base`**: the local currency against the base over each horizon;
  **`commodities`**: per root, the front-against-next annualised roll and the front month's return.
- **Labels are a closed vocabulary.** Each label the board computes is bound to its asset class
  and its series count (`schema_universe.BOARD_LABELS`), and an asset class the code cannot read
  is refused. The region's policy rate is the first series of a `policy_minus_*` real rate, else
  the second of a `10y_minus_policy` curve; where both name one they must agree.
- **The grid's macro families read the board.** A grid market's FX rate, policy rate, CPI,
  ten-year yield, curve and CFTC contract are its same-name region's cells (lite's per-market
  series, regrouped by Session 8), and a cell the board declares unmapped hands its declared reason
  to the leaf.
- **US credit and volatility wired.** Session 10 declared BAA10Y, VIXCLS and VXVCLS for the
  board; their cells (`us.credit`, `us.vol_index` with `short_over_long`) now read them.
- **Freshness**: a series cell against `grid.freshness_days.macro`, a commodities cell against the
  daily-bars window (curves are collected with the bars, after each venue's close).

Rejected: resampling or interpolating a series to a common date for a change (a change reads the
newest value released on or before each end).

### D132 · The realised-volatility forecast: HAR-RV (§5.5, §5.11)

- **Model.** Corsi's HAR-RV, fitted per instrument by ordinary least squares (`numpy.linalg
  .lstsq`): the mean daily variance over the next `h` sessions on today's, the past week's mean
  and the past month's mean (`weekly_sessions` 5, `monthly_sessions` 22, Corsi's). One direct
  regression per horizon in `horizons_days` (21 and 63), annualised on the strike band's trading
  year. The in-sample R² is reported beside each forecast.
- **Close-to-close squared returns**, overnight gaps included: the forecast is compared with an
  implied vol, which prices the total variance to expiry (lite's rule for `iv_minus_hv`).
- **History.** The daily window the collectors hold; a fit needs `min_sessions` (252) returns,
  and every training target lies inside the history held on `as_of`. `n_obs` counts the training
  rows, whose `h`-session targets overlap.
- **Refusals**: too little history, a singular design, or a forecast variance that is not positive
  (a linear model extrapolated below zero) is no forecast.

The values are judgements for Session 17's base rates and the vol-mispricing component, not
evidence of anything: neither consumer trades on them before its own test.

Rejected: a log-HAR (it changes the estimand from variance to log variance and needs a bias
correction to forecast the variance), and a range estimator as the variance proxy.

### D133 · The overnight-gap distribution (D67)

- **A gap** is the log move from a session's close to the next session's open, over the daily
  window. Its quantiles at `grid.overnight_gaps.quantiles` (1, 5, 50, 95 and 99 %) are leaves of
  the instrument, on at least `min_gaps` (60) gaps.
- **Earnings.** A gap whose window (the previous close to this open, on the listing venue's
  calendar) holds a report time from the earnings dates held by `as_of` is an earnings gap. The
  quantiles outside earnings are a separate leaf set; the earnings gaps' minimum and maximum are
  leaves (a stock reports four times a year, too few for a quantile). A window the calendar cannot
  place is counted as unplaced and left out of both, never guessed. A fund, an index or a
  commodity product reports no earnings (`inapplicable`); a stock without collected dates is
  `n/a`.
- **A restated open** (above `garman_klass_max_restated_open_share`) makes every gap a zero by
  construction, so the distribution is refused.

Session 17 pre-registers which quantile sizes a position (D67); this session measures them.

### D134 · Where the grid departs from lite

- **No vendor IV percentile.** No route ObliskIQ collects serves one (D25), so family 8 is the
  self-computed percentile of the snapshot's implied vol over `iv_percentile_window_weeks`, after
  `iv_self_source_min_sessions` accumulated snapshots; lite's `iv_percentile_source` switch and its
  vendor and divergence leaves are not carried. A cell has 45 leaves, not lite's 47.
- **Positioning reads Session 10's reading** (`sources.positioning`: a 156-week percentile after
  52 weeks of warm-up, the change over 4 reports), one owner of positioning. Lite's
  `commodities.cot` (a 5-year percentile, changes over 4 and 13 weeks) is not carried; its 13-week
  change is a follow-up if a consumer asks for it.
- **Weekly closes are derived** from daily bars (the store holds daily bars only, D25), so the
  long-run range reads the two years the backfill holds, and its window says how many weeks.
- **The daily window, the reference tenor and the trading year** are the collectors'
  (`collectors.backfill.window_years`, `collectors.vol.primary_tenor_days` and
  `trading_days_per_year`), as lite tied its grid to its backfill and its strike band.
- **`market_data_status_live` is not carried**: statuses are decoded at collection; a leaf carries
  the weakest status behind it, and an unreported one is `NULL`, never real-time.
- **Discount rates** for the solve (`numerics.implied_vol.solve.risk_free_series`): EUR reads the
  ECB's ESTR, which Session 10 declared, where lite read FRED's mirror of it; a rate is the value
  released by the chain's session, and an observation without a release date is not read (lite
  admitted one).
- **The census** counts the newest chain's contracts and reports family reach. The candidate gates
  are Session 12's, so until then it reports every gate untested (`untested`) and the step is
  degraded; Session 12 feeds its gates in.
- **Screen bounds.** The ranked views' conditions read lite's `screen.cheap_iv_percentile_max` and
  `extended_iv_percentile_min` (`config/defaults/screen.yaml`), which Session 12's gates read too.

### D135 · The options sleeve's configuration (§5.6, D66)

- **The budget is `capital.budgets.option_premium` of investable capital**, read through Session
  7's `SleeveBudgets`, not lite's `option_sleeve.budget_pct`. The prompt's "current NAV" is
  investable capital (Session 7's managed capital less unmanaged holdings and open
  reservations): the sleeve's budget falls when capital falls, and one key owns the share. A
  second key for the same fraction would let the two drift apart.
- **The pool** is that budget less the penalty balance and the premium open positions commit
  (`penalty.py`, lite's formula). Without the commitments the budget would not be a cap.
- **The activation condition** has three legs, each true, false or untested, and the sleeve trades
  only when all three hold (D66):
  - **evidence**: `option_sleeve.activation.hypothesis` (H8) is registered with the outcome
    `sufficient`;
  - **budget**: the budget buys the pre-registered `activation.median_premiums` of the session's
    median premium. The number belongs to the pre-registration, so without one this leg is
    untested;
  - **friction**: a round trip's commission plus the currency conversion costs at most
    `friction_max_share_of_premium` of a median premium.
  H8 is not in `research/pattern_registry.yaml`, so the sleeve runs in shadow.
- **`friction_max_share_of_premium` is 0.15**, lite's own derivation turned into a gate. Lite set
  `min_premium_base_currency` at EUR 20 because a round trip's commission (EUR 3.00 at Eurex's EUR
  1.50 a contract) is 15 % of it. The gate keeps that share at every premium, with the conversion
  priced by the `spot_fx` schedule at IDEALPRO. It is a judgement, quoted, and H8's
  pre-registration may set its own.
- **Not ported**, each with its reason in the file that would hold it:
  - `fx_inflation.rank_on`, `flagged_market_rule` and `require_physical_hedging`: each could take
    one value only. Ranking reads base-real returns and nothing else, and the mandate prohibits FX
    forwards. A key that cannot change is a decision that looks made and is not.
  - `instruments.allow_futures_options` stays false. A futures option needs its own
    never-exercise rule and a wider close buffer, which the sleeve does not carry (Follow-ups).
  - Lite's screen buckets (cheap, coiled) and its commodity-proxy regression wait for their
    consumers (Follow-ups).

Rejected: an activation leg that defaults to holding when it cannot be read. An untested leg keeps
the sleeve in shadow, as an untested gate keeps a contract from counting.

### D136 · The option surface (§5.7)

- **Where it lives.** The option surface is a leaf set of each underlying's `instrument` entity in
  the measurement store (indicator `option_surface`), not a table of its own: D130's one store,
  queried by `surface_view`.
- **Tenors.** For each tenor in `grid.option_surface.tenors_days` (95, 135 and 180) the listed
  expiry nearest it is read, and only one within `smile.term_structure.max_target_deviation_days`.
  Another tenor is never published under this one's name, and the leaf's window carries the actual
  days.
- **Per tenor**, off the expiry's hygiene-filtered smile (the one the gates and the zombie test
  read):
  - the at-the-money vol, and the vol at each |delta| of `grid.option_surface.deltas` (0.10 and
    0.25) on each wing;
  - the risk reversal at `smile.skew_delta`;
  - the at-the-money call's theta as a share of its value per day;
  - liquidity: the median spread, the open interest, and the share of contracts the candidate
    gates' liquidity test passes.
- **Per underlying**: the implied-vol percentile and the term slope, computed by the grid's own
  families, one implementation.
- **The values are judgements.** The tenors sit inside the entry window (`tenor.entry_dte_min` to
  `entry_dte_max`), and the deltas inside the smile's delta band and below the money. A config
  check refuses either outside its bounds.
- A chain older than `grid.freshness_days.option_chain` makes every leaf it feeds stale. Every
  absent leaf names its reason (no chain, no expiry near the tenor, no smile, no wing reaching the
  delta, no rate).

Rejected: interpolating between expiries to a constant tenor. That is a vol nobody can trade, and
it hides which listed expiry the number came from.

### D137 · Where the contango check is wired (§5.3, D113)

- **The check.** `engines/commodity`'s `CurveContangoCheck` implements Session 8's `ContangoCheck`.
  A futures-tracking product passes only while its root's newest curve on `as_of` is classified,
  fresh (within `grid.freshness_days.daily_bars`) and inside
  `commodities.term_structure.contango_max_annualised_for_futures_wrapper`. An unclassified, stale
  or missing curve fails closed. The acceptance test runs it through the `eu_priips` policy on a
  futures-tracking ETC.
- **The order path keeps `UnavailableContangoCheck`**, which refuses every futures-tracking product:
  - The policy registry is in `jurisdiction`, which may not import the store (import contract).
  - A synchronous store read inside the async pre-trade path would block the event loop.
  - The service does not yet schedule the commodity step, so the curve the check would read is
    whatever was last computed by hand.
  The universe declares no futures-tracking product today (its ETCs are physical), so the
  fail-closed check refuses nothing yet. Wiring it, from the service's scheduled compute through
  the store worker, is a follow-up.

Rejected: reading the curve on demand from the order path, which would block the event loop and
open a second reader of the curve.

### D138 · The sleeve's store (§5.6, §8)

- **Migration 012** rebuilds `measurements` to add two entity kinds, `market` (a grid market's
  four-way FX decomposition and carry test) and `commodity_root` (a futures root's curve,
  positioning, cycle and inventory). SQLite cannot change a CHECK in place.
- **`option_positions`**: identity is fixed at the open, the position closes once with its reason,
  and it is never deleted. `close_by` is stored, computed from the last tradable session of the
  option and its underlying less `tenor.mandatory_close_trading_days`.
- **`option_ledger`** is append-only (lite's), in the base currency:
  - Events: a premium committed, a loss detected, a loss closed, a qualifying gain, and an option
    gain.
  - The balance is replayed from the events on every read. `penalty_balance_after` is the audit
    trail, which `PenaltyLedger.verify` checks against the replay.
  - A commitment is released by its position closing, never by a second row.
  - An event dated before the newest one is refused, so a replay is ordered by its dates.
- **`zombie_observations`**: one review per position per session. The streak counts consecutive
  reviews, and the penalty is booked once, at confirmation (`zombie_test.book_penalty_at:
  detection`).
- **`option_shadow_entries`**: every contract that failed no gate while the sleeve runs in shadow,
  with its quote, its premium in the base currency, the gates it left untested and why the sleeve
  did not trade. This is H8's forward record, and a rebuild of a session replaces that session's
  rows.

### D139 · The candidate gates, composed once (§5.6)

- **One implementation.** `decision/gates.py`'s `CandidateGates.candidate` is the gate composition
  that the census, the sleeve's shadow log and Session 17's decision layer all run. The gates are
  the instrument, tenor, the catalyst buffer, the mandatory close, liquidity, the smile, the
  concurrency cap and the pool.
- **The entry gates** need a size and a thesis, so they apply once one exists (`entry`): position
  size, the payoff ratio at a named target, the entry's place in its range, and the theta budget.
- **Results.** Each refusal carries a reason code (`Gate`). A gate whose input is absent is
  **untested**, with why, and a waived gate (a rule in configuration, such as the open-interest
  depth that waives a wide spread) counts as passed.
- **The mandate is not a switch.** The instrument gate refuses a futures underlying and a
  leveraged or inverse description unconditionally. `InstrumentsConfig` already refuses a
  `prohibited` list that drops a mandate category, so a gate conditioned on that list would only
  look configurable.
- **Freshness.** Candidates come from each underlying's newest chain. A chain older than
  `grid.freshness_days.option_chain` is skipped with why, and a surface older than that leaves
  the IV-percentile gate untested. A stale quote is no candidate.
- **The census verdicts**:
  - `viable`: at least `census.min_viable_contracts` contracts clear every gate fully tested;
  - `untested`: untested gates could explain the shortfall;
  - `thin`: they could not;
  - `no_data`: no contract was scanned.
  The step is ok only when `viable`.
- **Offline.** A compute run has no account read, so the pool's gates are untested
  (`OFFLINE_BUDGET`), and the census reads `thin` or `untested`, never `viable`. `Census.run`
  takes the pool (`affordability`) from a caller that read the account, which is Session 17's
  service.
- **The mutation check** breaks three invariants on a copy of the module: a crossed book fails
  before the depth waiver, the pool subtracts the commitments, and the entry window starts above
  the minimum hold. `tests/unit/test_gates_mutation.py` records the test that kills each mutant.

Rejected: each caller filtering with its own subset of the gates. The census would count contracts
the sleeve refuses.

## Session 13 — Core engine (2026-10-25)

### D140 · One core mechanism for research and the live book (§5.4, D64, D80)

- **The rules Session 4c tested are the live rules.** `engines/core/` holds the mechanism that was
  `research/strategies.py`'s `CoreStrategy`:
  - `rules.py`: role targets, the trend scale, the outer band, the losses-first sale order and
    the held costly reductions;
  - `weights.py`: the environments' risk balance and the equity overlay;
  - `trades.py`: the delta trades in whole units, sells first, and the buys scaled by one common
    factor to the cash.
  Research's `CoreStrategy` subclasses `CoreRules` and adds only Session 4e's total-risk
  scaling, and `research/simulate.py` sizes its trades with `engines.core.trades`. D64's contract
  (decision, execution, ibkr and engines never import research) holds because the code moved
  into `engines/`: research imports it, never the reverse.
- **The live engine** (`engines/core/engine.py`, `plan_portfolio`) runs those rules on the live
  book and returns a `TargetPortfolio` (`domain/core.py`): the targets and status of each role
  and vehicle, the trades, the trades withheld with their reasons, and notes. It is pure. The
  decision layer (`decision/rebalance_core.py`) reads the store, the quotes and the lots, and
  turns the trades into intents under the live guards.
- **Parity** (`tests/integration/test_core_parity.py`): research's `build_strategy` runs on the
  locked pre-registration through the simulator over a fixture panel. At every rebalance date
  the live engine plans on the same closes, read from the market store, with the simulator's
  book as the live book. Weights and trades must be equal. The test covers A (with a band
  breach and sales) and B (with tilt scales, swap-partner buys and held reductions), each
  case asserting that it exercised what it covers.
- **The risk budget's independent check** is skfolio's `RiskBudgeting` (a convex solver),
  test-only. It is a dev dependency, and an import contract keeps skfolio, scipy, scikit-learn
  and cvxpy out of the package. The legacy optimiser's still-valid risk-parity cases are
  ported. Its other methods were dropped by the plan.

Rejected:
- A live port beside the research code: two implementations of one rule drift, and the tested
  core would no longer be the traded one.
- The live engine importing `research/`: this breaks D64's contract.

### D141 · The variants are configuration, held to the pre-registration (§5.4, D79, D91, D95)

- **`core.variants`** holds the ten variants Session 4c registered:
  - the equity reference (buy and hold);
  - 60/40 (banded weights);
  - A, B and C (risk balance; B and C tilt);
  - D10 to D50 (the equity overlay with the tilt).
  Their constants are in `core.yaml`: environments, budgets, the declared volatilities and
  correlations, bands, tilt and the tax-aware benefit fraction.
  `tests/unit/test_core_config.py` holds every constant to
  `research/preregistrations/core-allocation.md`, and the computed weights to the report's.
  A change is a new pre-registration and its verdict, not an edit.
- **`core.variant: A`** is the owner's point (D91), and Session 4e left it standing (D95). A
  risk balance's weights are solved at load by `quant/risk_budget.py`, in the
  pre-registration's environment order, and rounded to 10⁻⁶ with the remainder on the largest
  weight. A solve that does not converge, or an invalid matrix, refuses the configuration
  (`CoreError`).
- **Removed:** the Session 6 skeleton (`core.skeleton_targets`), `decision/rebalance_basic.py`
  and `trading.inner_band`. The pre-registration's rebalance returns every role to its target,
  so its inner band is the target itself. A separate inner band would trade a book the
  simulator holds still.

### D142 · When the core trades: start, band, cash, hold (§5.4)

Each cycle the engine names one trigger:
- **Start.** The book holds nothing: every role is bought to its target.
- **Band.** A role's weight is outside its outer band, the smaller of 5 points and 25 % of its
  target. Every role goes back to its target as the simulator rebalances:
  - a leg within `research.simulation.no_trade_band_fraction` of its target is left;
  - whole units, sells first;
  - the buys scaled by one factor to the cash and the proceeds after costs;
  - a sale that costs more than it raises is not made.
  Only a band breach sells: sells beyond the outer band only.
- **Cash.** Inside the bands, the book holds at least the minimum trade more cash than its
  targets leave in cash (a deposit, income, the proceeds of an earlier sale). The roles below
  their targets are bought with that cash, and nothing is sold: cash flows first. The simulator
  has no cash flows. With the minimum trade as the threshold, a band's whole-unit residue never
  starts a cash trigger, so parity holds.
- **Hold.** Nothing trades.

Under the live guards:
- A band's buys may spend only the cash spendable now. Spendable cash is the smaller of the
  account's and the book's cash, less open reservations, deposits not yet released, open buys'
  commitments and the cash floor. The rest waits for the sales' proceeds, and a later cycle's
  cash trigger buys it.
- A leg below `trading.min_trade` or its own commission is not made, and the reason is recorded.

The band and the cash checks run every cycle on that cycle's prices (the daily drift check). The
trend is evaluated monthly (D144).

### D143 · A role's vehicles on the live book (§5.4, §5.8, D120, D122, D123)

- **Vehicles.**
  - The selector's best-ranked vehicle is the role's primary.
  - A tax-aware variant also gets the swap partner `SwapPartners` finds. The partner needs a
    measured correlation from the research store (`orchestration/correlations.py`, read once a
    day). Without one, the role has no partner, and the reason is recorded.
  - An identity IBKR did not confirm is never bought, and neither is a vehicle without a quote
    this cycle.
- **Held vehicles stay with their role:**
  - a held vehicle that is still proposable is bought before the primary (the tested rule), so
    a change in the ranking alone never churns the book;
  - a held vehicle that is no longer proposable is closed: never bought, still counted in its
    role's weight and sold when the role is reduced. It is never switched for the primary on its
    own, which would realise its gain;
  - a held vehicle that no role names (`unlisted:`) has a target of zero, so it is outside its
    band and is sold.
- **Unfilled roles.** A role with no vehicle that may be bought keeps at most what it holds
  (held only). One that holds nothing (unfilled) is held at zero and untilted. Its weight stays
  in the book's cash: today that is gold and broad commodities, whose venues' capabilities are
  unverified (16 % of A). Spreading the weight over the other roles would change the variant's
  risk balance into one nobody tested.

### D144 · The trend on the live book (§5.4, D82)

- **Monthly, on the last completed month end.** The tilt reads month-end closes from the market
  store's daily bars:
  - the last bar on or before each month end, within `research.simulation.max_price_age_days`;
  - converted to the base currency at `BaseRates.per_base_on`, within
    `research.data.max_fx_age_days`;
  - a month with no such close restarts the run, with a note.
  `quant/trend.py`'s `trend_measures` and `settled` serve research and the live book alike.
- **History.** A role's trend is read on its vehicle with the longest history: the primary is
  aliased to it. A role with too few month ends on every vehicle is left whole, its reason
  stated: the static weight is the fallback (D89). If the cash role has too little history,
  momentum has nothing to be measured against, and every role is left whole.

### D145 · Tax-aware execution on the live book (§5.4, §5.16, D110)

- **The rule is the one tested:**
  - sales take losses first, and a lot of unknown basis comes last;
  - a trend reduction is made only while the tax it adds is at most
    `core.tax_aware.benefit_fraction` of the value sold, or it is held and the portfolio says so;
  - a buy the two-month rule would defer is made through the swap partner.
  The answers come from the live lots (`TaxHooks`: basis, trial tax, deferral).
- **Unknown tax holds the reduction.** The tax book may be incomplete for a security (D110).
  Research knows every lot; the live book fails closed.
- **D110's guard stays.** The rebalancer never buys a vehicle inside its open loss window. For a
  variant that tilts, the engine has already sent that buy to the partner. For A, which has no
  partner rule, the buy is withheld, with its reason, until the window closes. This is the one
  place the live core is stricter than the tested core: research's A deferred the loss, and its
  after-tax result counted the deferral.

### D146 · The core's StrategyValue (§5.15, D108)

`capital/core_value.py`'s `CoreStrategyValue` notes the portfolio of each survey, and the
reservation planner reads it:
- **Overweight.** A core position's target weight is its vehicle's weight in that portfolio,
  times the core's allocation. "Above its target" is measured against it.
- **Exits.** A core position whose vehicle has a target of zero (unlisted, or its role's
  target is zero) is an exit the strategy makes anyway.
- **Protection and expected return.** The core has neither.
- **The satellite** keeps the default's answers.
- **Before the first survey** the core states no target and exits nothing.

The planner's `reserve` step runs before the cycle's rebalance, so it reads the previous cycle's
portfolio, one cycle stale.

Rejected: the planner re-running the engine. It would read the store and the quotes a second
time for the same answer.
