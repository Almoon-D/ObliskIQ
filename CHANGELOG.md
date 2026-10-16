# Changelog

All notable changes to ObliskIQ are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every version below 1.0.0 is a
pre-release; the release workflow publishes each version's section as its release notes.

## [0.5.1] - 2026-10-16

Design review (owner request): evidence before machinery. A red-team of the plan against the
code and the legacy evidence, challenged twice by two other reviewers, recorded as DECISIONS
D60–D71. Process and documents only; no runtime code changes.

### Changed

- MASTER_PLAN: the objective is the after-tax log growth of liquidation value within the
  owner's drawdown limit, with an equity reference and a 60/40 scored under the same rule
  (§1, D60); every capability runs under a computed activation condition, never a NAV
  threshold (§2, D66); the core's variant, including an equity-dominant one, is selected by an
  after-tax verdict (§5.4); the research pipeline has a protocol, registered combinations and a
  decision rule with four mutually exclusive outcomes (§5.9, D62–D64); LLM-derived evidence is
  forward only (§5.10, D70); sizing uses a gap-adjusted loss and protective exits handle gaps
  without market orders (§5.11, §5.12, D67); currency gains enter the tax ledger (§5.16, D69);
  capital follows research evidence and live results are monitored through a twin (§5.19,
  D65); the transport's fallback is open again (§7, D68); the session sequence (§10).
- SESSION_PROMPTS: T1, the owner's first run on the deployment machine (§A.5); new Sessions 4b
  (research harness, with the tax engine and European commissions), 4c (core verdict), 4d
  (first selection hypothesis) and 19a (capacity and scale, built before 1.0.0); Session 14
  absorbed into 4c; Sessions 5–13 and 15–21 adjusted (D61–D71).
- ROADMAP: the new sequence, the design-review log, live checks for attached orders, the
  reporting lag and stop-limits through a gap, and T1 as the first live run.
- AGENTS.md: the research harness's rules, and software tests are not evidence of returns.

## [0.5.0] - 2026-10-16

Session 4a — Pre-trade rails. The pre-trade gate replaces Session 4's refuse-everything gate:
no order reaches the broker unless every check passes, and every refusal carries a reason code.
No IBKR call was made (the paper Gateway login does not complete from a cloud container); the
paper order test is built and tested against the fake broker and queued VERIFY-LIVE.

### Added

- `execution/pretrade.py`: the gate, still the only issuer of a clearance. It works from the
  stored intent and checks, in three stages that each report every failure: the intent and the
  write asked for; the paper guard, the mode, the kill switch, the instrument, prohibited
  products, buy-side eligibility (the mandatory close of an option on an ineligible ETP),
  approval and the calendars' act-now view (an option also inside the windows its venues share,
  date-critical inside its mandatory-close window); the broker reads, the quote policy and the
  collar (a protective stop against its spec); and, in one store transaction with the
  clearance, the holdings registry, no short (I2), the cash in the order's currency against
  every open commitment and the floor, no borrowing by the whatif's own cost, and the caps. 42
  reason codes (`Reason`), each tested with its positive control. A clearance carries the
  order's notional and expires with the earliest quote it rests on.
- Class by effect (I3): an order that reduces risk waives exactly the kill switch, the caps,
  the order count, buy-side eligibility, the cash check and approval. An option close refused
  for want of a real-time quote alerts the owner.
- `execution/exposure.py`: the position (the lower of the broker's read and the store's after
  every reported fill) and every sell that could still reach the broker, in any process.
- `execution/exits.py`: the exit coordinator (I5). An exit's other checks pass before its
  protective stop is cut; the stop is restored (modified back, or placed again) when the exit is
  refused, cancelled or expires; a failed restore alerts and is retried.
- `execution/flatten.py` and `obliskiq flatten --confirm`: closing orders for every managed
  position, at the bid, through the coordinator; they pass with the kill switch on.
- `execution/approvals.py` and `obliskiq approvals list|approve|reject`: the approval queue, off
  by default, per sleeve (`execution.approval.sleeves`).
