# Changelog

All notable changes to ObliskIQ are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every version below 1.0.0 is a
pre-release; the release workflow publishes each version's section as its release notes.

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
