# ObliskIQ Known Limitations

**Version:** 2.2 (claude/real-run-readiness)
**Last Updated:** 2026-10-02
**Repo version:** 6.15.0

## What this document is now

The honest, current list of what ObliskIQ does **not** do, what runs on
non-trustworthy data, and where the real risk is. The aspirational v1–v4
"roadmap" from the previous version has been removed — it described features and
phases that never shipped and a grading that no longer reflects reality.

For the full, call-path-accurate map of load-bearing vs scaffolding modules, see
`ObliskIQ/docs/SYSTEM_CENSUS.md`. For the structural map, see
`ObliskIQ/docs/REPO_DIGEST.md`. Those two are the live source of truth.

---

## Status of the first real run (6.15.0)

What is **verified offline** (golden-path E2E, `tests/test_golden_path_e2e.py`): the
real orchestrator runs Phase 1 → paper trade with scripted AI transports and a blocked
network, twice in a row, with provider failover (Gemini → OpenRouter → NVIDIA → Groq)
and every safety rail in the path.

What is **not verified, and cannot be from a sandbox** — it needs your secrets and a
network that serves Yahoo Finance:

- **No live run has ever completed.** Provider keys, the chosen free model IDs and the
  free-tier quotas are only knowable from a real call (`scripts/preflight.py --probe`,
  then the `smoke` dispatch of *Paper Run*).
- **Gemini free-tier limits** are visible only in AI Studio, and are per *project*, not
  per key — extra Gemini keys on one account do not add quota.
- **Free OpenRouter/NVIDIA model IDs rotate.** The router verifies at startup and the
  order/models are editable in `config/operator_panel.toml`; a rotated ID shows up as
  "All AI providers failed" with a per-provider reason.
- **Groq's free tier (≈8K tokens/min) cannot carry a full prompt**; a run served by a
  below-full-context provider may only hold/exit, never open new BUYs.
- **Yahoo Finance is the single point of failure for prices.** A total outage aborts the
  run before any AI call (`market_data_unavailable`); a partial outage degrades the
  affected blocks honestly. yfinance can rate-limit shared runner IPs.
- **Alpha Vantage free tier is ~25 calls/day/key**: fundamentals are cached 30 days and
  fall back to Yahoo; foreign listings use Yahoo directly.
- **Accuracy is unproven.** Nothing in the repo demonstrates a predictive edge. The AI
  scoreboard statistics, the paper gate and the sizing calibration are being redesigned
  so that an edge (or its absence) becomes detectable in months rather than years — see
  `docs/AI_SCOREBOARD.md` / `docs/PAPER_TRADING_GATE.md` for the current (pre-redesign)
  thresholds, which cannot discriminate skill from luck at today's sample sizes.

---

## The headline limitation: this is a CHASSIS, not proven alpha

The deterministic Strategy Engine's walk-forward verdict vs SPY is **FAIL**:

- **v1:** FAIL (`ObliskIQ/docs/STRATEGY_V1_VERDICT.md`).
- **v1.1** (pre-registered fixes — delta execution, BIL cash sleeve, point-in-time
  regime feed): **FAIL again** — OOS Sharpe 0.801 vs SPY 0.858; signal IC 0.039
  (p 0.22) → below the Simons gate (`ObliskIQ/docs/STRATEGY_V1_1_VERDICT.md`).

Per the pre-registration, that is a **hard stop**: no further parameter tweaks
against the 2016–2026 decade. v2 hypotheses (TLT directional sleeve, faster trend
gate, sector-neutral momentum) must be designed fresh and pre-registered.

**What this means:** the current, defensible value of ObliskIQ is a reliable,
honestly-instrumented **execution / risk / intelligence chassis** — Phase-1 free
data, deterministic Phase-2 screening, Kelly + regime risk rails, the
MonitoringSuite kill switch, paper-trading with a 90-day gate, and the AI
Judgment Scoreboard that grades every proposal. It is **not** a system with a
demonstrated risk-adjusted edge over the index. Treat all engine/AI output as a
reference signal pending a passing OOS verdict, not a trading recommendation.

---

## Demoted: the statistical-pattern pillar (Simons)

As of chore/v8-census the pattern subsystem is **demoted to research** and no
longer runs on the live `auto`/`paper` path:

- On honest data, **0 of 31 patterns are TRUSTED** (the augmented sample sizes
  that produced earlier high-confidence figures were fabricated and purged —
  `docs/PROVENANCE_PURGE_REPORT.md`).
- The pre-execution **revalidation gate** approved nothing, so it could only ever
  *falsely abort* a healthy run. It was removed from `run_pre_execution_checks`;
  the MonitoringSuite kill switch is now the sole blocking pre-exec gate.
- `pattern_library`, `pattern_detector`, `pattern_validation_gate`,
  `pattern_backtest`, `pattern_discovery`, `ic_provenance` remain **importable**
  for a future pre-registered OOS revalidation, but they are not a live pillar.

