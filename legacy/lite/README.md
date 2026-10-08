# ObliskIQ-lite-MCP

Global volatility-dislocation research and execution workflow for an EU retail
IBKR account. Two components: an MCP server that owns the data and the
computation, and a Claude Code skill that owns the judgment.

**`ROADMAP.md` is the entry point** — it says what is open and which phase is
next. `docs/NEXT-SESSIONS.md` carries the paste-ready prompt for an open phase
when there is one, one session each, and the record of the prompts that have
already run. `docs/DECISIONS.md` says why the design is as it is, and which
alternatives were rejected on what evidence.

## Layout

```
ObliskIQ-lite-MCP/
├── ROADMAP.md                        what is open, and what is next — START HERE
├── BUILD-BRIEF.md                    architecture, the store schema, the tool surface
├── CLAUDE.md                         standing rules for every session
├── config.yaml                       every gate, cap, threshold and window
├── .env.template                     copy to .env and fill  (.env is gitignored)
├── .mcp.json                         IBKR MCP server config  (create locally)
├── universe.yaml                     the tracked instruments and futures roots
├── references/
│   ├── ibkr-playbook.md              IBKR API quirks, appended to as they are found
│   ├── macro-playbook.md             FRED / ECB / CFTC / EIA / ONS / StatCan, verified live
│   ├── marketdata-playbook.md        marketdata.app: the one-IP rule, credit tiers
│   └── commodities-universe.md       commodity proxies, purity caveats, regime signals
├── pyproject.toml                    package metadata and dependencies
├── src/obliskiq/
│   ├── config.py                     config.yaml loader — no key has a code default
│   ├── env.py                        environment loader — paths, keys, ports, tokens
│   ├── logging_setup.py              structured JSON logging
│   ├── errors.py                     the exception hierarchy
│   ├── util/                         safe_float, HTTP retry with capped backoff
│   ├── store/                        SQLite store and forward-only migrations
│   ├── quant/bsm.py                  Black-Scholes, greeks, implied-vol solver
│   ├── ibkr/                         OAuth 1.0a + Client Portal endpoint layer
│   ├── collect/                      the unattended collectors and their entrypoint
│   ├── compute/                      the grid, the engines and the risk maths
│   ├── server/                       the MCP server: tools, auth, entry gates
│   └── macro/                        risk-free rate, series declarations, source fetchers
├── Dockerfile                        the server, containerised
├── deploy/                           systemd units and cron for the VM
├── docs/DEPLOY.md                    step-by-step deployment, assuming no experience
├── docs/ORACLE-CLOUD.md              making the machine on Oracle Cloud
├── docs/GOOGLE-CLOUD.md              making the machine on Google Cloud
├── docs/COLLECTORS.md                collector operation and deployment
├── docs/COMPUTE.md                   the engines and the rules they enforce
├── docs/SERVER.md                    the tool surface, auth and deployment
├── docs/DECISIONS.md                 why the design is as it is, and what was rejected
├── docs/NEXT-SESSIONS.md             prompts: the open one, and those that ran
├── docs/archive/                     session reports, kept as evidence and not maintained
├── tests/
│   ├── test_skill_config_consistency.py   no guarded document restates a config value
│   ├── test_no_magic_numbers.py           no threshold is a literal in Python
│   ├── test_no_unread_config_keys.py      no config key is without a consumer
│   ├── test_no_unread_universe_keys.py    nor any key in universe.yaml
│   └── test_*.py                     config, env, store, quant, oauth, cp client
└── .claude/skills/obliskiq-lite/
    └── SKILL.md                      the skill itself
```

`BUILD-BRIEF.md` stays at the root deliberately. It describes how the system is
built, not how to think with it — inside the skill folder it would load
architecture into context every time the skill triggers.

## The division of responsibility

**`config.yaml` owns every number.** Gates, caps, thresholds, windows, tenors.

**`SKILL.md` owns judgment**: method, discipline, evidence, failure modes. It
names config keys rather than restating their values, so changing a threshold
never requires editing the skill. `tests/test_skill_config_consistency.py`
enforces this over SKILL.md, `BUILD-BRIEF.md` and this file — it fails if a
threshold-shaped number reappears in any of them.
`tests/test_no_unread_config_keys.py` enforces the inverse, so a key nothing
reads cannot sit in `config.yaml` looking like a decision.

