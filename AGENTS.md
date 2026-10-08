# AGENTS.md — ObliskIQ

A global, autonomous portfolio manager for one IBKR account: a core (risk balance plus a
tax-aware trend tilt), a satellite (the Asymmetry Hunter, defined risk) and a hard
decision, risk and execution layer. Python 3.14, Pydantic v2, IBKR Client Portal Web API
over OAuth 1.0a.

## Read first, in this order

1. `docs/MASTER_PLAN.md` — the design. Do not re-derive or relitigate it.
2. `docs/SESSION_PROMPTS.md` — your session's section is your task.
3. `docs/DECISIONS.md` — why things are as they are; what was rejected.
4. `docs/ROADMAP.md` — what is done, what is open, the session log.
5. `docs/knowledge/` — observed broker and data-source behaviour, tagged by route and date.

If the code contradicts the plan or your prompt, stop and report it in the PR description
before writing code around it.

## Hard rules (money and safety)

- Mandate: no leverage, no shorting, no written options, no futures positions, no
  leveraged or inverse ETPs; long vanilla options only. What may be bought is decided by
  the active jurisdiction profile; reading any instrument as data is always allowed.
- No negative cash balance in any currency. Trades in a foreign currency are funded by a
  spot conversion first.
- Orders are always limit-capped: patient limits, IBKR algos with a limit cap, stop-limit
  exits. Never a plain market order.
- The LLM never sizes, never builds or sends orders, never overrides a gate. `llm/` must
  not import `decision/` or `execution/` (import-linter contract).
- Money paths fail closed. Only whitelisted reply messages are confirmed. `o354` (no market
  data) is confirmed only if the asset class's quote policy accepts delayed quotes, a fresh
  delayed quote exists and the order is limit-capped inside the collar; otherwise cancel
  and alert. Unknown message → cancel and alert. Options need real-time quotes by default.
- Idempotency: the intent id (`uuid7`) is the `cOID`; look it up before any placement.
- Kill switch: cancels working entry orders and blocks new risk; keeps protective exits.
  `flatten` is a separate, explicit command.
- **IBKR credentials.** Cloud sessions may hold **paper** credentials (OAuth keys or the
  paper login the Gateway uses), never live ones, and never set profile `live`. With
  profile `paper`, every broker write is refused unless the account id has a paper prefix
  from config; profile `live` needs the promotion gate and an owner command (the guards
  live in code, not in prose). Live calls in a session are read-only, except the paper
  order tests a prompt names explicitly, at minimum size or within a managed-capital cap
  the prompt states. Once the deployed bot runs paper continuously, sessions make no IBKR
  calls unless the owner says it is paused (one brokerage session per username). Without
  credentials, build against fixtures and mark checks `VERIFY-LIVE(<what>)` in code and
  in `docs/ROADMAP.md` → "Live verification queue".
- Never print, log or commit secrets, account ids, tokens, `.env` files, keys or personal
  data (a test scans for them). Prompts sent to LLMs carry no account identifiers or
  balances.
- The kill switch and mode downgrades must work when everything else is failing.

## Engineering rules

- **Nothing hardcoded.** Values live in `config/` (expert defaults in `config/defaults/`,
  owner overrides in `config/owner.yaml`); jurisdiction and tax rules in
  `config/jurisdictions/<id>.yaml` plus policies registered in `jurisdiction/`; venues,
  currencies, schedules, budgets and thresholds are config. Every key has a consumer (test
  enforced); no magic numbers in decision paths (test enforced).
- **One owner per fact.** Config = values; code = mechanics; `DECISIONS.md` = why;
  `docs/knowledge/` = observed external behaviour; `MASTER_PLAN.md` = design.
- **Honest degradation.** Missing or immature input → explicit status and zero weight.
  Never fabricate, silently interpolate or substitute an unlabelled proxy. An untested gate
  is reported as untested, never as passed.
- **Point-in-time.** Every store read is bounded by `as_of`; macro aligns on release date.
- Pydantic v2 only; domain models frozen with `extra="forbid"`.
- `Decimal` at the order boundary; timezone-aware UTC datetimes; local time through
  `zoneinfo`; sessions through `exchange_calendars`. Never hardcode a trading time.
- asyncio for I/O; numpy vectorisation for compute; no blocking calls in the event loop;
  no mutable module globals.
- Port from `legacy/` (see `legacy/MANIFEST.md`), never from memory. Keep a legacy
  module's tests when porting it; adapt, don't delete, unless the plan drops the feature.
- Search before creating. Extend existing code rather than writing parallel versions.
- `mypy --strict` on `src/`. Small commits with conventional prefixes (`feat:`, `fix:`,
  `refactor:`, `test:`, `docs:`, `chore:`).
- Keep files tidy: documents state the current truth. No version banners or change
  narratives ("revised", "previously", "the new version") inside docs or code comments —
  history belongs in git, `CHANGELOG.md` and `DECISIONS.md`.

## Subagents

