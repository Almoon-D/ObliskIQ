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

### D6 · Options: one block when IV is cheap, on real-time quotes (§4, §5.6, §5.13)

Rejected: adding to long options after the move confirms, which pays a higher premium for the
same convexity, and delayed quotes for options, whose wide spreads make stale mids misprice
(MASTER_PLAN §4, lines 163 and 168). Pyramiding is for shares only; stocks, ETFs, funds and
bonds accept delayed quotes within a maximum age.

### D7 · Bonds by a computed rule; swap partners on a different index (§4, §5.3, §5.16)

A direct bond qualifies only when its minimum increment and per-order minimum commission fit
the role's amount and it beats the best fund all-in — no account-size threshold. Swap
partners differ in issuer **and** index by default: RIRPF art. 8 makes securities homogeneous
by issuer ("un mismo emisor"), and a different index removes any anti-avoidance argument at
negligible tracking cost.

### D8 · Two IBKR transports behind one client (§4, §5.12)

OAuth 1.0a where IBKR grants it; otherwise the Client Portal Gateway with automated login,
unattended in paper (paper logins have no second factor). Evidence: IBKR told the owner on
2026-10-08 that OAuth 1.0a is for institutional clients, while lite kept the OAuth route as its
route of record (`legacy/lite/docs/DECISIONS.md:248-250`) and ran it live on 2026-09-10
(`…/DECISIONS.md:2124`): 3,832 chain rows, 21 underlyings (`legacy/lite/ROADMAP.legacy.md:127-129`).

### D9 · Standard CPython 3.14, not free-threaded (§4, §7)

Parallel computation runs through a process pool (`compute.workers`) and the code stays
thread-safe; Session 14 benchmarks 3.14t and switches only if it is clearly faster with every
dependency available. Evidence: numpy releases the GIL inside its kernels; 3.14t is slower
single-threaded and not every scientific wheel ships a free-threaded build.

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
lifetimes and 4.3% of firms created all net wealth 1926–2016 (Bessembinder 2018).

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
version would be a new entry here.

### D23 · The LLM contract also keeps out the broker

`obliskiq.llm` may import neither `decision`, `execution` nor `ibkr`: the order endpoints live
in `ibkr`, and the LLM never builds or sends orders (AGENTS.md). This adds a contract and
leaves the plan's rule (§5.10) as written. Each import contract was shown to break on a probe.
