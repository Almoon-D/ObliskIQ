# AI Judgment Scoreboard (v6.16.0)

**Does any AI layer carry information about forward returns beyond luck — and how long will it take to know?**

The MasterPrompt drives Phase 2 (ticker proposals) and Phase 4 (trade recommendations). The scoreboard
grades them against what the market actually did. v6.16.0 rebuilt its statistics, because the v5.40 verdict
could not tell skill from luck at any sample size this system will ever produce (see *Why the old verdict
could not work*).

> Jim Simons: *"If it's not statistically significant, it doesn't exist."* — and a p-value only means
> something for ONE pre-specified test.

---

## What it does

1. **Logs every AI output at proposal time** (`ai_proposals`): Phase 2 proposals, Phase 4 decisions,
   Asymmetry Hunter dossiers, and the **weekly forecast panel** — each with the `provider:model` that actually
   answered (so a fallback provider's quality is measurable) and a prompt-version hash.
2. **Scores them retrospectively** (Phase 1 of each session) at 2 / 4 / 8 / 12 weeks (panel: 4 / 12 weeks):
   forward **USD return minus the proposal's own regional benchmark**.
3. **Judges each layer separately** with one pre-registered test (below). Layers are never pooled.
4. **Measures calibration** of the AI's stated probabilities and feeds the result to position sizing.
5. **Writes a monthly report** (`reports/scoreboard/`) and prints the same via `python main.py scoreboard`.

---

## The pre-registered verdict (do not tune these against data)

| Rule | Value | Constant (`ai_scoreboard.py`) |
|---|---|---|
| Observation | one per (layer, ticker, direction, ISO week) — earliest wins | `dedupe_observations` |
| Primary horizon | **4 weeks** — no data-dependent horizon pick | `PRIMARY_HORIZON` |
| Unit of analysis | the **weekly cohort**: mean excess return of that week's independent observations | — |
| Statistic | mean of cohort means, **Newey-West** SE (lags = horizon − 1 = 3, floored at the iid variance — overlap can only add dependence), 95% interval | `NW_LAGS` |
| Minimum before any verdict | **26 weekly cohorts** and **30 independent observations** | `MIN_WEEKS_FOR_VERDICT`, `MIN_OBS_FOR_VERDICT` |
| Meaningful edge | **+0.5 pp** per 4 weeks (forecast panel: rank IC **0.03**) | `MEANINGFUL_EXCESS_PP`, `MEANINGFUL_PANEL_IC` |

| Status | Condition |
|---|---|
| `insufficient_data` | fewer than the minimum cohorts/observations. Nothing is concluded. |
| `adds_value` | the whole 95% interval is above 0. |
| `anti_predictive` | the whole 95% interval is below 0. |
| `decoration` | the interval includes 0 **and its upper bound is below the meaningful edge** — it rules out an edge worth having. |
| `inconclusive` | the interval includes 0 but cannot rule out a meaningful edge. **Absence of evidence is not evidence of absence.** |

Excess return is `position_return(USD) − benchmark_return`, with the benchmark chosen per proposal
(`global_vehicles.benchmark_for_symbol`): US names → SPY; a foreign listing, ADR or country ETF → the first
(broad) ETF of its region (e.g. Japan → EWJ, Europe → VGK); a benchmark that is itself proposed → ACWI.
Non-USD listings are converted with the Yahoo FX leg stored on the row; a missing FX or benchmark quote leaves
the horizon unscored (retried until stale), never a made-up return. AI picks are also paired against the
equal-weight forecast-panel universe over the same weeks (`vs_universe_baseline`), as a deterministic comparator.

### Why the old verdict could not work

The v5.40 verdict ran a proportion test of "beat SPY" over every logged row: (1) the same name proposed on
consecutive runs counted several times though its returns overlap almost completely; (2) "beats SPY half the
time" is not the no-skill null of a long-biased book; (3) SPY is the wrong yardstick for a Tokyo listing; (4) 60
rows detect only a hit rate ≥ ~68%; (5) Phase 2, Phase 4, dossiers were pooled. Detecting a rank IC of 0.05 on
*proposals* needs ≈ ((1.96 + 0.84) / 0.05)² ≈ **3,100 independent observations** — about a decade at the rate
this pipeline emits them.

---

## The weekly forecast panel — the statistically powered test

`modules/analytics/forecast_panel.py`. Once per ISO week (first run of the week, ~5 free-tier AI calls) the AI is
asked the **same question about the same fixed 188-name universe** (strategy mega-caps, regional ETFs/ADRs,
core and sector ETFs; sha-hashed — `universe_hash`, pinned by a test):

> for each instrument: P(its 4-week USD return beats ACWI) and its expected 4-week excess return.

The AI sees only trailing price statistics (1m / 3m / 12-1m momentum, 60d volatility, distance from the 52-week
high) and a one-line market context — never outcomes. The 12-1 momentum value is **logged alongside** every
forecast so the question "does the AI add anything to the oldest factor in the book?" is answered by incremental
IC, not asserted. Outcomes arrive 28 days later.

Reported (`python main.py scoreboard`, monthly report): per-cohort **rank IC** vs the realised excess return, the
momentum baseline's IC, **incremental IC**, **top-decile − bottom-decile spread**, **Brier skill** against the
base rate, and a **reliability table**.

**Honest power.** One cohort's rank IC has standard error ≈ 1/√188 ≈ 0.073. Consecutive weekly cohorts share 3 of
their 4 weeks of returns, so ~4 weekly cohorts carry the information of one: a year (52 weeks) ≈ 13 independent
cohorts → SE ≈ 0.02, which resolves a true IC of roughly **0.06 at 80% power**; the 26-week minimum
(≈ 6–7 independent cohorts, SE ≈ 0.03) resolves roughly 0.08. A real IC below ~0.05 is invisible to any
experiment this size — and an IC that small is not a basis for trading either. The panel's job is to find out
quickly whether the AI is *useful at all* (≥ 0.06) or *noise* — in months instead of a decade.

Safeguards: a stale price feed or short history is never forecast (no filling in); a cohort is logged only when
≥ 60% of the universe was forecast, otherwise the week retries on the next run; two consecutive failed AI batches
stop the run; the panel uses its **own circuit-breaker scope**, so an optional workload hitting a rate limit can
never block Phase 2/4 of a trading run; `FORECAST_PANEL_ENABLED=0` turns it off.

---

## Calibration → sizing (pre-registered)

A language model's stated win probabilities are uncalibrated until measured, and Kelly sizing is brutally
sensitive to an overstated edge. `AIScoreboard.calibration_state()` returns the share of the stated edge that may
be believed; the orchestrator passes it to `Phase4DecisionEngine.apply_quarter_kelly_clamp(probability_shrink=…)`,
which sizes from `0.5 + shrink × (stated − 0.5)`:

* **shrink = 0.5** (keep half the stated edge) until the forecast panel has **≥ 100 graded forecasts across ≥ 8
  weekly cohorts with a positive Brier skill** against the base rate;
* then **shrink = 1.0**. Any error or doubt stays at 0.5. Both the stated and the effective probability are
  recorded on every decision.

---

## Where it plugs in

| Pipeline step | Hook | What happens |
|---|---|---|
| **Phase 1** (`run_phase1`) | `score_due_proposals()`, `generate_value_added_report()`, `run_weekly_forecast_panel()` | Settles the past (batched price lookups), writes the monthly report, logs this week's panel cohort. |
| **Phase 2** | `log_phase2_proposals()` | Every AI-proposed ticker logged. |
| **Phase 4** | `log_phase4_proposals()` | Every AI decision logged **before** conviction/sizing overrides — we grade the AI's own judgment. |
| **Phase 4 sizing** | `calibration_state()` | Sets the probability shrink for the Kelly clamp. |

All hooks are defensively wrapped — the scoreboard can never break the trading pipeline (Dalio: graceful
degradation).

---

## Schema (`ai_proposals`)

One row per (run, phase, ticker). Key columns:

| Column | Meaning |
|---|---|
| `proposal_id` (unique) | `{run_id}_{phase}_{ticker}` — idempotent logging |
| `phase`, `source` | `phase2` / `phase4` / `asymmetry` / `panel`; `masterprompt` / `asymmetry_hunter` / `forecast_panel` |
| `iso_week` | `YYYY-Www` cohort key |
| `ticker`, `direction`, `action`, `conviction` | the proposal |
| `win_probability`, `payoff_ratio` | AI-stated Kelly inputs (Brier-scoreable); panel: P(outperform) |
| `expected_excess`, `baseline_signal`, `universe_hash` | forecast panel only |
| `benchmark_ticker`, `fx_symbol`, `fx_price_at_proposal` | USD / regional-benchmark scoring |
| `model_name`, `prompt_version`, `prompt_hash`, `system_version` | attribution (`model_name` = `provider:model` that answered) |
| `pos_ret_{2w,4w,8w,12w}`, `spy_ret_*`, `excess_*` | forward-return scoring — **`spy_*` columns hold the BENCHMARK's price/return** (names kept to avoid a destructive migration) |
| `scored_horizons` (JSON) | which horizons are scored — makes scoring idempotent |

---

## Design guarantees

- **Idempotent** — each (proposal, horizon) is scored at most once.
- **Non-fatal** — every public entry point swallows and logs its own exceptions.
- **No network at proposal time** — logging is a pure DB write.
- **Network frugal** — memoised per (symbol, date); one batched request per date when the fetcher supports it;
  the panel is graded at 4w/12w only.
- **Stale-safe** — a horizon with permanently-missing prices is closed out with NULLs 30 days after maturity.
- **Reproducible** — bootstrap intervals are seeded; a verdict does not change between two runs on the same data.

---

*Implemented in `modules/analytics/ai_scoreboard.py`, `forecast_panel.py`, `stat_utils.py`. Tests:
`tests/test_ai_scoreboard.py`, `test_scoreboard_statistics.py`, `test_forecast_panel.py`,
`test_stat_utils_inference.py`.*
