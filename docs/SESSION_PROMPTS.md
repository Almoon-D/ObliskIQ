# ObliskIQ — Session Prompts

Committed by Session 1 as `docs/SESSION_PROMPTS.md`. Build sessions 1–20 (with 4a, 4b and
19a; there is no Session 14), research sessions 4c, 4d, 4e and 21, and the owner's T1, run in
order, one PR each (a pre-registration in a PR of its own), merged and released by the agent
itself (protocol in `AGENTS.md`). The design lives in `docs/MASTER_PLAN.md`; this file says
what each session does. Section F holds the templates for feature and maintenance sessions
after the build.

---

## A. One-time setup (owner, before Session 1)

### A.1 Repository

`Almoon-D/ObliskIQ` exists: private, `main`, AGPL-3.0 `LICENSE`, Python `.gitignore`,
merged branches deleted automatically. Change:

1. **Settings → General → Pull Requests:** untick "Allow merge commits" and "Allow rebase
   merging"; keep "Allow squash merging" with the default message "Pull request title and
   description". One commit per session on `main`, and release notes that read well.
2. **Settings → Code security:** enable Dependabot alerts and Dependabot security updates.
   Weekly version updates come from `.github/dependabot.yml` (Session 1).
3. **Settings → Actions → General:** leave "Workflow permissions" read-only. The release
   workflow asks for the write permissions it needs in its own file.
4. **Rulesets (optional, needs GitHub Pro on a private repository):** a rule on `main`
   requiring the `ci` status check. Do not require approving reviews: the agent cannot
   approve its own PR, so every session would stop at the merge.

No GitHub secrets: CI runs offline and releases use the built-in `GITHUB_TOKEN`. The free
plan's 2,000 Actions minutes a month cover CI comfortably. Session 1 creates the labels it
uses.

### A.2 IBKR (in the IBKR portal)

IBKR answered (2026-10-08) that OAuth 1.0a is for institutional clients and that retail
accounts reach the Web API through the Client Portal Gateway. The plan therefore supports
both (MASTER_PLAN §5.12): `gateway` for paper, `oauth` for the live account where IBKR grants
it (DECISIONS D42).

1. **Paper account:** use the paper account of your **main** username (Settings → Paper
   Trading Account). Note its paper username and account id (it starts with `DU`). No
   separate API username is needed for paper: the paper login is a different username
   from your live login, so your own sessions never take over the bot's.
2. **Market data sharing:** in Settings → Paper Trading Account, enable sharing of your
   live market data with the paper account. Subscriptions stay yours, for your own use as
   well, and are not billed twice. Subscribe on the main username: **OPRA L1** (USD 1.50 a
   month, waived at USD 20 of monthly commissions) and **Eurex Core L1** (waived). US stock
   and ETF streaming from Cboe One and IEX is complimentary; everything else is traded on
   delayed quotes with the safeguards in MASTER_PLAN §5.13. Later, only if the scoreboard
   earns it: LSE L1 (GBP 1.00), Bolsa de Madrid Plus L1 (EUR 7.00), Xetra L1 (EUR 16.25),
   Euronext Cash L1 (EUR 89.10), the US network bundles.
3. **Paper runs use the Gateway.** OAuth keys are for the live account only (DECISIONS
   D42): no paper OAuth keys are generated or stored, and a paper run never reads any.
4. Trading permissions for the asset classes and regions the bot may reach (stocks,
   options, bonds, mutual funds; US, Europe, UK, Asia-Pacific) on the account; the paper
   account mirrors them.
5. Sign the W-8BEN if IBKR asks (US dividend withholding).

### A.3 Claude Code environment

Create a **new** environment named `ObliskIQ` (claude.ai/code → environment settings).
Your existing environment holds the credentials ObliskIQ-lite-MCP used, most likely for
the live account: keep it for lite and never select it for ObliskIQ sessions. Everything
in an environment is readable by the session's code, which is why this one holds paper
credentials and capped keys only.

**Network access:** `Custom`, with "include defaults" ticked and the domains in §D.
Switch to `Full` only for Sessions 3, 4b, 4c, 4d, 4e, 9, 10 and 21 (the Gateway download and
login, issuer sites, central-bank and regulator feeds, long research histories), then
back to `Custom`.

**Environment variables** (paste, then fill the placeholders):

```
FRED_API_KEY=<your FRED key>
EIA_API_KEY=<your EIA key>
SEC_USER_AGENT=ObliskIQ research <your email>
OBLISK_IBKR_PROFILE=paper
OBLISK_IBKR_ACCOUNT_ID=<paper account id, starts with DU>
OBLISK_IBKR_TRANSPORT=gateway
OBLISK_IBKR_GATEWAY_USERNAME=<paper username>
OBLISK_IBKR_GATEWAY_PASSWORD=<paper password>
```

OAuth variables and key files are never set in this environment: they are the live
account's (DECISIONS D42), and the `oauth` transport refuses any profile but `live`.

Optional, added when the session that uses them comes up:

```
TELEGRAM_BOT_TOKEN=<a bot made for ObliskIQ with @BotFather>    # Sessions 6 and 19
TELEGRAM_CHAT_ID=<your chat id with that bot>                   # Sessions 6 and 19
GEMINI_API_KEY=<free-tier key>                                  # Session 16
<PROVIDER>_API_KEY=<the paid provider you choose, with a spend cap set at the provider>  # Session 16
```

The LLM key names are whatever `config/llm.yaml` declares (Session 16), so adding a
provider later is a YAML entry plus one variable. Not used: `ESTAT_APP_ID` (e-Stat has no
per-observation release dates) and `MARKETDATA_API_TOKEN` (marketdata.app allows one
device per account, which cloud sessions cannot hold). No GitHub token: sessions use the
built-in GitHub access. The Gateway's local address is configuration. Session 3 settled that
the Gateway installs and runs inside a cloud session but its login did not complete there (most
likely because the session's outbound address changes from one connection to the next;
`docs/knowledge/ibkr.md`),
so sessions build against fixtures and the live checks run on the machine.

Without paper credentials, leave the IBKR variables out: sessions then build against
fixtures and queue live checks.

### A.4 Files for Session 1

Attach `MASTER_PLAN.md`, `AGENTS.md` and this file. Start the session on `ObliskIQ` and,
if the session picker allows several repositories, also select `Inversor-Inteligente` and
`ObliskIQ-lite-MCP`; otherwise download their ZIPs (GitHub → Code → Download ZIP) and
attach them. `market-pulse-bot` is public; the session clones it. After Session 1 merges,
archive `Inversor-Inteligente` on GitHub (Settings → Archive this repository). Keep
ObliskIQ-lite-MCP as it is.

The TWS API 10.50/10.51 download is not needed: that is the TWS socket API and IB
Gateway, a different product from the Client Portal Gateway. Session 3 downloads the
Client Portal Gateway itself. The TWS API with IBC is the fallback only if T1 fails (§A.5,
DECISIONS D68).

### A.5 The deployment machine (owner, T1)

Run this beside Sessions 4b–4d and finish it before Session 6. It proves what a cloud
session cannot: that the paper Gateway logs in unattended, and what IBKR's order endpoints
really answer (DECISIONS D68).

1. A small always-on Linux machine with a stable outbound address (a home machine or a VPS;
   MASTER_PLAN §5.20). Install git, uv and Java 11; clone `Almoon-D/ObliskIQ` at the latest
   release; `make setup`; `uv run playwright install chromium`.
2. Put the paper variables of §A.3 in the machine's environment (never OAuth keys) and set
   `OBLISK_IBKR_GATEWAY_JAVA_HOME` to the Java 11 home.
3. Day 1: `uv run obliskiq preflight --read-only`. It installs the Gateway, logs in
   unattended and walks the read path. Keep the masked report.
4. Day 2, after IBKR's daily reset: run the same command again. A fresh unattended login
   after a reset is the second thing T1 proves; the in-process re-login is Session 6's.
5. `uv run obliskiq mode set paper --reason "T1 paper order test"`, then
   `uv run obliskiq preflight --paper-order-test` (one order per configured instrument that
   cannot fill, read and cancelled), then `uv run obliskiq mode set shadow --reason "T1 done"`.
6. Paste the masked reports into a short session: *"Record T1's findings per SESSION_PROMPTS
   §A.5."* It records the shapes in `docs/knowledge/`, closes or re-dates the matching rows
   of the ROADMAP's live verification queue, and bumps the patch version.

T1 passes when both logins complete unattended and both preflights finish. If the
unattended login cannot complete on the machine, stop and choose (MASTER_PLAN §11): a spike
of the TWS API with IBC, or a daily manual login. From T1 on, a session's live step is a
command list the session writes for you to run on the machine, with the bot paused; you
paste the masked output into a short follow-up session that records it.

---

## B. Running a session

1. New cloud session on `ObliskIQ` with the `ObliskIQ` environment and the network from
   §C.
2. Pick the model, then `/effort <level>` from §C. Opus 5.5 and Sonnet 5.5 default to
   `medium`, so set it explicitly.
3. Start in auto (or accept-edits) permission mode, not plan mode: the prompt needs no
   approval, and plan mode echoes the whole plan back into the session's context. Paste the
   prompt. From Session 2 on, the one-line launcher is enough: the agent prints its section
   from `main` with `make prompt S=<N>`, including corrections earlier sessions made.
4. The agent opens the PR, reviews its own diff against the prompt, fixes, merges, confirms
   the release and logs the session in `docs/ROADMAP.md`. Read that entry and the release
   notes before the next session.
5. A PR left open with the `needs-owner` label: answer its summary, then run a short
   session: *"Resolve PR #n per its needs-owner summary, then follow the end-of-session
   protocol."* Any other request after the merge (a follow-up, a fix) also goes to a fresh
   session: a long session pays its whole context again on every turn.
6. One brokerage session per username. If you run the bot on a machine between sessions
   (possible from Session 6; the machine itself from T1), stop it before any session whose §C row has a live IBKR
   check and restart it afterwards. Once you deploy for good (§E), sessions skip their
   live checks.
7. Sessions 4, 4a, 4b (the tax engine), 5, 13 and 17 touch money most directly. If one of them runs short of
   context or its review leaves too many findings, the agent merges a coherent, tested
   subset and writes `Session <N>a` for the rest (AGENTS.md). Let it; never ask it to
   rush.

## C. Session table

| # | Session | Model | Effort | Network | Live IBKR (paper) |
|---|---|---|---|---|---|
| 1 | Foundation | Opus 5.5 | xhigh | Custom | — |
| 2 | Store, calendars and numerics | Sonnet 5.5 | xhigh | Custom | — |
| 3 | IBKR read path | Opus 5.5 | xhigh | **Full** | Gateway trial, read-only preflight |
| 4 | Order path, kill switch and modes | Fable 5.1 (else Opus 5.5) | xhigh | Custom | — |
| 4a | Pre-trade rails | Fable 5.1 (else Opus 5.5) | xhigh | Custom | one order test |
| T1 | Deployment machine (owner, §A.5) | — | — | — | unattended logins, preflight, paper order test |
| 4b | Research harness | Fable 5.1 (else Opus 5.5) | xhigh | **Full** | — |
| 4c | Core verdict | Opus 5.5 | xhigh | **Full** | — |
| 4d | First selection hypothesis | Opus 5.5 | xhigh | **Full** | — |
| 4e | Total-risk scaling | Opus 5.5 | xhigh | **Full** | — |
| 5 | Execution mechanics | Fable 5.1 (else Opus 5.5) | xhigh | Custom | conversion and stop-limit tests |
| 6 | Walking skeleton | Opus 5.5 | xhigh | Custom | one supervised cycle |
| 7 | Capital, cash flows and tax | Opus 5.5 | xhigh | Custom | read-only |
| 8 | Universe and market-data collectors | Sonnet 5.5 | xhigh | Custom | conid pinning, snapshots |
| 9 | Broker capabilities and vehicle selector | Opus 5.5 | xhigh | **Full** | capabilities (whatif only) |
| 10 | Macro, positioning, insiders and events | Sonnet 5.5 | xhigh | **Full** | — |
| 11 | Measurement store and views | Sonnet 5.5 | xhigh | Custom | — |
| 12 | Engines and gates | Opus 5.5 | xhigh | Custom | — |
| 13 | Core engine | Fable 5.1 (else Opus 5.5) | xhigh | Custom | — |
| 15 | Satellite engine | Opus 5.5 | xhigh | Custom | — |
| 16 | LLM layer | Opus 5.5 | xhigh | Custom | — |
| 17 | Decision layer | Fable 5.1 (else Opus 5.5) | xhigh | Custom | — |
| 18 | Scoreboard, patterns, capital and promotion | Opus 5.5 | xhigh | Custom | — |
| 19 | Cockpit, settings and deployment | Sonnet 5.5 | xhigh | Custom | full preflight |
| 19a | Capacity and scale | Opus 5.5 | xhigh | Custom | bond and fund capabilities (whatif only) |
| 20 | Red-team and cleanup | Fable 5.1 (else Opus 5.5) | xhigh | Custom | — |
| 21 | Research: second batch | Opus 5.5 | xhigh | **Full** | — |

The paper Gateway login does not complete from a cloud container (docs/knowledge/ibkr.md):
until T1 succeeds, the live steps in this column are queued `VERIFY-LIVE`; from T1 on, each
is a command list the session writes for the owner to run on the machine (§A.5). There is
no Session 14: the core's backtest and verdict are Session 4c (DECISIONS D61).

Only Session 1 needs attachments. Fable goes where a subtle mistake costs money; Opus on
architecture and statistics; Sonnet on well-specified ports. Every session runs one
effort level above what its work strictly needs, capped at `xhigh`; `max` is not used.

## D. Custom network allowlist

```
api.ibkr.com
api.stlouisfed.org
data-api.ecb.europa.eu
www.stat-search.boj.or.jp
stats.bis.org
www.federalreserve.gov
publicreporting.cftc.gov
www.cftc.gov
api.eia.gov
www.ons.gov.uk
www150.statcan.gc.ca
data.sec.gov
www.sec.gov
efts.sec.gov
gamma-api.polymarket.com
api.elections.kalshi.com
api.gdeltproject.org
www.nasdaqtrader.com
www.ishares.com
query1.finance.yahoo.com
query2.finance.yahoo.com
fc.yahoo.com
mba.tuck.dartmouth.edu
www.aqr.com
cdn.cboe.com
www.justetf.com
raw.githubusercontent.com
api.telegram.org
generativelanguage.googleapis.com
```

Before Session 16, add the API domain of the paid LLM provider you choose.

---

## Common header (inside every prompt)

> You are building ObliskIQ, session N. `AGENTS.md` is your operating manual and
> `docs/MASTER_PLAN.md` is the design — follow them; do not re-derive or relitigate them.
> Port from `legacy/` (see `legacy/MANIFEST.md`), never from memory. Nothing hardcoded:
> when you build the consumer of a block of lite's `config.yaml`, port the keys you
> consume into `config/defaults/` with lite's values (a different value needs a
> `DECISIONS.md` entry). IBKR: paper credentials only, under the rules in `AGENTS.md`;
> without them, fixtures and `VERIFY-LIVE(...)`. Finish with the end-of-session protocol
> in `AGENTS.md`, release check included.

---

## Session 1 — Foundation

**Launcher:** none — paste the full prompt and attach the three files (and the legacy
repositories or ZIPs).

