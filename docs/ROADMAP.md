# Roadmap

What is done and what is open. The design is `docs/MASTER_PLAN.md`; each session's task is
its section of `docs/SESSION_PROMPTS.md`; why things are as they are is `docs/DECISIONS.md`.

## Sessions

| # | Session | Outcome | Status | Release |
|---|---|---|---|---|
| 1 | Foundation | scaffold, docs, owner settings and defaults, jurisdiction registry, CI, releases, legacy snapshot | done | v0.1.0 |
| 2 | Store, calendars and numerics | store and migrations, calendar and phase layer, BSM, IV, smile, stats, inference, costs | done | v0.2.0 |
| 3 | IBKR read path | OAuth and Gateway transports, session lifecycle, client, rate limits, quote status, contract identity, trading schedules, preflight | done (live login blocked in the cloud) | v0.3.0, v0.3.1 |
| 4 | Order path, kill switch and modes | intents, order client, reply and quote policy, clearance lock, state machine, paper guard, modes, kill switch | done (paper orders not yet placed live) | v0.4.0 |
| 4a | Pre-trade rails | pre-trade checks with reason codes, eligibility skeleton, approval queue, flatten, paper order test | done (paper order test not yet run live) | v0.5.0 |
| T1 | Deployment machine (owner) | unattended login on two days, read-only preflight, paper order test, a Java 21 and 25 side test, on Ubuntu 26.04 (SESSION_PROMPTS §A.5) | logins passed (owner, 2026-10-20 and 2026-10-21): both days' unattended login and read-only preflight passed under WSL2 Ubuntu with Java 11, and Java 21 and 25 passed once (21 Oct); the paper order test has placed no order (outside the windows on Xetra and the NYSE; on ASX inside its window the first live-orders read lacked `snapshot: true`); open: the order test, after the follow-up below | v0.12.1 |
| 4b | Research harness | point-in-time research data, simulator with costs and the `es` tax engine, statistics, decision rule, trial registry, European commissions | done (no real-data study: 4c, 4d) | v0.6.0 |
| 4c | Core verdict | pre-registration, then one after-tax run of the core's variants against the references; a frontier from which the owner picks the variant | done (every candidate inconclusive; the owner's point resolves to A, D91) | v0.7.0, v0.7.1, v0.7.2 |
| 4d | First selection hypothesis | data audit, choice among H1, H2, H4, pre-registration, one confirmation run, H5 beside it | done (the audit chose H1; H1 and H5 insufficient) | v0.7.3, v0.8.0, v0.8.1 |
| 4e | Total-risk scaling | data checks, covariance estimators, a total-risk strategy, a bootstrap that respects lookbacks and a long-history check, pre-registration, one confirmation run against static A on the objective (D89, D90, D92) | done (every candidate inconclusive and outside the owner's point; A stays, D95) | v0.8.6, v0.9.0, v0.9.1, v0.9.2 |
| 5 | Execution mechanics | currency manager, pricing, resting stops with gap handling and attached orders, executor, reconciliation | done (live steps a command list for the owner; attached orders not adopted, D99) | v0.10.0 |
| 6 | Walking skeleton | service, cycles, skeleton targets → intents → paper → reconcile → digest, on the T1 machine | done (the live paper cycle a command list for the owner, docs/RUNBOOK.md) | v0.11.0 |
| 7 | Capital, cash flows and tax | managed capital, cash-flow ledger, reservation planner, owner questions, lots wired to fills, currency lots, TWR | done (IBKR's cash-activity shapes not observed: no paper login from the cloud) | v0.12.0 |
| 8 | Universe and market-data collectors | global universe with hedged classes and look-through, stock candidates, venue profiles, eligibility policy, collectors (option surfaces stored forward) | done (collectors built against fixtures: no IBKR call from the cloud; holdings and research history loaded live) | v0.13.0 |
| 9 | Broker capabilities and vehicle selector | capabilities measured, core candidates, swap partners, vehicle selector | done (capabilities from the owner's paper run and lite; the probe on paper is a command for the owner) | v0.14.0 |
| 10 | Macro, positioning, insiders and events | the sources a registered hypothesis, component or safety rule consumes; incident feeds; catalyst calendar; a minimal EventExtractor (H7's clock) | done (every built source but GDELT collected live from the cloud; the extractor waits on its key; H7 pre-registered, #46) | v0.15.0 |
| 11 | Measurement store and views | measurement store, equity grid, macro board, rankings, correlations, IV solve, gap distributions | done (built and accepted on an offline fixture store; the census reports the gates untested until Session 12) | v0.16.0 |
| 12 | Engines and gates | option surface, options sleeve (behind its activation condition), fx, commodity, risk engine, gates | planned | |
| 13 | Core engine | the variants 4c registered (4e found no total-risk specification sufficient, D95), risk budgeting, tax-aware trend tilt, rebalancing | planned | |
| 15 | Satellite engine | Hunter components in shadow, measured independence, entry rules, lifecycle, triggers, selection, pattern registry | planned | |
| 16 | LLM layer | provider and model registry, evaluation set, budget, structured outputs, six roles on Session 10's stamped series | planned | |
| 17 | Decision layer | expression selection, sizing by gap-adjusted loss, activation conditions, whole-book risk, breakers, inertia, intents | planned | |
| 18 | Scoreboard, patterns, capital and promotion | TWR attribution, TCA, grading, calibration, twins and monitoring, capital by evidence, promotion | planned | |
| 19 | Cockpit, settings and deployment | MCP cockpit, Telegram, settings flow, dashboard, Docker, machine, backups, updates | planned | |
| 19a | Capacity and scale | bonds, baskets, mutual funds, capacity limits, venues and currencies, tax at scale, every activation condition | planned | |
| 20 | Red-team and cleanup | failure-mode audit, mutation tests, `legacy/` deleted, docs truth pass, 1.0.0 | planned | |
| 21 | Research: second batch | H3, H6, H9, the Hunter's components and agreement rule, batch-1 candidates not chosen | any time after 4d (Hunter parts after 15) | |

There is no Session 14: the core's backtest and verdict are Session 4c (DECISIONS D61).

## Session log
### Session 11 — Measurement store and views (2026-10-23, v0.16.0)

**Landed.**
- Migration `011_measurements.sql` and `store/measurements.py` (D130): one long-format table of
  every leaf of every entity (equity-grid cell, macro-board cell, instrument, correlated pair) as
  of a session, with status, reason and reason code, `n_obs`, window, units, source, the weakest
  market-data status behind the value and `inputs_hash`. Reads are bounded by `as_of`; a build
  replaces its own session only.
- `grid/`: lite's 15 indicator families (45 leaves per cell, D134), its estimators and window
  honesty, per-input freshness by requirement group and the macro blanket; the equity grid
  (market × sector, 192 cells) and the macro board (region × asset class, D131) as builders that
  write measurements and views that read them; the four ranked views with trajectories; the
  cross-asset correlation matrix keyed on legs with norms and shifts; family reach; the census.
- The session's implied-vol solve (`grid/iv_session.py`): chains a route priced without vols, at
  the money, discounted at the rate released by the chain's session
  (`numerics.implied_vol.solve.risk_free_series`), written to the snapshot's own column with its
  method; non-convergence counted and the step degraded above the configured share.
- HAR-RV (`grid/har.py`, D132) and overnight-gap distributions with earnings flagged on the venue
  calendar (`grid/gaps.py`, D133), as instrument leaves.
- `obliskiq compute <solve-iv|measure|rankings|census|all>` (`orchestration/compute.py`) under one
  run record, exit codes as the collectors'.
- Config: `config/defaults/grid.yaml` (lite's `grid` and `census` keys with lite's values, and the
  board, HAR and gap settings), `config/defaults/screen.yaml` (lite's two IV-percentile bounds),
  `smile.skew_delta` and `smile.term_structure`; `us.credit` and `us.vol_index` wired to Session
  10's BAA10Y, VIXCLS and VXVCLS.

**Tests.** Lite's window-honesty, isolation and validation tests ported (`test_grid_*.py`,
`test_cli_compute.py`, `test_store_measurements.py`), with failure branches: refusals per family,
restated opens, a rebased CPI, mixed frequencies and units changes in a correlation, a singular or
negative HAR fit, an unplaceable earnings window, an unconverged solve, a step that raises. A
build is point in time (data released after `as_of` changes no row and no hash) and
reproducible. `make check` green (see the PR).

**Acceptance.** `tests/integration/test_measurement_acceptance.py`: an offline fixture store
(`tests/support/measure_rig.py`, synthetic and deterministic) yields both views with honest
statuses: measured cells on their native instruments, the option families where a chain is
collected, unmapped cells told apart from unseeded ones, every leaf not `ok` with its reason, the
disclosure flagging a two-stock fund. The import contracts and an AST walk prove the compute run
reaches neither `ibkr` nor `httpx`, and `compute all` runs with network sockets refused. On an
empty store `compute all` writes 8,640 grid leaves and 813 board leaves, all honestly absent, and
exits degraded.

**Deferred.** The census's gates are Session 12's: until they exist it reports every gate
untested and its step is degraded (D134). Nothing else of the prompt. No IBKR call from the
session.

**Live findings.** None: no live call.

**Prompt edit.** §12 named `config/defaults/{liquidity,smile}.yaml` as the files Session 2
started; Session 11 added smile's `skew_delta` and `term_structure` and created
`config/defaults/screen.yaml` with lite's two IV-percentile bounds, so §12 now names them (a
fact, not scope).

**Dependabot.** No patch or minor update was open; the major cryptography 50 (#9, #5) stays red
and in Follow-ups (D96).

### Session 10 — Macro, positioning, insiders and events (2026-10-23, v0.15.0)

**Landed.**
- `config/sources.yaml` (D124): every source besides IBKR with its consumers, activation
  condition, release-date policy, schedule and staleness window. Sixteen sources are built and
  fourteen declared with the condition that would build them (Eurostat, BoE, SNB, Bundesbank, BoC
  Valet, ABS, RBA, BCB SGS, Norges Bank, MAS, Banxico, CBRT EVDS, DBnomics, the MOF's JGB yields).
  `data/macro/catalog.py: backtest_series` refuses a series without a defensible release date.
- `data/macro/`: FRED and ALFRED (`output_type=4`, vintage windows clamped to ALFRED's vintages,
  `value_vintage`), the ECB and the SDMX decoder, EIA v2, ONS, StatCan, the BoJ, the BIS and the
  Fed's excess bond premium. 74 series with units, frequency and release rule, guarded on units
  and frequency.
- `data/positioning/` and `data/insiders/` (D126): CFTC archives and Socrata (net non-commercial
  share of open interest, percentile with warm-up, velocity, by release date); SEC Form 4 forward
  from EDGAR's daily index, cluster inputs by filing date.
- `data/events/` (D127): Polymarket and Kalshi snapshots and velocity; GDELT's whole UTC days and
  Welford pulse; the news feeds (RSS 2.0, RDF, Atom), deduplicated, stored raw, an issuer filing's
  press release read from EDGAR; VADER's fallback tone; Yahoo earnings dates; the catalyst
  calendar (`obliskiq sources catalysts`).
- Halts (D125): `calendars/halt_feeds.py` (market-pulse-bot's structured-feed reader and the
  Nasdaq Trader halts as instrument incidents) and `data/halts.py`. The heartbeat reads the feed
  every five minutes through `Calendars.accept_incidents`; the last state is kept, restored at
  the start and alerted after three failures. XNYS takes the single-stock feed beside XNAS.
- `data/runner.py` and the schedule (D129): sources run by name (`obliskiq sources
  run|health|list|backfill`) and as service jobs `source:<id>.<n>`. Each run's verdict is the
  source's health (ok, stale, failing), shown by `obliskiq status` and the heartbeat file.
- `llm/` (D128): the EventExtractor (`ExtractedEvent`, escaped template-string prompt and its
  hash, one retry then fail closed, a context-window guard, the monthly budget, every call
  appended), run after each news source and by `obliskiq llm extract|status`. Without
  `GEMINI_API_KEY` it is skipped and the items wait.
- H7 pre-registered as a forward test in its own PR (#46, merged 2fcc76e) before any extractor
  run, and locked (`research/preregistrations/llm-events.lock`). `research/prereg.py` reads `kind:
  forward_test`, and the history commands refuse it. H6, H7 and H9 are in the pattern registry; a
  test resolves every `hypothesis:` consumer against it.

**Tests.** As the acceptance names them:
- every fetcher against fixtures captured live on 2026-10-23 (sidecar notes in each
  `tests/fixtures/*/README.md`);
- COT and Form 4 point in time (a report before its release, a filing before its filing date, are
  absent);
- no source without a release-date policy feeds a backtest;
- the halt scope (an instrument blocked, its venue and the venue's other instruments not; a feed
  refused a venue-scoped incident).

Also tested:
- failure branches of every collector (HTTP errors, non-JSON, a throttled GDELT, a missing SEC
  agent, a body over its cap, a DTD, an unreadable filing);
- the Welford z-score against the distribution before the observation;
- the extractor's escaping, hash, retry, failing closed, failed call, budget and context window;
- the service running source jobs, the extractor after the news feeds, and the halt feed's
  failure, alert and restart.

`make check` green (see the PR).

**Acceptance.** Each fetcher has fixture tests and a live probe: every built source but GDELT
collected live into a fresh store on 2026-10-23 (`docs/knowledge/macro-sources.md`, its status
table). GDELT answered 429 to this egress, and its fixtures are live captures from the same day
(VERIFY-LIVE below).

**Deferred.** Nothing of the prompt. The extractor's first live run waits on `GEMINI_API_KEY` (the
owner's). GDELT's collection is verified from the deployment machine. No IBKR call from the
session.

**Live findings** (`docs/knowledge/macro-sources.md`):
- The BoJ's database holds no JGB yields (the MOF publishes them); `SESSION_PROMPTS.md` §10
  deliverable 2 corrected to match.
- EIA released the week ending 2025-12-19 ten days later, so the lag is 10, not lite's 3.
- The CFTC's archive refuses httpx's default user agent (403).
- A missing EDGAR index is a 403 with an XML `AccessDenied`. EDGAR's company feeds carry no text;
  the press release is in the filing (an 8-K's EX-99.1).
- The issuers' investor-relations pages and ESMA's feed refuse the container or lack a feed.
- GDELT omits the same hours in every query and throttles the shared egress.
- Kalshi's recession series is `KXRECSSNBER`.
- Nasdaq Trader's halts listed NASDAQ and AMEX markets only.
- `yfinance` returned no earnings dates for LISN and JDEP.

**Prompt edit.** §10 deliverable 2 named JGB yields among the BoJ series; corrected to USDJPY and
the monetary base, the yields being the MOF's (a fact, not scope).

### Same-index swap partners (owner request, 2026-10-23, v0.14.1)

The owner allowed a swap partner on the same index from another issuer (D123):
`config/owner.yaml` sets `vehicles.swap_partners.same_index: true`; the expert default stays off.

- On Session 9's research history, world equity gains a partner: SPPW and XDWD (MSCI World, State
  Street and Xtrackers, 1.000 monthly over five years). The other roles are unchanged.
- MASTER_PLAN §11 records the ruling (item 19); §4, §5.4 and the glossary say a partner tracks a
  different index unless the owner allows the same one.
- The tests that judge the switch set it themselves; one checks the owner's file turns it on and
  the default leaves it off.

No IBKR call.

### Session 9 — Broker capabilities and vehicle selector (2026-10-22, v0.14.0)

**Landed.**
- Broker capabilities (D119): `config/defaults/capabilities.yaml` holds 17 checks (shares on the
  NYSE, ASX, Euronext Amsterdam and Paris and Madrid; UCITS ETFs on Xetra, the LSE, Amsterdam,
  Milan, SIX and Paris, and US ETFs in New York; ETCs on the LSE, Milan and Xetra; US and Eurex
  options), each reading verified only on the Client Portal route. `decision/capabilities.py`
  gates the selector on search, quote and order and lists the permissions held.
  `execution/capability_probe.py` and `obliskiq universe capabilities --probe` read them on the
  paper account, whatif only (`docs/RUNBOOK.md`).
- Core roles and candidates (D121): `core.roles` with the nine roles of 4c's variants and their
  series; 30 more instruments (33 candidates, two to five per role, among them the hedged classes
  IWLE, IBC5 and IBB1), each confirmed on its issuer's page and justETF, conids pinned by ISIN
  through IBKR's connector; `IBIS2` (Xetra's ETF segment) in `ibkr_exchanges`
  (`docs/knowledge/universe-sources.md`).
- `decision/vehicle_selector.py` (D120): gates with reasons, then the seven weighted criteria;
  `decision/swap_partners.py` (D122); `quant/tracking.py` and `research/tracking.py` (monthly
  correlations in EUR, point in time); `obliskiq universe vehicles <exposure> [--primary]`.
- `es.yaml`: `retained_income` (LIRPF art. 94) and the swap rule the law sets (another issuer);
  an ETC is taxed as a debt security (D122).

**Tests.** The named ones: a hedged and an unhedged class of one fund ranked as distinct vehicles;
a same-index partner refused while the switch is off; no vehicle proposed whose capability is
unverified (every role, every candidate); every ranked vehicle's seven criteria with a reason, and
every rejection with its gate. Also: each gate's reason, qualification rules for further types,
the all-in cost and each way it cannot be priced, real-time data costed, every criterion's score,
the window on a holiday, the role and series validation, the capability schema and verdicts, the
probe's every step on the order rig (options included), the correlation by hand, point in time and
across ISO years, and the CLI's output and refusals. `make check` green: 4,192 tests, coverage
99.65 %.

**Acceptance.** Every core role has at least two candidates whose identifiers two sources
confirmed. From fixtures (every candidate's venue verified) the selector returns an eligible
vehicle for each of the nine roles and gives each rejection its gate and reason. With today's
capabilities and the research history loaded (as of 2026-10-22), seven roles have a proposable
vehicle on Xetra (world SPPW and XDWD; EM XMME and VFEA; euro government 3–7 years DBXQ and X57E;
long DBXG and IBCL; inflation-linked TIUP; Treasuries XUTD; cash XEON); gold and broad commodities
have none until the probe verifies the LSE and Milan, and their Xetra lines (4GLD, UIQK) wait on
a KID. Capabilities are recorded from the owner's paper run (T1, 2026-10-21) and lite (2026-09-10),
the rest queued.

**Deferred.** No IBKR call from this session (AGENTS.md: the paper login does not complete from a
cloud container, and the deployed bot holds the brokerage session): the probe, IBIS2 and the conids
on CP are in the queue below. Lite's ADR map moves to Session 15 and positions outside the
universe to Session 11 (Follow-ups).

**Live findings.** IBKR's contract search through the connector [MCP ✓ 2026-10-22]: Xetra's ETF
segment is `IBIS2`; the search returns no Xetra line of EUNL, IS3N, SXRP, IUST, SXRM, PPFB or VGVF;
Euronext's `CSH` is IBKR's `CSH1`; UBS's USD class of the CMCI fund is described "H. CHF". Tracking
read monthly over five years: 0.92 to 0.99 for unhedged classes; hedged classes 0.39 to 0.91 against
an unhedged series in EUR; weekly returns understate tracking (XMME 0.70 weekly, 0.98 monthly).
Legge 199/2025 raised Italy's standard transaction-tax rate to 0.4 % from 2026-01-01 [Normattiva
✓ 2026-10-22]; Légifrance and ubs.com refuse the container.

### Session 8 — Universe and market-data collectors (2026-10-22, v0.13.0)

**Landed.**
- `config/universe.yaml` (D112): lite's universe ported whole and regrouped into roles,
  attributes and exposures, plus IWDA, EMIM and IBGM with their UK facts and the sixteen iShares
  US country funds: 97 instruments (26 tradable candidates, 37 option underlyings), the grid's
  16 markets × 12 sectors complete (measured or unmapped with a reason), the macro board's 17
  regions × 9 asset classes, 16 futures roots, 12 commodity proxies, holdings sources and the
  candidates' scanners and gaps. The loader merges it as one section and the schema refuses an
  incomplete matrix, an unresolved reference or an exchange code nothing maps.
- `jurisdiction/eligibility_eu_priips.py` (`eu_priips`, D113), selected by `es.yaml` and wired
  into pre-trade; a `ContangoCheck` that fails closed until Session 12; leveraged and inverse
  products rejected at ingestion and recorded (`universe_rejections`). The skeleton allowlist, its
  config key and `ConfiguredDirectory` are gone; `UniverseDirectory` replaces them; tax facts come
  from the universe.
- Venue profiles (D114): currencies, trading permission and board lot per venue with sources;
  `settlement_sessions` may be unknown; XTAE and ten IBKR codes added; `market_data.feeds` and
  `owner.yaml: market_data.plan` (OPRA L1, Eurex Core L1, the complimentary US feed).
- `data/market/` (D115, D116): the universe sync (seed, pin report, supersede, reject), the
  backfill (watermark and overlap, venue-dated bars), snapshots judged against the plan, the chain
  collector (strike band, sigma ladder, declared venue, multiplier per row, surfaces stored forward)
  and the curve collector; per-target retry and pacing, step deadlines, run records; one
  collection job per venue after its close, the uncovered folded into New York's
  (`collectors.schedule`). Migration `009_universe.sql`: rejections, holdings files and lines,
  scanner rows, candidate resolutions, the chain row's multiplier.
- `data/candidates.py` (D117): iShares holdings (both sites' forms) and scanner runs, listings
  matched across files, credited per cell, point in time, every cell covered or its gap declared,
  refreshed every `refresh_days`, resolved and pinned on demand.
- Research history (D118): `research data load-universe` through `research/data.py`.
- CLI: `obliskiq collect <universe|candidates|backfill|snapshots|chains|curves|daily>` and
  `obliskiq universe validate|pin`.

**Tests.** Ported from lite: idempotence (a rerun replaces its session), run continuity (every
step recorded, the run's row and verdict), sequence isolation (a broken step and a step past its
budget do not stop the next), outage (every step error, nothing written), the strike band and the
tenor window, realised volatility and the sigma ladder, the European chain (the declared venue sent
when the search omits it; zero rows fail and write neither half), universe seeding and
supersession, the backfill's watermark. Also: eligibility for every asset class and the US
ETFs' reasons, the store's point-in-time reads of holdings and scans, crediting and coverage on
real files, the refresh cadence, resolution on demand, the schedule across daylight saving and the
fallback, the service's collection job, the CLI's output and refusals, every refusal of the
universe's schema, and every universe key's reader. `make check` green: 4,124 tests, coverage
99.63 %.

**Acceptance.** The universe validates. Every grid cell has candidates or a declared reason:
182 of 192 cells hold candidates and the other 10 are the declared gaps, on the live iShares files
of 2026-10-20 (UK funds) and 2026-10-21 (US funds), 3,954 companies, read 2026-10-22.
Eligibility explains each US-domiciled ETF's refusal (`obliskiq universe validate`: "SPY is an etf
domiciled in US, UCITS False, with its PRIIPs KID unavailable: …"). Research history is loaded for
the 23 tradable candidates (2026-10-22): EXH1, EXV7, EXH4, EXV5, EXH3, EXV4, EXV1, EXV3, EXV2,
EXH9, EXI5 from 2008-01-02 (4,775–4,776 closes); FCX, ALB, CCJ, MOS, BG from 2006-01-03 (5,233);
NTR from 2018-01-02 (2,213); ASML, SAN, TTE from 2006-01-02 (5,318–5,328); IWDA from 2009-09-25
(4,314); EMIM from 2014-06-02 (3,171); IBGM from 2008-01-02 (4,812); and for the largest
candidate of each cell, 181 of 181. The chain collector stores surfaces with date, source and
venue per row (fixtures; the live run is the VERIFY-LIVE item below).

**Deferred.** No IBKR call from this session: the paper Gateway login does not complete from a
cloud container (AGENTS.md) and the deployed bot holds the brokerage session. The collectors' live
round (snapshots, history, the chain walk, futures, the scanner, pins for TOPIX, BARN, LISN and
JDEP, descriptions for the 47 pinned instruments without one) is in the queue below.

**Live findings.** iShares' holdings files and Yahoo's histories are reachable from the cloud
container (docs/knowledge/research-sources.md); Yahoo's symbol forms for Hong Kong, class shares and
iShares' `NG.` were found by the first load of the candidates and are configuration now. Lite's
"ISF is UK-domiciled, not UCITS" was wrong (D112). Pins were read through the owner's IBKR
connector (contract search, read-only) [MCP ✓ 2026-10-22].


### T1, second day (owner, 2026-10-21, v0.12.1)

Run on the owner's machine (Ubuntu 26.04.1 under WSL2, `main` at v0.12.0, mode `shadow` before and
after each run, account masked `DU*******`), six logins from 21:55 to 23:33 UTC, each at least ten
minutes after the previous one's start and each on its first attempt. Shapes and times:
docs/knowledge/ibkr.md, "T1, second day".

**Landed.**
- **Day-2 login and `preflight --read-only` (Java 11): every step passed.** With day 1 (2026-10-20),
  T1's own criterion, two unattended logins and two finished preflights, is met.
- **Java 21 (Temurin 21.0.12.1) and Java 25 (25.0.4.1): the same read-only preflight passed** from a
  copy of `config/` outside the clone. One day each: `ibkr.gateway.java_major_versions` stays
  `[11]` until a second day passes (D96). No Java 17 or `--add-opens` run was needed.
- **Playwright 1.64 on Ubuntu 26.04.1:** six unattended logins; no manual login was needed.

**Not landed: no order was placed.**
- Xetra and NYSE, outside their windows (the owner's request): the pre-trade gate refused both with
  `venue_not_tradable` before anything reached IBKR. That is the order window working, not a
  fault; it was not widened.
- ASX (BHP, conid 4036812, dry run from a config copy, inside its window): the calendar gate passed,
  then the gate refused with `account_unreadable` because the first live-orders read of a session
  had no `snapshot: true`. The in-window rerun on Xetra and the NYSE will refuse the same way.
  **Follow-up below.**

**Windows.** Not tried again: the Gateway starts only under WSL2 Ubuntu.

**Next.** The snapshot follow-up, then `preflight --paper-order-test` inside both windows
(16:00 to 17:15 Madrid time; 15:00 from 26 October), plus one more day of the Java 21 and 25 side
runs. Paste the masked reports into a short session: *"Record T1's findings per SESSION_PROMPTS
§A.5."*

### Session 7 — Capital, cash flows and tax (2026-10-21, v0.12.0)

**Landed.**
- `capital/`: managed and investable capital (`CapitalReader`): a capped book kept as its own
  account (its start, the owner's flows, its fills' cash, its holdings), the whole account as the
  NAV; investable = `min(managed, NAV − unmanaged) − reserved`; anything unreadable manages
  nothing, with why (D106). `capital.budgets` (70/20/5/5) and `capital.satellite_ceiling` behind
  one reader; an inactive sleeve's budget stays in the core. External holdings (lite's
  `external_positions`, `obliskiq holdings external add|list|remove`), stale after
  `positions.external_data_stale_days`.
- The cash-flow ledger (migration `008_capital.sql`): deposits, withdrawal reservations and their
  events, withdrawals, adjustments; `obliskiq cash deposit|reserve|withdrawn|cancel|classify|list`,
  each echoing and recording nothing without `--confirm` (D107). Deposits are deployed in tranches
  (`capital.deployment.tranche_days`).
- Reconciliation explains cash by fills and recorded flows; a gap a recorded deposit or withdrawal
  explains is matched to it; any other is a pending cash event that freezes new buys until
  `cash classify` (D107; closes Session 6's `cash_arrived` follow-up).
- The reservation planner (`capital/planner.py`, the cycle's new `reserve` step): the cash line,
  then sales by tax + trading + strategy cost, losses before gains, the two-month rule respected,
  settlement per venue (`settlement_sessions` with sources in `venues.yaml`) less a buffer,
  pacing by expected return, `StrategyValue` with a default that protects a thesis in its entry
  window; while a reservation is short the rebalancer sells nothing; the plan in the digest (D108).
- Owner questions (`obliskiq questions list|answer <id> <option> --confirm`): proceed, postpone,
  reduce, after year-end where the date allows, a later date for a strong position held; the
  default (proceed) applies at the deadline; alerts when asked and when expired (D109).
- Tax on the live path (`tax/book.py`): the heartbeat applies every paper or live fill to Session
  4b's lot engine at its trade date's rate (`fx_rates`, from the ledger), and saves lots, open
  loss windows, ledger and lot numbering with the fill in one transaction (D74); currency lots
  under `tax.currency_gains` (LIRPF arts. 14.2.e, 33.1, 33.5.e, 46.b; docs/knowledge/tax-es.md);
  incomplete fills alerted and the hooks silent for that security; the decision hooks from the
  live lots; `obliskiq tax report --year` (CSV, not tax advice) (D110).
- The time-weighted return over the NAV rows, flows neutralised (`capital.twr.flow_timing`), in
  the digest (D111).
- Also: `eligibility.skeleton_tax_facts` (ISIN, issuer, tracked index of the skeleton's funds and
  the paper test's two stocks, from IBKR's contract search [MCP ✓ 2026-10-21]); the exit reason
  `withdrawal`; the US treaty's 15 % and Spain's 0.2 % transaction tax read in the BOE's text.

**Tests.** The prompt's cases: fills opening and closing lots with fees against the engine; a live
buy after a loss sale deferring the loss and releasing it; currency lots on a conversion and its
repatriation; carry-forward expiry; a reservation met on time across a holiday and across the
daylight-saving change; the cash line used at once and a fresh thesis spared; a question when only
a protected thesis is left, postpone moving the reservation, no answer → proceed; the year-end
option only when the date allows; a weak position sold at once and a strong one held to the last
safe days and never past them; deposit tranches; the unexplained-cash freeze and its
classification; TWR equal to a hand calculation; switching the jurisdiction swaps the policy. Also
every reason the reader and the planner stop, a withdrawal's sales going ahead while only
unexplained cash freezes new buys, the CLI's refusals, the service's reserve step and alerts, and
the heartbeat's tax book. `make check` green: 3,969 tests, coverage 99.79 %.

**Dependabot.** certifi 2026.10.19 (Almoon-D/ObliskIQ#37) merged; cryptography 50
(Almoon-D/ObliskIQ#5, Almoon-D/ObliskIQ#9) stays in the follow-ups (D96).

**Deferred.** Item 10's live reading of the ledger and the account's activity (no paper
credentials in a cloud session, and the Gateway login does not complete from one): VERIFY-LIVE
below. Dividends and interest into the tax ledger, and opening lots for holdings bought before the
bot: follow-ups.

**Live findings.** No IBKR account or order call from this session. The skeleton's ISINs were read
through the owner's IBKR connector (contract search, read-only) [MCP ✓ 2026-10-21].

### Session 6 — Walking skeleton (2026-10-20, v0.11.0)

**Landed.**
- `obliskiq serve` (`orchestration/`): a heartbeat every 5 minutes that runs the executor's cycle
  (reprices, fills, stops, reconciliation), four decision cycles a trading day anchored to Tokyo's,
  Xetra's and New York's sessions through the calendars, and a daily digest; run records in
  001_core's `runs` and `run_steps` (lite's sequence and run log); recovery from the run log under a
  single-process lock (dead runs `interrupted`, intents resumed by cOID, recent missed cycles
  caught up, older ones recorded missed); SIGTERM; an injectable clock; the keepalive and the kill
  switch's sweep; IBKR's trading schedule as the calendars' cross-check, refreshed from one
  contract per venue; a downgrade to shadow after three failed cycles (D102).
- `capital/` (`whole_account | cap`, default cap 1,000 euros) and `decision/rebalance_basic.py` over
  `core.skeleton_targets`: three of variant A's roles in UCITS funds (IWDA on the LSE in USD, EMIM
  and IBGM on Amsterdam), weights A's scaled, conids read from IBKR's contract search and confirmed
  at every start; inner band, whole units, the minimum trade never below the commission floor, the
  cash floor (D104).
- The NAV writer (lite's `store/nav.py` and `collect/nav.py`; migration `007_service.sql`).
- `reporting/`: Telegram or the log, alerts once while their condition holds (a durable state
  file), the run summary, the daily digest with venue badges (D105). Health: the heartbeat file and
  `OBLISK_HEALTH_PING_URL`.
- CLI: `serve`, `run-cycle --mode shadow|paper [--dry-run]`, `status`, `intents list [--all]`,
  `holdings list|assign`; every command that opens a brokerage session takes the lock.
- `docs/RUNBOOK.md`; DECISIONS D102–D105.
- Changes the skeleton needed: a shadow limit order fills at the touch once its limit reaches it and
  is repriced on the real clock (D103: under D52 no skeleton ETF could fill in shadow); a capped book
  converts its shortfall alone; IBKR's warning text is kept with each reply (D101's open question);
  a deferred Gateway repair leaves the session lost until its deadline without repeated alerts
  (Session 3's follow-up); the exit reason `rebalance`.
- Closed follow-ups: Session 5's alert hook to Telegram, the suppressed-warning reset, the sweep
  loop and the downgrade in the service; alerts repeated once until their condition changes;
  `Pricer.initial` where intents are made; the trading schedule registered; the shadow fill that
  could not reach its limit; one process places a given intent (the lock).

**Tests.** The prompt's cases over the stateful fake broker on a simulated clock
(`tests/integration/test_service_e2e.py`): cycle 1 creates and fills intents in shadow and on fake
paper (the conversion first) and cycle 2 does nothing; a crash between place and fill orders
nothing twice; the kill switch stops cycle 3; a three-day run across the EU's daylight-saving change
fires every cycle at its venue's local time; Thanksgiving skips New York's cycles only; a session
lost at the daily reset is logged in again and a failed re-login reaches the owner within one
heartbeat (`tests/unit/test_service_keepalive.py`). Also the digest, stuck intents, blocked venues,
unconfirmed targets, the downgrade, catch-up and missed jobs, the lock, the health file and ping,
the rebalancer's every reason to hold back, alerts once per condition and the CLI, `run-cycle
--mode shadow --dry-run` on fixtures included. `make check` green: 3,840 tests, coverage 99.76 %.

**Live steps: a command list for the owner** (docs/RUNBOOK.md, "Session 6's live paper cycle"):
one supervised paper cycle with `capital.mode: cap` and a cap of 800 euros, dry run first, then the
cycle; fills, the conversion (expect `o10164` declined, its text now recorded), reconciliation and
the digest; Telegram once if its variables are set. Paste the masked outputs into a short session.

**Dependabot.** Only cryptography 50 is open (Almoon-D/ObliskIQ#5, Almoon-D/ObliskIQ#9), a major in
the follow-ups (D96); nothing to merge.

**Deferred.** The live paper cycle (no IBKR call from the cloud). Protective-intent renewal is
Session 17's (the skeleton carries no stops). Conversions in shadow (a foreign buy waits there).

**Live findings.** No IBKR order or account call from this session. The skeleton's conids and
listings were read through the owner's IBKR connector (contract search and four snapshots,
read-only) [MCP ✓ 2026-10-20]; VERIFY-LIVE rows added below.

### Session 5 execution test, first live run (owner, 2026-10-20, v0.10.1)

Run on the paper account over the Gateway (Ubuntu on WSL2: the Gateway's launcher is `bash`, so it
does not start on Windows), commit 1a5f680 then this version, NYSE window open, account masked
`DU*******`. Two attempts at 19:31 and 19:34 Madrid on v0.10.0; one at 20:00 on v0.10.1.

- **v0.10.0: stopped at "(a) conversion whatif".** HTTP 503 from `…/orders/whatif`, three
  backoffs, both attempts. The FCX identity step passed (conid 7089, the allowlist's corrected
  value). A read-only probe showed the 503 is the conversion flags, not a precondition (D101).
  `reconcile run`: incomplete once ("the live orders are not a complete snapshot", new risk
  frozen), green on the second run.
- **v0.10.1: the whatif passes** (SELL conid 12087792 at most 1.12126; amount 112.35 USD,
  commission 1.78 EUR, no warning). The gate cleared the conversion and it was sent. IBKR
  answered `o10164`; the policy confirms it only for cash-quantity orders that are not
  conversions, so it was declined, the order cancelled by the bot failed (`RequestRejectedError`)
  and the order stayed at IBKR: id 857299469, `Inactive`, total quantity 111.26, remaining 111.26,
  no fill, TIF `CLOSE`. No trade, no USD cash, no position. `reconcile run` green. Phases (b)
  and (c) (the FCX buy, the stop, the restart check) were not reached; `--finish` was not run.
- **Mode** set to paper for each run and back to shadow after it.
- **Minimum forex commission:** about 1.78 EUR on 100 EUR (1.8 %): size the test with it.
- **T1.** `--paper-order-test` was run on 2026-10-19 after Xetra closed (EXV1 refused
  `venue_not_tradable`, FCX conid corrected); the day-2 login check, the Java 21/25 test and a run
  with Xetra and the NYSE both open are not done.
- **Windows.** The Gateway cannot start there; one test module imports `fcntl`; 49 tests failed
  and 8 errored on Windows (not investigated). The repository is Linux-only in practice.
- **Venues open during the run (19:55 Madrid):** XNYS, BVMF, XMEX, XSGO, XEUR; every Asian venue
  was closed.

### Session 5 — Execution mechanics (2026-10-20, v0.10.0)

**Landed.**
- `execution/currency.py` and `execution/fx.py`: the currency manager (D98). Buys in a foreign
  currency are funded by one limit-capped conversion per currency and batch, sized by what they
  commit at their limits plus a buffer against the cash left after open commitments, raised to a
  minimum where the base cash and the cap allow; the trades wait for its fill; a failed
  conversion leaves them unplaced and alerts; foreign cash beyond the buffer comes home. Rates
  from IBKR's ledger, else the pair's quote.
- The gate (`execution/pretrade.py`): `conversion_unavailable` is gone. A conversion is cleared on
  its own path: a configured pair (`conversion_pair`), the kill switch and the freeze, the pair's
  quote and collar, the cash it converts from against every commitment and the floor, lite's
  single-foreign-currency cap on what it buys (`currency_cap`, `exposure_unreadable`). New risk
  is refused while the latest reconciliation is not green (`reconciliation_frozen`). A reducing
  stop-limit is checked against its own intent's protective spec (replacing
  `max_limit_offset_fraction`); a modify of a triggered stop is held to the sell collar or the
  owner's emergency limit. `check(simulated=True)` checks a shadow intent as if it were sent.
- `execution/pricing.py`: an IBKR algo with a limit cap where enabled per venue and asset class
  and offered (Adaptive, off), else a patient limit a tick inside the mid, repriced toward the
  touch; the cost model gets the listing venue (`cost_venue`). Every order routes SMART and the
  European commission rows price SmartRouting; `spot_fx` prices a conversion (D97).
- `execution/protective.py`: stop-limits resting GTC (D99), placed after fills and resized with
  them, verified each heartbeat (missing or short → re-placed and alerted, kill switch on or
  off), cut to the position, raised by roll-ups, repriced through a gap toward the bid in steps;
  the limit offset from volatility and gaps with a floor and a ceiling.
- `execution/executor.py`: one cycle syncs, settles exits (I5), expires, follows fills (holdings
  registered, stops placed), reprices or gives up inside the window, retries at the next window,
  stages, funds, places (exits through the coordinator), verifies stops, reprices gaps,
  repatriates and reconciles; `resume` looks every intent up by its cOID after a restart (D100).
- `execution/reconcile.py`: orders, trades, positions and cash per currency against the store and
  the last green settled run; drift and partial data freeze new risk; a grace period for IBKR's
  lag; `obliskiq reconcile run|accept --reason`.
- `obliskiq preflight --execution-test [--finish]` (`execution/live_test.py`): Session 5's live
  steps for the owner (below).
- Store: migration `006_execution.sql` (the intent's protective spec; append-only
  `reconciliations` with their anchoring reads). Config: `execution.currency`, `pricing`,
  `protective`, `reconcile`, `positions.max_single_foreign_currency_pct` (lite),
  `ibkr.preflight.execution_test`; FX joins the real-time-preferred quote policy.
- T1's findings: FCX is conid 7089 on the NYSE (the allowlist and the paper order test now say
  so), EXV1's conid is confirmed, and the read-only preflight passed on the machine
  (docs/knowledge/ibkr.md, "T1").

**Tests.** The prompt's cases: a conversion that fails before the trade
(`test_execution_currency.py`, `test_execution_executor.py`), a projected negative foreign
balance, batching across two intents, repatriation above the buffer, an algo not offered
(`test_execution_pricing.py`), reprices then a cancel at the window's end with the exit's stop
restored, a partial fill resizing the stop, a partial exit beside the stop, a missing stop
re-placed with the kill switch on, a stop re-placed while flatten's exit works unlisted, a crash
after place, a restart with staged and submitted intents, drift freezing, partial data never
green, the kill switch during a reprice, a gap repriced through the gate never to a market order,
the grace period, the offset widening with volatility and gaps (`test_execution_protective.py`,
`test_execution_reconcile.py`). No module spells an uncapped order type
(`test_order_path_guards.py`). Every new module has its failure branches tested (the boundary
refusing, reads failing, the mode unreadable, the rules or the quote missing). `make check`
green; 3,731 tests, coverage 99.87 %.

**Live steps: a command list for the owner** (SESSION_PROMPTS §A.5), on the machine, with no bot
running, inside the NYSE's order window (16:00 to 21:45 Madrid time; 15:00 to 20:45 in the week
of 26–30 October):

1. `uv run obliskiq mode set paper --reason "Session 5 execution test"`
2. `uv run obliskiq preflight --execution-test`: converts EUR 100 into dollars (whatif first),
   buys one FCX share at the touch, places and lists its GTC stop-limit, measures IBKR's lag.
3. The process has exited. Ideally the next day, after IBKR's reset, inside the window:
   `uv run obliskiq preflight --execution-test --finish`: the stop still listed, the share sold
   at the touch, the stop cancelled, nothing left working.
4. `uv run obliskiq reconcile run`
5. `uv run obliskiq mode set shadow --reason "Session 5 execution test done"`
6. T1's open steps: day 2's read-only preflight, the Java side test, and `--paper-order-test`
   inside both venues' windows (16:00 to 17:15 Madrid time).

Paste the masked reports into a short session: *"Record Session 5's execution test and T1 per
SESSION_PROMPTS §A.5."* It records the shapes in docs/knowledge/ibkr-orders.md, sets
`reconcile.grace_seconds` from the measured lag and closes the rows below.

**Dependabot.** Only cryptography 50 is open (Almoon-D/ObliskIQ#5, Almoon-D/ObliskIQ#9), a major
already in the follow-ups (D96); nothing to merge.

**Deferred.** Attached orders (D99): not adopted, not probed; a later session probes a whatif of
the package before designing its clearance. The research simulator does not price conversions.

**Live findings.** No IBKR call from this session. The owner's T1 report: the login and the
read-only preflight pass on the machine; the paper order test placed nothing (EXV1 after Xetra's
close, FCX's conid). VERIFY-LIVE items added below.

### Dependency currency, amended (owner request, 2026-10-19, v0.9.4)

The proposal's author reviewed D96 and asked for four amendments; all are adopted, two with
changes (D96).

- **T1's failure path is fixed before the first login** (SESSION_PROMPTS §A.5, "If a step fails").
  T1 changes machine, address, OS and Playwright at once; with no working baseline that is
  accepted. A password accepted without a session is followed first by one manual Gateway login
  on the machine, which tells the automation from the environment.
- **Merging.** Green patch and minor Dependabot updates are merged at a session's start. A major
  one needs its changelog read and the paths CI fakes checked. Playwright of any size, the
  Gateway archive and Java also need a read-only preflight on the machine. cryptography 50 was
  caught by `filterwarnings = "error"`, by design.
- **The calendars.** No instrument trades on XSHG, XBOM or XSES; reading closed past 2026 is
  accepted, and the dated review is replaced by Session 8's coverage precondition.
- **The side test** reads a copy of `config/` through `OBLISK_CONFIG_DIR` (`config/owner.yaml`
  is tracked), and after a locked username or a refused password no side run happens until T1
  has passed on Java 11.

**Tests.** `make check` green; documents only.

**SESSION_PROMPTS edits.** §A.5: the side test's configuration and stop rule, and "If a step
fails".

### Dependency currency (owner request, 2026-10-19, v0.9.3)

A proposal to run the newest stable version of everything, triggered by the Java 11 pin. Adopted
with changes (D96): the newest stable release by default, every pin a registered exception with a
reason, a review date and an exit, and green Dependabot pull requests merged at each session's
start.

- **The pile-up was unmerged updates.** Four Dependabot pull requests were open, the oldest from
  2026-10-12. Almoon-D/ObliskIQ#14 (Playwright 1.64, numpy, pydantic, ruff 0.17 and others) and
  Almoon-D/ObliskIQ#7 (setup-uv 10.3.0) passed `make check` on a merge with `main` and are merged.
  Almoon-D/ObliskIQ#9 and #5 (cryptography 50) are red: 50 deprecates the finite-field
  Diffie-Hellman whose parameters `oauth.py` loads, a follow-up.
- **Java.** Session 3's readings do not separate Java 11 from 21: no version opened a session, and
  the one Java 11 run that reached "Client login succeeds" still got `401`. The Gateway is built
  for Java 8 and its guide says it was tested on OpenJDK 11; IBeam runs it on 17. Java 11 is
  built by Temurin to at least October 2027, so the list stays `[11]` and T1 tries 21 and 25.
  No root cause is known: the login fails in the cloud on every version.
- **The OS.** Ubuntu 26.04 LTS on x86_64: Playwright supports it from 1.61, 22.04's support ends
  in May 2027, and 24.04 is the fallback.
- **Python 3.15.** Every locked package installs on 3.15.0 (PyYAML from its source). The suite
  passes there except the three tests that pin 3.14. It moves after T1 (follow-up, by
  2026-11-30).
- **T1** is not held for any pin.

**Tests.** `make check` green, coverage 99.85 %; no code path changed (a docstring and a
configuration comment).

**SESSION_PROMPTS edits.** §A.5: the OS, the JDKs, `playwright install --with-deps chromium`,
the Java side test, and the record session setting the Java list.

### Session 4e — Total-risk scaling (2026-10-18, v0.8.6 → v0.9.2)

**The data checks** (Step 1, its own pull request before the harness; DECISIONS D93). Every
series the study and its long-history check read was checked on 2026-10-18 for metadata, span,
duplicates, impossible values, continuity and alignment with MSCI's calculation days
(`research/explore/risk_scaling_data/`, `docs/knowledge/research-sources.md`). No return or
statistic was computed.

- The main sample keeps Session 4c's proxies (A is the benchmark), read on a daily grid of MSCI's
  calculation days. It runs from 2004-12-31, the first month end on which every proxy of A has a
  price, to 2026-09-30, with daily data from 2000-12-29 for the estimators.
- The bases hold only WORLD, EM, the two euro bond roles and cash. No euro inflation-linked proxy
  or periphery curve is needed.
- Long histories: MSCI in EUR, daily from 2000; Ken French's developed markets, daily from
  1990-07 in USD (EM mapped to them); AQR's *Century of Factor Premia*, monthly from 1926-07,
  excess returns over local cash plus the US bill.
- New loaders: Ken French's daily tables and AQR's text dates (`kf_developed_daily`,
  `aqr_century_market`).
- One stale value: CRSOX repeats its net asset value on five sessions in May 2014.

**The harness** (Step 2, its own pull request before the pre-registration, v0.9.0; D94):

- `quant/covariance.py`: an EWMA, a window and their blend; Ledoit–Wolf shrinkage toward the
  declared matrix; a prior in equivalent observations. Tested against closed forms and an
  independent implementation.
- The `total_risk` strategy in the core's own mechanism: a risky share of a target volatility
  over the forecast one, capped at one, with a floor and a largest change. Its declared twin.
  Comparisons beside the tests against the reference.
- Daily marks inside resampled months (D88) and the drawdown on them: the owner's point within its
  horizon, a grid of horizons, episodes per decade and the share of time beyond.
- D92 rule 3 at validation, and a block-length sensitivity.
- The forecast check (QLIKE, never an outcome) and the long-history check (before tax, by month or
  day).
- A development window for exploration. A candidate outside the owner's point cannot be
  sufficient.
- The toy study runs every part end to end in about four seconds.

**The pre-registration** `research/preregistrations/core-risk-scaling.md` merged on its own
(69c8cc8) and is locked there (`core-risk-scaling.lock`). It sets static A against three total-risk
specifications and two declared twins:

- E-fast and E-blend: WORLD 90 % and EM 10 % at a target of 8.91 %, with the fast estimator (a
  21-day EWMA) or the blend (half that EWMA, half a 261-day window).
- B-fast: WORLD 60 % and EGB_INT 40 % at 9.15 %, with the fast estimator.
- E-declared and B-declared: the same bases at their declared shares (58.8 % and 100 %).

Its targets are sized so that a design fall equals the owner's 30 %. Six tests: each candidate
against A (theta 1 % a year) and against its twin (0.5 %), with Bonferroni over all six. Its
power before data: 0.040 and 0.045 against A (detectable 5.05 % and 4.61 % a year), and 0.085 and
0.239 against the twins (1.59 % and 0.96 %).

**Exploration** on the development window (2001-01 to 2004-11), three trials, never evidence
(`research/reports/core-risk-scaling-exploration-20261018T141123Z.md`). Each comparison was
inconclusive, from −0.67 % to −0.08 % a year. The run showed three wordings of the development
run's report to fix (robustness and the long history "not run there", a path shorter than the
horizon); no computation and no constant changed.

**The confirmation run** (once, inputs SHA-256 `581a488c…`, 24 minutes on four workers) wrote
`research/reports/core-risk-scaling.md` (D95). Every adequacy check passed: coverage 100 %, 261
months. Each interval is at level 0.9833 (Bonferroni over six tests).

| Test | Estimate / yr | Interval | Outcome | Power at theta |
|---|---|---|---|---|
| E-fast against A (theta 1 %) | 1.56 % | [−2.07 %, 4.58 %] | inconclusive (sign uncertain, outside the owner's point, low power) | 0.044 |
| E-blend against A | 1.34 % | [−2.54 %, 4.82 %] | inconclusive (sign uncertain, outside the owner's point, low power) | 0.039 |
| B-fast against A | 1.33 % | [−1.79 %, 4.13 %] | inconclusive (sign uncertain, outside the owner's point, low power) | 0.055 |
| E-fast against E-declared (theta 0.5 %) | 0.06 % | [−1.06 %, 0.90 %] | inconclusive (sign uncertain, outside the owner's point, low power) | 0.124 |
| E-blend against E-declared | −0.16 % | [−1.42 %, 0.94 %] | inconclusive (sign uncertain, outside the owner's point, low power) | 0.087 |
| B-fast against B-declared | −0.51 % | [−1.23 %, 0.21 %] | insufficient (outside the owner's point, low power) | 0.247 |

- **The owner's point.** Shares of 20-year paths reaching 30 % on daily closes:

  | Portfolio | Reaches 30 % |
  |---|---|
  | A | 0.0 % |
  | E-fast | 20.9 % |
  | E-blend | 22.0 % |
  | B-fast | 16.2 % |
  | E-declared | 46.8 % |
  | B-declared | 40.1 % |

  Scaling halved the breach of each base held at its declared share, but no candidate came near
  5 %. On the one history the twins fell 33.12 % and 31.05 %, the candidates 24.55 % to 29.25 %
  and A 14.73 %.
- **What estimation adds.** Nothing measurable in growth: E-fast +0.06 % and E-blend −0.16 % a year
  against their twin. For B-fast an effect of 0.5 % is ruled out. Each candidate turned over
  64–116 % a year against 18 % for its twin. Its growth over A came from the base's equity, not
  from scaling.
- **Robustness.**
  - Against A, the sign holds in both halves for every candidate. The break-even cost multipliers
    are 3.05–3.27, below the declared cost uncertainty of 3.67.
  - With 12- and 36-month blocks the candidates' breaches are about as above. With 60-month
    blocks (paths nearer the one history) E-fast's is 4.9 % and B-fast's 3.7 %. The registered
    blocks are 24 months.
- **The long histories** (before tax, descriptive). Scaling lowered the breach on the daily
  histories (MSCI in EUR: E-fast 15.6 % against its twin's 40.7 %; Ken French: 32.5 % against
  45.8 %). It did not on AQR's monthly century (32.2 % against 20.9 %), where the estimators react
  a month late.
- **The forecast check** (never an outcome). Every estimator forecast its base's next-month
  variance better than the declared constant (QLIKE −0.19 to −0.15, intervals below zero).
- **Selection.** The registry counts nine trials (three exploratory). No test's deflated Sharpe
  stands out from the best of nine (probabilities 0.000 to 0.246).
- **What follows** (D95):
  - No specification is sufficient, and no inconclusive one respects the point, so nothing goes to
    Session 13 or to shadow.
  - E-fast and E-blend are `tested` and B-fast `retired` in `research/pattern_registry.yaml`.
  - The frontier at the owner's point still resolves to A, so no new point is offered for the
    owner's choice (D60).
- **Reproduce.** `obliskiq research data load ecb_fx ff3_market kf_developed_daily
  aqr_century_market msci_world_eur msci_em_eur ecb_eonia ecb_estr bbk_bund_04y bbk_bund_05y
  bbk_bund_16y bbk_bund_17y euro_cash bund_zero_05y bund_zero_17y tip ief gld crsox`, then
  `obliskiq research run research/preregistrations/core-risk-scaling.md` on a fresh research
  store. The run is single-shot, so the store must not already hold this id's confirmation.

**SESSION_PROMPTS edits.** Session 13: 4e found no specification sufficient (D95), so the core
engine does not scale total risk.

**The verdict's scope** (v0.9.2, after an outside review). D95 now also states what the test does
not show:

- adaptation in general: only total-risk scaling of fixed bases was tested;
- that estimation cannot add growth: only half a point a year is ruled out, for B-fast;
- a breach probability free of its resampling model;
- the whole managed book.

Two owner follow-ups record what no session yet covers: D89's next adaptation test, and checking
the whole book against the owner's point.

**Tests.** `make check` green, coverage 99.85 %; the development run's report wording is tested.

**Live findings.** No IBKR call. The sources behaved as Step 1 recorded.

### Session 4e decided on the objective (owner request, 2026-10-18, v0.8.5)

A challenge of D89–D91 at the owner's request; the owner accepted its four rules (D92).

- **The binding limit is growth.** At the owner's point A reaches 30 % on 0.1 % of paths against
  the 5 % allowed, with a median after-tax log growth of 3.22 % a year.
- **The gate.** An adaptation is sufficient on D60's objective against static A at the owner's
  point. A forecast's accuracy is a check that the estimator works: a forecast that follows
  recent volatility beats a constant one almost by construction.
- **The mechanism.** Session 4e scales total risk (a risky share of target volatility over
  forecast volatility, capped at one, the rest in cash) on bases with more equity than A, each
  paired with its declared twin; no trend tilt. Re-estimated risk-balance weights come later.
- **The bootstrap.** 4c's 12-month blocks cut the tilt's 12-month window: on a path about 54 % of
  it comes from an unrelated block ((L + 1) / (2b), checked by simulation). Later studies use
  blocks of at least twice the longest lookback and report a sensitivity over block lengths.
- **The long history.** The breach at the point is also measured on the longest history each
  candidate allows (MSCI from 2000, Ken French from 1990, AQR from 1926). A's breach rests on
  2005–2026 alone.
- 4c's result and D91 stand: Session 13 builds A by default.

Documents only. No IBKR call.

**SESSION_PROMPTS edits.** §C (4e's row), Session 4e (goal, reading, steps, the study's id
`core-risk-scaling`), Session 13 (the 4e report's path, `quant/covariance.py` scaling total risk).

### The owner's point (owner request, 2026-10-18, v0.8.4)

The owner accepted the proposal for the probability: 30 % reached on at most 5 % of 20-year
paths (D91).

- On Session 4c's frontier the point resolves to A, the static risk balance.
- A reaches 30 % on 0.1 % of paths, with a median after-tax log growth of 3.22 % a year.
- Session 13's precondition is met.
- Session 4e reports this point beside its grid.

Documents only. No IBKR call.

**SESSION_PROMPTS edits.** Session 4e's precondition, Session 13's precondition.

### Owner decisions and the global data audit (owner request, 2026-10-17, v0.8.3)

A challenge of the static core (D80) at the owner's request, the owner's decisions it led to,
and a desk audit of the global data. Documents and one exploration script; no code or
configuration changed. No IBKR call.

**The owner's decisions** (D88, D89):
- **Drawdown tolerance: 30 %** from the previous peak of the value the bot manages; unmanaged
  holdings do not count. The horizon is very long (the owner does not need the money now).
  - It is a research and selection limit, not a guaranteed maximum loss and not an order to sell.
    No breaker threshold is set from it.
  - The peak is the managed NAV at the day's last cycle, raised once a day; the drawdown is
    measured from it at every cycle.
  - The probability of breaching it is open, and so is how it is stated: over a number of years,
    or as a rate that needs none.
- **No exposure caps by asset class.** They become optional configuration keys, absent by
  default; the mandate is unchanged.
- **The option sleeve** keeps its premium budget of at most 5 % and lite's penalty balance.
- **Adaptation is a requirement.** The allocation adapts within the owner's limits through
  rules a pre-registered test finds sufficient, one mechanism at a time. The static variants
  are the fallback and the benchmark. Risk estimation comes first, in a new Session 4e.

**D80 narrowed** (D90): no multi-year estimate before 2005 holds for the proxies 4c chose, not
in general. An estimator shrunk toward the declared matrix needs no pre-history. 4c's result
stands.

**The audit** (`research/reports/global-data-audit.md`, availability probed on 2026-10-17, no
return or statistic computed). US data are used by convenience in three places:
- 4c's IL role (TIP);
- 4c's structure check (US market only, where AQR's *Century of Factor Premia* covers
  several markets from 1926);
- H6's measures (VIX, the excess bond premium).

Gold, commodities and Treasuries are US vendors with global or intended exposure, not bias.

- Main gap: euro inflation-linked bonds. A Yahoo series exists only from 2008 (`IBCI`).
- Verified free and global:
  - MSCI regional indices from 2000;
  - the ECB all-issuer curve from 2004 and convergence yields for Italy and Spain from 1991;
  - the ECB CISS from 1980;
  - OECD leading indicators for 22 areas;
  - JGB yields from 1974;
  - AQR multi-asset data from 1926 and 1972;
  - Ken French regions from 1989–1990.
- Traps: FRED's ICE BofA spreads hold three years only; `stoxx.com` failed TLS verification
  from the container.

**The tolerance's implications** (`research/reports/drawdown-tolerance-implications.md`):
- 4c's frontier at 30 % resolves to A at 5–10 % and to the 60/40 at 25–50 %.
- The equity reference respects 30 % only at a probability of at least 72.3 % (paths of about
  20.75 years).
- An illustration from assumptions alone (`research/explore/drawdown_horizon/horizon.py`) shows
  the probability rising with the horizon.

**SESSION_PROMPTS edits.**
- The opening paragraph and the network note (4e).
- §C (4e's row).
- Session 4e (new).
- Session 13 (precondition, goal, reading, `quant/covariance.py` from 4e).
- Session 17 (optional exposure caps; breaker thresholds apart from the tolerance).
- Session 21 (H6 with euro-area measures).

### Documentation review (owner request, 2026-10-17, v0.8.2)

The documents that steer later sessions, checked against each other and the code after
Session 4d, starting from five points an earlier agent raised; documents only, no code or
configuration changed.

**The five points.**
- The 4c record (the first run not evaluable, the fix, the repeat; nine candidates
  inconclusive; the owner's point needed before Session 13 only): correct as written.
- T1: D68, §A.5 and MASTER_PLAN §10 set the owner's order (before Session 6); Session 6's
  precondition says what the session does if T1 has not passed. A plan and its contingency,
  not a contradiction: left as is, with the sessions table's T1 status brought up to date.
- The core's static risk balance on a declared covariance (D80, D82): MASTER_PLAN §5.4 and
  Session 13's prompt already bind Session 13 to it, and a re-estimated variant needs a new
  pre-registration. No change.
- Costs and tax still open (currency conversions, routing, currency lots): already assigned
  (Session 5's Follow-ups; currency lots in Session 7's prompt, D69), and
  `research/reports/core-allocation.md` states that conversions are free and which
  commissions are unverified. Direct routing is the higher of IBKR's two fixed rates
  (D77). No change.
- Stale premises: AGENTS.md's opening line (the core and OAuth) and the README (the core, the
  status) corrected.

**Also corrected.**
- The subagents the owner removed on 2026-10-16: SESSION_PROMPTS §B step 4 and §C.1, and D22
  marked withdrawn.
- DECISIONS entries a later one superseded now say so, as D8 does for D42: D5 (D60, D61), D9
  (D61), D12 (D65); D61 cites D30, not D31, for the pricing of both legs.
- MASTER_PLAN §5.4: the two references are on the frontier and can be the owner's pick
  (D60, D79). §5.5 item 8: no selection hypothesis is sufficient yet (D84, D87). §10 row 3:
  both transports.
- SESSION_PROMPTS: the opening count of sessions; Session 6's skeleton roles (the equity
  reference has one fund, so the skeleton completes 3–4 roles from §5.4); Session 10's H2
  parser and Session 15's shadow hypotheses after Session 4d (D84, D87); the feature-session
  template's path to weight (D62, D65, D66).
- `docs/knowledge/README.md` lists the research sources 4c and 4d added.

**SESSION_PROMPTS edits.** The opening paragraph, §B step 4, §C.1 (removed), Session 6 item 3,
Session 10 item 4, Session 15's goal, §F.1 item 2.

### Session 4d — First selection hypothesis (2026-10-17, v0.7.3 → v0.8.1)

**The data audit** (Step 1, its own pull request before the pre-registration):
`research/reports/selection-data-audit.md`, DECISIONS D84. Measured on 2026-10-17, with the scripts
in `research/explore/selection_audit/` (exploration, D64):

- **H2** (insider-purchase clusters): the SEC's insider data sets are point in time from 2006Q1
  (17,257 clusters of three insiders in 6,262 issuers), but free prices miss 74.5 % of the 2006
  events and 11.6 % of the 2025 ones. Not adequate.
- **H4** (the stock-level composite): the same prices miss 76.5 % of the sampled 2006 issuers and
  12.5 % in 2025; point-in-time fundamentals start in 2009. Not adequate.
- **H1** (country momentum through UCITS): MSCI serves 51 of the 53 countries in MSCI ACWI since
  2001 (not Venezuela or Russia), at most 4.1 % of the members on a date. Adequate, and chosen.
- What would make H2 and H4 evaluable: a survivorship-free US price history with delisting
  returns (CRSP; Norgate US Platinum at US$630 a year), the owner's decision.

**The harness** (its own pull request, before the pre-registration, v0.8.0):
- The cross-section study kind (DECISIONS D85): members over stated spans, a member the vendor
  does not serve named without a series and counted as lost on every date; the universe and the
  momentum top group with a keep buffer and a lagged placebo, trading only what changes and
  reset once a year; the Fama–MacBeth series of each test's monthly excess log return after
  costs and tax, its block-bootstrap interval with Newey–West beside it, the sign in each
  subperiod, the break-even cost multiplier and the placebo; the development and confirmation
  samples. `obliskiq research power`, `run` and `data coverage` read either kind.
- A trailing volatility stop in the simulator, watched on daily closes, a gap borne in full (D86).
- Datasets for the 51 MSCI country indices MSCI serves; the cross-section template.
- At the study's size (51 members, daily rows, 225 month ends) one run takes about ten seconds on
  synthetic random walks (a timing check, no real data read).

**The pre-registration** `research/preregistrations/selection-country-momentum.md` merged on its
own (89ea75e) and is locked there (`selection-country-momentum.lock`). Its power before data: 0.38
for H1 at theta 2 % a year (detectable 3.37 %), 0.34 for H5 at 1 % (detectable 1.80 %).

**Exploration** on the development sample (2001-12 to 2007-12, two trials, never evidence,
`research/reports/selection-country-momentum-exploration-20261017T174052Z.md`): H1 +4.19 % a year,
inconclusive; H5 −4.15 %, insufficient. It showed no defect of the harness, and nothing was changed.

**The confirmation run** (once, inputs SHA-256 `968bf6af…`) wrote
`research/reports/selection-country-momentum.md` (D87). Every adequacy check passed:

- at most 2.3 % of the members on a date were not served (Russia, until March 2022);
- coverage was 100 %;
- the sample had 224 months.

Each interval is at level 0.95 (Bonferroni over two tests).

| Test | Estimate / yr | Interval | Outcome | Power at theta |
|---|---|---|---|---|
| H1 country momentum (theta 2 %) | −1.04 % | [−3.76 %, 1.39 %] | insufficient (low power) | 0.329 |
| H5 volatility-scaled stop (theta 1 %) | −5.46 % | [−7.92 %, −3.92 %] | insufficient (negative, low power) | 0.162 |

- **What it shows.** H1's top third of countries by 12-1 momentum grew 1.04 % a year less after
  costs and tax than holding every member equally (0.98 % less before tax). An effect of 2 % a
  year is ruled out, and the effect is below theta even without costs. The top group turned over
  216 % a year and cost about EUR 48,000 against EUR 10,000 for the universe.
- **H5.** The stop fired 588 times. It cost 5.46 % a year against the same group without it,
  below zero in both subperiods.
- **Robustness.**
  - H1's sign differs between 2008–2016 (−3.09 %) and 2017–2026 (+0.82 %).
  - The lagged placebo shows nothing (−0.12 %, [−2.76 %, 1.87 %]).
  - No break-even cost multiplier exists for either test.
- **Selection.** The registry counts four trials (two exploratory); the deflated Sharpe of each
  test is far below what the best of four would show by chance (probability 0.000).
- **What follows** (D87): both are `retired` in the new `research/pattern_registry.yaml`, where H2
  and H4 are `idea` entries with the data they need. No batch-1 selection hypothesis earns
  capital or a shadow run.
- **Reproduce.** `obliskiq research data load msci_<country>_eur ...` (the 51 country datasets),
  then `obliskiq research run research/preregistrations/selection-country-momentum.md` on a
  fresh research store. The run is single-shot, so the store must not already hold this id's
  confirmation.

**SESSION_PROMPTS edits.** Session 8: no universe comes first for Session 4d's hypothesis, since
none was left in shadow (D87).

**Tests.** 3,474 passing, coverage 99.84 %. `cross_section.py`, `cross_section_report.py` and the
stop are at 100 %. A test holds `research/pattern_registry.yaml` to its lifecycle until Session
15's loader.

**Live findings.** No IBKR call. SEC, Yahoo, MSCI and Tiingo formats and behaviour recorded in
`docs/knowledge/research-sources.md`: Yahoo throttled silently after about 1,700 rapid symbols,
and reuses a delisted company's symbol for another; MSCI's endpoint does not serve Russia or
Venezuela.

### Session 4c — Core verdict (2026-10-17, v0.7.0 → v0.7.2)

**Landed (the harness, its own pull request before the pre-registration).**
- Several candidates against one reference, one test each with Bonferroni across them; one trial
  per candidate; every portfolio before and after tax; the frontier and its resolution to the
  simplest portfolio the bootstrap cannot tell apart from the best; the aspiration as a
  distance; power before data per candidate (DECISIONS D79).
- The core's variants as strategies: static weights by role (stated, risk budgets by environment
  on a declared covariance, or an equity overlay), wide bands, the trend tilt, tax-aware
  execution with swap partners; `quant/risk_budget.py` and `quant/trend.py` for Session 13 (D80,
  D82).
- MSCI, ECB Data Portal and Bundesbank loaders, an overnight-rate accrual and a constant-maturity
  zero as derived series, expense ratios deducted from index proxies (D78); a pre-tax structure
  check of the tilt on the Ken French market from 1926 (D79).
- The simulator at bootstrap scale: indexed series, kept prices, closes and trend states, worker
  processes that leave the result unchanged; the lot engine's copy built field by field (D81).
- Fixed: a rounding excess of replacement units after a part sale of a lot that carries a
  deferred loss (D81). Found in self-review, before any run: Decimal residues that could sum a
  tax-held book's weights above 1 (the run would have been not evaluable) or sell a kept
  position by one unit, now a weight quantum and a unit residue; a reduction's tax priced on its
  sales together (`LotEngine.trial_tax`); a structure subperiod annualised over the months it
  holds.
- Tests: 3,400 passing; coverage 99.83 %; every module in `research/`, `quant/risk_budget.py`
  and `quant/trend.py` at 100 %.

**The pre-registration** `research/preregistrations/core-allocation.md` merged on its own
(7537c6c) and is locked there (`core-allocation.lock`).

**The first confirmation run was not evaluable (D83).** Every adequacy check of the data passed:
every series loaded and as observed, all 250 rebalance dates priced, 249 months. The historical
runs completed. A bootstrap path then stopped the run: a sale of one unit of the commodity
vehicle would have cost more than it raised (the USD 6 minimum commission on a resampled price
below it), and the simulator treated that as an error. No estimate, interval, path statistic or
frontier was computed. The descriptive structure check ran. The fix (v0.7.1): such a sale is not
made, its units count as nothing at the horizon, and reports count these sales. It changes no
pre-registered constant and is merged before the repeat.

**The confirmation run** (repeated after the fix, on the same inputs: inputs SHA-256 `c5207063…`)
wrote `research/reports/core-allocation.md`. Every adequacy check passed. Each candidate is one
test against the equity reference: annualised after-tax log growth of liquidation value,
2005-12 to 2026-09. Each interval is at level 0.9889 (Bonferroni over nine).

| Candidate | Estimate / yr | Interval | Outcome | Power at θ = 1 % |
|---|---|---|---|---|
| 60/40 | −2.18 % | [−5.16 %, 1.42 %] | inconclusive (sign uncertain, low power) | 0.038 |
| A | −4.06 % | [−9.79 %, 2.90 %] | inconclusive (sign uncertain, low power) | 0.016 |
| B | −4.88 % | [−10.76 %, 2.42 %] | inconclusive (sign uncertain, low power) | 0.016 |
| C | −4.39 % | [−9.44 %, 1.75 %] | inconclusive (sign uncertain, low power) | 0.019 |
| D10 | −4.93 % | [−10.58 %, 1.40 %] | inconclusive (sign uncertain, low power) | 0.017 |
| D20 | −3.47 % | [−7.99 %, 1.44 %] | inconclusive (sign uncertain, low power) | 0.022 |
| D30 | −2.00 % | [−5.74 %, 1.52 %] | inconclusive (sign uncertain, low power) | 0.035 |
| D40 | −0.55 % | [−4.23 %, 1.73 %] | inconclusive (sign uncertain, low power) | 0.060 |
| D50 | +0.74 % | [−3.72 %, 2.06 %] | inconclusive (sign uncertain, low power) | 0.068 |

- **What it shows.** No candidate is shown to out-grow buying and holding global equities after
  tax, and none is shown to grow less. That is the outcome the pre-registered power predicted, and
  it is not evidence against any of them.
- **The frontier is the owner's material.** Median after-tax growth against the share of paths
  that reach each drawdown limit:
  - The equity reference reaches a 50 % drawdown on 13.4 % of paths and a 40 % drawdown on
    37.3 %.
  - The 60/40 reaches 30 % on 10.7 % of paths.
  - A reaches 20 % on 4.9 % of paths.
  - D10 reaches 10 % on 0.9 % of paths.

  The resolution rule picks, as the drawdown tolerance tightens: the equity reference (40 % at
  50 %, or 50 % at 25 % or more), then the 60/40 (30–50 % limits at stricter probabilities),
  then A (20 %, and 30 % at 5–10 %), then D10 (10 %). The bootstrap cannot tell any of these
  apart from the best median at its point.
- **Robustness.**
  - The sign holds in both halves for every candidate except D40 (first half positive) and D50
    (second half negative).
  - No break-even cost multiplier exists: every estimate is below θ even without costs.
  - The tilted variants B and C turn over about 100 % a year at the illiquid tier's costs,
    about €23,000 over the run, against about 18 % for A.
- **A reading, not a test.** On the historical path D50 matches the reference year by year from
  2016 and pays no tax before the horizon. That is consistent with the tax-aware rule holding
  every trend reduction once embedded gains made its tax exceed 5 % of the value sold. A fresh
  start in 2016 has no gains, so its tilt trades, which is why D50's second half differs.
- **The structure check** (descriptive, US market before tax, 1927–2026): the tilt grew 9.47 % a
  year against 9.72 % held, an interval of [−1.88 %, 1.54 %]. Its maximum drawdown was −49 %
  against −84 %.
- **Selection.** The registry counts 18 trials, the first run's nine not-evaluable ones
  included; the deflated Sharpe uses all 18, which is harsher.
- **The owner's choice of a point** is recorded later, in DECISIONS (D60), not this session.

**SESSION_PROMPTS edits.** Session 13: `quant/risk_budget.py` and `quant/trend.py` exist; no
covariance estimator unless the chosen variant re-estimates its weights (D82).

### Session 4b — Research harness (2026-10-16 → 2026-10-17, v0.6.0)

**Landed.**
- Tax (`tax/`, `jurisdiction/`, DECISIONS D74): `TaxPolicy` beside `EligibilityPolicy`;
  `es_irpf` and the `generic` template, with every value and its source under `tax:` in
  `config/jurisdictions/es.yaml`; the lot engine (FIFO, the two-month rule matched unit by unit
  with deferral and release, the hooks `tax_cost`, `wash_sale_conflict`, `harvestable_losses`),
  the ledger (offsets, a four-year carry-forward, the savings scale with its tax years, the
  foreign-tax credit, payment by 30 June) and `tax_lots`. `docs/knowledge/tax-es.md` cites each
  rule's article and the BOE text read on 2026-10-16. `test_no_tax_constants` keeps tax values
  out of code.
- Research data (`research/data.py`, `research/xlsx.py`, `store/research.py`, D72, D73): the
  research store in its own file, series source-tagged and point in time (as observed, vintages
  or a declared lag), loaders for yfinance, Ken French, AQR, FRED, ALFRED and the ECB on fixtures
  recorded on 2026-10-16, base-currency conversion, coverage and survivorship.
- The harness (`research/simulate.py`, `stats.py`, `decide.py`, `strategies.py`, `prereg.py`,
  `registry.py`, `study.py`, `report.py`, D75, D76): the simulator with delta execution, costs on
  the listing venue and tax as it arises; the statistics of D62–D63 against closed forms; the
  decision rule with a property test that every interval lands in exactly one outcome; the trial
  registry, the lock verified against git, single-shot confirmation runs and budgeted
  exploration; the report.
- `obliskiq research data load|coverage`, `research power`, `research run [--explore]`,
  `research lock`; `research/preregistrations/TEMPLATE.md`, `research/explore/README.md`.
- European commissions on Xetra, the LSE, Euronext Amsterdam and Paris, Borsa Italiana and SIX
  (D77): a UCITS ETF order is priced end to end and filled in shadow.

**Acceptance.** A toy study (two ETFs on Xetra, rebalanced against held) runs end to end through
`obliskiq research run` on a pre-registration merged and locked in a git repository, and its
liquidation values (11,916.9865 and 11,930.9185), its taxes and its primary effect match the
hand calculation written out in `tests/unit/test_research_study.py`. A rotating portfolio pays
2024's tax in July 2025 that buy-and-hold defers to the horizon. Perturbing every observation
published after the horizon changes no output.
- Tests: 3,275 passing. Coverage measured 99.83 %; floor raised from 99.4 % to 99.5 %. Every
  module in `research/`, `tax/` and `jurisdiction/` at 100 % (`yfinance_history`, the one call
  that reaches Yahoo, is marked network code).

**SESSION_PROMPTS edits.** Session 4c: the harness runs one candidate against one reference per
pre-registration; 4c extends it to several candidates in a pull request of its own, writes the
lock with `obliskiq research lock`, and the confirmation report is `research/reports/<id>.md`.
Session 7: the open lots are saved whole; the engine's loss windows and ledger need persistence
beside them.

**Deferred.** No real-data study (4c, 4d). The engine's loss windows and ledger are not
persisted (Session 7). Currency conversions are free in the simulator and no currency balance is
held (Session 5's currency manager, D69's currency lots in Session 7).

**Live findings.** No IBKR call. Vendor formats recorded in `docs/knowledge/research-sources.md`:
a plain client got HTTP 429 from Yahoo, yfinance got through; ALFRED's answer does not echo the
API key. IBKR's European fixed schedule read on its published page.

### Design review (owner request, 2026-10-16, v0.5.1)

A red-team of the plan against the code and the legacy evidence, challenged twice by two other
reviewers; process and documents only, no runtime code changes. DECISIONS D60–D71.

**Decided.**
- The objective (D60): the annualised after-tax log growth of liquidation value within the
  owner's drawdown limit, tax simulated as it arises and the rest paid at liquidation; an
  accumulating global equity ETF bought and held and a global 60/40 scored under the same rule,
  as references and a possible outcome, not the destination. Tax is a cost, not a principle.
  Research is separate from authorisation: 4c reports a frontier and the owner's drawdown
  tolerance picks the variant before Session 13; the aspiration (30–50 % a year or more) is
  reported as a distance, never a gate.
- Evidence first (D61): Sessions 4b (research harness), 4c (core verdict) and 4d (one selection
  hypothesis) before Session 5; T1 on the deployment machine in parallel; Session 14 absorbed
  into 4c; Session 13 builds 4c's variant; Session 19a builds what pays at larger capital
  (owner: implemented before 1.0.0); Session 21 is the second research batch. No engine deleted,
  no capital budget moved before evidence.
- The decision rule (D62): not evaluable; sufficient (L ≥ θ); insufficient (U < θ, negative
  when U < 0); inconclusive (L < θ ≤ U, positive when L > 0) — exhaustive and mutually
  exclusive; power reported beside it.
- The protocol (D63): pre-registered combinations with a mechanism, tested beside their
  components and counted; dependence explicit; minimum samples in effective observations; a
  break-even cost multiplier against a declared cost uncertainty.
- Research code (D64): the harness under every rule of `src/`; exploration outside the coverage
  floor and never evidence.
- Capital follows evidence; live results are monitored through a twin with alpha-spending
  boundaries, and a breach reduces risk rather than declaring a verdict (D65).
- Activation conditions instead of NAV thresholds; an absolute position floor (D66).
- Gap-aware protective exits and sizing, attached orders probed, reconciliation grace; no
  market-order escalation (D67).
- T1 before Session 6; the TWS API with IBC as the fallback if it fails (D68).
- Gaps fixed by later sessions: European commissions (4b), currency-gain tax (7), hedged share
  classes and look-through (8, 9, 17), lite's caps restated per sleeve (17) (D69).
- LLM-derived evidence is forward only, so a minimal EventExtractor starts H7's clock in
  Session 10 (D70); the first research batches (D71).
- Session 19a is kept at the owner's request against two reviewers' proposal to build each
  capability when it first activates: built last, behind its conditions, in a fixed order
  (D66).

**Withdrawn from the review's first rounds.** Cutting components on the strength of the legacy
record (absence of evidence, not evidence against); "a weekly 188-name panel calibrates
quickly" (it counted predictions; n_eff decides); a fixed "costs doubled" check and a
six-of-seven-days pass mark for T1 (no stated uncertainty or failure rate behind them); VIX as
a gate for three-to-six-month and single-name options; tax minimisation as a principle.

**SESSION_PROMPTS edits.** §A.5 (T1) added; §A.3, §B, §C and §E follow the new sequence;
Sessions 4b, 4c, 4d, 19a added; Sessions 5–13 and 15–21 edited as listed in D61–D71; Session 14
removed.

**Owner, before Session 13.** A point on Session 4c's frontier: the drawdown tolerance, the
probability of breaching it and the horizon (MASTER_PLAN §11). No research session waits for
it.

### Session 4a — Pre-trade rails (2026-10-16, v0.5.0)

**Landed.**
- `execution/pretrade.py`: the pre-trade gate, the only issuer of a clearance, working from the
  stored intent in three stages (local; broker reads; one store transaction with the clearance)
  that each report every failure with a reason code (D54). Checks: the intent and the write asked
  for, conversions (refused until Session 5), the paper guard, the mode, the kill switch, the
  instrument, prohibited products, buy-side eligibility with the mandatory close, approval, the
  calendars' act-now view (`tradability` with the order's conid; an option also inside
  `common_windows` and date-critical inside its mandatory-close window), the positions, live
  orders and trades, the quote policy and the collar (a protective stop: its spec), the holdings
  registry, no short (I2), the cash in the order's currency against every open commitment and the
  floor, no borrowing by the whatif's cost, and the caps per order, per 24 hours and in count. A
  clearance carries its notional and expires with the earliest quote it rests on.
- I2 (`execution/exposure.py`, D55), I3 (the waivers, D56) and I5 (`execution/exits.py`, D58): an
  exit's other checks pass before its protective stop is cut; the stop is restored when the exit
  is refused, cancelled or expires; a failed restore alerts and is retried at every `settle`.
- `obliskiq flatten --confirm` (`execution/flatten.py`), `obliskiq approvals list|approve|reject`
  (`execution/approvals.py`, migration `003_pretrade.sql`), `obliskiq preflight --paper-order-test`
  (`execution/paper_test.py`), the order path a command opens (`execution/runtime.py`).
- `jurisdiction/eligibility.py`: `EligibilityPolicy` and the skeleton policy, selected by
  `config/jurisdictions/es.yaml`; policy factories take the configuration (D57).
- Config: lite's `instruments` keys and `tenor.mandatory_close_trading_days`
  (`config/defaults/instruments.yaml`), the skeleton allowlist (`config/defaults/eligibility.yaml`),
  `execution.pretrade`, `execution.approval`, `execution.flatten`, `ibkr.preflight.paper_order`.
- The ledger's rate to the base currency per currency; the whatif's currency; an import contract
  keeping `jurisdiction` off the broker, the store and execution.
- The fake broker serves the gate's reads; the order rig carries the gate over test instruments
  (stocks, a UCITS ETF, a US ETF, options on a stock and on the ETF, a leveraged ETF, a future and
  an option on it).

**Reason codes and their tests.** Every code of `execution.pretrade.Reason` has
`test_reason_<code>` in `tests/unit/test_pretrade_reasons.py`, each beside its positive control,
and `test_every_reason_code_has_its_test` holds the module to that: `intent_unknown`,
`intent_state`, `intent_expired`, `terms_mismatch`, `modify_changes_order`, `order_not_intents`,
`conversion_unavailable`, `paper_guard`, `mode_unreadable`, `mode_forbids_write`, `intent_mode`,
`profile_mismatch`, `kill_switch_on`, `instrument_unknown`, `prohibited_product`, `ineligible`,
`mandatory_close`, `approval_missing`, `venue_not_tradable`, `option_expired`,
`account_unreadable`, `quote_policy`, `instrument_halted`, `collar`, `protective_spec`,
`holding_unmanaged`, `holding_unassigned`, `holding_other_sleeve`, `short`, `written_option`,
`fills_unreconciled`, `currency_not_held`, `fx_rate_unavailable`, `whatif_refused`,
`margin_use`, `insufficient_cash`, `cash_unaccounted`, `nav_unavailable`, `order_notional_cap`,
`daily_notional_cap`, `daily_order_cap`. The prompt's cases: stale and delayed quotes, options
without a real-time quote on the option and on the underlying, the DST window edge, an incident, a
calendar disagreement near an option's last session but not for a stock, and the option-close
alert in `test_pretrade_markets.py`; I2 in `test_pretrade_no_short.py` (a sell above the position,
an exit beside a resting stop, listed late, two processes at once, a modify counted once, a stale
position after a fill, a conversion not refused as a short); I3 with the kill switch on in
`test_pretrade_effects.py`; I5 in `test_exits.py`; flatten, the paper order test, approvals, the
commitments the gate adds up, the CLI and the runtime in their own modules.

**SESSION_PROMPTS edits.** Session 5: the gate refuses every conversion until Session 5 replaces
`conversion_unavailable`; partial exits go through `ExitCoordinator`, whose `settle` the executor
calls each cycle. Session 6: the approval CLI is Session 4a's `obliskiq approvals`. Session 8:
`ConfiguredDirectory` goes with the skeleton allowlist. Session 12: `tenor.mandatory_close_trading_days`
already lives in `config/defaults/instruments.yaml`.

**Deferred.** Funding by conversion (Session 5): until then an order in a currency the account
does not hold is refused. Nothing runs `ExitCoordinator.settle` on a schedule yet (Session 5's
executor, Session 6's service).

**Live findings.** None: no IBKR call was made (the paper Gateway login does not complete from a
cloud container). The paper order test is queued below for the deployment machine.

### Session 4 — Order path, kill switch and modes (2026-10-13 → 2026-10-15, v0.4.0)

**Landed.**
- `domain/orders.py`: `OrderIntent` (uuid7 id, sleeve, thesis, role, terms, reason, mode,
  approval, expiry, parent and protective links, state), `OrderTerms` whose every price policy
  carries a limit (limit, stop-limit, algo with a limit cap) and whose digest binds a clearance,
  `PretradeClearance`, `QuoteEvidence`, `IntentEvent`, `BrokerOrder`, `Fill`, `Position`,
  `CashBalance`, `HoldingAssignment`, `ModeChange`, `KillSwitchRecord`.
- Migration `002_orders.sql`: intents, intent events (append-only), clearances, broker orders,
  fills, account reads with position and cash snapshots, the mode audit and the kill switch
  (append-only), and the holdings registry (append-only). `store/orders.py` reads the system's
  own state whole (D46); `store/worker.py` runs the store on one thread for async callers.
- `ibkr/orders.py` and `ibkr/order_decode.py`: whatif, `place_idempotent` (paper guard, binding,
  cOID lookup in a complete live-orders snapshot and the trades, last check, clearance spent,
  one send), the warning loop, reply, modify, cancel, status, live orders, trades, reset of
  suppressed warnings, conversion fields (`isCcyConv`, `fxQty`), the cOID short form (D45, D47).
- `execution/`: the state machine and intent book with property tests; lite's discipline
  (`intents.py`); the quote policy per asset class and the collar (`quotes.py`); the reply policy
  (`replies.py`); modes (`modes.py`); the kill switch with its sweep (`killswitch.py`); the
  pre-trade gate refusing every intent (`pretrade.py`); the clearance ledger (`ledger.py`); the
  write boundary (`boundary.py`); shadow fills with the cost model (`shadow.py`).
- `obliskiq kill on|off|status` (`kill on` works without a loadable configuration) and
  `obliskiq mode show|set`.
- The stateful fake broker (`tests/support/fake_broker.py`) and the order rig, with the tests the
  prompt lists: unknown reply; `o354` under each condition; whatif warning; rejection; cancel
  race; crash between place and status; duplicate intent and cOID; every I1 refusal; I4 at the
  boundary and in the sweep (protective-role buy and conversion cancelled, exit sell and stop
  kept, gate refusing, store unavailable, listed late); a downgrade mid-cycle; a cancel after a
  downgrade to shadow; a live account under the paper profile. Guard tests: only the gate
  constructs or records a clearance; only the order client spells the write endpoints; no order
  type but `LMT` and `STOP_LIMIT` is reachable (a property over generated terms).
- `docs/knowledge/ibkr-orders.md`: endpoints, fields and message ids, tagged.
- Review round 1 hardening: the kill switch's file flag is checked with `lstat` (an uncheckable
  entry is ON), its sweep reads live orders row by row and its loop survives any failure; a declined
  placement whose cleanup is unverified stays `submitted`; any 5xx is unknown; order ids are
  digits only; the boundary refuses an expired intent, an intent of another mode and a modify
  of another intent's order; a cancel needs the paper guard alone; an entry buys; triggers keep
  a clearance spent once and an intent's identity fixed; a structural test pins D46's state
  reads; the fake broker invalidates an open warning on any other request.
- `test-breaker` against I1, I3, I4, the reply policy, modes and the state machine: 141 cases in
  `tests/unit/test_breaker_*.py` and a property file. Of its eight defects, the four it shared
  with the review were already fixed; the other four are fixed too: a warning is confirmed only
  after the kill switch and mode are re-checked; any adds-risk clearance in a sell's history
  cancels it in the sweep; a sell is sized in units and a modify keeps the kind of sizing; a
  delayed quote needs a sigma even at zero delay. Its observations added a collar ceiling
  (`execution.collar.max_fraction`), the live levels refused at the boundary itself, and a
  cancel that tolerates a store that is down.
- Review round 2: the sweep cancels what the broker alone shows adds risk before it reads the
  store, and after one failed store read classes the remaining sells from the broker; a dangling
  symlink flag reads ON; an intent's expiry is checked again before the send and before each
  warning is confirmed; a declined placement's cleanup counts as unverified unless shown.
- Session 4 follow-ups closed: the order path's paced endpoints in the rate groups; the
  transport readied outside the pacing slot and a re-arm that stops at a dead transport (D53);
  `run_read_only` tested through its own `build_transport` (OAuth and Gateway); the
  magic-number guard's allowlist for money decisions (D50).

**Reply message ids and their tests** (`execution.reply_policy`, D49):
`o10153` any order, `o10331` stop-limit only, `o10164` and `o10223` cash quantity only
(`tests/unit/test_execution_replies.py`); `o354` under the quote policy and the collar
(`test_no_market_data_*` there, and end to end in `test_ibkr_orders.py` and
`test_execution_boundary.py`); every other id, and a warning without one, declined
(`test_an_id_outside_the_policy_is_declined`, `test_a_warning_without_an_id_is_declined`,
`test_an_unknown_warning_is_declined_cancelled_and_alerted`).

**SESSION_PROMPTS edits.** Session 4a's READ names the files of Session 4's code it reads.

**Deferred.** I2 (no short) and I5 (no unprotected exit) are enforced by Session 4a's gate; the
`clearances` table carries conid, side and quantity for its reservations. Nothing calls
`OrderClient.reset_suppressed()` or runs the kill switch's sweep loop yet (Session 6's service).

**Live findings.** None: no IBKR call was made (the paper Gateway login does not complete from a
cloud container). Every order shape is VERIFY-LIVE below.

### Reading budget and the Session 4 split (2026-10-12, v0.3.2)

**Owner request.** Sessions spend too many tokens reading; cut the cost without weakening the
build. Owner decision: split Session 4 into 4 and 4a (D44).

**Measured.** A profile of every transcript event of Sessions 2 and 3 (D43): the context grew
mostly from front-loaded legacy reading, whole-file `MANIFEST` reads by subagents, polling,
plan-mode echoes and a follow-up run in a long session; test output and diffs were small.

**Landed.** AGENTS.md "Read first" by section and a new "Context budget"; the paper Gateway
login is not attempted from a cloud container; the reviewer reads its own diff; the
environment's GitHub tools stand in for `gh`. `make prompt S=<id>` with tests, including one
that every pinned legacy range covers whole sections. `.claude/agents/`: `explorer` cites
ranges and never stands in for money-path source; `port-checker` and `reviewer` read by grep
and by section. MASTER_PLAN §10 and the sessions table show Sessions 4 and 4a.

**SESSION_PROMPTS edits.** §B steps 3, 5 and 7 (`make prompt`, auto mode, a fresh session for
follow-ups, 4a listed); §C table rows 4 and 4a and the cloud-login note; Session 4 replaced by
Session 4 (order path, kill switch and modes, behind a fail-closed pre-trade clearance) and
Session 4a (pre-trade rails, approval queue, `flatten`, paper order test); the clearance lock
stated once in Session 4's item 4 as invariants I1–I5 with their tests (the gate, no short
across working and cleared sells, class by effect with exact waivers, a kill switch that works
when everything else fails, no unprotected exit) and referred to by 4a, Session 5 and D44; the
`exit` role; Session 5's precondition, READ, items 1–3 (conversions, reprices and protective
stops under the lock), TESTS and ACCEPTANCE (VERIFY-LIVE); the references in Sessions 5, 7 and 8
now name 4a; Session 8 inspects `universe.yaml` by script; pinned legacy ranges in the READ
blocks of Sessions 5, 6, 8, 10, 11, 12, 17 and 19.

**Follow-ups retargeted.** Cash and quote reads → Sessions 4 and 4a; `tradability` with the
conid and the instant view → 4a; the commission-schedule live check → 4a.

### Session 3 follow-up — paper over the Gateway, OAuth live-only (2026-10-12, v0.3.1)

**Owner request.** Run paper through the Gateway with the automated headless login and the
paper username and password from the environment; never look for or require OAuth `.pem` files
for paper. Owner decision: OAuth keys are the live account's (D42).

**Landed.** `transport_refusal` (OAuth refuses any profile but live, the Gateway profile live)
before any credential is read, in `build_transport`, the preflight and the CLI; `Environment` no
longer looks for the key files at load; tests for each (a paper run with OAuth variables pointing
at nothing never opens them). AGENTS.md, MASTER_PLAN §5.12 and §5.18, `.env.example`, D42.

**Live findings.** The container's outbound address changes from one connection to the next
(four addresses in ten fresh connections; one per kept-alive connection). One read-only run of
`obliskiq preflight --read-only` over the paper Gateway: password accepted (SRP `M2`), then
"Authentication failed"; no further login, to stay clear of a lock-out. The likely cause is the
changing outbound address (not observed directly); the next attempt is on the deployment machine
(`docs/knowledge/ibkr.md`). The Session 3 VERIFY-LIVE rows stay open.

**SESSION_PROMPTS edits (facts changed by this work).** §A.2 intro (Gateway for paper, OAuth for
live), §A.2 step 3 (no paper OAuth keys), §A.3 (the paper-OAuth variables and setup script
removed; the cloud Gateway outcome stated).

### Session 3 — IBKR read path (2026-10-12, v0.3.0)

**Landed.**
- `ibkr/transport/`: the `Transport` protocol; OAuth 1.0a ported from lite (renegotiation inside
  the refresh margin and on recovery); the Client Portal Gateway — pinned archive (size and
  SHA-256), a per-install `localhost` certificate pinned by the client (hostname checks on) and
  by the browser (SPKI), a Java-version gate, process control, a Playwright paper login, re-login
  on session loss, profile `live` refused (DECISIONS D33, D34).
- `ibkr/session.py` (re-arm, re-authenticate, 403 never retried, backoff with `Retry-After`,
  tickle keepalive, competing and lost sessions alerted), `ibkr/ratelimit.py` (D35),
  `ibkr/errors.py` (D36), `ibkr/guard.py`, `ibkr/redact.py`, `ibkr/client.py` with
  `decode.py`/`models.py`/`history.py` (D37, D38, D40), `ibkr/identity.py`,
  `ibkr/schedule_source.py` (D39), `ibkr/preflight.py` and `obliskiq preflight --read-only`.
- Configuration: `ibkr.yaml`, `http.yaml`, `paths.ibkr_gateway`; `OBLISK_IBKR_GATEWAY_JAVA_HOME`.
- `docs/knowledge/ibkr.md`: lite's verified findings condensed with their route tags, IBKR's
  documented pacing, changelog, availability codes, field ids, schedule, rules, per-user market
  data and live→paper sharing, and this session's Gateway trial.
- Tests: 2,279 passing. Coverage measured 99.58 %; floor raised from 98.9 % to 99.4 %. The
  concurrency test (200 requests against virtual time, every window checked) found a
  float-rounding hole in the sliding window — expiry and wait disagreed, so a full per-minute
  window admitted a burst at one instant — fixed with a regression and a property test.

**Deferred.**
- The store watermark of the history walk and the per-target retry of lite's backfill: Session 8
  (collectors). Lite's `test_a_permanent_no_data_error_is_not_retried_per_target_either` and the
  route `SOURCE` tag go with them.
- Lite's `test_hostile_input.py` cases on server tools (exits, entries, external positions, IV
  percentile records): Sessions 7 and 15, with those tools. The hostile-input cases of the read
  path are in `tests/unit/test_ibkr_hostile_input.py`.
- Lite's universe sync (`test_collect_conid_supersede.py`): the supersession rule is ported in
  `ibkr/identity.py`; the sync that calls it is Session 8.

**Live findings** (`docs/knowledge/ibkr.md`, "Session 3"). The Gateway was tried in this cloud
container: it installs from the pinned archive, runs on Java 21 and 11, serves TLS with the pinned
per-install certificate, and the headless form login gets IBKR to accept the paper password (SRP
proof returned) once keys are typed with a pause — but the Gateway session was never
established (`/sso/Dispatcher` re-rendered the login in nine of ten attempts; the tenth reached
"Client login succeeds" and then `sso/validate` answered 401 "Access Denied"). IBKR documents that
it does not support automating this login. The read-only preflight therefore stopped at its
transport step; no snapshot, history, rule, algo, schedule or scanner reading was taken through
ObliskIQ, and the "owner logged in live elsewhere" comparison could not be run. Every one is
queued below.

**VERIFY-LIVE.** See the queue: the Gateway login on the deployment machine and everything the
preflight reads after it.

**Contradictions with the prompt, decided and recorded.** The prompt's `ibkr/errors.py` sits beside
the shared `obliskiq/errors.py`; IBKR's error classes subclass `UpstreamError` there. The
"third ScheduleSource" is the first source built on Session 2's protocol beside the library and
synthetic schedules; registering it with the calendars is Session 6's (follow-up below). Lite's
`accept_first_on_no_exact_match: true` would contradict "ambiguous resolution refused" and is
`false` (D37). The live step (prompt item 10) could not pass its first sub-step from a cloud
container.

**Review.** Round 1 (fresh `reviewer`): one blocking finding — re-authentication and the
keepalive stopped silently on failures that were not `IbkrError` (an unreachable OAuth token
endpoint, a browser that would not launch) — and fourteen non-blocking. All were fixed in the PR
(D41): any failure now counts, alerts and leaves the session marked lost; a waiting request holds
no concurrency permit (a tickle no longer queues behind scanner requests); incomplete position and
cash reads raise; the endpoint groups open only after the account guard; concurrent 401s share one
repair; OAuth retries are signed afresh; Gateway re-logins are spaced; secrets stay out of child
processes and command lines; schema bounds for IBKR's ceilings, paced endpoints and paper
prefixes; `daily_between` honours `latest`; 6509 codes and spelled flags each decode by their own
rule; the `L` price prefix is read as lite read it. Round 2 (fresh `reviewer` on the delta): no
blocking finding; eleven non-blocking, fixed in the PR (signing failures not retried twice and
counted by the breaker; OAuth `Retry-After`; alert hooks run outside the repair context; an
environment allowlist for the Gateway's child processes; a deferred re-login stops the attempts
and says when; non-object ledger rows named unreadable; tests for each) or recorded (the order
path's paced endpoints, Session 4; the daily bar stamp per venue, VERIFY-LIVE). Round 3 (fresh
`reviewer` on that delta, the last round): no blocking finding; eight non-blocking, six fixed in
the PR (a token endpoint busy to the end is an upstream fault counted by the breaker; the
deferral message; `OBLISK_*` never allowed in the child environment, `JDK_JAVA_OPTIONS` added;
the login browser gets the allowlisted environment; the VERIFY-LIVE code marker; docs) and two
recorded as follow-ups below (token negotiation inside the limiter slot; retrying a deferred
repair from the keepalive).

**Process.** The harness assigned the branch `claude/magical-bardeen-ct4oql`; GitHub steps of the
end-of-session protocol ran through the GitHub tools instead of `gh`. Test ports were delegated to
subagents (OAuth, client and session, field units). `test-breaker` and `port-checker` stopped on
a usage limit before reporting; the adversarial tests (pacing under concurrency, hostile input,
guard) were then written in the session.

### Session 2 — Store, calendars and numerics (2026-10-09, v0.2.0)

**Landed.**
- `domain/`: Instrument, Bar, Snapshot, ChainRow, CurvePoint, MacroObservation, Venue, Phase,
  Incident, SessionObservation and the enums; frozen, `extra="forbid"`, no NaN or infinity,
  UTC-only datetimes; `domain` is a leaf of the import graph.
- `store/`: WAL connection with foreign keys fixed in code, atomic forward-only checksummed
  migrations (`001_core.sql`), mappers, and repositories whose every read — COUNT(*) included —
  takes a required keyword `as_of` (DECISIONS D25–D27).
- `calendars/`: intervals, library-backed / synthetic / uncovered schedules, phases, incidents
  and the `Calendars` facade with the planning and act-now views (D28); the 27 venues, the 23
  IBKR codes and the side sections in `config/defaults/venues.yaml` (D29).
- `quant/`: `bsm`, `iv_solve` (the pure solver parts), `smile`, `stats`, `inference`, `costs`
  with `numerics.yaml`, `smile.yaml`, `liquidity.yaml`, `costs.yaml` (D30, D31).
- Guards: hardcoded trading times, keyed mappings read by an attribute chain, structural
  point-in-time reads, import contracts for the new packages (D32); the Session 1 exact-leaf
  tests became "every leaf has a source".
- Docs: DECISIONS D24–D32, CHANGELOG 0.2.0, `docs/knowledge/{calendars,costs}.md`.
- Tests: 1,709 passing. Coverage measured 99.91 %; floor raised from 98.6 % to 98.9 %.

**Deferred.**
- Holiday lists for XSHE, XNSE, CHIA and XSPX: the library lacks them, so those venues are
  `blocked(calendar_not_covered)` until a list is sourced (Session 8).
- Realised-volatility estimators (lite `collect/vol.py`): a collector concern, Session 8.
- Config with no consumer yet, left out so the unread-key guard stays honest: `solve.enabled`,
  `smile.skew_delta`, `smile.term_structure.*`, `liquidity.spread_waiver_min_open_interest`
  and lite's `data.*` (Sessions 11 and 12 extend `smile.yaml` and `liquidity.yaml`).
- Vendor-percentile columns of lite's snapshots and a macro vintage column: added by migration
  with their first live route (Sessions 8, 10, 11).
- Lite reads and tests with no counterpart yet, to land with their consumers: dividend-yield,
  NAV, FX-ratio and CPI year-on-year reads, `BarSeries.trailing_high` and `tail`, the
  `contract_id_for_symbol` lookup, the universe/grid reads (Session 8); lite's ECB-release,
  `BaseCurrencyRates`, real-rate and `recent_daily_closes` point-in-time cases (Sessions 10, 11);
  the thesis-freeze, `external_positions` and `option_ledger` store tests (Session 7 and
  Session 15). The 019-step migration walks and the 2000-row runner cases are replaced by
  fresh-chain tests with small tables.
- Costs: only repository-sourced commission schedules are filled; ES, FR and IT transaction
  taxes are unconfirmed against official texts, the IT rate is `null` (`tax_unknown`).

**Live findings.** None: no IBKR or data-source call was made. The library facts in
`docs/knowledge/calendars.md` are `exchange_calendars` 4.13.2 readings, not exchange readings.
Five tax rates were looked up on the web (GB, HK official; ES, FR secondary; IT not
confirmed): `docs/knowledge/costs.md`.

**VERIFY-LIVE.** See the queue below. The config rows carry `verify_live: true`, shown by
`obliskiq settings show`.

**Contradictions with the prompt, decided and recorded.** 27 venues, not 26 (XEUR exists in the
library since 4.13.2); numpy only, no scipy (MASTER_PLAN §7 and D17 named it); lite has no
`snapshots`, `runs` or `run_steps`: they are its `underlying_snapshots`, `collector_sequences`
and `collector_runs`, renamed to the prompt's names; `tradable_window` is `tradable_windows`
(it returns segments); `market_data_status` is the decoded enum with the raw flag beside it;
bars are daily only, keyed by the listing venue's session date.

**SESSION_PROMPTS edits** (facts changed by this session's work, no scope added): Session 3
deliverable 4 (the `ScheduleSource` is synchronous and cache-backed); Session 4 pre-trade
check (`Calendars.tradability` and `common_windows`, not `venue_status`); Session 8 READ
(`calendars` keys were ported here); Session 10 deliverable 6 (`accept_incidents`); Session 12
READ and deliverable 2 (`smile.yaml` and `liquidity.yaml` exist; `last_tradable_session` and
`session_before`).

**Review.** Port checks (calendars, store, quant and costs) and two adversarial test rounds
(the `Calendars` facade, the cost model) found and fixed: naive instants, unmultiplied option
legs, malformed tax inputs, an unknown tax priced as a number, incident paths that could
unblock a date-critical order, and manual-file reload and feed acceptance erasing each other.
Round 1 of the fresh review found three blocking items, fixed in the following push: an
instrument re-upsert could erase a supersession, the market-data flag decoder read negated flags
as real-time, and a tax with no notional was priced at zero. Its non-blocking findings are fixed
or recorded in Follow-ups.

**Process.** The harness assigned the branch `claude/pensive-fermi-ybb3ff`; GitHub steps of
the end-of-session protocol ran through the GitHub tools instead of `gh`.

### Session 1 — Foundation (2026-10-08 → 2026-10-09, v0.1.0)

**Landed.**
- `docs/MASTER_PLAN.md`, `docs/SESSION_PROMPTS.md` and `AGENTS.md` verbatim from the
  attachments (byte-identical); `CLAUDE.md` = `@AGENTS.md`.
- `legacy/`: lite @ `b1ebd490b439ce22e03de0ca25085a81d4ccd840` (314 files), old @
  `68280e13ad8d935600a593976fb8cce1bda53bfe` (81 files: the §6.2 paths, the listed tests and
  fixtures, LICENSE), pulse @ `900dbf404320917f41beac5ac56865c238aebbbe` (31 files). All three
  commits are the ones MASTER_PLAN names. `legacy/MANIFEST.md` has one row per file and lists
  the imports `legacy/old/` makes that §6.2 leaves out; agent instructions renamed
  (`CLAUDE.legacy.md`, `_claude/`); excluded from ruff, mypy, pytest, coverage,
  import-linter and packaging, and guarded by `tests/unit/test_legacy_snapshot.py`.
- Scaffold: Python 3.14 (uv-managed 3.14.6), uv 0.12.23, `uv.lock`, Makefile, ruff,
  `mypy --strict`, pytest with asyncio auto, pre-commit, import contracts (each contract was
  shown to break on a probe import), the 20 packages of §8 with ownership docstrings,
  `.claude/agents/` (reviewer, test-breaker, port-checker, explorer).
- Ported from lite with their tests: `errors.py`, `logging_setup.py`, `util/{numeric,
  timeutil,http}.py`; `env.py` rebuilt on pydantic-settings (DECISIONS D18, D20).
- Configuration: `config/defaults/{logging,paths}.yaml`, `config/owner.yaml`,
  `config/jurisdictions/es.yaml`; the loader with provenance and plain-language errors; the
  jurisdiction policy registry; the append-only settings audit log; `obliskiq config
  validate` and `obliskiq settings show`.
- Guard tests: unread config keys, committed secrets and personal data, magic numbers in
  decision paths, packaging (real wheel and sdist builds), `.env.example` completeness,
  the CHANGELOG section, workflows, legacy snapshot, package layout; a config-load smoke test.
- CI (`ci`), the release workflow, Dependabot (uv, github-actions), the PR template.
- Tests: 350 passing. Coverage measured 99.6%; floor set to 98.6%.
- Review: two rounds by a fresh reviewer, no blocking finding in either. The non-blocking
  findings were fixed in this PR (the audit log hardened against non-finite values, short
  writes and concurrent readers and writers; read-only, picklable profile policies; wider
  magic-number scope; `llm` ⟂ `ibkr`; `--env-file`; clearer messages; DECISIONS evidence),
  except those listed under Follow-ups.

**Deferred.** Nothing in scope. Error classes, the `http` config keys and every other
section of lite's `config.yaml` arrive with their consumers (DECISIONS D2, D20).

**Live findings.** None: this session made no IBKR or data-source call.

**VERIFY-LIVE.** None added.

**Repository settings vs SESSION_PROMPTS §A.1.** The session's `gh` was not authenticated
and the GitHub tools it had do not read repository settings, so only part could be checked:
private ✓, default branch `main` ✓, AGPL-3.0 ✓. Squash merging was disabled: GitHub refused
the protocol's squash merge (405) on 2026-10-09 and the owner enabled it. Not readable, for
the owner to confirm: merge commits and rebase merging disabled; the squash message "Pull
request title and description"; automatic deletion of merged branches; Dependabot alerts and
security updates; Actions workflow permissions read-only; the optional ruleset requiring `ci`.

**Process.** The harness assigned the branch `claude/funny-keller-biu3b6` instead of
`s01-foundation`; GitHub steps of the end-of-session protocol ran through the GitHub tools
instead of `gh`. The new `.claude/agents/` definitions load from the next session; this
session's review ran the `reviewer` instructions through a general-purpose subagent.

## Live verification queue

Checks built against fixtures that still need a live reading. Each names the session that
settles it.

| Item | Added | Settled by |
|---|---|---|
| IBKR exchange codes → venues (38 codes in `ibkr_exchanges`; Sessions 8 and 9 pinned 57 universe conids with their listing exchanges through the owner's IBKR connector [MCP ✓ 2026-10-22]; the ten codes Session 8 added and Session 9's `IBIS2` are unverified on CP) | 2 | the collectors' live round |
| Venue phase windows: pre-market, auctions and post-market per venue (pulse values, unmeasured) | 2 | T1 or the first command list after it |
| SGX mid-day break hours (the XSES lunch override; the library skips the break) | 2 | T1 or the first command list after it |
| B3 regular close follows New York daylight saving time (the BVMF seasonal close) | 2 | T1 or the first command list after it |
| XEUR session hours versus single-stock option hours (options use `common_windows`) | 2 | T1 or the first command list after it |
| IBKR's trading-schedule format as a `ScheduleSource` (regular hours only are compared) | 2 | T1 or the first command list after it |
| Order window 30 min after the open and 15 before the close | 2 | 18 (TCA) |
| Holiday lists for XSHE, XNSE, CHIA, XSPX (blocked until sourced; XNSE holds data only) | 2 | 19a |
| Precomputed calendars end 2026-12-31 for XSHG, XBOM, XSES (`Calendars.coverage()`); the universe holds data only there (510300 on XSHG), collected in New York's job past that date (D96, D116) | 2 | 19a (a tradable candidate there) |
| IBKR market-data flag spellings: `FROZEN_DELAYED` → `frozen`, an unlisted or negated flag → NULL with the raw flag kept (CP's 6509 letter codes are decoded by `ibkr/decode.py`; MCP spellings unchanged) | 2 | T1 or the first command list after it |
| US stock, US option and Eurex option commission schedules | 2 | T1 (the paper order test), 18 (TCA) |
| US Treasury commission and the EUR-bond schedule above EUR 8,500 of face | 2 | 19a (bond rule) |
| Transaction taxes: FR's rate against CGI art. 235 ter ZD (Légifrance refused the container, 2026-10-22); IT's regulated-market rate and paying side in Legge 228/2012 art. 1 c. 491 and 494 (Legge 199/2025's 0.4 % standard rate read on Normattiva, 2026-10-22); GB, HK against a trade confirmation (ES's 0.2 % read in Ley 5/2020's BOE text, 2026-10-21). The core's ETFs and ETCs pay none of them | 2 | 15 (the first French or Italian share a thesis orders) |
| Friction tiers (spread, slippage, impact, option half-spread) are legacy assumptions | 2 | 18 (TCA) |
| Snapshot availability and delay: a US stock (Cboe One/IEX), a European UCITS ETF, a US option (OPRA), a Eurex option (Eurex Core), each with the owner logged in live elsewhere and not | 3 | T1 or the first command list after it |
| One market-scanner run (`/iserver/scanner/params`, `/run`) | 3 | T1 or the first command list after it |
| Snapshot field ids from IBKR's table: 7633 strike IV, 7638 open interest, 7087 historical vol, 7310 theta, 7311 vega | 3 | T1 or the first command list after it |
| `/iserver/secdef/search` without `secType` still returns sections | 3 | T1 or the first command list after it |
| `/contract/trading-schedule` shape, holidays omitted, and agreement with the calendars (closes the Session 2 phase-window, SGX, B3 and XEUR rows) | 3 | T1 or the first command list after it |
| `/iserver/auth/ssodh/init` with `publish`/`compete` as query parameters through the Gateway (lite used them over OAuth) | 3 | T1 or the first command list after it |
| `ssoExpires` and the daily reset as seen by the keepalive, re-logged in unattended (D105) | 3 | the first week of `serve` on the machine |
| The daily bar's `t` stamp per venue (the history window is filtered on the UTC date of the bar start; a venue stamping before UTC midnight would shift by a day) | 3 | 8 |
| Fixture shapes in `tests/fixtures/ibkr/` (from IBKR's reference and lite's readings, none captured through ObliskIQ) | 3 | T1 or the first command list after it |
| Order shapes: place, reply, modify, cancel, live orders (the `snapshot` flag on a session's first call), status, trades, whatif (docs/knowledge/ibkr-orders.md) | 4 | T1 (the paper order test) |
| The cOID: maximum length, `order_ref` in live orders and trades, the duplicate-cOID refusal and its window | 4 | T1 (the paper order test) |
| Warning ids on a stop-limit placement and modify (`o10331`; the reference's modify example shows `o0`) | 4 | the execution test (Session 5's command list) |
| Routing by `conidex` for a non-SMART venue | 4 | T1 (the paper order test) |
| A conversion as ObliskIQ sends it: `LMT` with `isCcyConv` and `fxQty` (the amount from) and no `quantity`; both sides; its trades' size and price; the `399` odd-lot notice as a reply or a notice (D98) | 4, 5 | the execution test |
| What a cancel of an order that has just filled answers | 4 | T1 (the paper order test) |
| `Inactive` after acknowledgement (an order the destination rejected); read as working | 4 | T1 (the paper order test), the execution test |
| Whether a `/tickle` between a warning and its answer invalidates the warning | 4 | T1 (the paper order test) |
| `obliskiq preflight --paper-order-test` end to end: identity, rules, algos, quote, whatif, place, replies, the cOID listed, status, cancel; record the shapes in docs/knowledge/ibkr-orders.md | 4a | T1 |
| Whether a whatif registers its cOID (the gate previews with its own) | 4a | T1 (the paper order test) |
| Whatif margin fields on a paper margin account, and the currency in its amount strings | 4a | T1 (the paper order test) |
| How long the positions read lags a fill; whether the trades lag the live orders' `filledQuantity`; then `reconcile.grace_seconds` from it (a judgement of 120 s until then) | 4a | the execution test (item d) |
| The live orders show a modify or a cancel at once (the exit coordinator clears the exit on the next read) | 4a | the execution test (`--finish`) |
| Positions closed during the day listed at zero | 4a | the execution test (`--finish`) |
| IBKR's attached orders: a whatif of an entry with a child stop-limit linked by `parentId`, then whether IBKR lists, reports and cancels the child (not adopted, D99) | review | a later session, before attached orders are designed |
| A triggered stop-limit that does not fill through a gap on paper, and its repricing | review | the first gap on paper (Session 6 on) |
| A fresh unattended Gateway login after IBKR's daily reset on the machine: settled, day 1 (2026-10-20) and day 2 (2026-10-21) passed under WSL2 Ubuntu with Java 11 [CP ✓ 2026-10-21]; the in-process re-login is Session 6's | review | settled (T1) |
| The live orders' `snapshot: true` on a session's first read: absent on the first read after a login (the ASX order test, 2026-10-21; the execution test's first `reconcile run`); a pre-read before the read that counts, then the same read on a second call | review | the follow-up fix, then T1's order test |
| The stop-limit's `orderType`: ObliskIQ sends `STOP_LIMIT`, IBKR's order-types page writes `STP LMT` | 5 | the execution test |
| A GTC stop listed in a later session's live orders, and its status readable by id (D99) | 5 | the execution test (`--finish` the next day) |
| The spot-FX commission (0.20 bp, at least USD 2.00, charged in the base currency) against a conversion's trade | 5 | the execution test |
| The cOID kept unique for 24 hours (the place-order reference) | 5 | T1 (the paper order test) |
| The Adaptive algo's parameters and its limit cap, before `execution.pricing.algos` enables it | 5 | a paper order with the algo (Session 6 on) |
| The skeleton's conids through `/iserver/secdef/search` and `/iserver/contract/{conid}/info` (IWDA 78999785 LSEETF USD, EMIM 153454120 AEB EUR, IBGM 43614383 AEB EUR; read on MCP) | 6 | Session 6's live paper cycle (`run-cycle --dry-run`) |
| A whole cycle on paper: the conversion (and IBKR's text of `o10164` on it), three buys, their fills, the holdings registered to the core, reconciliation green, the digest | 6 | Session 6's live paper cycle |
| Telegram's `sendMessage` in HTML mode from the machine, and a digest split under the cap | 6 | Session 6's live paper cycle (with `TELEGRAM_*` set) |
| The shadow fill at the touch (D103) against paper fills of the same orders | 6 | 18 (TCA) |
| The health ping (`OBLISK_HEALTH_PING_URL`) from the machine | 6 | the first week of `serve` |
| The Gateway login on Java 21 and on Java 25 (the side test): passed once each, 2026-10-21 [CP ✓ 2026-10-21]; a second day lets them into `ibkr.gateway.java_major_versions` (D96) | D96 | a second day of the side runs (the order test's rerun day) |
| Playwright 1.64 logs in on Ubuntu 26.04 with the pinned-key flag: settled, six first-attempt logins on 2026-10-21 under WSL2 Ubuntu 26.04.1 [CP ✓ 2026-10-21] | D96 | settled (T1) |
| A manual Gateway login on the machine through an SSH-forwarded browser: not needed, the unattended login was accepted with a session | D96 | closed (T1) |
| European commissions (`europe_eur`, `europe_usd`, `lse_gbp`, `six_gbp`, `six_chf`, SmartRouting since D97) against an executed UCITS order | 4b | T1 (the paper order test), 6, 18 (TCA) |
| Tax-free fund switches at IBKR (`fund_switch_deferral_at_broker: false` until verified) | 4b | 19a (mutual funds) |
| The savings scale for tax year 2027 (the table is verified through 2026) | 4b | the monthly tax-source check (§5.16), 2027 |
| `yfinance_history` against Yahoo from the deployment machine (the call is network code outside the coverage floor; its frame is pinned by a fixture). From the cloud container it worked on 2026-10-17 (4c loaded TIP, IEF, GLD and CRSOX) | 4b | T1 |
| How IBKR reports dividends, interest, fees, withholding and conversions in the ledger and the account's activity; the classifier's fixtures from them (Session 7's item 10) | 7 | the first session with a working paper login |
| Settlement lags read in secondary sources only (`verify_live` in `venues.yaml`: BVMF, XSGO, XSWX, XSHG, XSHE, XKRX, XTAI, XSPX, XEUR) | 7 | 8 (the venues the universe admits) |
| The DGT's rulings on currency balances (V2422-20, V0463-21, V1613-25, V0152-26) in their official text | 7 | the monthly tax-source check (§5.16) |
| The ledger's per-currency `exchangerate` as the trade date's rate for tax (recorded by the NAV step and the heartbeat) against the rate IBKR reports on a foreign fill | 7 | Session 6's live paper cycle |
| The skeleton's tax facts (ISIN, issuer, tracked index) against `/iserver/contract/{conid}/info` | 7 | Session 6's live paper cycle |
| The collectors' live round (`obliskiq collect daily`, read-only): the underlying snapshot field set on every venue and its availability against the plan (`market_data.feeds`, their prices and their sharing with the paper session); the daily bar's `t` stamp per venue (bars are dated in the venue's zone); the chain walk (`secdef/search` OPT months and venues against the pinned conid, `secdef/strikes`, `secdef/info` per strike with `maturityDate`, `multiplier`, `local_symbol`; the declared venues FTA, MEFFRV, MONEP, EUREX); `/trsrv/futures` for the 16 roots on their native exchanges; one scanner run (`STK.US.MAJOR`) | 8 | the first command list after T1's order test |
| Pins for TOPIX, BARN, LISN and JDEP (`obliskiq universe pin`), and resolved descriptions for the 47 pinned instruments without one (an instrument without one is never ordered) | 8 | the collectors' live round |
| Venue profiles: trading permissions, listing currencies and lot conventions (`venues.yaml`, `verify_live: true`); Stocks: United States, Australia and Germany were held on the paper account by accepted previews [CP ✓ 2026-10-21] (`docs/knowledge/ibkr-capabilities.md`) | 8 | the capability probe (below), then each venue's first order |
| A stock candidate's holdings ticker and exchange name to IBKR's symbol and code, per venue (`collectors.candidates.exchanges`; resolution on demand) | 8 | the first candidate a thesis pins |
| The capability probe on the paper account (`obliskiq universe capabilities --probe`, whatif only): search, quote, contract rules, algos and a preview per check, on every venue `capabilities.yaml` lists, US and Eurex options included; then the readings recorded in `capabilities.yaml` and `docs/knowledge/ibkr-capabilities.md` by a reviewed change | 9 | the first command list after T1's order test |
| Order types and algos per asset class, by name (T1 counted 12 types and 10 algos for SPY, 9 types for BHP, without names) | 9 | the capability probe |
| The trading-permission field 7768's values on a snapshot (IBKR's field table) | 9 | the capability probe |
| Xetra's `IBIS2` segment on CP, and the core candidates' conids, currencies and classes through `/iserver/contract/{conid}/info` (read through IBKR's connector) | 9 | the capability probe |
| UIQK's currency and share class (IBKR describes conid 128562052 as "H. CHF"; its ISIN is the USD A-acc class) | 9 | the capability probe |
| The PRIIPs KIDs of TIP1D, UIQK and Xetra-Gold, and Xetra-Gold's custody fee at IBKR (ubs.com refused the container; ineligible until read) | 9 | the next universe review |
| The DGT's reading of a bullion-backed ETC's result (a gain, or capital income under LIRPF art. 25.2) in an official text | 9 | the monthly tax-source check (§5.16) |
| GDELT's collection end to end from the deployment machine (the cloud session's shared egress met HTTP 429 on nearly every request, 2026-10-23; the fixtures are that day's live answers) | 10 | the first week of `serve` |
| An NYSE-listed halt in Nasdaq Trader's feed and its `Market` code (only NASDAQ and AMEX were listed on 2026-10-23); until seen, XNYS's mapping can add blocks and never lift one | 10 | the first NYSE halt while `serve` runs |
| A halt with its resumption as the feed shows it (`ResumptionDate`, `ResumptionTradeTime` formats; none was listed on 2026-10-23) | 10 | the first resumed halt while `serve` runs |
| The EventExtractor's first call: Gemini's OpenAI-compatible reply, its `usage`, and the `json_schema` response format accepted (no key in the session) | 10 | the owner setting `GEMINI_API_KEY` |
| The service's source jobs, the halt refresh and the extractor on the machine (built and tested over the fake broker and a mock web) | 10 | the first week of `serve` |
| `yfinance`'s earnings dates from the deployment machine (worked from the cloud, 2026-10-23) | 10 | the first week of `serve` |
| `obliskiq compute all` on a store the collectors filled live: each family's reach and its dominant blocker against the fixture store's, the session solve on a route that prices chains without vols, the coverage floor, and the run's time on the machine | 11 | the first command list after the collectors' live round |

## Follow-ups

- Session 12: the candidate gates feed `grid/census.py` (`Census.run`), which reports every gate
  untested until then (D134); the option surface's leaves join `measurements` under their own
  entity kind or the instrument's.
- The service runs no compute step yet: `obliskiq compute all` by hand. A scheduled job after
  each region's collection lands with the first engine that reads the store on its own (Session
  12 or 13).
- Lite's 13-week positioning change (`commodities.cot`) is not carried (D134); add it to
  `data/positioning/readings.py` if a consumer asks for it.
- `GEMINI_API_KEY` in a developer's environment makes `test_cli_llm.py` and `test_env.py` fail
  locally (they assume the key absent); CI is unaffected. Clear the key inside those tests.
- CFTC release exceptions: the 2025 government shutdown delayed the October–November reports,
  which the fixed lag of three days dates early; an exceptions list from the CFTC's notices before
  a backtest reads those weeks (D126).
- A 6-K without an exhibit carries its release after the form's cover page (Santander's); the
  cover's boilerplate opens the text the extractor reads. Skip the cover in
  `data/events/rss.py: filing_text` when Session 16 measures extraction quality.
- Session 15: the event components read `data/events/readings.py` (velocity, pulse),
  `data/positioning/readings.py` and `data/insiders/clusters.py`; GDELT's asset map from the old
  ObliskIQ's `gdelt_regions.json` is component logic and lands there.
- Session 16: the LLM layer builds on the extractor's stored series without breaking its segment
  (model `gemini-3.1-flash-lite`, prompt hash `5bed4ad9…ad77`); a change of either is a new segment
  and, for H7, a new pre-registration.
- Source jobs run inline in the service loop, as the market collections do (D129): a throttled
  GDELT run (two queries, three attempts each, spaced and backed off) can hold the heartbeat for
  about seven minutes at 01:00 UTC, inside Tokyo's session. Run source jobs beside the loop before
  an order on XTKS depends on the heartbeat's pace.
- A declared source is built only when a consumer appears (D66): Eurostat's flash HICP dates,
  the BoE, the SNB and the rest of the regional list in `config/sources.yaml`.

- Dependency currency (D96), each with its review date:
  - by 2026-11-30: Python 3.15. Widen `requires-python` to `<3.16`, try 3.15 in CI, then move
    `.python-version`, CI, mypy's and ruff's targets, `tests/unit/test_packaging.py` and
    MASTER_PLAN §7. The machine verifies it with one `preflight --read-only`.
  - next Dependabot round: one uv version for CI and the machine, in `[tool.uv]
    required-version`, read by `setup-uv` in place of `ci.yml`'s `version: "0.12.23"` (whether
    setup-uv reads that key is to be confirmed in that pull request's CI log).
  - before the live account's OAuth runs: cryptography 50 (Almoon-D/ObliskIQ#9, red;
    Almoon-D/ObliskIQ#5 is the same update). Parse the DH parameter file without
    `load_pem_parameters`, whose finite-field DH is deprecated (`oauth.py`); the exchange is
    already `pow`.
  - Every venue of a tradable candidate or option underlying is covered through
    `calendars.horizon_years` (a test); XSHG holds data only (510300), and past 2026-12-31 its
    collection folds into New York's job. A tradable candidate on XSHG, XBOM or XSES needs a
    calendar first.
  - The Gateway's Java list, and 2027-07-01 at the latest: Java 21 and 25 each passed one day
    (2026-10-21); a second day of side runs adds them, newest first (D96). Temurin builds Java 11 to
    at least October 2027.
- **T1's order test.** `preflight --paper-order-test` has placed no order (docs/knowledge/ibkr.md,
  "T1, second day"). Before the rerun: the first live-orders read of a session lacks `snapshot: true`
  and the pre-trade gate, the cOID lookup and `reconcile` all treat it as incomplete, so a fresh
  process cannot place an order. A session reads the live orders once before the read that counts
  (the endpoint's documented pre-flight) and fails closed if the second read is also incomplete;
  failure-branch tests for both. It is a money path: its own session, not a patch inside a record.
  Then the order test inside both windows, and the Java side runs on that day.
- **Owner:** H2 (insider-purchase clusters) and H4 (the stock-level composite) are not evaluable
  with free prices (DECISIONS D84). A survivorship-free US price history with delisting returns
  would make them evaluable: CRSP through WRDS, or Norgate US Platinum at US$630 a year
  (2026-10-17). Buying one is the owner's decision; Session 21 tests them if it is bought. The
  satellite has no tested selection signal (D87), and this is the cheapest step toward one.
- **Owner:** the point is set (D91). Session 13 builds A, the static risk balance: 30 % reached
  on at most 5 % of 20-year paths. Session 4e's total-risk specifications were neither
  sufficient nor within that point, so A stays (D95). Before Session 10: an
  LLM provider key, or the minimal extractor waits (D70). Also open: whether a hedge inside a UCITS share class fits the mandate (D69), a deep emergency
  limit for protective exits (off by default, D67), buying option-surface history for H8
  (D71).
- Research harness: the moving-block bootstrap draws a sample's first and last months less often
  than the others, so on a short sample its interval can sit beside the mean (Session 4e's
  forecast check on 46 development months). A circular block bootstrap would not; a change applies
  only to pre-registrations written after it.
- **Owner:** D89's next adaptation has no session. Re-estimated risk-balance weights (D92), a
  blend with a multi-year window and return timing are untested (D95). Scheduling the next test,
  and on what data, is the owner's decision: the 2004–2026 sample has now been read for total-risk
  scaling, so a new pre-registration states what its author has seen.
- **Owner:** no session checks the whole managed book against the owner's point. A's breach
  (0.0 % of paths) was measured on the core alone, but the tolerance applies to everything the
  bot manages (D88). The satellite (Session 15) and the option sleeve add to it. Sessions 15 and
  17 are the natural places, and adding the check is a scope decision.
- Research harness: the simulator treats conversions as free (D75); `costs.yaml`'s `spot_fx` row
  prices them now (D97). A change applies only to pre-registrations written after it.
- Tax: apply the personal and family minimum to the savings base if the owner's general base
  leaves part of it unused (the engine errs high without it). No session holds it yet.
- Tax on the live path (D110): dividends, interest and withholding enter the tax ledger from the
  cash-flow ledger's classified adjustments; opening lots for holdings and currency the account
  held before the bot's first fill (entered by hand with their cost and date), so the hooks
  answer for them; a session before the live account (Session 18 at the latest).
- Session 15: `StrategyValue` for the satellite (probes, pending catalysts, exits it makes
  anyway); Session 13 for the core, Session 17 for the decision layer (D108).
- Session 19: Telegram and MCP answers to owner questions and the cash commands (D109).
- Session 13: the core engine replaces `core.skeleton_targets` and `decision/rebalance_basic.py`
  (and, with Session 8's universe, the skeleton's allowlist entries).
- The session that records Session 6's live cycle: decide `o10164` on a conversion from IBKR's
  text, now kept in the intent's history and the declined warning's alert (D101, D105).
- Session 19: the deployment's unit or container replaces docs/RUNBOOK.md's example systemd unit.
- When IBKR's pacing binds (the first weeks of `serve` show it): the gate reads the live orders and
  the trades fresh for every clearance, and IBKR paces each at one request per 6 s here, so an
  exit beside a stop (check, cut, clear) takes about 18 s and a cycle of N orders at least 6·N s;
  the heartbeat now runs the executor's cycle, reconciliation included, every 5 minutes (D102).
  Share one read per cycle under the store lock, keeping I2's rule that a sell cleared after the
  read still counts.
- The kill switch classes a sell by the most cautious of its intent's spent clearances (D48); once
  reconciliation knows the terms the broker accepted, it can class by those instead.
- Session 18 (I5): the boundary writes an intent only in the mode it was made for, so an
  automatic `live → live-small` downgrade would freeze every modify of an intent made under
  `live`, stop resizes and restores included. When the live levels can write, let a downgrade
  between two writing modes keep protective modifies of existing intents working.
- First session with captured paper rows (Session 6's live cycle): the sweep cancels a live-order
  row it cannot read (D48), which could strip a protective stop whose row fails validation for
  another field; read the raw `side` and `secType` of an unreadable row and keep a readable
  non-`CASH` sell.
- Session 17: a protective intent lives 365 days (`execution.protective.intent_ttl_days`);
  its GTC stop outlives it, but the boundary refuses the stop's modifies after it expires. Renew
  the intent (a new one adopting the order) before its end (D99).
- Session 18: calibrate the shadow fill at the touch (D103) against paper and live fills; it
  simulates no queue position, size beyond the displayed quote or impact. Shadow converts nothing,
  so a buy in a currency the account holds no cash in waits in shadow; simulate the conversion if
  L1's shadow days need it.
- A later session: IBKR's attached orders (D99). A whatif of an entry with a child stop-limit
  first (read-only), then the package's clearance (I1, I5), then the paper probe.
- Session 12: `tenor.mandatory_close_trading_days` lives in `config/defaults/instruments.yaml`;
  the rest of lite's `tenor` block joins it. `allow_futures_options` stays false until the
  never-exercise rule exists.
- Session 18: calibrate the gate's caps (`execution.pretrade.caps`), the cash floor, the protective
  spec's limit offset and gap steps, the pricing's start and reprices, and the currency manager's
  buffer, minimum and limit fraction from the transaction-cost analysis.
- **Owner:** paper runs use the Gateway with the automated login (owner, 2026-10-12; OAuth is
  the live account's, D42). Its login did not complete from a cloud container (most likely the
  outbound address changing per connection), so live runs happen on the deployment machine
  (SESSION_PROMPTS §A.5, T1 and Session 6's live paper cycle in docs/RUNBOOK.md); sessions build
  against fixtures. If the login fails there too, the owner chooses between a TWS API with IBC spike
  and a daily manual login (D68).
- Session 19: the deployment image's base is the OS D96 chose (Ubuntu 26.04), with the Java
  versions `ibkr.gateway.java_major_versions` lists after T1 and
  `playwright install --with-deps chromium` for the locked Playwright.
- Session 10: the `macro` CHECK `release_date >= reference_date` rejects a flash estimate
  stamped with a period-end reference date, and the whole batch rolls back; relax or split it
  with the first live macro route.
- Session 11: lite's range-based volatility estimators (Parkinson, Garman-Klass and the restated
  open share, `collect/vol.py`) and their tests, with the indicators that read them; Session 8
  ported the close-to-close estimator and the sigma ladder.
- Session 19a: holiday lists for the synthetic venues (XSHE, XNSE, CHIA, XSPX). XNSE holds data
  only (NIFTYBEES): its collection runs on the synthetic weekday calendar, and a quote on an
  unlisted holiday carries the route's own status.
- Session 10: feeds enter through `Calendars.accept_incidents(origin, incidents)`, which
  replaces one origin wholesale; a single-stock halt uses instrument scope.
- Session 12: lite's `mandatory_close_trading_days` (10) is `tenor.mandatory_close_trading_days`
  (Session 4a). Lite's own test expects 2026-06-05 for a 2026-06-19 holiday expiry at N = 10; the
  new anchor gives 2026-06-04, one session more conservative.
- Session 12: lite counted "N days back from expiry", excluding the expiry; the anchor is
  now the last tradable session itself, so N sessions including it is `session_before(..., sessions=N-1)`.
- Dependabot keeps `exchange-calendars` current; its holiday lists lapse every year, and a
  library upgrade that changes a session fails the golden snapshot for review.
- Research history (D118): during a session Yahoo's last row is that session at its price so far,
  stored as observed on its day; drop a row whose session has not closed when it is loaded
  (`research/data.py`), so a read as of that day never sees a close that was not one.
- Session 12: the options the chain collector records join the order path's directory
  (`UniverseDirectory.from_settings(options=…)`, each with its multiplier) when the options sleeve
  orders one; Session 8 stores them.
- Session 15: a pinned stock candidate is seeded and collected, but it is not in
  `config/universe.yaml`, so the order path neither describes it nor judges it eligible (both
  refuse, fail closed); its attributes (a share, its venue's lot) join the directory and the
  policy when the satellite first orders one.
- Session 11: positions held outside the universe (lite's portfolio union) are not collected. The
  vehicle selector ranks universe instruments only (D120), so a held instrument joins the universe
  by a reviewed entry; Session 11's views decide which ones to propose.
- Session 15: lite's ADR map (legacy/old global_vehicles.py `ADR_MAP`): a company's US depositary
  line as another listing of a stock candidate, chosen when a thesis first orders it (D121).
- Session 13: the rebalancer chooses each role's vehicle through the selector, passing it the
  tracking correlations (`research/tracking.py`), the market inputs and the trading windows as
  `obliskiq universe vehicles` does (`cli/vehicles.py`), and swaps through `SwapPartners`.
- Session 18: the cost book prices an ETF of unknown market capitalisation at the illiquid friction
  tier (2.12 % a round trip, an assumption), so the selector separates European ETFs mostly by fee;
  a friction tier for ETFs by average daily traded value, from TCA (D120).
- The selector does not score the withholding a fund suffers by its domicile (MASTER_PLAN §5.8):
  every equity candidate is Irish and a fund's closes are net of it (D120). It joins when a
  candidate of another domicile competes for an equity role.
- Swap partners exist today for euro_govt_long (DBXG ↔ IBCL, 0.996) and, since D123, world_equity
  (SPPW ↔ XDWD, 1.000): the other roles' partners list on venues whose capabilities are
  unverified (VHVE on XMIL, EMIM and TPSA on XAMS, MTB on XPAR, VDTA on XMIL, the gold ETCs and
  commodity ETFs on XLON and XMIL) and appear when the probe verifies them. MSCI EM and FTSE
  Emerging read 0.93 to 0.94, below `min_correlation`; EMIM (MSCI EM IMI) reads 0.993 against
  XMME.
- The cash role's second and third candidates are unmeasured: Yahoo has no `CSH1.PA`, and CSH2's
  history starts on 2025-03-17 (36 monthly returns from 2028-03).
- **Owner:** rule on currency-hedged share classes (`vehicles.hedged_share_classes`, MASTER_PLAN
  §11).
- Session 11: add a column-targeted writer for the solved implied volatility (snapshot upserts
  replace the whole row); add a date-leading index on `bars` if a cross-sectional read needs it.
- A migration is forward-only: a later session that changes a table adds `002_*.sql`; it never
  edits `001_core.sql`.
- **Owner:** confirm the repository settings listed in the Session 1 log entry.
- **Owner:** after the merge, archive `Almoon-D/Inversor-Inteligente` (SESSION_PROMPTS §A.4).
- **Owner:** the copyright notice and package author name the owner (`pyproject.toml`,
  `src/obliskiq/__init__.py`), as in ObliskIQ-lite-MCP; confirm or ask for a change.
- GitHub's dependency graph also reads the manifests inside `legacy/`
  (`legacy/lite/pyproject.toml`, `legacy/lite/requirements.txt`,
  `legacy/pulse/pyproject.toml`), so Dependabot alerts may name them until Session 20
  deletes `legacy/`; they are never installed.
- Session 19: a release created with `GITHUB_TOKEN` does not trigger `on: release`
  workflows, so the image build must be a job in `release.yml` (as its prompt says).
- Release workflow: when a pending run is replaced by a newer push to `main`, the replaced
  version is never released (the newer commit carries its own version). Recovery: re-run the
  cancelled run from the Actions tab; it tags its own commit.