**The MCP server (`obliskiq-lite-mcp`) owns state and computation**: the SQLite
store, the collectors, the grid, the ledgers, the risk maths. Ledger writes
validate against `config.yaml` and reject gate violations server-side, so the
skill is never the only thing standing between a bad trade and the ledger.

**One owner per fact.** `config.yaml` thresholds · `SKILL.md` judgment ·
`references/*-playbook.md` observed API behaviour · `BUILD-BRIEF.md`
architecture and schema · `ROADMAP.md` what is open · `docs/DECISIONS.md` why.

## Running a build session

1. Open the repository in Claude Code (web or CLI).
2. `ROADMAP.md` says which phase is next. If it names an open phase, copy that
   phase's prompt out of `docs/NEXT-SESSIONS.md` and paste it as the first
   message; if the next item is not a session, that file says so and says what
   running it needs instead.
3. Each session ends in one pull request. Do not start a session until the
   previous one's pull request is merged.

The store, the collectors, the computation layer, the server, the sleeve's
viability, live verification against the IBKR MCP connector, the first real chain
snapshot and a validation pass that recomputed indicators by hand from the raw
store are all behind it. What each session settled — and what it falsified — is
in `docs/DECISIONS.md`, with the session reports kept as evidence under
`docs/archive/`.

**`ROADMAP.md` owns what is left**, and this page deliberately does not restate
it: a second list of open work is a list that goes stale in one of the two
places. What remains at the time of writing needs a host rather than a session —
the marketdata.app backfill enforces one IP per account, which no cloud sandbox
can hold.

## Local configuration

1. Copy `.env.template` to `.env` and fill it in. `.env` is gitignored; never
   commit it.
2. Register the IBKR MCP server in `.mcp.json`. The connector configured on
   claude.ai does **not** carry over to Claude Code — it is a separate
   environment, and this is the step most likely to fail silently.
3. Confirm `.gitignore` still covers `.env`, `*.db`, `*.sqlite`, `*.sqlite3`
   and `data/`.

Collectors are a standalone entrypoint, separate from the server process, and
are designed to run unattended on a scheduler on a small always-on cloud VM.
Nothing in the pipeline assumes a human or a chat session is present.

## The collectors

```
obliskiq-collect preflight   the six OAuth 1.0a steps, one at a time. Writes nothing
obliskiq-collect universe    seed universe.yaml + the live account into the store
obliskiq-collect nav         one NAV observation. First in `daily`: today's figure only
obliskiq-collect backfill    historical and incremental bars
obliskiq-collect vendor-chain  US option chains, today or history (marketdata.app)
obliskiq-collect chain         EU option-chain snapshot           (irreplaceable)
obliskiq-collect curve         daily futures term structure       (recoverable)
obliskiq-collect macro       FRED / ECB / CFTC / EIA / ONS / StatCan
obliskiq-collect daily       the scheduled sequence, in order
obliskiq-collect status      what the last runs did. Reads the store, collects nothing
obliskiq-collect connector   probe the IBKR Custom Connector. No credentials needed
```

`daily` is ordered by what a missed day costs, irrecoverable first, and every step
runs inside its own guard: a step that fails is recorded with its reason and
stepped over, never allowed to abandon the collection that cannot be redone. Each
invocation writes one `collector_sequences` row carrying the steps it planned
before it runs any of them, so a day half collected is identifiable as such from
the store alone rather than from an exit code nobody saw.

`status` is the failure channel, and it is deliberately the only one: nothing here
phones home. It reads the store, so it works when every upstream is down.

Option-chain history **cannot be retrieved retrospectively from IBKR**, so for the
three European listings a day not collected is gone permanently. The twelve US
listings collect through `vendor-chain`, where history *is* retrievable. Everything else here can be rebuilt from the
APIs at any time — futures curves included, as a live check established: per-contract
`get_price_history` works and `search_futures` takes `include_expired`, so a gap
in `curve_snapshots` is recoverable and a gap in `chain_snapshots` is not. `docs/COLLECTORS.md` covers deployment,
scheduling, the sigma fallback ladder and how to read a run.

The IBKR side runs against recorded fixtures with no network at all, which is how
it was built. Seed the universe first — the collectors refuse to run against an
empty one rather than reporting success over nothing:

```
export OBLISK_DB_PATH=/tmp/oblisk.db
obliskiq-collect universe --source fixture --fixtures tests/fixtures/ibkr
obliskiq-collect daily --skip vendor-chain --skip macro --only SPY \
    --source fixture --fixtures tests/fixtures/ibkr
```

