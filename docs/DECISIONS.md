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
attributed where copied. A further dependency needs an entry here.

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
is retried up to `max_attempts`.

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

## Owner decisions (2026-10-17)

### D88 · The drawdown tolerance, exposure caps and the option budget (D60; MASTER_PLAN §1, §11)

The owner set the drawdown tolerance at **30 % from the previous peak of the portfolio's total
value**, measured as D60's mark-to-market value of what the bot manages. Holdings the bot does
not manage do not count toward it (owner). The horizon is very long: the owner does not need the
money now. The objective stays D60's: compound growth of wealth net of costs and tax.

**Meaning.** The tolerance is the limit D60's research uses to filter candidates. A candidate
respects it when the share of simulated paths whose drawdown reaches 30 % is at most the
acceptable probability, and the core's variant is chosen by the same rule. It is **not** a
guaranteed maximum loss, and **not** an order to sell when it is reached. The breakers of
§5.11 keep thresholds and keys of their own, and this entry sets none of them to 30 %.

**Open.** These stay open. Reports show their implications without assuming a value
(`research/reports/drawdown-tolerance-implications.md`). Until both are set, Session 13's
precondition is unmet.

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
before any data are read, not assumed.

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
