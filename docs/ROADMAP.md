# Roadmap

What is done and what is open. The design is `docs/MASTER_PLAN.md`; each session's task is
its section of `docs/SESSION_PROMPTS.md`; why things are as they are is `docs/DECISIONS.md`.

## Sessions

| # | Session | Outcome | Status | Release |
|---|---|---|---|---|
| 1 | Foundation | scaffold, docs, owner settings and defaults, jurisdiction registry, CI, releases, legacy snapshot | done | v0.1.0 |
| 2 | Store, calendars and numerics | store and migrations, calendar and phase layer, BSM, IV, smile, stats, inference, costs | done | v0.2.0 |
| 3 | IBKR read path | OAuth, session lifecycle, client, rate limits, quote status, contract identity, trading schedules, live preflight | next | |
| 4 | Order path and safety rails | intents, order client, reply policy, pre-trade checks, paper guard, modes, kill switch, live order test | planned | |
| 5 | Execution mechanics | currency manager, pricing, resting stops, executor, reconciliation | planned | |
| 6 | Walking skeleton | service, cycles, static targets → intents → paper → reconcile → digest | planned | |
| 7 | Capital, cash flows and tax | managed capital, cash-flow ledger, reservation planner, owner questions, lots, `es` tax policy, TWR | planned | |
| 8 | Universe and market-data collectors | global universe, stock candidates, venue profiles, eligibility policy, collectors, research history | planned | |
| 9 | Broker capabilities and vehicle selector | capabilities measured, core candidates, bond rule, swap partners, vehicle selector | planned | |
| 10 | Macro, positioning, insiders and events | point-in-time macro, COT, Form 4, Polymarket, Kalshi, GDELT, RSS, incident feeds | planned | |
| 11 | Measurement store and views | measurement store, equity grid, macro board, rankings, correlations, IV solve | planned | |
| 12 | Engines and gates | option surface, options sleeve, fx, commodity, risk engine, gates | planned | |
| 13 | Core engine | pre-registration, fixed risk budgeting, environments, tax-aware trend tilt, rebalancing | planned | |
| 14 | Core backtest and verdict | after-tax walk-forward harness, single verdict run, promoted variant | planned | |
| 15 | Satellite engine | Hunter components, measured independence, entry rules, lifecycle, triggers, pattern registry | planned | |
| 16 | LLM layer | provider and model registry, evaluation set, budget, structured outputs, six roles | planned | |
| 17 | Decision layer | expression selection, two-regime sizing, whole-book risk, breakers, inertia, intents | planned | |
| 18 | Scoreboard, patterns, capital and promotion | TWR attribution, TCA, grading, calibration, shadow patterns, capital allocation, promotion | planned | |
| 19 | Cockpit, settings and deployment | MCP cockpit, Telegram, settings flow, dashboard, Docker, machine, backups, updates | planned | |
| 20 | Red-team and cleanup | failure-mode audit, mutation tests, `legacy/` deleted, docs truth pass, 1.0.0 | planned | |
| 21 | Research: first pattern batch | pre-registered tests of §5.9; passes go to shadow | any time after 18 | |

## Session log

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
- Tests: 1,467 passing. Coverage measured 99.91 %; floor raised from 98.6 % to 98.9 %.

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
| IBKR exchange codes → venues (23 codes in `ibkr_exchanges`, including `FTA`, `CDE`, `AMEX`, `BATS`, `LSEETF`) | 2 | 8 (conids) |
| Venue phase windows: pre-market, auctions and post-market per venue (pulse values, unmeasured) | 2 | 3 (trading schedules) |
| SGX mid-day break hours (the XSES lunch override; the library skips the break) | 2 | 3 |
| B3 regular close follows New York daylight saving time (the BVMF seasonal close) | 2 | 3 |
| XEUR session hours versus single-stock option hours (options use `common_windows`) | 2 | 3 |
| IBKR's trading-schedule format as a `ScheduleSource` (regular hours only are compared) | 2 | 3 |
| Order window 30 min after the open and 15 before the close | 2 | 18 (TCA) |
| Holiday lists for XSHE, XNSE, CHIA, XSPX (blocked until sourced) | 2 | 8 |
| Precomputed calendars end 2026-12-31 for XSHG, XBOM, XSES (`Calendars.coverage()`) | 2 | library upgrade; 6 (digest) |
| `market_data_status` decoding: `FROZEN_DELAYED` → `frozen`, unknown flag → NULL with the raw flag kept | 2 | 3 |
| US stock, US option and Eurex option commission schedules | 2 | 4 (paper order test), 18 (TCA) |
| US Treasury commission and the EUR-bond schedule above EUR 8,500 of face | 2 | 9 (bond rule) |
| Transaction taxes: ES, FR, IT rates against official texts; GB, HK against a trade confirmation | 2 | 7 (`es` tax policy), 8 |
| Friction tiers (spread, slippage, impact, option half-spread) are legacy assumptions | 2 | 18 (TCA) |

## Follow-ups

- Session 3: `ScheduleSource` is synchronous and cache-backed (refresh asynchronously;
  `observe()` only reads); `None` means "no information", not "closed". Register IBKR's
  trading schedule as the third source and record the endpoint and format in
  `docs/knowledge/ibkr.md`.
- Session 4: call the instant view — `Calendars.tradability(venue, at, ...)` — and, for an
  option, `common_windows` with the underlying's venue; `venue_status` is the planning view.
- Session 8: pin the IBKR codes with contracts; source holiday lists for the four synthetic
  venues; port lite's realised-volatility estimators (`collect/vol.py`).
- Session 10: feeds enter through `Calendars.accept_incidents(origin, incidents)`, which
  replaces one origin wholesale; a single-stock halt uses instrument scope.
- Session 12: lite's `mandatory_close_trading_days` (10) has no key in `config/` yet. Lite's own
  test expects 2026-06-05 for a 2026-06-19 holiday expiry at N = 10; the new anchor gives
  2026-06-04, one session more conservative.
- Session 12: lite counted "N days back from expiry", excluding the expiry; the anchor is
  now the last tradable session itself, so N sessions including it is `session_before(n=N-1)`.
- Dependabot keeps `exchange-calendars` current; its holiday lists lapse every year, and a
  library upgrade that changes a session fails the golden snapshot for review.
- Session 8: `history_exchange` NULL means "send none" (`domain/instrument.py`), so the loader
  sets it explicitly; decide where the `tradable`/`untradeable_reason` invariant and the unique
  grid cell live now that the universe is configuration.
- Session 11: add a column-targeted writer for the solved implied volatility (snapshot upserts
  replace the whole row); add a date-leading index on `bars` if a cross-sectional read needs it.
- Session 3: the route enum that the `source` columns lack (lite refused unknown routes).
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
- Session 4 (first thresholds in a decision path): the magic-number guard accepts any
  module-level UPPER_CASE constant; decide whether decision paths take only an explicit
  allowlist of protocol and unit constants (a DECISIONS entry either way).
- Session 19: a release created with `GITHUB_TOKEN` does not trigger `on: release`
  workflows, so the image build must be a job in `release.yml` (as its prompt says).
- Release workflow: when a pending run is replaced by a newer push to `main`, the replaced
  version is never released (the newer commit carries its own version). Recovery: re-run the
  cancelled run from the Actions tab; it tags its own commit.