```
You are building ObliskIQ, session 1. Three files are attached: MASTER_PLAN.md (the
design), AGENTS.md (your operating manual), SESSION_PROMPTS.md (all session prompts).
Read all three completely first. AGENTS.md is not in the repository yet, so follow the
attached copy; from Session 2 on it loads automatically.

The repository already contains LICENSE (AGPL-3.0, keep it), a Python .gitignore (extend
it) and a short README (replace it).

Legacy repositories (attached, as ZIPs, or cloned where reachable):
- Almoon-D/ObliskIQ-lite-MCP (AGPL-3.0) — expected commit b1ebd49
- Almoon-D/Inversor-Inteligente (Apache-2.0) — expected commit 68280e1
- Almoon-D/market-pulse-bot (AGPL-3.0, public:
  git clone https://github.com/Almoon-D/market-pulse-bot) — expected commit 900dbf4
If one is unreachable, STOP and say which. If a commit differs, proceed and record the
actual SHA.

GOAL: a clean, typed, tested Python 3.14 scaffold; the docs; the configuration system
(expert defaults, owner settings, jurisdiction profiles); CI; automatic releases; a
curated legacy snapshot. No trading logic.

DELIVERABLES
1. Docs verbatim from the attachments: docs/MASTER_PLAN.md, AGENTS.md (root),
   docs/SESSION_PROMPTS.md. CLAUDE.md containing exactly one line: @AGENTS.md
2. Legacy snapshot under legacy/ (read-only; excluded from ruff, mypy, pytest collection,
   coverage, import-linter and packaging):
   - legacy/lite/: the whole lite repository except .git, docs/NEXT-SESSIONS.md and
     docs/archive/; its ROADMAP.md kept as ROADMAP.legacy.md (live measurements).
   - legacy/old/: every path in MASTER_PLAN §6.2 (port, reference and knowledge rows),
     including ObliskIQ/modules/analytics/pattern_library.py, plus the tests and fixtures
     they need: ObliskIQ/tests/llm_fakes.py, ObliskIQ/tests/fixtures/cot_annual_sample.csv,
     ObliskIQ/tests/fixtures/phase4_responses/, and ObliskIQ/tests/test_{asymmetry_hunter,
     asymmetry_risk_rails,stat_utils_inference,ai_scoreboard,scoreboard_statistics,
     forecast_panel,analyst_router,ai_providers,llm_analyst,degraded_provider_policy,
     cftc_cot_history,gdelt_client,polymarket_client,continuity_guard,decision_contract,
     paper_gate,kill_switch_wiring,monitoring_suite,transaction_costs,crisis_database,
     run_summary,walk_forward_backtester,opportunistic_optimizer,crisis_sizing,
     rss_sources,vader_financial_lexicon}.py.
     Keep original paths. Do NOT copy anything MASTER_PLAN marks "not ported".
   - legacy/pulse/: the whole market-pulse-bot repository except .git and deploy/.
   - Neutralise legacy agent instructions: rename every CLAUDE.md and AGENTS.md inside
     legacy/ to *.legacy.md, and every .claude/ directory to _claude/.
   - legacy/MANIFEST.md: per file — source repository, SHA, original path, licence,
     target package, action (port / reference / knowledge / registry). Keep each legacy
     LICENSE beside its tree.
3. Scaffold: pyproject.toml (name "obliskiq", version 0.1.0, AGPL-3.0-or-later,
   requires-python ">=3.14,<3.15", src layout, script `obliskiq`, py.typed); uv.lock;
   .python-version (3.14); Makefile (setup, check, test, fmt, lint, types, imports);
   ruff; mypy --strict on src/; pytest (asyncio auto; coverage floor = measured − 1,
   ratchet only); pre-commit; .claude/agents/{reviewer,test-breaker,port-checker,
   explorer}.md per SESSION_PROMPTS §C.1 (model and effort in frontmatter); import-linter
   contracts (llm ⟂ decision, execution; quant,
   grid, engines ⟂ ibkr, httpx, execution; nothing imports legacy). src/obliskiq/ with
   the packages of MASTER_PLAN §8, each __init__.py docstring stating what it owns — no
   stubs, no placeholder logic.
4. Foundation modules ported from legacy/lite with their tests: errors.py,
   logging_setup.py (json|text), util/ (numeric, timeutil, http helpers), env.py
   (pydantic-settings) reading exactly these names: FRED_API_KEY, EIA_API_KEY,
   SEC_USER_AGENT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OBLISK_IBKR_PROFILE (paper|live),
   OBLISK_IBKR_ACCOUNT_ID, OBLISK_IBKR_TRANSPORT (oauth|gateway),
   OBLISK_IBKR_GATEWAY_USERNAME, OBLISK_IBKR_GATEWAY_PASSWORD, OBLISK_IBKR_CP_BASE_URL,
   OBLISK_IBKR_CONSUMER_KEY, OBLISK_IBKR_ACCESS_TOKEN, OBLISK_IBKR_ACCESS_TOKEN_SECRET,
   OBLISK_IBKR_SIGNATURE_KEY, OBLISK_IBKR_ENCRYPTION_KEY, OBLISK_IBKR_DH_PARAM, plus
   OBLISK_ paths and logging. LLM keys are not declared here: Session 16 reads them by
   the names config/llm.yaml declares; the OAuth realm is config (Session 3). Secret
   values are SecretStr and never appear in repr, logs or errors (test).
5. Configuration system (config package, Pydantic v2, fail-loud):
   - config/defaults/*.yaml (expert defaults) + config/owner.yaml (the owner's panel:
     plain-language comments, overrides the defaults) merged into one validated model.
   - config/jurisdictions/es.yaml (id and name only for now) and, in the jurisdiction
     package, a policy registry that resolves a profile's policy ids when a policy is
     requested; an unknown or missing id fails loudly. Later sessions add the keys they
     consume: the eligibility policy id (Session 4, then `eu_priips` in Session 8), the
     tax policy id `es_irpf` and its data (Session 7).
   - Keys added only with a consumer. Now: account.base_currency, jurisdiction, paths,
     logging. `obliskiq config validate` and `obliskiq settings show`.
   - An append-only settings-change audit log, wired now, used later.
6. Guard tests ported or adapted: test_no_unread_config_keys, test_no_committed_secrets,
   test_no_magic_numbers (wired, scoped to decision paths), test_packaging; a config-load
   smoke test; a test that CHANGELOG.md has a section for the version in pyproject.toml.
7. CI (.github/workflows/ci.yml): one required job `ci` on Python 3.14 (uv-managed)
   running make check; superseded runs cancelled.
8. Releases (.github/workflows/release.yml): on push to main, read the version from
   pyproject.toml; if tag v<version> does not exist, create it on that commit and publish
   a GitHub Release whose notes are that version's CHANGELOG section; mark it a
   pre-release below 1.0.0; fail loudly if the section is missing; idempotent on re-runs;
   `permissions: contents: write` in this workflow only. Session 19 adds the Docker image.
9. .github/dependabot.yml: ecosystems `uv` and `github-actions`, weekly, minor and patch
   updates grouped, label `dependencies`. Labels `needs-owner` and `dependencies` created
   with gh. Read the repository settings with gh and list in the PR any that differ from
   SESSION_PROMPTS §A.1 (report them; do not change settings).
10. .github/pull_request_template.md: Plan · Changes · Tests · Live findings · Risks ·
    Money-path checklist (fails closed; failure-branch tests; no secrets; no hardcoded
    values; config keys have consumers; point-in-time reads; eligibility respected; paper
    guard intact) · Follow-ups · VERIFY-LIVE.
11. docs/DECISIONS.md seeded with short entries (3–6 lines, evidence cited) for MASTER_PLAN
    §2, §4, §5.2, §6.4, §7 and §11. docs/ROADMAP.md: session table, "Session log", "Live
    verification queue", "Follow-ups". CHANGELOG.md (Keep a Changelog format, a 0.1.0
    section); README.md (one screen: what it is, pre-alpha, not trading yet);
    .gitignore additions (/data/, *.db*, .env, .env.*, !.env.example, secrets/, *.pem —
    anchored so the src/obliskiq/data package stays tracked); .env.example (names only);
    docs/knowledge/README.md.

ACCEPTANCE
- make check green locally and in CI.
- `obliskiq config validate` and `settings show` work; an invalid owner.yaml fails loudly
  with a readable message.
- legacy/ complete per MANIFEST (spot-check 10 paths across the three trees), excluded
  everywhere, imported by nothing; no CLAUDE.md, AGENTS.md or .claude/ remains under its
  original name inside legacy/.
- After the merge, release v0.1.0 exists as a pre-release with the CHANGELOG notes.
- ROADMAP has the Session 1 log entry; DECISIONS has the seeded entries.

OUT OF SCOPE: store, IBKR, data, engines, trading logic.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 2 — Store, calendars and numerics

**Launcher:** `Execute Session 2 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 2. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §3.3, §5.7, §5.12 "Calendars and phases", §6.3, §7, §8, §9). Port from
legacy/, never from memory. Nothing hardcoded. Finish with the end-of-session protocol.

PRECONDITION: Session 1 merged. Otherwise STOP.

GOAL: the persistent store, the calendar and phase layer, domain base models and the
numerical core.