`--skip vendor-chain` is what keeps this offline. That step reaches
marketdata.app through a client of its own and ignores `--source`, so with
`MARKETDATA_API_TOKEN` set it spends credits on the live per-symbol tier — SPY
included. `docs/COLLECTORS.md` owns the rule.

Two outcomes that look like faults and are not. `universe` reports `partial` and
exits 1: the recorded fixtures cover a subset of the universe, so four
commodity-proxy symbols resolve to nothing. And `--only SPY` names no futures
root, so the curve step reports `skipped` — the collectors do not share a
namespace, and inside the `daily` sequence a filter naming no target is a filter
artifact rather than a failed collection. Asked for directly,
`obliskiq-collect curve --only SPY` still fails, because there you named
something specific and did not get it.

## The computation layer

```
obliskiq-compute solve-iv    invert mid prices into an ATM implied vol, per chain
obliskiq-compute grid        market x sector x the fifteen indicator families
obliskiq-compute rankings    the four ranked views and the correlation matrix
obliskiq-compute fx          the four-way decomposition per market
obliskiq-compute commodity   curves, wrappers, positioning, inventory, proxies
obliskiq-compute census      the weekly all-gates count
obliskiq-compute all         solve-iv, grid, rankings, fx and commodity, in order
```

A second entrypoint, separate from both the collectors and the server. It
**touches no network**: every engine reads the store the collectors filled and
writes derived tables back, which is what makes the whole layer testable in a
sandbox with no IBKR access, and why the systemd unit can enforce
`PrivateNetwork=true`.

Markets are measured on their **native index contract**, never a US-listed
wrapper, and a market x sector cell with no mapped instrument is written as
`n/a` with a reason rather than left out. Most cells are absent on a young
store; the coverage report says how many and why, grouped by reason code.
`docs/COMPUTE.md` covers the engines, the grid axes and the rules the code
enforces rather than the caller.

## The MCP server

```
obliskiq-mcp stdio     local transport for Claude Code; binds nothing
obliskiq-mcp http      streamable HTTP on OBLISK_MCP_HOST:OBLISK_MCP_PORT
```

The third entrypoint, and the only one that binds a port. It wraps the store, the
grid, the ledgers and the risk engine as sixteen MCP tools, so the workflow runs
from Claude Code **or** claude.ai. It reimplements nothing — every tool composes
the engines above — and it makes no network requests of its own: live quotes,
positions and NAV are passed in by whoever holds the IBKR connector.

Ledger writes are gated **in the server**. `log_entry` refuses a prohibited
instrument, an entry outside the tenor window, an illiquid contract, a premium
the sleeve cannot afford, an option entry that does not say how many contracts it
is, a book already at its concurrency cap, a mandatory close date that disagrees
with the trading calendar, and an add to a losing position with no logged
re-underwrite. `log_exit` refuses any reason outside
`discipline.exit_reasons_allowed`. No tool can edit a thesis.

The option sleeve budget is a **shared pool**: `log_entry` books what the entry
commits, and `available = budget_pct × NAV − penalty_balance − committed_premium`
until the position closes. That is what makes `option_sleeve.budget_pct` the cap
it is specified as, rather than an allowance each of
`option_sleeve.max_concurrent_positions` entries could claim in full.

Bearer token from `OBLISK_MCP_AUTH_TOKEN`. **The server refuses to start when it
would bind beyond loopback without one**, before anything binds — it holds the
position ledger and can write to it, and must never be reachable
unauthenticated. `docs/SERVER.md` covers the tool surface, the transports,
containerised and systemd deployment, and connecting a client.

## Running it on a free cloud VM

The collectors have to run **every day, unattended**. Option-chain history cannot
be retrieved retrospectively from IBKR (European listings only; US chains come
from marketdata.app, where history is retrievable), so a day nobody collected is gone
permanently — which is a laptop's failure mode, not a server's. Two clouds have a
free tier that covers this: Oracle's Always Free machine, and Google's Always Free
`e2-micro`. Google's is free apart from its public address, which is billed; the
walkthrough compares the two.

**[`docs/DEPLOY.md`](docs/DEPLOY.md) is a complete walkthrough written for someone
who has never used a server, a terminal or IBKR's API settings.** Every command is
copy-pasteable and says what it does. Budget about two hours, most of it waiting.
Making the machine is the one provider-specific part, and it has a page each:
[`docs/ORACLE-CLOUD.md`](docs/ORACLE-CLOUD.md) and
[`docs/GOOGLE-CLOUD.md`](docs/GOOGLE-CLOUD.md).