- `execution/paper_test.py` and `obliskiq preflight --paper-order-test`: per configured
  instrument, the contract confirmed by search, rules, algos, a quote, a whatif, then one buy
  that cannot fill, its status, its replies and the cOID in the live orders, and a cancel.
- `execution/account.py` (the gate's broker reads), `execution/instruments.py` (the instrument
  directory and the prohibited categories), `execution/runtime.py` (the order path a command
  opens).
- `jurisdiction/eligibility.py`: the `EligibilityPolicy` protocol and the skeleton policy, which
  `config/jurisdictions/es.yaml` selects until Session 8.
- Migration `003_pretrade.sql`: `approvals` (append-only) and `stop_resizes`.
- `config/defaults/instruments.yaml` (lite's `instruments` keys and `tenor.mandatory_close_trading_days`),
  `config/defaults/eligibility.yaml` (the skeleton allowlist), and `execution.pretrade`,
  `execution.approval`, `execution.flatten` and `ibkr.preflight.paper_order`.
- The fake broker serves what the gate reads (session, accounts, positions, ledger, snapshots,
  rules, algos, contract search); the order rig carries the gate over test instruments.

### Changed

- Policy factories take the configuration as well as the profile (`PolicyRegistry.resolve`).
- The ledger's cash balances carry IBKR's rate to the base currency; a whatif names the currency
  of its amounts.
- An import contract keeps `jurisdiction` away from the broker, the network, the store and
  execution.

## [0.4.0] - 2026-10-15

Session 4 — Order path, kill switch and modes. Everything at and below the broker-write
boundary, locked behind a pre-trade clearance whose gate still refuses every intent (Session 4a
builds its checks), so no order can reach the broker yet. No IBKR call was made: every order
shape is VERIFY-LIVE.

### Added

- Order domain (`domain/orders.py`): intents with uuid7 ids, order terms whose every price
  policy carries a limit (a plain market order cannot be expressed), the pre-trade clearance
  bound to an intent, an action and the terms' digest, broker orders, fills, positions, cash
  balances, holding assignments, mode changes and kill-switch records.
- Migration `002_orders.sql` and `store/orders.py`: intents, an append-only event history,
  clearances spent once, broker orders, fills, account reads with position and cash snapshots,
  the mode audit, the kill-switch flag and the holdings registry; `store/worker.py` runs the
  store on one thread for async callers.
- IBKR order client (`ibkr/orders.py`, `ibkr/order_decode.py`): whatif, an idempotent placement
  (the cOID looked up in a complete live-orders snapshot and the trades before anything is
  sent), the warning loop under a reply policy, modify, cancel, status, live orders, trades,
  conversion fields, the cOID short form, and the reset of suppressed warnings.
- `execution/`: the intent state machine with property tests, lite's order discipline, the
  quote policy per asset class and the collar, the reply policy (`o354` only under the quote
  policy and inside the collar; unknown warnings declined, cancelled and alerted), modes with
  audited upgrades and automatic downgrades, the kill switch (file and database flags, a sweep
  that cancels what adds risk and keeps the sells that reduce it), the fail-closed pre-trade
  gate, the clearance ledger, the write boundary (mode, paper guard, kill switch, clearance) and
  shadow fills priced with the cost model.
- `obliskiq kill on|off|status` (`kill on` works even when the configuration does not load) and
  `obliskiq mode show|set`.
- `config/defaults/execution.yaml`: lite's `discipline` keys the order path reads, and
  `execution` (modes, orders, reply policy, quote policy, collar, kill switch).
- The stateful fake broker and order rig in `tests/support/`, guard tests (only the gate
  constructs or records a clearance; only the order client spells the write endpoints and only
  the write boundary places or modifies; only limit-capped order types are reachable; the
  order path's state reads are the listed ones) and `docs/knowledge/ibkr-orders.md`.
- Schema triggers: a clearance is spent once and never changed or deleted; an intent's identity
  never changes.
- Adversarial tests (`tests/unit/test_breaker_*.py`, a property file) against the clearance
  lock, class by effect, the kill switch, the reply policy, modes and the state machine.

### Changed

- `BrokerSession.request(..., retry=False)` sends a write at most once.
- The transport is readied (an OAuth token renewed) before a request takes its pacing slot, and
  a re-arm stops at the first request the transport cannot sign (DECISIONS D53).
- `/iserver/account/orders`, `/trades` and `/pnl/partitioned` each have a rate group at one
  request per 6 s.
- `run_read_only` takes a login driver; it is tested end to end through the transport it builds.
- The magic-number guard requires every numeric constant in `execution` and `decision` to be a
  listed protocol fact (DECISIONS D50).

## [0.3.2] - 2026-10-12

Sessions read what their work needs, when it needs it (DECISIONS D43); Session 4 is split into
Sessions 4 and 4a (D44). Process and documents only; no runtime code changes.

### Added

- `make prompt S=<id>` prints the common header and one session's prompt. Tests cover the
  extraction (4 versus 4a, 1 versus 10, unknown and missing ids) and check that every line
  range a prompt pins in `legacy/` covers whole sections.

### Changed

- AGENTS.md: "Read first" reads by section; a new "Context budget" (legacy per deliverable,
  frozen ranges for prose and YAML, code by symbol, money-path mechanics read at the source,
  no polling, read once); the paper Gateway login is not attempted from a cloud container; the
  reviewer reads its own diff; the environment's GitHub tools stand in for `gh`.
- `.claude/agents/`: `explorer` cites `path:start-end` ranges and never stands in for
  money-path source; `port-checker` and `reviewer` read `MANIFEST`, `DECISIONS` and
  `MASTER_PLAN` by section.
- SESSION_PROMPTS: Session 4 becomes Session 4 (order path, kill switch and modes, behind a
  fail-closed pre-trade clearance) and Session 4a (pre-trade rails, approval queue, `flatten`,
  paper order test). The pre-trade clearance is specified as five invariants with their tests
  (the gate; no short against every sell that could still reach the broker; class by effect
  with a fixed list of waivers; a kill switch that works when everything else fails; no exit
  leaving a position unprotected); later prompts pin frozen legacy ranges; §B asks for auto
  mode and a fresh session for follow-ups. MASTER_PLAN §10 and the ROADMAP follow the split.

## [0.3.1] - 2026-10-12

Paper runs use the Gateway; OAuth is the live account's (DECISIONS D42).

### Changed

- The `oauth` transport refuses any profile but `live`, as the Gateway refuses `live`; the
  refusal comes before any credential is read, so a paper run never looks for an OAuth key
  file. `obliskiq preflight --read-only` prints `refused: …` (exit 1) for such a combination.
- `Environment` no longer checks that the OAuth key paths exist when it loads; the live OAuth
  transport looks for them and refuses a missing file by name.
- AGENTS.md, MASTER_PLAN §5.12, `.env.example` and SESSION_PROMPTS §A: cloud environments hold
  the paper Gateway login only, never OAuth keys.
- `docs/knowledge/ibkr.md`: the second Gateway attempt from a cloud container, and its likely
  cause (the outbound address changes per connection).

## [0.3.0] - 2026-10-12

Session 3 — IBKR read path. Read-only: no order endpoint is touched.

### Added

- `ibkr/transport/`: one transport interface with two implementations chosen by
  `OBLISK_IBKR_TRANSPORT` — OAuth 1.0a (ported from lite: DH prepend, signed big integer, the
  live-session-token signature check, renegotiation before expiry) and the Client Portal Gateway
  (pinned archive, a per-install `localhost` certificate pinned by the client and the browser,
  a Java-version gate, a headless paper login with Playwright, re-login on session loss; refuses
  profile `live`).
- `ibkr/session.py`: the request pipeline with re-arm on 401 and on the lost `/accounts`
  precondition, re-authentication, no retry on 403, jittered backoff with `Retry-After`, the
  tickle keepalive, competing-session and session-loss detection with an alert hook.
- `ibkr/ratelimit.py`: sliding windows per endpoint group below IBKR's published caps, a global
  window, a concurrency cap, per-group pauses and circuit breakers.
- `ibkr/errors.py`: errors classified by body, each with its retry advice.
- `ibkr/client.py`: portfolio (accounts, positions, ledger with the NAV in base currency only,
  cash per currency), snapshots with the settle loop and declared-unit decoding, history with a
  probe-chunked walk, contract search with the declared-venue tie-break, contract info, strikes,
  futures ladders, bond filters, funds, trading rules, algos, the trading schedule and the
  scanner; `ibkr/identity.py` (conid, venue and currency pinned; ambiguity refused; corrections
  supersede); `ibkr/guard.py` (the account must be listed and, under paper, paper-prefixed).
  The endpoint groups refuse every read until the guard has passed; incomplete position and cash
  reads raise rather than return short; snapshot field ids follow IBKR's field table where
  lite's differed (DECISIONS D37, D41).
- IBKR's trading schedule as a calendar `ScheduleSource` (`ibkr/schedule_source.py`).
- `obliskiq preflight --read-only`; without credentials it prints "no credentials" and exits 2.
- `config/defaults/ibkr.yaml`, `config/defaults/http.yaml`, `paths.ibkr_gateway`,
  `OBLISK_IBKR_GATEWAY_JAVA_HOME`; `docs/knowledge/ibkr.md`; IBKR fixtures with a sidecar note.
- Dependencies: `cryptography`, `playwright` (1.56).

### Changed

- `RetryPolicy.from_settings` and a public `retry_after_seconds` in `util/http.py`.

## [0.2.0] - 2026-10-09

Session 2 — Store, calendars and numerics. No broker access yet.

### Added

- `domain/`: frozen, `extra="forbid"` base models with no NaN or infinity and UTC-only
  datetimes — `Instrument`, `Bar`, `Snapshot`, `ChainRow`, `MacroObservation`, `Venue`,
  `Phase`, `Incident`, `SessionObservation` — and the market-data-quality helper.
- `store/`: SQLite with WAL, foreign keys and a busy timeout; a forward-only migration runner
  that checksums each script, applies it atomically and refuses edited, missing,
  out-of-order or transaction-controlling migrations; `001_core.sql` with the final table
  shapes; repositories whose every read — `COUNT(*)` included — takes a required keyword
  `as_of`, with macro series aligned on release date; row mappers that raise on a stamp they
  cannot parse.
- `calendars/`: UTC interval algebra; library-backed, synthetic and uncovered schedules with
  seasonal closes and lunch overrides; the seven-phase session model with transitions; manual
  incidents that can block and never open, with a loader that fails closed; and the
  `Calendars` facade — `venue_status` (planning view), `tradability` and `tradable_windows`
  (act-now view), `common_windows` for options, `last_tradable_session`, `session_before`,
  `common_sessions_between`, `last_completed_session`, `coverage` — with a schedule
  cross-check that alerts on a disagreement and blocks only date-critical orders.
- Configuration: `config/defaults/venues.yaml` (27 venues, the IBKR exchange-code map,
  synthetic calendars, lunch breaks, seasonal closes, order offsets, incident grace),
  `store.yaml`, `numerics.yaml`, `smile.yaml`, `liquidity.yaml`, `costs.yaml`, and
  `config/manual-incidents.example.yaml`.
- `quant/`: a numpy-only Black–Scholes–Merton engine with admissibility checks, a convergence
  mask, OTM-twin implied-volatility solving and a vega guard; the smile hygiene and
  strike-smile helpers; descriptive statistics; Newey–West, Benjamini–Hochberg, Spearman,
  Brier and moving-block-bootstrap inference with every threshold a required argument; and
  a `Decimal` cost model — commission schedules per venue, asset class and currency
  (including the bond schedules), transaction taxes by issuer country, and the friction
  tiers — that reports `no_schedule` or `tax_unknown` rather than a guess.
- Tests: calendar cases for both daylight-saving changes, the Tokyo and Hong Kong lunch
  breaks, the Hong Kong half day, the US post-Thanksgiving half day, Good Friday and Easter
  Monday, synthetic venues, incidents, source disagreement and option expiry; a golden
  snapshot of 23 venues on nine dates; migration failure branches on file databases;
  point-in-time reads; Hypothesis properties (put-call parity, implied-volatility round trip,
  price monotone in volatility, Newey–West at least the independent standard error, interval
  algebra, store round trips); a guard against hardcoded trading times.
- Import contracts: `domain` is a leaf; `store`, `calendars` and `quant` are mutually
  independent; `calendars` never reaches `ibkr`, `httpx` or `store`.
- `docs/knowledge/calendars.md` and `docs/knowledge/costs.md`.

### Changed

- Dependencies: `numpy` and `exchange-calendars` (pandas only transitively); no scipy — the
  normal CDF is `0.5 * erfc(-x / sqrt(2))`.
- The unread-config-key guard reads keyed mappings (`KEYED_MAPPINGS`) and list records; the
  exact-leaf tests of Session 1 became "every leaf has a source"; `settings show` shortens
  long values.
- Tooling: Hypothesis profiles (`ci` is derandomised), a narrow warning filter for a numpy
  deprecation inside `exchange-calendars`, a per-file `ARG002` ignore for tests, and
  `.gitattributes` fixing LF endings for migrations.

### Fixed

- Behaviour that the legacy code got wrong and the port does not repeat, each recorded in
  `docs/DECISIONS.md` (D24–D32): a clock difference of wall times across a daylight-saving
  change; a manual-incident loader that dropped every manual incident after one typo; a commission
  estimate that never charged the per-share fee on equities; an open-interest gate that
  passed when the figure was missing; an unbounded `COUNT(*)` that saw rows from the future.

## [0.1.0] - 2026-10-09

Session 1 — Foundation. No trading logic yet.

### Added

- The design documents: `docs/MASTER_PLAN.md`, `docs/SESSION_PROMPTS.md` and `AGENTS.md`
  (loaded through `CLAUDE.md`), plus `docs/DECISIONS.md`, `docs/ROADMAP.md` and
  `docs/knowledge/README.md`.
- A read-only snapshot of the three legacy repositories under `legacy/` — ObliskIQ-lite-MCP
  @ `b1ebd49`, Inversor-Inteligente @ `68280e1` (the MASTER_PLAN §6.2 paths) and
  market-pulse-bot @ `900dbf4` — with `legacy/MANIFEST.md` listing every file's source,
  commit, licence, target and action. Legacy agent instructions are renamed so no tool
  loads them.
- The Python 3.14 scaffold: `pyproject.toml` (AGPL-3.0-or-later), `uv.lock`, the `obliskiq`
  package with one documented package per area of the plan, a `Makefile`
  (`setup`, `check`, `test`, `fmt`, `lint`, `types`, `imports`), ruff, `mypy --strict`,
  pytest with a coverage floor, pre-commit hooks and import contracts (the LLM layer cannot
  reach decision, execution or the broker; compute cannot reach the broker, httpx or
  execution; nothing imports `legacy/`).
- Foundation modules ported from ObliskIQ-lite-MCP with their tests: the error hierarchy,
  structured logging (JSON or text, UTC), tolerant numeric coercion, UTC time helpers and
  the HTTP retry loop.
- The environment loader: every credential and deployment location the plan names, with
  every key, token, account id and personal identifier held as a secret that never reaches
  a repr, a log line or an error message.
- The configuration system: expert defaults in `config/defaults/`, the owner's panel in
  `config/owner.yaml`, merged into one validated model that remembers where every value
  came from and fails with plain-language, file-and-key messages; the `es` jurisdiction
  profile and the policy registry that resolves a profile's policies; an append-only,
  hash-chained, lock-protected settings-change audit log.
- The command line: `obliskiq config validate` and `obliskiq settings show`, with
  `--config-dir` and `--env-file`.
- Guard tests: every config key has a consumer, no committed secrets or personal data, no
  magic numbers in decision paths, packaging, the environment template, the CHANGELOG
  section for the current version, the workflows and the legacy snapshot.
- CI (`ci`: `make check` on uv-managed Python 3.14), automatic releases from this file,
  weekly grouped Dependabot updates and the pull-request template.
