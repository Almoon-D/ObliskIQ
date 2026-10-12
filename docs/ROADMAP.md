# Roadmap

What is done and what is open. The design is `docs/MASTER_PLAN.md`; each session's task is
its section of `docs/SESSION_PROMPTS.md`; why things are as they are is `docs/DECISIONS.md`.

## Sessions

| # | Session | Outcome | Status | Release |
|---|---|---|---|---|
| 1 | Foundation | scaffold, docs, owner settings and defaults, jurisdiction registry, CI, releases, legacy snapshot | done | v0.1.0 |
| 2 | Store, calendars and numerics | store and migrations, calendar and phase layer, BSM, IV, smile, stats, inference, costs | done | v0.2.0 |
| 3 | IBKR read path | OAuth and Gateway transports, session lifecycle, client, rate limits, quote status, contract identity, trading schedules, preflight | done (live login blocked in the cloud) | v0.3.0, v0.3.1 |
| 4 | Order path, kill switch and modes | intents, order client, reply and quote policy, clearance lock, state machine, paper guard, modes, kill switch | next | |
| 4a | Pre-trade rails | pre-trade checks with reason codes, eligibility skeleton, approval queue, flatten, paper order test | planned | |
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
| IBKR exchange codes → venues (23 codes in `ibkr_exchanges`, including `FTA`, `CDE`, `AMEX`, `BATS`, `LSEETF`) | 2 | 8 (conids) |
| Venue phase windows: pre-market, auctions and post-market per venue (pulse values, unmeasured) | 2 | first session with a working login |
| SGX mid-day break hours (the XSES lunch override; the library skips the break) | 2 | first session with a working login |
| B3 regular close follows New York daylight saving time (the BVMF seasonal close) | 2 | first session with a working login |
| XEUR session hours versus single-stock option hours (options use `common_windows`) | 2 | first session with a working login |
| IBKR's trading-schedule format as a `ScheduleSource` (regular hours only are compared) | 2 | first session with a working login |
| Order window 30 min after the open and 15 before the close | 2 | 18 (TCA) |
| Holiday lists for XSHE, XNSE, CHIA, XSPX (blocked until sourced) | 2 | 8 |
| Precomputed calendars end 2026-12-31 for XSHG, XBOM, XSES (`Calendars.coverage()`) | 2 | library upgrade; 6 (digest) |
| IBKR market-data flag spellings: `FROZEN_DELAYED` → `frozen`, an unlisted or negated flag → NULL with the raw flag kept (CP's 6509 letter codes are decoded by `ibkr/decode.py`; MCP spellings unchanged) | 2 | first session with a working login |
| US stock, US option and Eurex option commission schedules | 2 | 4a (paper order test), 18 (TCA) |
| US Treasury commission and the EUR-bond schedule above EUR 8,500 of face | 2 | 9 (bond rule) |
| Transaction taxes: ES, FR, IT rates against official texts; GB, HK against a trade confirmation | 2 | 7 (`es` tax policy), 8 |
| Friction tiers (spread, slippage, impact, option half-spread) are legacy assumptions | 2 | 18 (TCA) |
| Client Portal Gateway headless paper login on the deployment machine (failed in the cloud container, docs/knowledge/ibkr.md) | 3 | 6 (machine run), 19 |
| `obliskiq preflight --read-only` end to end on paper (every step after the transport) | 3 | first session with a working login |
| Snapshot availability and delay: a US stock (Cboe One/IEX), a European UCITS ETF, a US option (OPRA), a Eurex option (Eurex Core), each with the owner logged in live elsewhere and not | 3 | first session with a working login |
| One market-scanner run (`/iserver/scanner/params`, `/run`) | 3 | first session with a working login |
| Snapshot field ids from IBKR's table: 7633 strike IV, 7638 open interest, 7087 historical vol, 7310 theta, 7311 vega | 3 | first session with a working login |
| `/iserver/secdef/search` without `secType` still returns sections | 3 | first session with a working login |
| `/contract/trading-schedule` shape, holidays omitted, and agreement with the calendars (closes the Session 2 phase-window, SGX, B3 and XEUR rows) | 3 | first session with a working login |
| `/iserver/auth/ssodh/init` with `publish`/`compete` as query parameters through the Gateway (lite used them over OAuth) | 3 | first session with a working login |
| `ssoExpires` and the daily reset as seen by the keepalive | 3 | 6 |
| The daily bar's `t` stamp per venue (the history window is filtered on the UTC date of the bar start; a venue stamping before UTC midnight would shift by a day) | 3 | 8 |
| Fixture shapes in `tests/fixtures/ibkr/` (from IBKR's reference and lite's readings, none captured through ObliskIQ) | 3 | first session with a working login |

## Follow-ups

- **Owner:** paper runs use the Gateway with the automated login (owner, 2026-10-12; OAuth is
  the live account's, D42). Its login did not complete from a cloud container (most likely the
  outbound address changing per connection), so the first live run is on the deployment
  machine (Session 6/19 checks it first); until then sessions build against fixtures. If the
  login fails there too, the fallback is a daily manual login.
- Session 4: tests that run `run_read_only` through its own `build_transport` (no injected
  transport): live OAuth with a passing (mocked) handshake, and the Gateway with a data directory
  and an injected login driver (review of v0.3.1).
- Session 4: OAuth token negotiation runs inside `headers()`, inside the limiter slot, and honours
  `Retry-After`; a token outage can hold every concurrency permit for minutes, and each of
  `rearm`'s sub-requests renegotiates. Before the order path and kill switch share the limiter,
  ensure the token outside the slot and stop `rearm` at the first `TransportUnavailableError`.
- Session 6: a repair deferred by the Gateway's re-login interval is retried only by a later
  failing read, which repeats the alert. Keep a lost flag and the deferral deadline; let the
  keepalive repair once it passes and fail reads fast, without a new alert, before then.
- Session 4: add the order path's self-paced endpoints (`iserver/account/orders`, `trades`,
  `pnl/partitioned`) to `IBKR_PACED_ENDPOINTS` and to the rate groups.