What it covers: registering IBKR OAuth 1.0a (the slow part — start it first),
choosing a provider and making the machine (including Oracle's "Out of capacity"
error, which everybody hits), the reserved address both clouds need, connecting
over SSH, installing, secrets, the schedule, reading a run, and the failures you
will actually meet.

**One thing to know before you start.** The IBKR half needs OAuth 1.0a approval,
which IBKR review by hand over several days. Until it comes through, the machine
runs the macro collectors and the whole computation layer, but collects **no**
option chains — the part that cannot be recovered later. Build the machine while
you wait; just do not mistake it for finished.

A session with the IBKR MCP connector open can take the option-chain photograph
in the meantime: see *Capturing a chain through the MCP connector* in
`docs/COLLECTORS.md`. It needs a human present, so it is a bridge until OAuth
arrives rather than a substitute for the machine.

The downloadable Client Portal Gateway and IB Gateway are **out of scope**: both
need a human to log in through a browser, which is precisely what an unattended
collector cannot do.

## Installing

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
```

Python 3.11+, tested in CI on 3.11, 3.12, 3.13 and 3.14. Dependencies are
declared in `pyproject.toml`; `requirements.txt` mirrors the runtime set for
environments that want it.

The floor stays at 3.11 deliberately. Nothing in the code needs a newer
language feature, and the collectors run unattended on a small cloud VM whose
system Python is whatever the distribution ships — Ubuntu 24.04 LTS is on 3.12,
Debian 13 on 3.13. A higher floor would mean installing and maintaining a
non-system interpreter on the one machine whose job is to need no attention.
The floor is not a ceiling: running on 3.14 is supported and tested.

## Running the tests

```
pytest -q
ruff check src tests && ruff format --check src tests
```

Activate the virtualenv rather than calling `.venv/bin/python -m pytest`. Two
tests in `tests/test_mcp_cli.py` resolve the `obliskiq-collect` console script
through `PATH` — the server shells out to the collectors, and that lookup is the
thing under test — so they fail when the script is installed but not on `PATH`.
CI installs with `pip install -e` on a runner where console scripts land there,
which is why this only shows up locally.

The skill/config consistency check also runs standalone with only PyYAML
installed, which is how CI runs it before the rest of the suite:

```
python3 tests/test_skill_config_consistency.py
```

## Where the numbers live

No threshold, window, floor or cap is written as a literal in Python. That
includes the engineering parameters — solver tolerances, retry backoff, SQLite
pragmas, IBKR field ids — which live under `runtime`, `http`, `numerics`,
`macro` and `ibkr` in `config.yaml`. `obliskiq.config` raises on a missing key
rather than supplying a default, because a default in code is a magic number
that has learned to hide.

## IBKR access

The Client Portal Web API is reached over **OAuth 1.0a self-service** with RSA
signing, registered through a web form in IBKR account settings. It runs
headless, which is what the collectors need.
[`docs/DEPLOY.md` §3](docs/DEPLOY.md) walks through the registration,
including the `openssl` commands for the keypairs and which variable each
artifact lands in.

The downloadable **Client Portal Gateway and IB Gateway are out of scope**. Both
require an interactive local login and cannot run unattended on a VM.

TLS verification is never disabled. `OBLISK_IBKR_CA_BUNDLE` may point at a custom
CA bundle; an unreadable bundle is a startup error, not a downgrade.

## Not investment advice

This is a research and analysis tool. It does not account for tax position or
full personal circumstances, and nothing it produces is personalised investment
advice.

## Licence

**GNU Affero General Public License v3.0 or later** (`AGPL-3.0-or-later`).
The full text is in [`LICENSE`](LICENSE), verbatim and unmodified; every source
file under `src/` carries an `SPDX-License-Identifier` line naming it.

Copyright (C) 2026 Alvaro de Luna Luque.

The AGPL's **section 13** is the clause that matters for this project rather
than being boilerplate: this ships a server users interact with over a network,
so anyone who runs a modified version and lets others reach it over a network
has to offer those users the corresponding source of their modified version.
Running it unmodified for yourself, which is what the deployment in
`docs/DEPLOY.md` describes, carries no such obligation.

This program is distributed in the hope that it will be useful, but **WITHOUT
ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the licence for details, and *Not investment
advice* above for what that means in this program's particular case.
