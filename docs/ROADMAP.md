# Roadmap

What is done and what is open. The design is `docs/MASTER_PLAN.md`; each session's task is
its section of `docs/SESSION_PROMPTS.md`; why things are as they are is `docs/DECISIONS.md`.

## Sessions

| # | Session | Outcome | Status | Release |
|---|---|---|---|---|
| 1 | Foundation | scaffold, docs, owner settings and defaults, jurisdiction registry, CI, releases, legacy snapshot | done | v0.1.0 |
| 2 | Store, calendars and numerics | store and migrations, calendar and phase layer, BSM, IV, smile, stats, inference | next | |
| 3 | IBKR read path | OAuth, session lifecycle, client, rate limits, quote status, contract identity, trading schedules, live preflight | planned | |
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
private ✓, default branch `main` ✓, AGPL-3.0 ✓. Not readable, for the owner to confirm:
merge commits and rebase merging disabled with squash kept ("Pull request title and
description"); automatic deletion of merged branches; Dependabot alerts and security
updates; Actions workflow permissions read-only; the optional ruleset requiring `ci`.

**Process.** The harness assigned the branch `claude/funny-keller-biu3b6` instead of
`s01-foundation`; GitHub steps of the end-of-session protocol ran through the GitHub tools
instead of `gh`. The new `.claude/agents/` definitions load from the next session; this
session's review ran the `reviewer` instructions through a general-purpose subagent.

## Live verification queue

Checks built against fixtures that still need a live reading. Each names the session that
settles it.

| Item | Added | Settled by |
|---|---|---|
| — | — | — |

## Follow-ups

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