READ: legacy/lite/src/obliskiq/store/* (including store/migrations/*.sql),
legacy/lite/src/obliskiq/quant/bsm.py, legacy/lite/src/obliskiq/compute/{iv_solve,smile,
stats,calendar}.py, legacy/lite/calendars.yaml, legacy/lite/docs/COMPUTE.md ("Reading two
numbers off one smile", "Three realised-vol estimators…");
legacy/pulse/src/market_pulse_bot/{calendar_engine,market_engine,config,halt_detector}.py,
config/exchanges.yaml, data/manual_incidents.yaml.example and tests/test_{market_engine,
incidents,config}.py; legacy/old/ObliskIQ/modules/analytics/{stat_utils,
transaction_costs}.py and their tests.

DELIVERABLES
1. store/: SQLite (WAL, foreign keys, busy timeout); forward-only migration runner with
   checksums that refuses edited migrations; a fresh chain from 001_core.sql with the
   final shape (as of lite migration 019) of: instruments (conid, listing exchange,
   currency, asset_class, domicile, ucits, kid_status, share_class, lot_size,
   option_exchange, history_exchange, roles, superseded_by), bars, snapshots
   (market_data_status realtime|delayed|frozen|none, quote_delay_seconds, implied_vol and
   implied_vol_solved in separate columns), chain_snapshots, curves, macro
   (reference_date, release_date, units, source), runs, run_steps. Repositories with
   as_of-bounded reads (COUNT(*) included). Port test_store, test_migration_chain,
   test_point_in_time.
2. calendars/ (MASTER_PLAN §5.12):
   - schedule.py: the market-pulse-bot schedule abstraction — exchange_calendars-backed
     schedules plus synthetic schedules for venues the library lacks, seasonal closing
     times, half-days, lunch breaks.
   - phases.py: the phase model (pre-market, opening auction, regular, lunch break,
     closing auction, post-market, closed) with transitions and next-event times.
   - incidents.py: the incident store with manual YAML entries (source, scope venue |
     instrument, start, expected end). Conservative: an incident can block, never open.
     Feed-driven entries arrive in Session 10.
   - config/defaults/venues.yaml: the 26 venues of exchanges.yaml (MIC, time zone, phase
     windows, seasonal closes, synthetic calendars, incident sources), plus a mapping from
     IBKR exchange codes for listing and option venues (e.g. IBIS, BM, AEB, SBF, LSE, BVME,
     EBS, TSEJ, SEHK, ASX, TSE, NYSE, NASDAQ, ARCA, EUREX, MEFFRV, MONEP, CBOE) to a
     venue profile, and the order offsets after the open and before the close. Every
     value carries a source; IBKR codes are VERIFY-LIVE until Session 8 pins conids.
   - venue_status(venue, date) = ok | blocked(reason), from the schedule, phases and
     incidents. Further ScheduleSources (Session 3 registers IBKR's trading schedule) are
     cross-checks: a disagreement alerts and is logged, and blocks only date-critical
     orders.
   - last_tradable_session(instrument venue, underlying venue, expiry): the last session
     on or before expiry on which both venues are open (a holiday expiry, or a Monday
     expiry after a Friday holiday, moves it earlier); with an unresolved disagreement,
     the earlier date wins.
   - API: session bounds local and UTC, is_open, phase_at, next open and close,
     tradable_window(venue, date) = the regular phase inside the configured offsets,
     minus lunch breaks, auctions and incidents.
   - Tests: the EU DST change (2026-10-25) and the US one (2026-11-01); the XTKS (IBKR
     TSEJ) and XHKG (IBKR SEHK) lunch breaks; a US half-day (2026-11-27); a holiday; a
     synthetic-calendar venue; a manual incident blocking a venue; a source disagreement
     → alert without blocking ordinary orders; an option expiring on a venue holiday and
     a Monday expiry after a Friday holiday → the earlier last tradable session. A test greps the package for hardcoded trading times. Port the
     market-pulse-bot tests that still apply.
3. domain/ base models (Pydantic v2, frozen, extra=forbid): Instrument, Bar, Snapshot,
   ChainRow, MacroObservation, Venue, Phase, Incident, plus store mappers.
4. quant/: port bsm.py as is (admissibility checks, OTM-side solving, vega guard),
   smile.py, stats.py, the pure solver parts of iv_solve.py, stat_utils.py →
   quant/inference.py, transaction_costs.py → quant/costs.py (commission schedules per
   venue and asset class, including the bond schedules in MASTER_PLAN §5.3, and per-venue
   transaction taxes, as config with a source per value, VERIFY-LIVE). Port lite's
   `numerics` keys you consume. Port their tests. Add hypothesis property tests:
   put-call parity, IV round trip over a grid of (S, K, T, r, q, σ), price monotone in σ,
   Newey–West SE ≥ the iid floor.

ACCEPTANCE: ported tests pass in intent (behavioural changes logged in DECISIONS); the
calendar tests above pass; coverage floor raised.
OUT OF SCOPE: collectors, IBKR, grid indicators, incident feeds.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 3 — IBKR read path

**Launcher:** `Execute Session 3 exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

```
You are building ObliskIQ, session 3. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §5.12, §5.13, §5.18, §5.20). Port from legacy/, never from memory.
Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–2 merged. Otherwise STOP.

GOAL: a robust, rate-limited, typed read client for the IBKR Client Portal Web API over
two transports — OAuth 1.0a and the Client Portal Gateway with automated paper login —
carrying over everything lite verified live, and verified live again on the paper account
when credentials are present.

READ COMPLETELY: legacy/lite/references/ibkr-playbook.md, legacy/lite/src/obliskiq/ibkr/*.py,
legacy/lite/docs/DECISIONS.md sections "What the first live Client Portal run settled",
"`HTTP 500` for "no data"…", "A 401 is repaired, a 403 is not", "The declared listing venue
now breaks the tie…", "Why the /accounts precondition is repaired on the failure…"; tests
test_oauth*.py, test_cp_client.py, test_ibkr_field_units.py, test_hostile_input.py,
test_collect_conid_supersede.py. Port lite's `ibkr` and `http` keys you consume.
REFERENCE (read, do not depend on it): IBeam (Voyz/ibeam, Apache-2.0) for the Gateway's
automated login, and IBKR's Client Portal Gateway documentation.

DELIVERABLES
1. ibkr/transport/: one interface, two implementations selected by OBLISK_IBKR_TRANSPORT.
   `oauth`: OAuth 1.0a signing; live-session-token negotiation (DH prepend, signed
   big-integer handling, LST signature verification never skipped); refresh before
   expiry. `gateway`: the Client Portal Gateway (download, pinned version and checksum in
   config; local address in config; its self-signed certificate pinned, never
   verification disabled) logged in by a headless browser with the paper username and
   password; re-login on session loss; refuses to run with profile live (a live login
   needs the owner's second factor). Above both, ibkr/session.py:
   /iserver/auth/ssodh/init; /tickle keepalive (interval from
   config); re-arm on 401; no retry on 403; /iserver/accounts precondition repaired on
   failure; detection of a competing session and of daily broker resets, with automatic
   re-authentication and an alert hook. Profiles paper|live.
2. Account guard: the account id in use must appear in /portfolio/accounts and, when the
   profile is paper, carry a paper prefix from config (default ["DU"]); otherwise refuse
   to start. Account ids never reach logs (masked).
3. ibkr/client.py by endpoint group: portfolio (accounts, paginated positions, ledger and
   NAV in base currency only, cash balances per currency); marketdata (snapshot with the
   settle loop that waits for a PRICE field; decoding with declared units and plausibility
   guards; availability decoded into realtime|delayed|frozen|none plus delay seconds);
   history (probe-chunked; never assumes len == requested); secdef (search with exact
   ticker scan and declared-venue tie-break, info, strikes, futures ladder, bond and fund
   search where the API supports them); contract rules (tick sizes, lot size, order types,
   cash-quantity types), /iserver/contract/{conid}/algos, the market scanner (parameters
   and run), and the contract's trading schedule.
4. The trading schedule registered as the third ScheduleSource of Session 2's agreement
   check: a synchronous, cache-backed source (refreshed asynchronously here; `observe()`
   only reads; None means no information, not closed) (endpoint and format recorded in
   docs/knowledge/ibkr.md; VERIFY-LIVE until seen).
5. ibkr/ratelimit.py: token buckets per endpoint group from config; history ≤ 10/s and
   ≤ 50/min; snapshot ≤ 100 conids and ≤ 50 fields per call (IBKR Web API changelog,
   2026-09-09 and 2025-12-10); global concurrency cap; Retry-After honoured; jittered
   backoff; per-group circuit breaker.
6. ibkr/errors.py: Auth, SessionExpired, Competing, RateLimited, NoDataPermanent (decided
   by body), Upstream, Decode — each with a retry policy.
7. Contract identity: conid + listing exchange + currency pinned; ambiguous resolution
   refused; corrections supersede, never delete.
8. docs/knowledge/ibkr.md: the condensed playbook keeping every verified finding with its
   route tag (CP/MCP/CONNECTOR) and date, plus the changelog facts above, the deprecation
   of API fundamental tags (2026-01-06), per-user market-data billing and live→paper
   sharing, with source links.
9. CLI `obliskiq preflight --read-only` (auth → accounts → guard → positions → cash →
   snapshot with availability → history → contract rules, algos and trading schedule for
   one stock). No credentials → prints "no credentials" and exits 2.
10. LIVE (if paper credentials are present): first try to run the Gateway inside this
    session (Java and a headless browser); record whether it works. Then run the
    read-only preflight, then snapshots
    for one US stock (complimentary feed), one European ETF (expected delayed), one US
    option (OPRA) and one Eurex option (Eurex Core), recording availability and delay for
    each, with you logged in live elsewhere and not (does the shared market data reach
    the paper session in both cases?); one scanner run. Record every observation in
    docs/knowledge/ibkr.md as
    [CP ✓ <date>], correcting fixtures where reality differs.
11. Tests: all ported, plus hostile inputs (non-finite numbers, crossed books,
    '%'-suffixed IV, thousands separators, C/H/L prefixes, skeleton first snapshot,
    delayed versus realtime status, competing-session response, daily reset).

ACCEPTANCE: the PR lists each playbook finding the lite code handled with its test; rate
caps cannot be exceeded under concurrency (test); no credentials or account ids in
fixtures (scrub test); live findings recorded or VERIFY-LIVE queued.
OUT OF SCOPE: orders (Session 4), collectors (Session 8).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 4 — Order path, kill switch and modes

**Launcher:** `Execute Session 4 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 4. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §5.12, §5.13, §5.19 carefully). This is the highest-risk code in the
project: every ambiguity fails closed. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–3 merged. Otherwise STOP.

GOAL: orders that can never be sent twice, are always limit-capped, confirm only the
replies the policy allows, and stop the moment anything is unexpected — with the kill
switch, the modes and the paper guard at the broker-write boundary, and no write possible
without a pre-trade clearance. Session 4a builds the pre-trade checks above this boundary.

REFERENCE (read at the source, do not depend on it): ibind (Apache-2.0). `pip download
ibind --no-deps -d /tmp/ibind` and read ibind/client/ibkr_client_mixins/order_mixin.py and
ibind/client/ibkr_utils.py (QuestionType, message ids, the question/answer loop); also
raw.githubusercontent.com/Voyz/ibind/master/docs/oauth/oauth_1a.md. Legacy, by symbol,
with the helpers they call: legacy/old/ObliskIQ/modules/execution/paper_gate.py
(`LiveTradingBlockedError`, `assert_live_trading_allowed`, the module docstring) and
monitoring_suite.py (`MonitoringSuite.check_trading_allowed`, `should_halt_reflexivity`);
legacy/old/ObliskIQ/tests/test_kill_switch_wiring.py whole, and the tests of those symbols
in test_paper_gate.py and test_monitoring_suite.py. Paper_gate's promotion criteria belong
to Session 18. Port lite's `discipline` keys you consume (limit-only entries, allowed exit
reasons: legacy/lite/config.yaml:333-350).

DELIVERABLES
1. domain/: OrderIntent (id = uuid.uuid7(); sleeve; thesis_id?; conid; side; Decimal
   quantity or cash quantity; price policy; tif; venue; reason; mode; approval_required;
   expires_at; parent and protective links; role entry | protective | exit | conversion; state),
   IntentEvent, BrokerOrder, Fill, Position, CashBalance.
2. Migration: intents, intent_events (append-only), broker_orders, fills, positions_snap,
   cash_snap, mode_audit, killswitch, and the holdings registry (conid → sleeve or
   `unmanaged`, who assigned it, when). Everything the bot did not open is `unmanaged`:
   counted in NAV and risk, never traded until the owner assigns it.
3. ibkr/orders.py: whatif, place (cOID = intent id; if IBKR caps the cOID length, a
   deterministic short form with a stored mapping), reply, status, live orders, trades,
   cancel, modify, and the fields for currency conversion orders (isCcyConv, fxQTY) —
   used in Session 5. place_idempotent(): looks the cOID up in live orders and trades
   before any placement. Only limit-capped order types can be built; never a plain market
   order.
4. The clearance lock. Session 4 builds the lock and ships the pre-trade gate failing
   closed (every intent refused, reason `pretrade_unavailable`); Session 4a builds the
   gate's checks. The order path guarantees these invariants (this is their one
   statement; Session 4a, Session 5 and D44 refer here); how it guarantees them is the
   implementing session's design, recorded in DECISIONS:
   - I1 Gate. No placement or modify reaches the broker without a PretradeClearance that
     only the gate (execution/pretrade.py) issues, bound to the order's terms (conid,
     side, quantity or cash quantity, order type, limit and stop prices, tif, conversion
     fields), used once, expiring with the quote it was checked against. A guard test
     fails if anything else under src/ constructs one.
   - I2 No short, ever. For each instrument, every sell that is working or could still
     reach the broker — cleared but not yet listed, in any process — never exceeds the
     long position (the lower of the broker's and the store's after every reported fill).
     A modify counts its order once, at its new quantity. A conversion is bounded by the
     cash check, not this one.
   - I3 Class by effect, never by role. A sell of a managed long position within I2
     reduces risk; every buy and every conversion adds risk. For an order that reduces
     risk the gate waives exactly: the kill switch, the per-order and daily notional caps,
     the maximum orders per day, buy-side eligibility, the cash and no-margin check, and
     approval. Every other check applies; a sell stop-limit that reduces risk is checked
     against its protective spec instead of the collar.
   - I4 The kill switch works when everything else fails. cancel needs no clearance and
     is allowed in every mode (the paper guard alone applies). ON cancels every working
     order that adds risk, whatever its role, and keeps the sells that reduce it; an order
     the store cannot classify is classed from the broker's live order (a buy or a
     conversion is cancelled, a sell kept); while ON, the sweep repeats, so an order in
     flight at the switch is caught.
   - I5 No exit leaves a position unprotected. An exit that needs the protective stop
     resized (flatten, a partial exit) passes its other checks first; the stop is restored
     when the exit is refused, cancelled or expires, and a failed restore alerts.
5. Reply policy (config): confirm only whitelisted message ids. o354 (no market data) is
   confirmed only when the asset class's quote policy accepts delayed quotes AND a fresh
   delayed quote exists AND the order is limit-capped inside the collar. Unknown message
   → confirmed=false, cancel, alert. The quote policy per asset class (options
   realtime_required on the option AND its underlying; others realtime_preferred with a
   maximum delay) and the collar = base + k·σ·√(delay) are defined here, because the
   reply policy needs them; Session 4a's pre-trade check reuses them.
6. execution/state.py: proposed → approved → staged → submitted → working →
   partially_filled → filled | cancelled | rejected | expired; illegal transitions raise;
   every transition appends an IntentEvent. Property tests.
7. execution/modes.py: shadow (simulated fills with quant/costs), paper, live-small, live;
   upgrades by explicit command with audit; downgrades automatic. The write boundary
   refuses any placement or modification the mode does not permit (cancel is allowed in
   every mode), and re-checks the paper guard (profile paper ⇒ paper-prefix account)
   before every write.
8. execution/killswitch.py: file + DB flag + CLI `obliskiq kill on|off`; checked
   immediately before every placement or modification that adds risk; ON behaves as I4
   (item 4). `obliskiq flatten --confirm` is separate (Session 4a).
9. A stateful fake broker built with respx (orders, replies, status, trades, cancels) that
   Session 4a and Session 5 extend.
10. docs/knowledge/ibkr-orders.md: endpoints, fields and message ids (from ibind and IBKR
    docs), each tagged; live shapes join it in Session 4a or when a paper login works.

TESTS (failure branches are the point), on the stateful fake broker: unknown reply; o354
with and without a delayed policy and a fresh quote; o354 with a stale quote, with a
delayed quote beyond the maximum age, and outside the collar; whatif warning; rejection;
cancel race; crash between place and status (no double placement); duplicate intent. I1:
place or modify without a clearance refused; a clearance reused, expired or presented for
changed terms (a moved stop price included) refused; the shipped gate refuses every
intent. I4: with the kill switch ON, a reduces-risk clearance passes the write boundary
and an adds-risk one does not; kill switch while working (a working protective-role buy
and a working conversion cancelled, a working exit sell and the protective stop kept),
also while the gate refuses, with the store unavailable (classed from the broker's live
orders), and for an order placed just before ON and listed late; a cancel after a
downgrade to shadow still reaches the broker. Mode downgrade mid-cycle; a placement the
mode does not permit; live account id under the paper profile → refused at the write
boundary. Session 4 or 4a runs `test-breaker` against I1–I5.

ACCEPTANCE: every reply-policy branch and write-boundary refusal tested; no plain market
order constructible (test); no broker write without a clearance (guard test); mypy strict;
import contracts green; the PR lists the reply message ids with their tests.
OUT OF SCOPE: the pre-trade checks, the approval queue, `flatten` and the paper order test
(Session 4a); currency conversion, pricing, protective exits, executor, reconciliation
(Session 5).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 4a — Pre-trade rails

**Launcher:** `Execute Session 4a exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 4a. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §5.11, §5.12, §5.13, §5.16 carefully). Every ambiguity fails
closed. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–4 merged. Otherwise STOP.

GOAL: no order reaches the broker unless every pre-trade check passes — mandate,
eligibility, holdings, cash, caps, quote policy, calendars, kill switch, mode and
approval — and every refusal carries a reason code.

READ: Session 4's code (the write boundary, the clearance, the quote policy and collar, the fake
broker: execution/{boundary,pretrade,ledger,quotes,replies}.py, store/orders.py,
tests/support/{fake_broker,order_rig}.py) and docs/knowledge/ibkr-orders.md; Session 3's cash and
quote reads; Session 2's Calendars (docs/knowledge/calendars.md). Port lite's `instruments` keys you
consume (prohibited categories, the futures-options switch, the leveraged and inverse markers:
legacy/lite/config.yaml:270-332) and any `discipline` keys Session 4 left
(legacy/lite/config.yaml:333-350).

DELIVERABLES
1. execution/pretrade.py replaces Session 4's fail-closed gate and stays the only issuer
   of PretradeClearance. Each failure has a reason code: paper guard (profile paper ⇒
   paper-prefix account); eligibility through an EligibilityPolicy protocol in
   jurisdiction/ (the eu_priips policy arrives in Session 8; until then es.yaml selects a
   skeleton policy that allows only the instruments in config
   `eligibility.skeleton_allowlist`, labelled skeleton and removed in Session 8);
   prohibited categories (futures, futures options, written options, leveraged and inverse
   products); no short (I2, Session 4's item 4; options sell-to-close only); never touch
   `unmanaged` holdings; no leverage (whatif shows no margin use; cash in the order's
   currency, read from Session 3's balances, covers the order and the floor — an order in
   a currency the account does not hold is refused until Session 5 can fund it by
   conversion); notional caps per order and per day; maximum orders per day; the quote
   policy per asset class and the collar (Session 4's definitions);
   `Calendars.tradability` ok at the order instant (Session 2's act-now view, not the
   planning `venue_status`; an option also inside `common_windows` with its underlying's
   venue); kill switch off; mode permits writes; approval present if required; the
   mandatory-close rule for options on ineligible ETPs; an option close refused for want
   of a real-time quote alerts the owner (an in-the-money option left to expiry may be
   exercised). The gate implements I2, I3 and I5 of Session 4's item 4, with the waivers
   listed there and no others. The paper guard, kill switch and mode are also checked at
   Session 4's write boundary: defence in depth, by design.
2. An optional approval queue (off by default, per-sleeve config).
3. `obliskiq flatten --confirm`: closing orders (role exit) for the managed positions,
   under I2 and I5 (Session 4's item 4); they reduce risk, so they pass with the kill
   switch ON; separate from the kill switch.
4. `obliskiq preflight --paper-order-test` — whatif, then place one non-marketable limit
   order for the minimum quantity of a UCITS ETF in the base currency and of a liquid US
   stock (both in the skeleton allowlist; if the paper account holds no USD, the stock
   order must be refused by the cash check, and that refusal is the recorded outcome),
   read status and reply messages, cancel — built and tested against the fake broker.
   LIVE: the paper Gateway login does not complete from a cloud container
   (docs/knowledge/ibkr.md), so from the cloud queue VERIFY-LIVE(paper order test) for
   the deployment machine (Sessions 6, 19). Where the login works and the guard passes,
   run it and record request and response shapes, reply message ids, cOID behaviour and
   the algos offered in docs/knowledge/ibkr-orders.md as [CP ✓ <date>].

TESTS (failure branches are the point), on Session 4's fake broker: every reason code with
its positive control; stale quote; delayed quote beyond the maximum age; option order
without a real-time quote on the option, and on the underlying; collar breach; DST window
edge; venue blocked by an incident; a calendar disagreement blocking an option order near
its last tradable session but not a stock order; option sell-to-open; a prohibited
product; an `unmanaged` holding; a currency the account does not hold; caps per order and
per day; approval missing; live account id under the paper profile → refused; an option
close without a real-time quote alerts. I2 (`short`): a sell above the position; with the
kill switch OFF, an exit beside a resting stop that together exceed the position; a sell
cleared but listed late by the broker still counts; two processes clearing one instrument
at once never together exceed it; a modify counts its order once; a stale broker position
after a fill does not re-arm a stop on a closed position; a conversion is not refused as a
short. I3 with the kill switch ON: flatten clears its closing orders; any buy is refused;
a protective stop is cleared, also past the daily cap; an exit above the per-order cap
passes; a protective-role buy, a sell of an `unmanaged` holding, an option close without a
real-time quote and a closing limit outside the collar are refused. I5: an exit refused,
cancelled or expired after the stop was resized → stop restored; a failed restore alerts.

ACCEPTANCE: every reason code tested; mypy strict; import contracts green; the PR lists
the reason codes with their tests and the live findings or the VERIFY-LIVE queued.
OUT OF SCOPE: currency conversion, pricing, protective exits, executor, reconciliation
(Session 5).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 4b — Research harness

**Launcher:** `Execute Session 4b exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

```
You are building ObliskIQ, session 4b. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §1, §5.4, §5.9, §5.16 and DECISIONS D60–D64, D69 carefully). No IBKR call.
Money paths fail closed; the tax engine is one. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–4a merged. Otherwise STOP.

GOAL: one research harness whose output can move capital, held to every rule of src/:
point-in-time research data, a simulator that prices costs and Spanish tax with the code the
live path will use, the statistics and the decision rule of DECISIONS D62–D63, and reports
that say sufficient, insufficient, inconclusive or not evaluable — with what they cannot
conclude.

READ: src/obliskiq/quant/{costs,inference,stats}.py and their tests; config/defaults/costs.yaml
and docs/knowledge/costs.md; src/obliskiq/jurisdiction/ (the registry and Session 4a's
EligibilityPolicy); legacy/old/ObliskIQ/modules/testing/walk_forward_backtester.py and its test
(delta execution, costs, bias disclosure); legacy/old/ObliskIQ/docs/STRATEGY_V2_DESIGN.md §0,
§3.4 and §3.5 (registration rules, minimum samples, annualisation guards).

DELIVERABLES
1. src/obliskiq/research/ (import contracts: decision, execution and ibkr never import it; it
   never imports execution or ibkr; live decision code never reads research data):
   - data.py: a research_history store (migration; source-tagged, point-in-time, each series
     with its vendor, download date and bias notes) and loaders for yfinance end-of-day prices,
     the Ken French and AQR data libraries, FRED/ALFRED and ECB reference rates; conversion to
     the base currency with ECB rates; coverage and survivorship reports (the share of a
     universe without a price, by year).
   - simulate.py: a monthly walk-forward simulator with delta execution, quant/costs.py on
     every leg (the listing venue, never SMART) and the tax ledger of item 2. Tax is simulated
     as it arises (D60): each realised gain or loss through the lot engine, each tax year's tax
     paid out of the portfolio at the profile's payment date in the following year (a value
     es.yaml carries with its source), withholding when income is received; at the horizon
     the remaining positions are sold and that tax paid, for every portfolio and reference.
   - stats.py: the annualised log growth of liquidation value, drawdowns and the probability of
     breaching a limit, a block bootstrap, Newey–West on a series, the Fama–MacBeth series of a
     cross-sectional effect, power to detect θ and the effect detectable at a given power,
     n_eff = n / (1 + (n − 1)ρ) with ρ estimated from a panel, the deflated Sharpe, Bonferroni
     intervals, the break-even cost multiplier.
   - decide.py: the decision rule of D62 — not evaluable; sufficient (L ≥ θ); insufficient
     (U < θ, labelled negative when U < 0); inconclusive (L < θ ≤ U, labelled positive when
     L > 0) — exhaustive and mutually exclusive by construction, power reported beside it.
   - registry.py: the trial registry (every exploratory trial with its inputs hash) and the
     pre-registration lock (SHA-256 and merge commit; a run refuses an unlocked or changed file).
   - report.py: a markdown report with inputs hashes, seeds, coverage, bias notes, the outcome
     and its labels, power, and what the test cannot conclude.
2. tax/ and jurisdiction/: the TaxPolicy protocol beside EligibilityPolicy in the registry; the
   Lot domain and migration; a LotEngine with the policy's lot method;
   jurisdiction/tax_es_irpf.py (`es_irpf`), selected by es.yaml with its data there: FIFO
   (LIRPF art. 37.2); homogeneity per RIRPF art. 8 applied conservatively (the same ISIN, or the
   same issuer and the same underlying index); the two-month rule (LIRPF art. 33.5.f) with
   deferral and release; losses offset and carried forward (years from config); realised gain
   and tax from the savings-base rate table, which records its tax year (verify yearly); ETF
   switches taxable; fund-switch deferral at this broker false until verified; swap partners
   differing in issuer and index by default; a `generic` policy (FIFO, no wash-sale rule) as a
   template; hooks tax_cost(intent), wash_sale_conflict(intent), harvestable_losses(). The
   simulator uses the same engine; Session 7 wires it to fills.
   docs/knowledge/tax-es.md cites the article and an official source per rule, "not tax advice".
3. Commission schedules in config/defaults/costs.yaml for the European venues the core's
   vehicles trade on (Xetra, the LSE, Euronext Amsterdam and Paris, Borsa Italiana, SIX at
   least), from IBKR's published schedule, each with a source, a date and verify_live
   (D69); a UCITS order is then priced, in shadow too.
4. research/explore/ with a README (outside the coverage floor, never evidence, D64) and
   research/preregistrations/TEMPLATE.md carrying every field of D63.
5. CLI: `obliskiq research data load|coverage`, `obliskiq research power <prereg>`,
   `obliskiq research run <prereg>`. Record the new dependencies (yfinance at least) in
   DECISIONS.

TESTS (no network): the log growth of a liquidated portfolio, one rebalance, one loss deferral
and its release, one carry-forward, one year of tax paid at its payment date and the terminal
liquidation's tax, hand-computed; a rotating portfolio pays tax during the horizon that a
buy-and-hold one defers (test); Newey–West,
Fama–MacBeth, the seeded bootstrap, power and n_eff against closed forms; the decision rule's
outcomes and labels, and a property test that every interval lands in exactly one outcome; a
point-in-time test that perturbs every observation after as_of and finds every output
unchanged; a run refused on an unlocked or edited pre-registration; a European ETF order priced
end to end and filled in shadow; loaders on recorded fixtures with sidecar notes.

ACCEPTANCE: a toy study runs end to end through `obliskiq research run` and matches a hand
calculation; the coverage floor holds over src/obliskiq/research, tax and jurisdiction; no tax
constant in code (test).
OUT OF SCOPE: any pre-registration or run on real data (Sessions 4c and 4d).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 4c — Core verdict

**Launcher:** `Execute Session 4c exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

```
You are building ObliskIQ, session 4c. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §1, §4, §5.2, §5.4, §5.9, §5.16 and DECISIONS D60–D63 carefully). Network is
open for research data. No IBKR call. Finish with the end-of-session protocol.

PRECONDITION: Session 4b merged. Otherwise STOP. The research does not wait for the owner's
risk numbers (DECISIONS D60): it reports a frontier, and the owner's drawdown tolerance picks a
point on it afterwards.

GOAL: the first investment experiment, executed: the core's variants and the two references
tested once, after tax, against the objective, in a reproducible report that shows the frontier
of after-tax growth against drawdown, states each outcome and what the test cannot conclude.

READ: research/preregistrations/TEMPLATE.md; legacy/old/ObliskIQ/docs/STRATEGY_V2_DESIGN.md
§0 (frozen at commit, amendment voids, single shot) and
legacy/old/ObliskIQ/docs/STRATEGY_V1_1_VERDICT.md (how the last verdict was reported).

ORDER MATTERS
Step 1 — Pre-registration in its own PR, merged before Step 2 (AGENTS.md session workflow):
research/preregistrations/core-allocation.md from the template:
- portfolios: the equity reference and the 60/40 reference (D60); A static risk balance with
  wide bands; B A + the tax-aware trend tilt (12-month TSMOM and a 10-month moving average,
  hysteresis, a scale floor above zero); C a growth-tilted risk budget + the tilt; D an
  equity-dominant book with the tilt as an overlay to cash, sized to each drawdown limit of
  the grid below; every constant;
- each role's long-history proxy in EUR (ECB rates) with its bias; the span (from 2005 at
  least, longer pre-tax history where it exists for the trend overlay's structure); monthly
  walk-forward; costs through quant/costs; tax through es_irpf (FIFO, the two-month rule, swap
  partners on a different index, offsets, carry-forward) as it arises, each year's tax paid at
  the profile's payment date, and the remaining positions liquidated and taxed at the
  horizon;
- the primary effect per variant: its annualised after-tax log growth minus the equity
  reference's, with the block-bootstrap interval; a grid of drawdown limits and breach
  probabilities wide enough to bracket a cautious and an aggressive owner, with the
  owner's stated aspiration (30–50 % a year, D60) reported as a distance, never a gate;
  θ with its justification; Bonferroni across the variants; the power to detect θ and the effect
  detectable at 0.8, computed before the run; robustness (subperiods, break-even cost
  multiplier against the declared cost range); the rule that variants the test cannot tell
  apart resolve to the simplest, lowest-turnover one;
- a constants hash and research/preregistrations/core-allocation.lock.
You may check that a series exists and its span; compute no return or statistic on it before
the merge. The harness (DECISIONS D76) runs one portfolio comparison, a candidate against a
reference, per pre-registration: extend `research/prereg.py` and `research/study.py` to several
candidates against the equity reference (one test each, Bonferroni across them) and add the
variants as strategies in `research/strategies.py`, under the harness's tests, in a pull
request of its own merged before Step 2; the lock is written with `obliskiq research lock`,
and a confirmation run writes `research/reports/<id>.md`.

Step 2 — The run, once: data through research/data.py with coverage reported; the verdict
through `obliskiq research run`; research/reports/core-allocation.md (every metric before and
after tax, a per-year table, bias disclosure, each outcome with its labels and power, what it
cannot conclude — future superiority, crisis behaviour beyond the episodes in the sample) and
the frontier: for each point of the grid, the variant with the highest median after-tax log
growth that respects it, with its outcome against the equity reference. The owner's choice of a
point is an authorisation recorded in DECISIONS afterwards (Session 13's precondition); this
session does not choose for the owner. If data is unreachable, the outcome is not evaluable and
ROADMAP says so — never fabricated.

ACCEPTANCE: the pre-registration is on main before the run; the verdict is reproducible by one
command; the report states each outcome with its labels, power and limits, and the frontier.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 4d — First selection hypothesis

**Launcher:** `Execute Session 4d exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

```
You are building ObliskIQ, session 4d. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §1, §5.5, §5.9 and DECISIONS D62–D64, D71 carefully). Network is open for
research data. No IBKR call. Finish with the end-of-session protocol.

PRECONDITION: Session 4c merged. Otherwise STOP.

GOAL: one selection hypothesis, chosen on the quality of its data, tested once with H5 beside
it, so the satellite is built for what the evidence supports.

STEP 1 — Data audit, committed as research/reports/selection-data-audit.md: for H1 (cross-asset
and country momentum through UCITS), H2 (insider-purchase clusters, SEC Form 4) and H4 (a
stock-level composite: nearness to the 52-week high, 12-1 momentum with a crash guard,
profitability), which free sources give point-in-time data, over which span, the share of the
universe lost to survivorship by year (measured, not assumed), the number of independent
observations, and the effect each could detect at power 0.8. Choose by D71: among the
candidates with adequate data, H2, then H4, then H1. If none has adequate data, record each as
not evaluable with the data that would make it evaluable and its cost, and stop after Step 1.

STEP 2 — Pre-registration in its own PR, merged before Step 3: research/preregistrations/
selection-<id>.md from the template: mechanism, universe, the signal fixed ex ante (no weight
search), horizon, the primary effect (for events: the abnormal return at the horizon, clustered
by date; for a cross-section: the Fama–MacBeth series of the top group's excess return after
costs and tax), θ with its justification, the development and confirmation samples, the trial
budget, robustness (subperiods, a placebo, the break-even cost multiplier), H5 (the same signal
with and without a volatility-scaled stop, gaps modelled) as a registered secondary test, and
the lock.

STEP 3 — Exploration on the development sample with every trial in the registry; one
confirmation run; research/reports/selection-<id>.md with the outcomes, labels, power and
limits. research/pattern_registry.yaml is created with this entry (Session 15 adds the legacy
definitions and the loader): `shadow` with its grading rule for a sufficient or
inconclusive-positive outcome, `retired` with its result for an insufficient one, `idea` with
the data it needs for a not-evaluable one.

ACCEPTANCE: the audit is committed before the pre-registration, and the pre-registration is on
main before the confirmation run; the verdict is reproducible by one command; the report states
what it cannot conclude.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 4e — Total-risk scaling

**Launcher:** `Execute Session 4e exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

Runs any time after Session 4d and before Session 13.

```
You are building ObliskIQ, session 4e. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §1, §5.4, §5.9 and DECISIONS D60, D62–D64, D79, D80, D88–D92 carefully).
Network is open for research data. No IBKR call. Finish with the end-of-session protocol.

PRECONDITION: Session 4d merged. Otherwise STOP. The owner's point is 30 % reached on at most
5 % of 20-year paths (D91): the study reports that point beside a grid of horizons and
probabilities.

GOAL: the first test of adaptation (D89, D92), one mechanism: does scaling the core's total
risk by a re-estimated volatility, on bases with more equity than A, grow the after-tax book
more than static A while respecting the owner's 30 % point?

READ: research/reports/global-data-audit.md and docs/knowledge/research-sources.md (sources
and their gaps); research/preregistrations/core-allocation.md and
research/reports/core-allocation.md (static A, the benchmark, and what 4c already showed of
2005–2026); src/obliskiq/research/ and src/obliskiq/quant/.

STEP 1 — Data, before anything else: the series the bases need (their equity roles and the
cash role in EUR; the IL role's euro proxy from 2008 only if a base holds IL) and the longest
histories the long-history check needs (MSCI from 2000, Ken French's developed regions from
1990, AQR's Century of Factor Premia from 1926, a cash rate for each), each checked for
adjustments, distributions, continuity, currency and match to the vehicle, and recorded in
docs/knowledge. Choose sources and span on those checks alone, never on a return or statistic.
If the data cannot support the study, record it as not evaluable with what would make it
evaluable, and stop.

STEP 2 — Harness, in its own PR merged before Step 3:
- quant/covariance.py: EWMA, a multi-year window, their blend, Ledoit–Wolf shrinkage toward a
  declared target, in the base currency; tested against closed forms and an independent
  implementation.
- A total-risk strategy in research/: fixed relative weights among the risky roles; a risky
  share of a target volatility over the forecast volatility, point in time, capped at one (no
  leverage), with a pre-registered floor and a largest change per rebalance; the rest in the
  cash role; under A's bands, minimum trade and tax-aware execution. Its declared twin applies
  the same rule to the declared matrix.
- The bootstrap (D92): blocks at least twice the longest lookback a candidate reads, a
  sensitivity over at least three block lengths, and the drawdown on daily closes inside each
  resampled month (D88).
- The long-history check (D92): the breach at the owner's point and the growth on the longest
  history each candidate's roles have, its currency, vendor and index biases stated.
- The forecast check: each estimator's out-of-sample forecast of the next month's realised
  variance of its base, scored by a pre-registered loss; reported as a check that the
  estimator works, never as an outcome (D92).

STEP 3 — Pre-registration in its own PR, merged before Step 4: research/preregistrations/
core-risk-scaling.md from the template.
- Static A, the benchmark, against at most three specifications and their declared twins, each
  fixed in full: the base (a fixed mix of 4c's roles), the target volatility and the rule that
  sizes it to the owner's point (design falls and the declared matrix, never 4c's breach
  table), the risky share's floor and largest change, the estimator (the prior's weight in
  equivalent observations, half-lives, the blend, the shrinkage rule), the bands, the warm-up
  and evaluation dates, the block lengths. No trend tilt: return timing is another mechanism
  (D89).
- Primary outcome (D92): each candidate's annualised log growth of liquidation value after
  costs and Spanish tax minus static A's, paired; θ with its justification. A candidate that
  does not respect the owner's point on daily closes is reported but cannot be sufficient.
- Secondary: each specification against its declared twin (what estimation adds); turnover;
  tax paid; the breach of 30 % (on daily closes from the running peak, D88) as the share of
  paths reaching it over each horizon of a pre-registered grid and as a rate (breaches per
  decade, the share of time beyond it); the frontier at the owner's point with every candidate
  under D79's rule.
- Power before data for each; a low power is reported as such, never as evidence against
  adaptation. Bonferroni across every test of the run; robustness (subperiods, the break-even
  cost multiplier, the block lengths, the long history); what of 2005–2026 its author had seen
  in 4c's report; the lock.

STEP 4 — Exploration on the development window with every trial in the registry; one
confirmation run; research/reports/core-risk-scaling.md with each outcome, its labels, power
and limits, and what it cannot conclude. A sufficient specification becomes a variant Session
13 builds beside the static ones (D89); an inconclusive-positive one that respects the point
runs in shadow; the others are recorded with their result. A new point on the frontier is the
owner's choice (D60), recorded in DECISIONS.

ACCEPTANCE: the data checks are committed before the harness, the harness is merged before the
pre-registration, and the pre-registration is on main before the confirmation run; the verdict
is reproducible by one command; the report states each outcome with its labels, power and
limits, the forecast check apart from the outcomes.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 5 — Execution mechanics

**Launcher:** `Execute Session 5 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 5. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §5.12, §5.13 and DECISIONS D67 carefully). Money paths fail closed.
Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–4d merged. Otherwise STOP. This session is independent of the
strategy: it runs as written whatever Sessions 4c and 4d found (DECISIONS D61). If T1
(§A.5) has recorded live order shapes, build on them; otherwise on fixtures.

GOAL: an intent becomes a filled, protected, reconciled position: funded in its currency,
priced patiently, protected at the broker, resumable after a crash.

READ: the code of Sessions 4 and 4a and docs/knowledge/ibkr-orders.md;
legacy/lite/config.yaml:118-133 `positions` (single-foreign-currency cap: port that key).

DELIVERABLES
1. execution/currency.py — the currency manager: base currency from config; working
   balances in other currencies allowed within the single-foreign-currency cap; a trade
   is funded in its own currency by a limit-capped spot conversion (isCcyConv / fxQTY)
   shortly before it; conversions batched to amortise the minimum commission; a buffer
   per active currency; excess repatriated; any intent that would leave a negative
   balance in any currency is refused (this extends Session 4a's cash check with funding
   by conversion; Session 4a's gate refuses every conversion, `conversion_unavailable`,
   until this replaces it). Every conversion, funding or repatriating, gets its own
   clearance and adds risk (Session 4's item 4), so it is refused with the kill switch ON.
   Valuation rates from IBKR; ECB reference rates only for reporting.
2. execution/pricing.py: (a) an IBKR algo or peg with a limit cap, chosen from the
   contract's algo list, enabled per venue and asset class in config (default off until
   verified live); (b) a patient limit at the reference mid ± k ticks (tick rules from
   contract rules), repriced up to N times every M minutes toward the touch, then
   cancelled and retried at the next tradable window. The collar widens with σ·√(delay).
   Each reprice is a modify with a fresh clearance for its new terms. Never a plain market
   order. Every leg reaches the cost model with its listing venue, never SMART.
3. execution/protective.py: protective exits rest at the broker as stop-limit orders
   (stop price and limit offset from the intent's protective spec), re-placed after
   fills, trailing roll-ups and partial exits; verified present at every heartbeat;
   missing → re-placed and alerted. Each placement and resize gets its own clearance
   under I2 and I3 (Session 4's item 4), so a stop within I2 goes through with the kill
   switch ON; partial exits (role exit) follow I5 through Session 4a's
   `execution/exits.py` (`ExitCoordinator.place_exit`; the executor calls `settle` each
   cycle). Tradability is not waived; this session
   decides how long a stop rests at the broker (its tif) and records it in
   DECISIONS. Gaps (D67): the protective spec's limit offset follows the instrument's
   volatility and its overnight-gap distribution (with a configured floor), replacing
   `execution.pretrade.protective.max_limit_offset_fraction` as the one rule; a triggered
   stop that has not filled is repriced toward the bid in configured steps, each through
   the gate as an order that reduces risk; a deeper emergency limit exists only if the
   owner enabled it (off by default). Attached orders: if the live probe in item 6 shows
   that IBKR's Web API accepts an entry with a child stop-limit linked by `parentId`, design
   the clearance of the package (I1 and I5) and record it in DECISIONS; otherwise record
   why not and keep placing the stop on the first fill.
4. execution/executor.py: stage → convert currency if needed → whatif → place_idempotent
   → replies → poll → reprice or cancel → protective exit → reconcile; staged and
   submitted intents resume by cOID after a restart.
5. execution/reconcile.py: broker orders, trades, positions and cash per currency vs the
   store; drift freezes new intents and alerts; partial or missing broker data is never
   reconciled as green. A configured grace period for IBKR's reporting lag runs before a
   just-placed order or a just-filled position counts as missing (measured live in item 6
   where possible, else a labelled judgement).
6. LIVE (if paper credentials are present and the guard passes): (a) a EUR→USD conversion
   whatif, then one minimum conversion on paper, verifying both cash balances; (b) buy the
   minimum quantity of a liquid US stock (added to the skeleton allowlist) with a limit
   inside the collar, place its
   protective stop-limit, verify it rests at the broker and survives a restart of the
   process, then sell the position with a limit and cancel the stop; (c) the same buy with
   an attached child stop-limit (`parentId`), recording whether IBKR accepts and links it;
   (d) the lag between a fill and the positions and trades reads. Record shapes,
   conversion commissions and stop behaviour in docs/knowledge/ibkr-orders.md as
   [CP ✓ <date>]. From T1 on, these steps are a command list for the owner to run on the
   machine (§A.5).

TESTS (failure branches): the conversion fails before the trade → the trade is not placed;
a projected negative balance in a foreign currency → refused; batching across two intents;
repatriation above the buffer; algo not offered → patient limit; reprice cycle then cancel
at the window end (I5: the stop restored if an exit is cancelled); partial fill →
protective stop resized; a partial exit beside the protective stop never oversells;
protective stop missing → re-placed, also with the kill switch ON; a stop re-placed by the
heartbeat while flatten's exit is working, and before the broker lists it, never
oversells; crash after place → resume by cOID without a second order; restart with staged
and submitted intents; reconciliation drift → freeze; partial broker data → not green; the
kill switch during a reprice keeps the protective stop; a gap through a stop's limit →
the stop repriced through the gate, never to a market order; an order inside the
reconciliation grace period is not drift, and one past it is; the spec's limit offset
widens with volatility and gaps; a package of entry and attached stop (if adopted) never
leaves the fill without its stop.

ACCEPTANCE: every money path tested on its failure branches; the PR lists the live
findings or the VERIFY-LIVE queued; no plain market order constructible anywhere (test).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 6 — Walking skeleton

**Launcher:** `Execute Session 6 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 6. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §3.4, §5.4 "Exits", §5.12, §5.14, §5.19). Finish with the end-of-session
protocol.

PRECONDITION: Sessions 1–5 merged. Otherwise STOP. T1 (§A.5) should have passed; if it
has not, build against fixtures and queue the live step.

GOAL: the system runs end to end — schedule → targets → intents → execution → reconcile
→ digest — with a deliberately trivial static target portfolio. Later sessions replace
stubs, not plumbing.

READ: legacy/lite/src/obliskiq/collect/{sequence,runlog,status}.py,
legacy/lite/docs/DECISIONS.md:382-511 "Running unattended";
legacy/old/ObliskIQ/modules/notifications/{telegram_notifier,run_summary}.py and
test_run_summary.py;
legacy/pulse/src/market_pulse_bot/{text_formatter,notification_backend}.py (badge language
and message state); legacy/lite/src/obliskiq/{store,collect}/nav.py. Port
lite's `runtime` keys you consume (legacy/lite/config.yaml:741-751).

DELIVERABLES
1. orchestration/: one asyncio service `obliskiq serve`: heartbeat (config interval);
   four decision cycles per trading day anchored to venue sessions through calendars
   (Asia, Europe, US, US-late; offsets in config), never UTC constants; job registry; run
   records and run_steps; crash recovery from the run log; graceful SIGTERM; an
   injectable clock. The keepalive logs in again unattended after IBKR's daily reset and
   alerts a failed re-login within one heartbeat (DECISIONS D68).
2. capital/ (minimal; Session 7 extends it): `capital.mode: whole_account | cap` and the
   cap in base currency, so a large paper balance behaves like a small account.
3. A temporary core: config `core.skeleton_targets` (labelled skeleton; removed in Session
   13) listing 3–4 roles, taken from the variant the owner chose from Session 4c's frontier
   (the equity reference until one is chosen) and completed with other core roles of
   MASTER_PLAN §5.4 when it has fewer, with candidate symbol, venue and currency; conids
   resolved
   through secdef and pinned (fixtures in tests; live if paper credentials are present)
   and listed in the skeleton allowlist. UCITS vehicles only, so they will also pass
   eu_priips in Session 8; at least one quoted in a currency other than the base currency,
   so a cycle exercises the conversion. Like the real core, these positions carry no
   protective stops.
4. decision/rebalance_basic.py: targets vs positions → intents with an inner band, integer
   shares, the minimum trade (`trading.min_trade`: absolute, % of managed capital, or both
   combined by max|min; never below the commission floor), the cash floor;
   `obliskiq holdings list|assign <conid> <sleeve>` over Session 4's holdings registry.
5. Wire the executor, currency manager, protective exits and reconciliation into the
   cycle, plus the NAV writer: lite's store/nav.py and collect/nav.py ported as the only
   code that writes NAV rows (Session 7 builds the return series on it).
6. reporting/: the Telegram notifier and run summary; a daily digest (mode, NAV,
   positions, targets, intents, fills, errors, venue status, health); alerts on failure,
   stuck intents, reconciliation drift, missing protective stops, competing sessions,
   blocked venues.
7. CLI: serve, run-cycle --mode shadow|paper [--dry-run], status, mode show|set, kill
   on|off, flatten --confirm, intents list (approvals are Session 4a's `obliskiq approvals
   list|approve|reject`), holdings list|assign.
8. Health: a heartbeat file and an optional ping URL (config).
9. docs/RUNBOOK.md: run in shadow and paper; stop; recover; read the digest; why a
   running bot must be stopped before a Claude session with live IBKR checks.
10. LIVE (on the T1 machine, as a command list for the owner, §A.5): one supervised paper cycle with
    `capital.mode: cap` and a cap of a few hundred euros in config/owner.yaml (the
    managed-capital cap this test is allowed): dry run first, then the real cycle; verify
    fills, the currency conversion, reconciliation and the digest; record findings. If
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, send the digest once.

TESTS: end to end with the stateful fake broker and a fake clock — cycle 1 creates and
fills intents in shadow and fake-paper; cycle 2 does nothing (inertia); a crash between
place and fill does not double-order; the kill switch stops cycle 3; a simulated
three-day run across the EU DST change fires cycles at the right local times; a holiday
on one venue skips that venue's cycle only; a session lost at the daily reset is logged in
again, and a failed re-login alerts within one heartbeat.

ACCEPTANCE: `obliskiq run-cycle --mode shadow --dry-run` on fixtures prints a sensible
plan; the RUNBOOK lets the owner start paper trading on a machine.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 7 — Capital, cash flows and tax

**Launcher:** `Execute Session 7 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 7. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §5.4, §5.15, §5.16, §11 carefully). Money paths fail closed. Finish
with the end-of-session protocol.

PRECONDITION: Sessions 1–6 merged. Otherwise STOP.

GOAL: the bot knows exactly how much capital it manages, plans for deposits and dated
withdrawals the moment they are announced, asks the owner before cutting what it should
not cut, tracks tax lots and applies the active jurisdiction's tax rules — all through
pluggable policies.

DELIVERABLES
1. capital/: investable capital = managed − reserved − unmanaged; paper flows adjust
   virtual managed capital only. Sleeve budgets `capital.budgets.*` (core, equity
   satellite, option premium, cash floor; defaults per MASTER_PLAN §11 row 2) and the
   owner's satellite ceiling, behind one reader that the planner, the options sleeve
   (Session 12) and the decision layer (Session 17) use; Session 18 moves the satellite
   budget. External holdings: port lite's external_positions (positions the owner holds
   elsewhere, entered by hand with an as-of date, `obliskiq holdings external
   add|list|remove`), flagged stale after lite's `positions.external_data_stale_days`.
2. Cash-flow ledger (domain and migration): Deposit, WithdrawalReservation(amount,
   currency, due_date, reason, state), Withdrawal, Adjustment. CLI (Telegram and MCP come
   in Session 19): `obliskiq cash deposit <amount> [--currency] [--date]`,
   `obliskiq cash reserve <amount> --by <date> [--reason]`, `obliskiq cash withdrawn <id>`,
   `obliskiq cash list|cancel <id>`. Every command echoes a summary and needs --confirm.
3. Reservation planner, acting as soon as a reservation exists (MASTER_PLAN §5.15):
   investable capital drops at once; targets are recomputed on the reduced capital; cheap
   sources execute at the next windows (the cash line, overweights, exits the strategy
   would make anyway); the rest is scheduled before due date − settlement lag − buffer
   (settlement lag added per venue to venues.yaml here, each with a source), choosing
   sells that minimise tax cost + trading cost + strategy cost. Strategy cost comes from
   a StrategyValue interface each sleeve implements (Sessions 13, 15 and 17 fill it in;
   here a default that protects positions inside their entry window, probes awaiting
   confirmation and theses with pending catalysts, and prefers the cash line and
   overweights). An expected return enters only from a layer whose research outcome is
   sufficient (MASTER_PLAN §5.9); otherwise the default ranking is the rule. Losses before gains; the two-month rule respected. Weights in config.
   The plan and its reasons go into the digest.
4. Owner questions: domain and migration for OwnerQuestion (id, kind, context, options
   each with its tax cost and strategy cost, a proposed default, a decision deadline,
   state, answer, audit). When meeting a date would cut protected positions, the planner
   asks: postpone to a proposed date, reduce the amount, or proceed; where the date allows
   it also offers tax-motivated timing (for example, selling after year-end). Without an
   answer by the deadline the withdrawal wins and the plan executes. CLI
   `obliskiq questions list|answer <id> <option>`; Telegram and MCP in Session 19;
   natural-language answers through CommandParser in Session 16.
5. Pacing the sales (MASTER_PLAN §5.15): each planned sale is paced by its expected
   return — sold at once or in tranches, proceeds set aside for the withdrawal, when the
   strategy would sell it anyway or expects little from it; held to the last safe days
   when it expects a strong move, with a later date offered to the owner through an
   OwnerQuestion. The last safe day is never crossed without an answer. Thresholds in
   config.
6. Deposit deployment: new cash follows targets, optionally in tranches over N days.
7. Unexplained broker cash changes (vs fills, fees, dividends, interest, recorded flows):
   freeze new buys, alert, wait for classification.
8. Time-weighted return over Session 6's NAV rows (flows neutralised; convention in
   config).
9. Tax on the live path: Session 4b's LotEngine and `es_irpf` wired to fills (each fill opens
   or closes lots; fees and FX at the trade date; `store/lots.py` saves the open lots whole, and
   the engine's open loss windows and its ledger need persistence beside them, DECISIONS D74);
   currency lots for foreign-currency
   balances under the TaxPolicy (DECISIONS D69), with the Spanish treatment of currency
   gains confirmed from an official source in docs/knowledge/tax-es.md; an annual CSV of
   realised results and deferred losses for the owner's return; the decision hooks
   (tax_cost, wash_sale_conflict, harvestable_losses) served from the live lots.
10. LIVE (read-only, if paper credentials are present): read the ledger, cash per currency
   and recent account activity to record how IBKR reports dividends, interest, fees and
   conversions (shapes into docs/knowledge/ibkr.md; the classifier's fixtures from them).

TESTS: fills opening and closing lots, partial lots and fees, against Session 4b's engine;
the two-month rule triggered by a live buy after a loss sale, with deferral and later
release; currency lots on a conversion and its repatriation; carry-forward expiry;
a reservation met on time across a holiday and DST; the planner using the cash line at
once and sparing a fresh thesis; a question raised when a protected thesis would be cut,
"postpone" moving the reservation, no answer by the deadline → proceed; the year-end
option offered only when the date allows; a weak position sold at once with proceeds set
aside, a strong one held to the last safe days and never past them without an answer;
deposit tranches; the unexplained-cash freeze;
TWR with flows equal to a hand calculation; switching the jurisdiction id swaps the
policy with no code change.

ACCEPTANCE: all money paths tested on failure branches; no tax constant in code (test);
docs/knowledge/tax-es.md covers currency gains with an official source, marked "not tax
advice".
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 8 — Universe and market-data collectors

**Launcher:** `Execute Session 8 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 8. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §5.3, §5.7, §5.13, §5.18). Port from legacy/, never from memory.
Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–7 merged. Otherwise STOP.

GOAL: a global universe where any instrument can be data and only eligible ones can be
bought, single-stock candidates for every grid cell, and the market-data collectors.

READ: legacy/lite/universe.yaml (all of it is ported: inspect it by script and by
section, not whole into the context), legacy/lite/src/obliskiq/collect/*.py,
legacy/lite/docs/COLLECTORS.md, legacy/lite/docs/DECISIONS.md:166-381, 2190-2331,
2673-2967 (sections "Instruments, PRIIPs and wrappers", "Data and collection", "The
European chains…", "The market axis, realigned for allocation", "Two collector defects…");
legacy/old/ObliskIQ/modules/config/global_vehicles.py. Port lite's `collectors` and `data`
keys you consume (legacy/lite/config.yaml:351-433, 1113-1260; Session 2 ported `calendars`
into `config/defaults/venues.yaml`).

DELIVERABLES
1. config/universe.yaml: grid axes ported from lite (markets, 12 sectors, unmapped_cells
   with reasons; the loader refuses an incomplete matrix); macro-board axes; instruments
   with roles (data | tradable_candidate | option_underlying), attributes (asset class,
   domicile, UCITS and KID status, share class and its currency hedge, fee, currency,
   listing venue, lot size, inception) and exposures (core roles, grid cells, macro-board
   entities), with look-through country, sector and currency weights from the issuer's
   holdings files where published (DECISIONS D69). US ETFs, index
   contracts and country ETFs remain data and option underlyings.
2. Stock candidates (MASTER_PLAN §5.3 "Finding the stocks"): per grid cell, the holdings
   of its representative ETFs from the issuer's published files (iShares first; source
   and date per file) and IBKR market-scanner runs per venue; normalised, de-duplicated
   across listings, resolved through secdef on demand and pinned when a thesis needs
   them; refreshed on a configured cadence; point-in-time (a stock enters a cell on the
   date the file showed it). Session 4d left no selection hypothesis in shadow (DECISIONS
   D87), so no cell comes first; every cell has candidates or a declared reason.
3. Venue profiles: extend venues.yaml with the fields it lacks, each value with a source
   and date — trading permission required, market-data cost and availability (MASTER_PLAN
   §5.13 price list), currency, lot conventions. Settlement lag (Session 7) and
   transaction taxes (Session 2's cost config) stay where they are.
   `owner.yaml: market_data.plan` defaults to OPRA L1, Eurex Core L1 and the
   complimentary US feed.
4. jurisdiction/eligibility_eu_priips.py (`eu_priips`): buy and hold eligibility computed
   from attributes; options on ineligible ETPs allowed with the mandatory close; leveraged
   and inverse products rejected at ingestion by resolved description (lite's match list);
   futures-tracking ETCs eligible only while lite's contango rule passes, through a
   ContangoCheck interface that fails closed until Session 12 implements it; corporate
   bonds off by default. es.yaml now selects it; wired into Session 4a's pre-trade; the
   skeleton allowlist policy and its config key are removed (the skeleton targets must
   pass eu_priips), and with them `ConfiguredDirectory` (execution/instruments.py), which
   describes the allowlist's instruments to the gate: a directory over the universe
   replaces it.
5. data/market/: port universe (seed, pin, supersede), backfill, snapshots (settle loop,
   availability status), the chain collector (strike band, sigma ladder, per-row venue),
   the curve collector (data only); source per row; run records; scheduling per venue
   after its close through calendars. NAV stays with Session 6's writer. The chain
   collector stores option surfaces forward from its first run for the option underlyings
   of active and registered hypotheses: this is H8's history (DECISIONS D71).
6. Research history for the tradable and stock candidates through Session 4b's
   research/data.py (its store and import contract already keep it from live decision
   code).
7. CLI: `obliskiq collect <universe|candidates|backfill|snapshots|chains|curves|daily>`,
   `obliskiq universe validate|pin`.
8. LIVE (if paper credentials are present): pin conids for the tradable candidates and
   option underlyings (IBKR exchange codes of Session 2 confirmed or corrected); one
   snapshot round recording which venues are realtime or delayed under the plan; one
   chain for a US and a Eurex underlying.
9. Port the collector tests (idempotence, run continuity, sequence isolation, outage,
   strikes, vol, European chain).

ACCEPTANCE: the universe validates; every grid cell has stock candidates or a declared
reason why not; eligibility explains why US-domiciled ETFs are rejected for purchase;
research history loaded for the tradable candidates (dates and coverage recorded); the
chain collector stores surfaces with their date and source.
OUT OF SCOPE: broker capabilities and the vehicle selector (Session 9).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 9 — Broker capabilities and vehicle selector

**Launcher:** `Execute Session 9 exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

```
You are building ObliskIQ, session 9. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §4, §5.3, §5.4, §5.8, §5.16). Network is open for research. Finish
with the end-of-session protocol.

PRECONDITION: Sessions 1–8 merged. Otherwise STOP.

GOAL: measure what the broker can really do through the Web API, and turn any exposure
into the best eligible vehicle with reasons. Bonds, mutual funds and stock baskets, which
pay only at larger capital, are Session 19a's (DECISIONS D66).

DELIVERABLES
1. Broker capabilities, LIVE with paper credentials (else VERIFY-LIVE), whatif only,
   nothing placed: per asset class and target venue, whether the Web API can search,
   quote and accept an order — stocks on each venue, UCITS ETFs and ETCs, US and Eurex
   options; the order types and algos per class; the account's trading permissions. Recorded in docs/knowledge/ibkr-capabilities.md and config; the
   selector never proposes a vehicle whose capability is unverified.
2. Core role candidates: for every role of the variants Session 4c registered, at least two
   eligible UCITS vehicles, currency-hedged share classes ranked as distinct vehicles
   (DECISIONS D69), preferring accumulating share classes and venues the market-data plan
   covers;
   identifiers confirmed by two sources (issuer site, justETF) in
   docs/knowledge/universe-sources.md; conids pinned live with paper credentials.
3. Swap-partner groups: per exposure, eligible vehicles from a different issuer on a
   different, highly correlated index (correlation threshold from config, measured on
   research history; examples S&P 500 ↔ MSCI USA, MSCI World ↔ FTSE Developed); a
   same-index partner only when the owner enables it.
4. decision/vehicle_selector.py: exposure → ranked eligible vehicles with reasons
   (eligibility, all-in cost including fee, spread, commission, transaction tax, FX
   conversion and market-data cost; liquidity; tracking correlation to the exposure's
   data series; tax treatment per the active TaxPolicy; currency; trading-window overlap).
   Weights in config. The selector accepts further vehicle types (bonds, funds, baskets)
   through their qualification rules, which Session 19a adds.
5. CLI: `obliskiq universe capabilities|vehicles <exposure>`.

TESTS: a hedged and an unhedged class of one fund ranked as distinct vehicles; a same-index swap partner is refused while the setting is off; an unverified capability
is never proposed; ranking reasons are complete.

ACCEPTANCE: every core role has ≥ 2 verified candidates; the selector returns an eligible
vehicle per role from fixtures and explains each rejection; capabilities recorded live or
queued.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 10 — Macro, positioning, insiders and events

**Launcher:** `Execute Session 10 exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

```
You are building ObliskIQ, session 10. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §5.5, §5.12 "Calendars and phases", §5.18). Network is open for the
sources. FRED_API_KEY, EIA_API_KEY and SEC_USER_AGENT may be set. Probe live where
possible and record observations with dates in docs/knowledge/macro-sources.md, which
also condenses legacy/lite/references/macro-playbook.md. Finish with the end-of-session
protocol.

PRECONDITION: Sessions 1–9 merged. Otherwise STOP.

GOAL: point-in-time macro, positioning, insider and event data for the core, the views
and the Hunter, plus the halt feeds that keep orders away from halted instruments. Every
source is declared in config/sources.yaml with its activation condition; a fetcher is built
when a registered hypothesis, a Hunter component or a safety rule consumes it (DECISIONS
D66).

READ: legacy/lite/src/obliskiq/macro/*.py, legacy/lite/references/macro-playbook.md (all),
legacy/lite/docs/DECISIONS.md:1194-1407 "Collection without credentials, and what
positioning measures"; legacy/old/ObliskIQ/modules/market/{cftc_cot_analyzer,gdelt_client,
polymarket_client,rss_news_enhanced,vader_financial_lexicon}.py,
scripts/backfill_cot_history.py, config/{gdelt_regions,polymarket_markets,
event_calendar}.json, docs/{COT_REAL,GEO_INPUTS}.md and the tests Session 1 snapshotted
for them (rss_sources, vader_financial_lexicon included);
legacy/pulse/src/market_pulse_bot/halt_detector.py, config/exchanges.yaml
(`incident_source`), README.md (incident coverage) and tests/test_incidents.py. Port
lite's `macro` keys you consume (legacy/lite/config.yaml:826-1112).

DELIVERABLES
1. data/macro/: port FRED/ALFRED (output_type=4, vintage windows, value_vintage
   initial|latest), ECB SDMX, EIA v2, ONS, StatCan and the SDMX decoder; series declared
   in config/sources.yaml with units and release-date policy; known_at and units guards.
2. Beyond lite: the BoJ Time-Series API (stat-search.boj.or.jp/api/v1 getDataCode: JGB yields,
   USDJPY, monetary base), the BIS Data Portal (policy rates, credit-to-GDP gaps,
   effective FX) and the Federal Reserve Board excess bond premium; release-date policy
   per series; anything without a defensible release date is unusable for backtests.
   The excess bond premium feeds H6 (Session 21). The other macro-board regions —
   Eurostat (HICP flash and activity), the Bank of England, the SNB, the Bundesbank, the
   Bank of Canada Valet API, the ABS and RBA, Banco Central do Brasil (SGS), Norges Bank,
   MAS, Banxico and CBRT EVDS (optional tokens BANXICO_TOKEN and EVDS_API_KEY), DBnomics as
   a live-only fallback — are declared with their activation condition and built when a
   registered hypothesis or component consumes them. Optional: FRED API v2 bulk backfill.
3. data/positioning/: CFTC COT (annual ZIP backfill + Socrata incremental); value = net
   non-commercial / open interest; point-in-time percentiles with warm-up; velocity;
   contract registry (US30Y code 020601).
4. data/insiders/: SEC EDGAR Form 4 (User-Agent from SEC_USER_AGENT) for US names among
   the stock candidates: open-market purchases by officers and directors; cluster inputs
   (counts, values, roles) point-in-time on the filing date. Session 4d did not test H2
   (DECISIONS D84); if a later batch does, its research loader and this live fetcher share
   one parser.
5. data/events/: Polymarket Gamma (curated markets and tag discovery; probability and
   velocity); Kalshi public market data (read-only); GDELT DOC 2.0 (region and theme
   volume and tone, Welford anomaly z-scores, warm-up); RSS (central banks, regulators,
   IR feeds of satellite names; dedupe; raw items stored for the EventExtractor). VADER
   only as an offline fallback tone.
6. Halt feeds into calendars/incidents.py through `Calendars.accept_incidents(origin,
   incidents)`, which replaces one origin wholesale: port market-pulse-bot's structured-feed
   parser and its Nasdaq Trader trade-halts feed, which is single-stock in scope. A
   single-stock halt blocks that instrument only, never the venue (record this
   instrument-level blocking in DECISIONS). Market-wide halts and exceptional closures
   stay manual entries, because no trustworthy free market-wide feed exists for most
   venues; a feed is treated as market-wide only once verified as such and declared in
   config. A feed failure keeps the last state and alerts.
7. Catalyst calendar: central-bank meetings and key releases in config with a source URL
   per date; earnings dates (research-grade, labelled).
8. Source health (ok, stale, failing; last success; rows) feeding status; scheduler hooks
   per source cadence.
9. A minimal EventExtractor (DECISIONS D70), the start of H7's clock: llm/ with one
   configured provider (the key name from config; skipped and queued when no key is set), the
   `ExtractedEvent` schema (entities, region, sector, type, tone, novelty, sources), structured
   output validated with one retry then fail closed, untrusted text escaped, a monthly budget
   cap, and every output stored with its item, the time it was produced, the model id and the
   prompt hash; nothing consumes it. research/preregistrations/llm-events.md, merged in its own
   PR before the extractor's first live run, registers H7 (do the features add to the price
   signal), its sample, horizon, θ and evaluation date. The llm import contracts hold.

ACCEPTANCE: each fetcher has fixture tests and a recorded live probe (or VERIFY-LIVE); COT
and Form 4 are point-in-time (as_of tests); no source without a release-date policy can
feed a backtest (test); the halt-scope tests pass.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 11 — Measurement store and views

**Launcher:** `Execute Session 11 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 11. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §5.7). Port from legacy/, never from memory. Compute touches no network.
Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–10 merged. Otherwise STOP.

GOAL: one long-format measurement store with the equity grid and the macro board as
views, rankings, cross-asset correlations, the session-level IV solve and a realised-vol
forecast.

READ: legacy/lite/src/obliskiq/compute/{frames,indicators,build_grid,rankings,
universe_census,iv_solve}.py, legacy/lite/docs/COMPUTE.md (all),
legacy/lite/docs/DECISIONS.md:512-874, 1408-1559, 1735-1854, 3144-3215 (sections "The
grid", "The correlation matrix reaches beyond equities…", "The implied term structure…",
"Reading the whole bar…", "The grid says which way it is moving", "A guard is verified by
mutation, not by being green"), and the matching tests. Port lite's `grid` and `census`
keys you consume (legacy/lite/config.yaml:434-572).

DELIVERABLES
1. Migration and store: measurements(entity_kind, entity_id, indicator, leaf, as_of, value,
   status ok|n/a|stale|inapplicable, reason, n_obs, window, units, source, inputs_hash).
2. grid/: frames (typed reads, macro on release date) and the 15 indicator families with
   their leaves, statuses and per-input freshness (ported), writing into measurements.
   Views as queries: the equity grid (market × sector) and the macro board (region × asset
   class, axes in config). Rankings (four views with trajectories), the cross-asset
   correlation matrix keyed on legs, the census and family reach.
3. The session-level IV solve (own column, provenance label, degraded runs) and a
   realised-volatility forecast (HAR-RV, numpy OLS) for the vol-mispricing component and
   for the base-rate probabilities of Session 17; per instrument, the distribution of
   overnight gaps (earnings dates flagged where known) for the gap-adjusted sizing and the
   protective spec (DECISIONS D67).
4. `obliskiq compute <solve-iv|measure|rankings|census|all>`.
5. Port all related tests (window honesty, isolation, validation).

ACCEPTANCE: an offline fixture store yields both views with honest statuses; ported tests
green; import contracts prove compute cannot reach ibkr or httpx.
OUT OF SCOPE: the option surface, engines and gates (Session 12).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 12 — Engines and gates

**Launcher:** `Execute Session 12 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 12. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §5.6, §5.7). Port from legacy/, never from memory. Compute touches no
network. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–11 merged. Otherwise STOP.

GOAL: the option surface view and the lite engines — options sleeve, risk, fx, commodity
— plus the candidate gates, composed once and reused everywhere.

READ: legacy/lite/src/obliskiq/compute/{gates,screen,risk_engine,penalty,fx_engine,
commodity_engine,catalysts,smile}.py, legacy/lite/docs/DECISIONS.md:37-131, 875-919,
1855-1911, 3144-3215 (sections "The option sleeve", "The equity sleeve", "Volatility, risk
and the screen", "The credit tier is a date comparison…", "A guard is verified by
mutation, not by being green"), and the matching tests;
legacy/lite/references/commodities-universe.md. Port lite's `option_sleeve`, `tenor`,
`zombie_test`, `equity_risk`, `fx_inflation`, `commodities`, `screen`, `liquidity` and
`smile` keys you consume (legacy/lite/config.yaml:37-117, 134-144, 156-269, 581-719;
Session 2 ported part of the last two into `config/defaults/{liquidity,smile}.yaml`;
extend those files; Session 4a ported `tenor.mandatory_close_trading_days` into
`config/defaults/instruments.yaml`, so the rest of `tenor` joins it there), except
`option_sleeve.budget_pct`: the option budget is read from Session 7's `capital.budgets`
(one owner; recorded in DECISIONS).

DELIVERABLES
1. The option surface view in the measurement store: per underlying, delta × tenor × IV,
   IV percentile, skew, term slope, liquidity, theta.
2. engines/options_sleeve/ exactly as lite (shared pool, penalty booked at detection,
   recovery on qualifying realised gains, concurrency cap, minimum premium, catalyst-driven
   tenor window, mandatory close by calendar, strike-smile N(d₂) zombie test over two
   sessions), its budget from `capital.budgets` on current NAV. The sleeve trades only when
   its activation condition holds (H8 sufficient and the budget buying the pre-registered
   number of median premiums within the friction gate, DECISIONS D66); until then it runs
   in shadow and its would-be entries are logged for H8. Every option date rule
   counts from Session 2's `Calendars.last_tradable_session` for the option and its underlying,
   minus a configured buffer of sessions (`session_before(…, sessions)`; N sessions including the
   last tradable one is sessions = N − 1).
3. engines/risk/ (lite risk engine: volatility-scaled stops, trailing stops, averaging-down
   rule, pre-earnings rule); engines/fx/ (four-way base-currency-real decomposition,
   carry-illusion test); engines/commodity/ (curves, roll, COT, inventory seasonal
   deviation, wrapper choice) implementing Session 8's ContangoCheck;
   docs/knowledge/commodities.md condensed from lite's commodities-universe.md.
4. decision/gates.py: lite's CandidateGates composed once and reused (screen, liquidity,
   smile, tenor, penalty, entry discipline), each refusal with a reason code.
5. `obliskiq compute <surface|fx|commodity|all>` extended.
6. Port all related tests.

ACCEPTANCE: an offline fixture store yields the option surface with honest statuses;
ported tests green; a mutation check on three gates (record which mutation each test
kills); a futures-tracking ETC turns ineligible when its contango breaches the limit; the
options sleeve stays in shadow while its activation condition fails (test).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 13 — Core engine

**Launcher:** `Execute Session 13 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 13. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §1, §2, §4, §5.2, §5.4, §5.16 and DECISIONS D60, D61 carefully). Money
paths fail closed. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–12 merged, Sessions 4c's and 4e's reports on main, and the owner's
choice of a point on the frontier (the drawdown tolerance, its probability and the horizon)
recorded in DECISIONS (D60; D88 and D91: 30 % of the value the bot manages, reached on at most
5 % of 20-year paths, which resolves to A on Session 4c's frontier). Otherwise STOP and ask the owner for that choice.

GOAL: the core as the owner's point on the frontier selects it — the variants 4c registered (4e
found no total-risk specification sufficient, D95), the chosen one by default — with tax- and
cost-aware rebalancing, built on the same research
code paths so the live core and the tested core cannot drift apart.

READ: research/preregistrations/core-allocation.md, research/reports/core-allocation.md and
research/reports/core-risk-scaling.md
(binding: every constant comes from there); src/obliskiq/research/simulate.py and
src/obliskiq/research/strategies.py (the rebalancing, tilt and tax-aware execution the verdict
used); src/obliskiq/quant/risk_budget.py and src/obliskiq/quant/trend.py (Session 4c, DECISIONS
D80, D82).

DELIVERABLES
- quant/covariance.py exists (Session 4e) for research. 4e found no specification sufficient
  (D95), so engines/core does not scale total risk. Session 4c's variants hold static weights
  from a declared covariance (D80).
- quant/risk_budget.py exists (Session 4c: cyclical coordinate descent, closed forms and an
  independent Newton solve in tests). Add the check against skfolio (test-only) and port the
  remaining still-valid cases of legacy/old/ObliskIQ/tests/test_opportunistic_optimizer.py.
- engines/core/: the registered variants with their constants (environments and risk
  budgets where used; the equity-dominant variant and its overlay to cash), roles filled by
  the vehicle selector, the trend tilt (12-month TSMOM and 10-month moving average,
  hysteresis, a scale floor above zero), freed weight to cash or short duration, wide inner
  and outer bands, minimum trade, integer shares, cash flows first, sells beyond the outer
  band only, tax-aware execution (losses first; gain-realising reductions only when the
  pre-registered benefit exceeds tax_cost; re-entry through swap partners), cash
  reservations respected, no stop orders, the StrategyValue interface implemented →
  TargetPortfolio. `core.variant` selects the variant, its default the owner's choice.
  Remove the Session 6 skeleton targets and their config.
- A parity test: on a fixture history, engines/core and research/simulate.py produce the
  same target weights and trades for the chosen variant.

ACCEPTANCE: each registered variant produces a TargetPortfolio from fixtures for a small and
a large account; the parity test holds; risk-budget tests (if built) green against closed
forms and skfolio; `core.variant` matches the DECISIONS entry.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 15 — Satellite engine

**Launcher:** `Execute Session 15 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 15. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §4, §5.2, §5.5, §5.6, §5.9, §6.4 and DECISIONS D62, D63, D71 carefully). Port from legacy/, never from
memory. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–13 merged. Otherwise STOP.

GOAL: the Asymmetry Hunter — deterministic components on typed point-in-time inputs,
measured independence, entry rules per expression, a frozen thesis lifecycle with
machine-checkable triggers, company selection among the stock candidates, and the pattern
registry. Components and their agreement rule run in shadow and are graded until the
research pipeline finds them sufficient (Session 21). Session 4d left no selection
hypothesis in shadow (DECISIONS D87); one a later batch leaves there runs through the same
lifecycle. The LLM is not built here; only the
interface its nominations arrive through.

READ: legacy/old/ObliskIQ/modules/analytics/asymmetry_hunter.py (all) and its tests,
docs/{ASYMMETRY_HUNTER,HUNTER_READINESS}.md, modules/execution/continuity_guard.py and its
test, modules/analytics/pattern_library.py (definitions); legacy/lite/src/obliskiq/store/
ledger.py and store/migrations/001_initial.sql (the frozen-thesis SQL trigger
trg_position_thesis_frozen), and compute/catalysts.py.

DELIVERABLES
1. domain/: Signal, HypothesisDraft, ThesisCandidate, Thesis (pre-registered target,
   expected payoff, stated probability, horizon and invalidations; no sizes — sizing
   owns them in Session 17), Trigger (discriminated union: price_cross,
   series_threshold, grid_cell, positioning, event_prob, date_reached,
   drawdown_from_entry, trend_state), ReunderwriteEntry, TriggerEvaluation.
2. Migration: signals, hypotheses, theses (immutable text and triggers through an SQL
   trigger), thesis_triggers, trigger_evaluations, reunderwrite_log, component_scores.
3. engines/satellite/components/: port the legacy components onto measurements,
   positioning and events, and add the others in MASTER_PLAN §5.5 item 1. Readiness per
   component (live, warming-up N/threshold, off). Weights, thresholds and k in config; a
   weights_hash on every Signal.
4. research/pattern_registry.yaml (Session 4d created it): the 31 legacy definitions
   (logic, citation, group from §6.4, state), each component and the k-of-n agreement rule
   as entries registered for Session 21's batch, and the §5.9 hypotheses not yet tested as
   `idea` entries; the legacy IC, n
   and hit-rate figures are not carried over. A loader validates states and transitions;
   components can be backed by registry entries in `shadow` or `active` state.
5. engines/satellite/independence.py: effective votes from the historical correlation of
   component signals (point-in-time, warm-up rule); a candidate needs ≥ k effective
   agreeing votes and ≥ 1 hard market input. Most runs produce none.
6. engines/satellite/lifecycle.py: candidate → approved or rejected (reason codes: vehicle
   exists, eligible, catalyst dated or horizon bounded, ≥ 1 price trigger + ≥ 1 data or
   time trigger, constraints) → active → closed (reason). Shares: probe, then scaled only
   on trend confirmation. Options: one block, no additions. Continuity: default hold; a
   flip needs a fired trigger or new information. A catalyst window or confirmation rule
   is mandatory.
7. engines/satellite/triggers.py: point-in-time evaluator each heartbeat; every evaluation
   logged; fired triggers emit exit or reduce requests for Session 17.
8. engines/satellite/selection.py: company scoring inside a sector or country thesis over
   the Session 8 stock candidates (liquidity, 12-1 momentum with a crash guard, quality
   and valuation where data exists, active registry patterns, honest degradation).
9. engines/satellite/learning.py: the interface for slow empirical-Bayes weight updates
   after a pre-registered minimum sample counted in effective observations (wired to
   grading in Session 18), versioned by weights_hash. The StrategyValue interface for the reservation planner.
10. CLI `obliskiq satellite scan|status|readiness` and `obliskiq patterns list|show`; a
    replay script (firing frequency over chosen historical windows; a sanity check, not a
    backtest).

ACCEPTANCE: the legacy Hunter tests pass in intent; two perfectly correlated components
count as one vote (test); an UPDATE on a thesis fails (SQL trigger test); the trigger DSL
round-trips JSON; an option thesis cannot receive a scale-up request (test); the replay
report is committed.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 16 — LLM layer

**Launcher:** `Execute Session 16 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 16. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §5.10 and DECISIONS D66, D70 carefully). Tests use fakes; with any configured key, run the live
probe and the evaluation. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–15 merged. Otherwise STOP.

GOAL: a provider-agnostic, budgeted LLM layer where adding or removing a model is a
configuration change, every model is evaluated before it is trusted, and the output is
only validated structured data that can never touch sizing or orders.

READ: legacy/old/ObliskIQ/modules/analysis/{analyst_router,providers,llm_analyst}.py,
modules/config/ai_providers.py, docs/AI_PROVIDERS.md, tests llm_fakes.py,
test_analyst_router.py, test_ai_providers.py, test_llm_analyst.py,
test_degraded_provider_policy.py.

DELIVERABLES
1. config/llm.yaml, the registry: providers (protocol openai_compatible | gemini |
   anthropic, base URL, key env name) and models (id, context window, capability flags:
   structured output, JSON mode, tool use; price per million input and output tokens;
   enabled). Each role lists an ordered chain of models. Adding a model is a YAML entry;
   adding a provider with a known protocol is a YAML entry plus its key; a new protocol is
   one adapter. A test adds a model to a fixture registry and uses it with no code change.
2. llm/providers: one OpenAI-compatible transport (DeepSeek, Qwen, Kimi, GLM, MiniMax,
   OpenRouter, Groq and any other compatible endpoint), Gemini (google-genai) and an
   optional Anthropic adapter. Ordered failover; per-model circuit breakers; Retry-After
   honoured; a loud error on a bad model id; models below a configured context size may
   extract events but never nominate hypotheses.
3. Budgets: a monthly budget in base currency; a spend ledger from the price table; paid
   budget spent → free models → no LLM. Trading never depends on the LLM (test).
4. Structured outputs: Pydantic v2 → JSON schema; native structured output where
   supported, else JSON mode; validate; one retry with the validation error; then fail
   closed. Temperature 0.
5. Prompt registry: t-string templates; a renderer that escapes and delimits every
   interpolated value as untrusted data; prompt hash and version on every call.
6. Roles and schemas: EventExtractor (wired into Session 10's events), HypothesisScout
   (→ HypothesisDraft → Session 15 corroboration; new ideas also go to the pattern
   registry as `idea`), ExposureMapper, DossierWriter, DevilsAdvocate, CommandParser (an
   owner message in any language → OwnerCommand: deposit, withdrawal reservation, setting
   change, answer to an OwnerQuestion; always returned for confirmation; plus a
   deterministic grammar fallback that works with no LLM). Schemas forbid extra fields; no
   field can carry an order or a size.
7. Evaluation: tests/fixtures/llm_eval/<role>/ with hand-labelled cases (inputs and
   expected outputs); `obliskiq llm eval <model>` scores validity, accuracy, cost and
   latency per role into docs/knowledge/llm-evals.md; `obliskiq llm list|probe`. A model
   is used for a role only after an eval run is recorded for it (config flag set by the
   owner).
8. A cache by input hash; an llm_calls table (provider, model, prompt hash, tokens, cost,
   latency, outcome); redaction (no account ids, balances or secrets in prompts).
9. Prompt-injection tests: news items carrying instructions change nothing beyond the
   schema.
10. Scheduling: the extractor per collector run; the scout weekly and on event spikes; the
    devil's advocate weekly. Each role runs in shadow within its cost share of NAV and
    influences a decision only when its activation condition holds (DECISIONS D66).
11. H7, forward only (DECISIONS D70): extend Session 10's minimal EventExtractor without
    breaking its stored series (a change of model or prompt starts a new labelled segment);
    evaluate H7 when its pre-registered sample is reached. No backtest of an LLM-derived
    signal on history counts as evidence.
12. LIVE (if a key is configured): one probe and one eval run per configured model,
    recorded.

ACCEPTANCE: every role tested with fakes (valid, malformed, timeout, failover, budget
exhausted, injection); CommandParser and the grammar fallback both parse "en 15 días
necesito retirar 1.000 €", "I added 1237,57€ to your cash" and "pospón la retirada al día
20" into the right commands; the import contract llm ⟂ decision, execution holds.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 17 — Decision layer

**Launcher:** `Execute Session 17 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 17. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §4, §5.5, §5.6, §5.8, §5.11, §5.13 and DECISIONS D66, D67, D69
carefully). Money paths fail
closed. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–16 merged. Otherwise STOP.

GOAL: turn the core TargetPortfolio, approved theses, fired triggers and cash reservations
into sized, gated, limit-capped OrderIntents over the whole book — trading from day one
with a fixed risk budget, and by evidence once a layer is calibrated.

READ: legacy/lite/src/obliskiq/server/guards.py; decision/gates.py and the engines
(Session 12 — reuse, don't duplicate); legacy/old/ObliskIQ/modules/execution/
phase4_decision_engine.py (apply_quarter_kelly_clamp only), modules/data/
crisis_database.py, research/validation/{stress_tester,monte_carlo}.py and
test_crisis_database.py. Port lite's `positions` and `portfolio_circuit_breaker` keys you
consume (legacy/lite/config.yaml:118-133, 145-155).

DELIVERABLES
1. decision/expression.py: a long option when the options sleeve is active (Session 12's
   activation condition), the IV percentile is at or below threshold,
   the chain is liquid, the tenor window is satisfiable (catalyst + buffer), the payoff at
   the named target meets the minimum, real-time quotes exist for the option and its
   underlying, and the sleeve can afford it — choosing the candidate with the most
   convexity per unit premium and the least correlation to the core; otherwise shares or
   an eligible fund through the vehicle selector, with a volatility-scaled stop and
   trailing stop. Bearish: long put or core underweight. FX conversion cost included.
2. decision/sizing.py, two regimes per layer (MASTER_PLAN §5.11), chosen by a
   CalibrationState interface (Session 18 fills it; until then every layer is
   uncalibrated):
   - Uncalibrated: shares sized so the gap-adjusted loss (the stop distance plus a
     pre-registered quantile of the instrument's overnight gaps from Session 11, wider
     across earnings dates; DECISIONS D67) is a configured share of NAV; probe = max(the
     minimum position, `sizing.probe_fraction` × full size), capped by that risk budget
     and the caps; if the cap falls below the minimum position or the minimum trade, the
     trade is skipped (never rounded up). The minimum position is an absolute floor
     derived from commissions, as lite's `option_sleeve.min_premium_base_currency` is,
     replacing `positions.min_size_pct_nav` (DECISIONS D66); lite's other `positions` caps
     are restated against the sleeve each governs and read on look-through exposure
     (DECISIONS D69). Options: a configured
     share of the sleeve, at least one contract, within affordability and the friction
     gate.
   - Calibrated (the pre-registered minimum of graded outcomes counted in effective
     observations, and Brier skill above zero): f ≤ `sizing.kelly_fraction` × Kelly(p_eff, payoff) with p_eff = p_base +
     shrink × (p_stated − p_base); p_base = N(d₂) at the target from the strike's smile
     IV (options) or the driftless random-walk probability of reaching the target before
     the stop within the horizon, volatility from the HAR-RV forecast (shares); shrink
     from calibration. Capped by sleeve budgets and risk limits.
   - Options in both regimes: integer contracts, the friction gate (round-trip commission
     plus FX cost ≤ a configured share of premium), a maximum number of adjustments per
     position.
3. Option exits: target, zombie, mandatory close, time window, and the premium-recovery
   partial exit (sell a configured fraction at a configured premium multiple). An option
   still held at the last tradable session minus the buffer is sold, never left to expire
   unattended (test: a Monday expiry after a Friday holiday is sold by Thursday).
4. decision/portfolio_risk.py: historical and parametric ES at `risk.es_confidence`;
   stress scenarios from the ported crisis database and stress tester; concentration
   (sector, country, single foreign currency); correlation clusters; liquidity (days to
   exit); FX in base-currency real terms — always over the whole book including unmanaged
   and external holdings (Session 7; stale external data makes the report UNRELIABLE,
   never a number); the optional exposure caps by asset class (D88), absent by default and
   read on look-through exposure.
5. decision/breakers.py: rolling-month drawdown → freeze new buys and audit; hard
   peak-to-trough → force shadow and alert. The peak is D88's: the managed NAV at the day's
   last cycle, stored and raised once a day when that NAV exceeds it, with the drawdown from it
   reported every cycle. Each threshold is a key of its own; the owner's drawdown tolerance
   (D88) is not one of them and never orders a sale.
6. decision/inertia.py: intents only for a band breach, an approved thesis, a share
   scale-up confirmation, a fired trigger, stop, zombie or mandatory close, a cash
   reservation, or a risk breach; monthly turnover and realised-gain budgets; tax hooks
   applied.
7. decision/intents.py: core + satellite + reservations (sleeve budgets from Session 7's
   `capital.budgets` reader, which Session 18 moves; unused satellite budget stays in the
   core) →
   OrderIntents with reasons and protective-exit specs; autonomy per sleeve from config
   (paper: autonomous). The StrategyValue interface completed for all sleeves.
8. Entry gates reused from Session 12; refusals carry reason codes surfaced in the digest
   and status.
9. decision/activation.py: the activation conditions of DECISIONS D66 for the sleeves,
   venues, sources and LLM roles that exist by now, each computed from configuration with
   its inputs reported (Session 19a adds the capabilities that pay at larger capital); no
   NAV threshold key (test).

ACCEPTANCE: on day one, with no graded outcome anywhere, an approved share thesis and an
approved option thesis both produce non-zero intents when their activation conditions hold,
and a refusal naming the failed condition when they do not (tests); a gap-adjusted size
below the stop-only size on an instrument with fat overnight gaps (test); a 20 % sleeve
holds more than four positions once its budget over the absolute floor allows it (test); end to end from a fixture
thesis to a share probe, a scale-up after a trend confirmation and an exit on a fired
trigger; an option thesis entered in one block and partially exited at the premium
multiple; hand-computed Kelly with each p_base; a refused case per gate; ES tests; breaker
drills; a cash reservation overriding a buy.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 18 — Scoreboard, patterns, capital and promotion

**Launcher:** `Execute Session 18 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 18. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §1, §5.5, §5.9, §5.11, §5.19 and DECISIONS D62, D63, D65 carefully). Port
from legacy/, never from memory.
Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–17 merged. Otherwise STOP.

GOAL: the system measures itself honestly: research outcomes and live monitoring drive
sizing trust, Hunter weights, pattern promotion and the satellite's capital, and operational
evidence drives mode promotion.

READ: legacy/old/ObliskIQ/modules/analytics/{ai_scoreboard,forecast_panel}.py,
docs/AI_SCOREBOARD.md, docs/PAPER_TRADING_GATE.md, modules/execution/paper_gate.py and
their tests (test_ai_scoreboard, test_scoreboard_statistics, test_forecast_panel,
test_paper_gate).

DELIVERABLES
1. research/preregistrations/scoreboard.md first, in its own PR merged before the rest
   (AGENTS.md session workflow): the decision rule of D62 for forward grading, horizons,
   minimum samples in effective observations (ρ estimated from history by Session 4b's
   harness, never counts of predictions), the calibration rule (when a layer becomes
   calibrated, the shrink mapping), the weight-update rule, the pattern shadow → active
   rule, the monitoring rule of D65 (the twin, the monthly looks, the alpha-spending
   boundaries and the action for each breach), the exploration budget for a sufficient
   layer and the pooled re-test that can raise it.
2. scoreboard/performance.py: TWR-based NAV series (Session 7); attribution core, equity
   satellite, options, cash; benchmarks (global 60/40 and ACWI in base currency from
   collected eligible-vehicle prices); realised and unrealised P&L; realised tax per the
   TaxPolicy.
3. scoreboard/tca.py: arrival vs fill vs cost model, per venue, order type and quote status.
4. scoreboard/grading.py: forward grading of signals, components, registry patterns
   (shadow and active), theses and LLM nominations; weekly cohorts; Newey–West intervals;
   minimum cohorts before verdicts; Brier and reliability for stated probabilities against
   the base rate. Port the weekly forecast panel (a fixed universe forecast every week, in
   base currency against the equity reference) and report its effective sample beside its
   count: cross-sectional forecasts share the market's move.
5. scoreboard/calibration.py → CalibrationState for sizing: a layer switches to calibrated
   only with the minimum graded outcomes and Brier skill above zero, and back when either
   fails. Component grades → Hunter learning (empirical-Bayes, shrunk, after the minimum
   sample).
6. Pattern lifecycle automation: shadow patterns graded daily at zero weight; promotion to
   active by a sufficient outcome and its activation condition, logged, reported;
   retirement only by a new pre-registered test.
7. scoreboard/monitor.py and capital/allocation.py (D65): a twin per active layer; the
   live-minus-twin gap and the twin against the backtest's predictive distribution,
   reviewed monthly against alpha-spending boundaries; a gap breach freezes the layer's new
   entries and alerts; a twin breach returns the layer to shadow at zero weight and
   alerts; an exploration budget only for a sufficient layer, within the owner's ceiling,
   raised only by the pooled re-test and an owner command; the core absorbs the rest.
   Every change audited and reported.
8. scoreboard/promotion.py: L0→L3 criteria (MASTER_PLAN §5.19 and config) including the
   breaker, kill and cash-flow drills; `obliskiq promotion status`; upgrades still need an
   owner command.
9. reporting/: a weekly report (markdown plus a Telegram summary) and a monthly report.

ACCEPTANCE: hand-computed Newey–West, Brier, n_eff and TWR tests; promotion cannot pass on
missing data; a gap-breach drill, a twin-breach drill and a budget step drill; repeated
monthly looks hold the pre-registered false-alarm rate (simulation test); weight updates,
calibration, budget steps and pattern promotion impossible before the minimum sample
(tests).
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 19 — Cockpit, settings and deployment

**Launcher:** `Execute Session 19 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 19. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §5.12, §5.15, §5.17, §5.20). Port from legacy/. Finish with the
end-of-session protocol.

PRECONDITION: Sessions 1–18 merged. Otherwise STOP.

GOAL: the owner can watch the bot, change settings, announce cash flows, answer its
questions and stop it — from Claude, Telegram or a daily dashboard — and the bot deploys,
updates and rolls back unattended on any small always-on Linux machine.

READ: legacy/lite/src/obliskiq/server/*, docs/{SERVER,DEPLOY,ORACLE-CLOUD,GOOGLE-CLOUD}.md,
deploy/*, Dockerfile and _claude/skills/obliskiq-lite/SKILL.md, all under legacy/lite/;
legacy/lite/ROADMAP.legacy.md:146-252 "What the second provider measured (2026-10-04)";
legacy/lite/docs/DECISIONS.md:1046-1159 ("Deploying to the VM…" and "Two providers, one
walkthrough"). Port lite's `server` keys you consume (legacy/lite/config.yaml:720-740).

DELIVERABLES
1. server/: the MCP cockpit (port: bearer auth, streamable HTTP, loopback by default, an
   unauthenticated /healthz that reveals nothing). Tools: get_status, get_health,
   get_portfolio, get_targets, get_intents, get_theses, get_patterns, get_grid_view,
   get_macro_board, get_scoreboard, get_settings, update_setting, cash_deposit,
   cash_reserve, list_cash, list_questions, answer_question, approve_intent,
   reject_intent, set_pause, kill_switch. A test asserts the exact list.
2. Settings flow: config/owner.yaml stays the source of truth; `obliskiq settings
   set|show|diff|history`; Telegram /set and MCP update_setting for whitelisted keys;
   remote changes may only tighten risk or manage budgets and cash flows within bounds;
   loosening a risk limit requires the CLI on the machine; every change validated,
   audited and applied at the next cycle boundary.
3. Telegram: /status, /positions, /intents, /theses, /questions, /answer <id> <option>,
   /settings, /set, /deposit, /withdraw, /confirm <code>, /pause, /kill — owner chat id
   only; free text routed to CommandParser and echoed for /confirm; open questions pushed
   with their options and deadline.
4. The daily dashboard: one self-contained HTML file (NAV, TWR vs benchmarks, sleeves and
   their budgets, positions, theses, patterns, intents, cash reservations, open questions,
   venue status, health, settings in force), sent through Telegram and served by the
   cockpit behind its token.
5. .claude/skills/obliskiq-cockpit/SKILL.md adapted from the lite skill: reading the
   cockpit, never inventing numbers, quoting refusal reasons.
6. deploy/: a Dockerfile with uv-managed Python 3.14, non-root, amd64 and arm64; systemd
   units for serve and the cockpit; an env template; nightly backup (sqlite .backup +
   integrity_check + zstd, pushed off-box); log rotation; heartbeat ping; with transport
   `gateway`, a pinned Client Portal Gateway container beside the bot, its automated login
   and health check.
7. Release images: extend release.yml to build and push the multi-arch image to
   ghcr.io/almoon-d/obliskiq:<version> on every release (`packages: write` in that
   workflow only); the amd64 image also builds in PR CI. The package stays private: the
   machine pulls with a GitHub token limited to `read:packages`, stored as a machine
   secret (deploy env template, DEPLOY.md).
8. deploy/update.sh <tag>: back up the store, pull the image, stop the service, start the
   new version (migrations run on start), wait for health (fresh heartbeat, `obliskiq
   status` ok, kill-switch state preserved) within a timeout; on failure stop it, restore
   the backup, start the previous tag and alert. Tested with stubbed docker and systemctl.
9. docs/DEPLOY.md: a provider-agnostic walkthrough (Oracle A1, Google e2-micro, any VPS, a
   home machine) reusing lite's measurements; secrets; first boot in shadow; the paper
   credentials and the paper managed-capital cap; updating and rolling back; the rule
   that cloud sessions stop calling IBKR once the bot runs; restore from backup.
10. `obliskiq preflight` in full: auth → accounts → guard → positions → cash → venue status
    → snapshot (availability recorded) → history → whatif for the minimum quantity of a
    core vehicle (never placed) → report. LIVE with paper credentials before the bot is
    deployed; if TELEGRAM_* are set, one test message.

ACCEPTANCE: the amd64 image builds in PR CI; tool-list and settings-permission tests;
update.sh rollback test; DEPLOY.md followable end to end.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 19a — Capacity and scale

**Launcher:** `Execute Session 19a exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 19a. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §2, §5.3, §5.8, §5.11, §5.16 and DECISIONS D7, D66 carefully). Money paths
fail closed. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–19 merged. Otherwise STOP.

GOAL: the machine scales with capital: every capability that pays only at larger capital is
built and turns on by its computed condition, so 1.0.0 serves a small account and a large one
with the same code and no NAV threshold anywhere.
The owner requires these in 1.0.0 (DECISIONS D66); each stays off until its condition holds.
Build in this order — bonds and bills, mutual funds, capacity limits, tax at scale, venues and
currencies, baskets — and split the session under AGENTS.md rather than rush the tail.

READ: decision/activation.py and decision/vehicle_selector.py and their tests;
docs/knowledge/ibkr-capabilities.md; quant/costs.py (the bond rows and the impact term).

DELIVERABLES
1. The bond rule (MASTER_PLAN §5.3), computed: a bond qualifies when its minimum increment
   is at most a configured share of the role's amount, the per-order minimum commission
   stays within the cost band, enough bonds exist to hold the duration target, and its
   all-in cost beats the best fund for the role; government bonds and bills as core
   vehicles; capabilities measured by whatif (minimum increment, commission), nothing
   placed.
2. Mutual funds (UCITS): cash-quantity orders, cut-off times, NAV pricing under the
   `nav_priced` quote policy; capability measured by whatif; ranked by the vehicle selector.
3. Stock baskets for an equity role: the basket rule (every member at least the minimum
   trade, commissions within the cost band, tracking error to the role's index within a
   configured bound) and the basket builder.
4. Capacity: order and position limits from each instrument's average daily value, with the
   cost model's impact term as input; each sleeve's capacity reported in the digest.
5. Venues, working currencies and market data: further venues and currencies activated by
   D66's conditions; a market-data subscription proposed to the owner when its cost is
   within the pre-registered share of the expected benefit it unlocks.
6. Tax at scale: the rate table's upper brackets exercised end to end; the annual export
   extended with what foreign-asset reporting needs, the obligations confirmed from
   official sources in docs/knowledge/tax-es.md (not tax advice).
7. decision/activation.py completed for every capability, and `obliskiq capabilities
   status` showing each condition, its inputs and whether it holds.
8. LIVE (a command list for the owner on the machine, §A.5): bond and fund whatifs on the
   paper account; nothing placed.

TESTS: a NAV ladder from a small account to a large one turns each capability on exactly
when its condition holds, and never through a NAV threshold key (test); the bond rule
excludes direct bonds for a role of a few thousand euros and admits them for a role of a few
hundred thousand; a basket refused when a member falls below the minimum trade; a capacity
limit caps an order; an unverified capability is never proposed.

ACCEPTANCE: every capability in D66 has its condition, its test on the NAV ladder and its line
in `obliskiq capabilities status`; live whatifs recorded or queued.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 20 — Red-team and cleanup

**Launcher:** `Execute Session 20 exactly as specified in docs/SESSION_PROMPTS.md on main.`

```
You are building ObliskIQ, session 20. AGENTS.md is your operating manual; MASTER_PLAN.md
the design. Finish with the end-of-session protocol.

PRECONDITION: Sessions 1–19a merged. Otherwise STOP.

GOAL: find the silent failures before money does, prove each guard by mutation, remove the
legacy snapshot, leave the docs true, release 1.0.0.

STEP 1 — A read-only audit first: docs/AUDIT.md listing the ten most probable silent
failure modes ranked by probability × € damage. Consider at least: a delayed quote treated
as fresh; an option order on a delayed quote or with a delayed underlying; FX conversion
direction inverted; a negative foreign-currency balance; a DST or holiday window error;
calendar sources disagreeing without blocking the venue; a single-stock halt blocking a
venue or a market-wide halt ignored; a duplicate order after restart; a reply
auto-confirmed that should not be (o354); the paper guard bypassed; a competing session
leaving the bot silently disconnected; an eligibility bypass through a new universe row or
stock candidate; a protective stop missing after a partial fill; the penalty not booked at
detection; a point-in-time leak in measurements, stock candidates or a backtest;
reconciliation green on partial data; the kill switch unreachable on an exception path, or
cancelling protective stops; LLM injection altering a hypothesis or an owner command; a
reservation planned late or missed across a holiday; the planner cutting a protected
thesis without asking, or an unanswered question leaving a withdrawal unmet; the two-month
rule mis-dated or a homogeneous vehicle used as a swap partner; TWR distorted by a flow;
integer rounding breaching the cash floor; uncalibrated sizing returning zero or
calibrated sizing used without calibration; the bond rule admitting a bond too large for
its role; an allocation step or a pattern promotion before its minimum sample; the
release workflow tagging twice or publishing without notes; an option left to expire
because its last tradable session was computed from the wrong venue or ignored a holiday;
the Gateway login silently lost; update.sh leaving the machine
on a broken version; a test outcome misclassified by the decision rule, or a research run
on an unlocked or edited pre-registration; a point-in-time leak in the research harness; a
capability turned on below its activation condition; a monitoring boundary that fires too
often or never across repeated looks; sizing by the loss at the stop instead of the
gap-adjusted loss; a triggered stop left unrepriced after a gap, or repriced past the
gate; currency gains left out of the tax ledger.

STEP 2 — For each of the ten: a failing test or a mutation test proving the guard catches
it, then the fix. A mutation pass (mutmut or manual) over decision/, execution/, capital/,
tax/, jurisdiction/ and calendars/; close surviving mutants.

STEP 3 — Cleanup. First the knowledge that must survive legacy/: check every
"knowledge" row of MASTER_PLAN §6 has landed in docs/knowledge/ (write
docs/knowledge/legacy-findings.md from the old repository's docs listed in §6.2, and
condense into DECISIONS.md any lite DECISIONS entry a current rule still rests on). Then
delete legacy/ and every tooling exclusion for it; confirm nothing references it; remove
dead code and unused config keys; a dependency audit.

STEP 4 — Docs truth pass: MASTER_PLAN against the code (amend only through DECISIONS);
ROADMAP with the owner's ordered checklist for whatever is still unverified live (paper
credentials, preflight, conid pinning, reply ids, algos per venue, delayed-quote o354
behaviour, collection hours, stop-limit behaviour, backup restore drill, update rollback
drill, kill drill, cash-flow drill); README status; CHANGELOG; version 1.0.0.

ACCEPTANCE: every top-ten item has a test that fails without its guard; make check green;
legacy/ gone; the ROADMAP checklist complete and ordered; release v1.0.0 published (not a
pre-release) with its image.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## Session 21 — Research: second batch

**Launcher:** `Execute Session 21 exactly as specified in docs/SESSION_PROMPTS.md on main.` (network: Full)

Runs any time after Session 4d; the Hunter's components and their agreement rule need Session
15. Later batches reuse this prompt with a new batch list.

```
You are building ObliskIQ, session 21. AGENTS.md is your operating manual; MASTER_PLAN.md
the design (read §3.1, §5.5, §5.9, §6.4 and DECISIONS D62, D63, D71 carefully). Network is
open. Finish with the end-of-session protocol.

PRECONDITION: Session 4d merged (and Session 15 for the Hunter's components). Otherwise STOP.

GOAL: give the second research batch an honest test with Session 4b's harness on
point-in-time data, so each hypothesis ends sufficient, insufficient, inconclusive or not
evaluable — and only the sufficient ones can earn capital.

STEP 1 — Data audit and pre-registration, in its own PR merged before Step 2 (AGENTS.md
session workflow): research/preregistrations/batch-2.md from the template, covering H3
(forced selling: spin-offs and index deletions), H6 (the core's risk conditioned on the
variance risk premium, the excess bond premium and trend, with euro-area measures beside the US
ones: research/reports/global-data-audit.md), H9 (COT hedging pressure for
commodity ETCs), the Hunter's components each alone and their k-of-n agreement rule as a
registered combination beside them, the batch-1 candidates Session 4d did not choose, and the
§6.4 re-test group: exact definitions (respecified where the legacy logic misstated the
cited paper), mechanisms, universe, span, point-in-time rules, horizons, θ per hypothesis
with its justification, the primary statistic (an event study clustered by date, or a
Fama–MacBeth series with Newey–West), power and the detectable effect, the development and
confirmation samples, the trial budget (combinations included), robustness, an assumed
post-publication decay, data adequacy per hypothesis (one without adequate point-in-time data
is not evaluable, never approximated), a constants hash and the lock.

STEP 2 — Exploration on the development samples with every trial in the registry; one
confirmation run through `obliskiq research run`.

STEP 3 — research/reports/batch-2.md with each outcome, its labels, power and limits. In the
registry: sufficient and inconclusive-positive → `shadow` with the grading rule Session 18
applies; insufficient → `retired` with its result; not evaluable → `idea` with the data it
needs. Never re-run with tweaks.

ACCEPTANCE: the pre-registration is on main before the results PR; the verdict is
reproducible by one command; shadow entries have tests and readiness statuses.
Finish with the end-of-session protocol in AGENTS.md.
```

---

## E. After the build (owner steps)

1. The machine from T1 (§A.5), set up per `docs/DEPLOY.md`: the paper credentials and the
   `read:packages` token there.
2. `deploy/update.sh v1.0.0` installs the released image.
3. Run `obliskiq preflight`; fix anything it reports in a short session (paste the output).
4. `obliskiq universe pin`, `obliskiq collect daily`, `obliskiq compute all`.
5. Set the paper managed-capital cap and the satellite budget ceiling in
   `config/owner.yaml`; run `serve` in shadow until `obliskiq promotion status` shows the
   L1 criteria met (L0 → L1). From here, Claude sessions do not call IBKR unless the bot
   is paused.
6. Switch to paper: fully autonomous. Use `/deposit` and `/withdraw` as needed, answer
   `/questions` when asked; observe until the L2 criteria are met.
7. Review `obliskiq promotion status`, the research reports and the scoreboard before any
   live-small step. Live-small is where real transaction costs are first measured, so it
   can come early with a small cap (MASTER_PLAN §5.19). Live credentials go on the machine
   only.

---

## F. After the build: feature and maintenance sessions

### F.1 Feature session

Model and effort: Opus 5.5, xhigh; Fable 5.1 (else Opus 5.5), xhigh, when the feature
touches sizing, execution, capital or tax. Network: Custom, unless the feature needs
research data.

```
You are working on ObliskIQ, a feature session. AGENTS.md is your operating manual;
docs/MASTER_PLAN.md the design.

FEATURE: <describe what you want, in your own words>

1. Read the MASTER_PLAN sections and DECISIONS entries the feature touches. If it changes
   the design, add a DECISIONS entry with its evidence and update MASTER_PLAN in the same
   PR. If it conflicts with a hard rule in AGENTS.md, stop and ask.
2. A new signal, pattern or sizing rule is pre-registered in research/preregistrations/
   before any test on data and tested by the research harness; it runs in shadow at zero
   weight only on a sufficient or inconclusive-positive outcome, and earns weight only on
   a sufficient one and its activation condition (DECISIONS D62, D65, D66).
3. If the deployed bot is running, make no IBKR calls: build against fixtures and queue
   VERIFY-LIVE items.
4. Branch f-<slug>; bump the minor version (patch for a fix) with its CHANGELOG section.

Finish with the end-of-session protocol in AGENTS.md; the release publishes on merge.
End by giving me the update command for the machine.
```

### F.2 Maintenance session (monthly, or when Dependabot PRs pile up)

Model and effort: Sonnet 5.5, xhigh.

```
You are working on ObliskIQ, a maintenance session. AGENTS.md is your operating manual.
1. Open Dependabot PRs: for each, read the upstream release notes for breaking changes,
   run make check, and merge the green ones; fix or close the rest with a reason.
2. Verify-yearly and dated values (tax rate table, market-data prices, commission
   schedules, transaction taxes, catalyst dates): refresh each with its source and date.
3. ROADMAP: close resolved follow-ups; list what is still open.
Bump the patch version with its CHANGELOG section, then finish with the end-of-session
protocol in AGENTS.md.
```

### F.3 Monthly tax-rules check (a scheduled Claude task)

Set it up after Session 7 merges: ask a Claude session to create a monthly scheduled task
on this repository with the prompt below (Sonnet 5.5, xhigh, network Full). It changes
nothing unless the law changed, and a change goes through the normal PR protocol.

```
You are working on ObliskIQ, the monthly tax-rules check. AGENTS.md is your operating
manual. For each active jurisdiction profile in config/jurisdictions/ (for es: the BOE
consolidated LIRPF and RIRPF, AEAT's current rate tables and manuals), compare the rules
and rates the profile encodes with the official sources as of today. If nothing changed,
append a dated "checked, no change" line to docs/knowledge/tax-<id>.md in a PR with no
version change. If something changed, update the profile data with citations, adapt the
policy code only if the rule itself changed, add tests, bump the patch version, and
follow the end-of-session protocol. Never guess: a rule you cannot confirm from an
official source is reported in the PR as unverified.
```

### F.4 Updating the machine

`deploy/update.sh v<version>` on the machine: it backs up the store, starts the new
version, checks health and rolls back automatically if the check fails. Releases are
listed on the repository's Releases page with their notes.