**Limitation:** until that revalidation is designed, run, and passed, ObliskIQ has
**no validated statistical-pattern signal** in production.

---

## Graded scaffolding (runs live, on non-trustworthy data)

These are wired but currently produce honestly-zeroed or empty output. Full table
with per-gap notes in `SYSTEM_CENSUS.md §2`:

| Area | Gap |
|------|-----|
| Asymmetry Hunter — sentiment (e) | Fear&Greed / put-call are **placeholders** → excluded. |
| Asymmetry Hunter — vol mispricing (b) | IV-rank warehouse **warming up** → 0 weight until history accrues. |
| Asymmetry Hunter — commodity (d) | Term-structure via MA **approximation**. |
| Asymmetry Hunter — polymarket/gdelt (g/h) | Velocity/anomaly stats **warming up**. |
| Phase-3 `pattern_performance` | `active_patterns` table is populated only by `run_weekly_pattern_discovery`, which has **no live trigger** → empty in the live path. |

---

## Genuine feature gaps (unchanged, still true)

### PostgreSQL warehouse
`modules/data/warehouse.py` supports SQLite; PostgreSQL requires `psycopg2-binary`
and raises if unavailable. Fine for single-node; a scale-out deployment needs the
PG path implemented and tested.

### Live / real-time market data
Historical data only (yfinance + free APIs). No real-time quote streaming, Level-2
book, or real-time options Greeks. IBKR write path exists but is **gated** behind
the 90-day paper-trading gate (`docs/PAPER_TRADING_GATE.md`).

### Portfolio optimization breadth
`opportunistic_optimizer.py` provides Kelly / mean-variance / Sortino. Not
implemented: Hierarchical Risk Parity, Black-Litterman with analyst views,
constrained max-Sharpe.

### Backtesting
The canonical walk-forward engine (`modules/testing/walk_forward_backtester.py`,
costs on) is the source of truth. Gaps: broader point-in-time universe (current
runs use a current-constituent snapshot — survivorship bias, disclosed in the
verdict docs), and a working Fama-French momentum-file parser
(`research/future/french_factors_downloader.py` — 5-factor downloads, momentum
file fails to parse).

### Notifications
Email + Telegram only. No SMS / push / Slack-Discord webhooks.

---

## Technical debt

### Test coverage
**Measured: 62.90% line coverage** over `ObliskIQ/modules`, from a real
`pytest -m "not live" --cov` run on this branch (v6.7.0) with **2,707 tests
passing, 0 failing** fully offline (no network, no provider keys).

The CI floor is now `--cov-fail-under=60` (`pytest.ini` + `pyproject.toml`), set
at the measured number minus a small margin so coverage can't silently regress.
This is an anti-regression line, **not** a target — see `docs/V8_ERROR_SWEEP.md`.

In CI the figure is typically a touch higher than 62.90%, because several tests
that need optional deps absent in the dev sandbox (vaderSentiment/yfinance/
wbgapi) run there. The earlier "~64% with files excluded" framing is
retired: the previously-hanging files (`test_phase1_intelligence`,
`test_phase3_ticker_analysis`) and the orchestrator suite are now handled
honestly (live-marked or mocked, see below) rather than excluded.

Weakest-covered areas: full-pipeline integration, options data fetching, sentiment
aggregation edge cases, and the IBKR/paper write paths.

### Test environment fragility (resolved in v6.7.0)
Tests that make **real network calls** (RSS/yfinance/CNN/CBOE) or need provider
keys are now marked `@pytest.mark.live` and **auto-skip offline** via
`tests/conftest.py::_live_enabled` (extends the existing `USE_MOCK_DATA`
convention). `pytest -m "not live"` is hermetic and green with no network/keys;
`RUN_LIVE_TESTS=1 pytest -m live` runs the live subset in CI. See `TESTING.md`
("two run modes") and `docs/V8_ERROR_SWEEP.md`.

### Documentation
`CLAUDE.md`'s non-editable narrative sections (Three Masterminds → Simons; "What
AI Receives → Pattern Triggers"; the OOS-revalidation audit) still present the
pattern library as a live pillar and should be reconciled with `SYSTEM_CENSUS.md`
by the maintainer (flagged in CLAUDE.md's editable Directory-Structure section).

### CI/CD
GitHub Actions runs tests + the twice-daily paper run. Missing: staging
environment, security scanning, dependency automation.

---

## Related documents

- `ObliskIQ/docs/SYSTEM_CENSUS.md` — load-bearing census (live map)
- `ObliskIQ/docs/V8_CENSUS_NOTES.md` — the v8 change record
- `ObliskIQ/docs/REPO_DIGEST.md` — structural map (regen: `make digest`)
- `ObliskIQ/docs/STRATEGY_V1_1_VERDICT.md` — the FAIL verdict + v2 hypotheses
- `docs/PROVENANCE_PURGE_REPORT.md` — why 0/31 patterns are trusted
- `docs/OPERATOR_RUNBOOK.md` — daily operation