- Sessions 4 and 4a: the order path reads cash from `IbkrClient.portfolio.cash_balances()`
  (Decimal per currency), quotes with `market_data_status`, `delay_seconds` and `delay_basis`
  (`documented_bound` for delayed quotes, D38), and calls `IbkrClient.start()`: the endpoint
  groups refuse every read until the account guard has passed (D41). Incomplete position or cash
  reads raise.
- Session 6: register `IbkrTradingSchedule` with `Calendars.from_settings(..., sources=[...])`,
  passing each venue's `is_business_weekday` as `is_trading_weekday` (the calendars expose it per
  venue), and refresh it from one reference conid per venue; pass the session's alert hook to
  Telegram.
- Session 8: the route tag for `source` columns (lite refused unknown routes) belongs to the
  collectors that write rows; the history walk gains its store watermark there.
- Session 19: the deployment image needs Java 11 for the Gateway (`ibkr.gateway.java_major_versions`)
  and `playwright install chromium` matching Playwright 1.56.
- Session 10: the `macro` CHECK `release_date >= reference_date` rejects a flash estimate
  stamped with a period-end reference date, and the whole batch rolls back; relax or split it
  with the first live macro route.
- Session 4a: `tradability` and `venue_status` take `conid` as an optional argument and ignore
  instrument-scoped halts without it; the pre-trade check must pass the order's conid.
- Session 4a: call the instant view — `Calendars.tradability(venue, at, ...)` — and, for an
  option, `common_windows` with the underlying's venue; `venue_status` is the planning view.
- Session 8: pin the IBKR codes with contracts; source holiday lists for the four synthetic
  venues; port lite's realised-volatility estimators (`collect/vol.py`).
- Session 10: feeds enter through `Calendars.accept_incidents(origin, incidents)`, which
  replaces one origin wholesale; a single-stock halt uses instrument scope.
- Session 12: lite's `mandatory_close_trading_days` (10) has no key in `config/` yet. Lite's own
  test expects 2026-06-05 for a 2026-06-19 holiday expiry at N = 10; the new anchor gives
  2026-06-04, one session more conservative.
- Session 12: lite counted "N days back from expiry", excluding the expiry; the anchor is
  now the last tradable session itself, so N sessions including it is `session_before(..., sessions=N-1)`.
- Dependabot keeps `exchange-calendars` current; its holiday lists lapse every year, and a
  library upgrade that changes a session fails the golden snapshot for review.
- Session 8: `history_exchange` NULL means "send none" (`domain/instrument.py`), so the loader
  sets it explicitly; decide where the `tradable`/`untradeable_reason` invariant and the unique
  grid cell live now that the universe is configuration.
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
- Session 4 (first thresholds in a decision path): the magic-number guard accepts any
  module-level UPPER_CASE constant; decide whether decision paths take only an explicit
  allowlist of protocol and unit constants (a DECISIONS entry either way).
- Session 19: a release created with `GITHUB_TOKEN` does not trigger `on: release`
  workflows, so the image build must be a job in `release.yml` (as its prompt says).
- Release workflow: when a pending run is replaced by a newer push to `main`, the replaced
  version is never released (the newer commit carries its own version). Recovery: re-run the
  cancelled run from the Actions tab; it tags its own commit.