`.claude/agents/` defines the helpers; each sets its model and effort in its frontmatter
(defaults from `docs/SESSION_PROMPTS.md` §C.1). Use them; do not do their jobs inline:

- `reviewer` — the fresh reviewer of the end-of-session protocol. Never sees the work
  before the diff.
- `explorer` — read-only searches across `legacy/` and the repository; returns paths and
  conclusions, not file dumps.
- `port-checker` — compares a ported module and its tests with the legacy original and
  lists behaviour that changed without a `DECISIONS.md` entry.
- `test-breaker` — writes failure-branch and property tests for money paths it did not
  write.

## Testing rules

- `make check` = `ruff format --check`, `ruff check`, `mypy --strict src`, `lint-imports`,
  `pytest` with the coverage floor. Green before opening a PR and before merging.
- No network in tests: HTTP mocked with `respx`; fixtures in `tests/fixtures/` with a
  sidecar note of source and date. Live probes are separate commands, never tests.
- Money paths (sizing, gates, intents, orders, replies, reconciliation, currency
  conversion, cash flows, tax lots, modes, kill switch) need failure-branch tests, not only
  the happy path.
- The coverage floor only ratchets up.

## Session workflow

1. Read the files above. Branch `s<NN>-<short-slug>` (feature sessions: `f-<short-slug>`)
   from an up-to-date `main`.
2. Put your plan (5–15 bullets) at the top of the PR description. Do not wait for approval
   unless a stop condition applies.
3. Keep to your prompt's scope; anything else goes to "Follow-ups" in the PR and ROADMAP.
4. In the same PR update `docs/ROADMAP.md` (session log: landed, deferred, live findings,
   VERIFY-LIVE items), `docs/DECISIONS.md` (real decisions only), `CHANGELOG.md` (a section
   for the new version) and the version in `pyproject.toml` (minor per build session or
   feature, patch for fixes). Never create or push tags: the release workflow publishes
   the release when the version change reaches `main`.
5. A pre-registration is merged in its own PR (no version change) before any PR that
   tests it, and its lock file records the file's SHA-256 and that merge commit. Squash
   merges would otherwise put the hypothesis and its result in one commit.
6. You may correct later prompts in `docs/SESSION_PROMPTS.md` only to fix facts, paths or
   dependencies your work changed — never to add scope. Log each edit.
7. If you cannot finish everything with quality, merge a coherent, tested subset and insert
   `Session <N>a` after yours in `docs/SESSION_PROMPTS.md` with the remainder.

## End-of-session protocol (mandatory, performed by the agent)

1. **PR.** Push and open it with `gh pr create` using `.github/pull_request_template.md`.
2. **Check.** `gh pr checks <n> --watch`. Then review with the `reviewer` subagent, which
   has not seen the work: give it `gh pr diff <n>`, `AGENTS.md` and the session's acceptance
   criteria; ask for blocking and non-blocking findings on correctness, money-path safety,
   mandate and eligibility violations, hardcoded values, point-in-time leaks, failure-branch
   tests, cleanliness (dead code, debug prints, stray files, TODOs without an issue,
   commented-out code, version narratives, secrets) and docs consistency. Post the findings
   with `gh pr comment`. Tick the template checklist yourself.
3. **Fix.** Blocking findings or red CI → fix, push, wait for CI, re-review the delta. At
   most three rounds. Non-blocking findings are fixed or recorded as follow-ups.
4. **Merge.** Only with green CI and no blocking finding: `gh pr merge <n> --squash`; if the
   proxy refuses, `gh api -X PUT repos/{owner}/{repo}/pulls/<n>/merge -f merge_method=squash`.
   Never delete branches yourself (GitHub deletes merged heads). Then
   `git checkout main && git pull` and run `make check` on `main`; if it is red, open a fix
   PR at once under this same protocol.
5. **Release.** Confirm the release workflow run on `main` succeeded
   (`gh run list --workflow release.yml --limit 1`) and the release exists
   (`gh release view v<version>`). If it failed, fix it in a patch PR under this protocol.

Never merge with failing CI, unresolved blocking findings, or an unchecked money-path box.
After round three with blockers left: leave the PR open, label it `needs-owner`, and
summarise why.

## Stop conditions (stop, report in the PR, do not improvise)

- The plan or prompt contradicts the code or a recorded decision.
- A step needs live credentials, an account action, a paid service or an irreversible change.
- A test can only pass by weakening a safety rule.
- A broker or vendor behaviour is unknown and guessing would affect a money path.

## Commands

```
make setup     # uv sync (uv-managed Python 3.14), pre-commit hooks
make check     # format, lint, types, import contracts, tests, coverage
make test      # pytest only
uv run obliskiq --help
```

## Layout

`src/obliskiq/` per `docs/MASTER_PLAN.md` §8 · `config/` · `tests/` · `research/` ·
`docs/` · `deploy/` · `legacy/` (temporary, read-only, excluded from all tooling; deleted
in Session 20).
