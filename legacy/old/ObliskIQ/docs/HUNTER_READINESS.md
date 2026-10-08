# Asymmetry Hunter — Readiness & Grading Clock

**Version:** v6.9.0 (updated v6.11.1) · **Status:** operator-facing

This is the deliverable for *Prompt 3 — Asymmetry Hunter Readiness + Start the
Grading Clock*. It makes the Hunter's state **honest and visible** and confirms
that grading has started, so in ~6 months we get an honest verdict on the Soros
layer. **No new signals** are introduced here.

> **Update (v6.11.1):** this doc was written while COT crowding (a) was still
> mocked (`Prompt 3` shipped before `Prompt 1`, v6.10.0 COT-Real). The example
> output, gate table, and firing-posture prose below have been updated to the
> live, reproduced state — see `docs/COT_REAL.md` and `docs/V9_RECONCILE_NOTES.md`.

It documents three things:

1. the `python main.py hunter-status` output spec,
2. how the grading clock is wired (and that dossiers are accruing), and
3. the daily-digest integration.

---

## 1. `python main.py hunter-status` — output spec

A **pure status query**: it runs no pipeline phase and makes no network call. It
inspects the *current* warm-up state of every component directly — warehouse
snapshot counts, `AsymmetryConfig` gates, the static event calendar — via
`asymmetry_hunter.compute_hunter_readiness()` and renders it with
`format_hunter_readiness()`. It works **before any pipeline run** (it does not
depend on a prior watchlist) and needs **no `GEMINI_API_KEY`**.

### What it prints

Real capture, reproduced 2026-07-01 against a warehouse backfilled via
`scripts/backfill_cot_history.py` (2015→present) — see `docs/COT_REAL.md` §6 for
the first captured confirmation. Exact per-contract week counts and calendar
fields (`NFP in N day(s)`) drift day to day; the component states and overall
shape are what matters here:

```
==================================================================
ASYMMETRY HUNTER — READINESS  (pure status query, no run)
==================================================================
Generated:    2026-07-01T16:51:31+00:00
Weights hash: 68d73c487e7ea5fe
Candidate rule: ≥ 3 INDEPENDENT components must agree at extremes

Components currently LIVE: 4 of 7  (crowding, regime_fragility, cross_asset, sentiment_extreme)

Per-component readiness:
  ✅ [LIVE       ] (a) Crowding (CFTC COT)   Crude Oil 155/52, Gold 155/52, 10-Year Treasury 155/52, 30-Year Treasury 155/52, Euro 155/52, S&P 500 155/52
        gate: Real CFTC COT feed + ≥52-week history → large-spec percentile ≥ 85 casts a contrarian vote
        LIVE for: Crude Oil, Gold, 10-Year Treasury, 30-Year Treasury, Euro, S&P 500. No COT coverage (structural 0-weight): ITA, EWG, EWJ, EWZ. Latest COT report: 2026-06-23.
  ⏳ [WARMING-UP ] (b) Vol mispricing (IV rank/skew)   GLD 0/60, TLT 0/60, SPY 0/60
        gate: ≥ 60 daily IV snapshots per asset → IV rank ≥ 90 (rich) or ≤ 10 (cheap)
        … NOT collected (structural 0-weight): USO, IEF, FXE, ITA, EWG, EWJ, EWZ — outside the IV-snapshot universe.
  ✅ [LIVE       ] (c) Regime fragility
        gate: Regime-transition composite ≥ 60/100 AND score trend rising → risk-off vote
  ✅ [LIVE       ] (d) Cross-asset divergence
        gate: |HY-OAS z − VIX z| ≥ 2.0σ (credit/VIX divergence) → risk-off vote
        … the per-asset commodity term-structure sub-signal is an MA-based APPROXIMATION …
  ✅ [LIVE       ] (e) Sentiment extreme
        gate: Composite sentiment ≥ 90th (euphoria) or ≤ 10th (panic)
        … Reddit / Twitter / CNN F&G / CBOE put-call — placeholders excluded.
  ⏳ [WARMING-UP ] (g) Polymarket divergence   0 snapshots (5 markets configured)
  ⏳ [WARMING-UP ] (h) GDELT geopolitical pulse   0/20 obs
  ✅ [LIVE       ] (f) Catalyst window (gate)   NFP in 1 day(s) [PROXIMATE]

Expected firing posture:
  RARE — 4 of 7 weighted components are LIVE (crowding, regime_fragility,
  cross_asset, sentiment_extreme); the candidate rule needs ≥3 INDEPENDENT
  components agreeing at extremes, so firing stays rare by design. It stays
  rare until more per-asset components warm up (e.g. IV ≥60 snapshots).

Last pipeline run: none yet — run `python main.py auto --paper` to generate one.
==================================================================
```

### Per-component gates (the exact bar each must clear)

| # | Component | State logic | Gate shown |
|---|-----------|-------------|------------|
| a | **Crowding (CFTC COT)** | `LIVE` if the mapped contract has ≥ `MIN_COT_HISTORY_WEEKS` (52) weekly observations, else `WARMING-UP`; reports `N/52` per contract. No CFTC contract exists for ITA/EWG/EWJ/EWZ → structural 0-weight for those four | real CFTC feed + ≥ 52-wk history → large-spec pctl ≥ `crowding_extreme_pctl` (85) |
| b | **Vol mispricing (IV rank/skew)** | `LIVE` if any IV-collected Hunter asset has ≥ `min_iv_history_days` (60) snapshots, else `WARMING-UP`; reports `N/60` per ticker | ≥ 60 daily IV snapshots/asset → IV rank ≥ 90 (rich) or ≤ 10 (cheap) |
| c | **Regime fragility** | `LIVE` — deterministic, always on | transition composite ≥ `regime_fragility_extreme` (60) AND trend rising |
| d | **Cross-asset divergence** | `LIVE` — credit/VIX z real; commodity term-structure sub-signal flagged **APPROXIMATION** | \|HY-OAS z − VIX z\| ≥ `cross_asset_z_div` (2.0σ) |
| e | **Sentiment extreme** | `LIVE` — regional matrix + social composite; placeholders excluded | composite ≥ `sentiment_high_pctl` (90) or ≤ `sentiment_low_pctl` (10) |
| g | **Polymarket divergence** | `LIVE` if a snapshot exists within `POLY_FRESH_DAYS` (4), else `WARMING-UP`; reports last-snapshot age + markets covered | curated markets snapshotted + \|our_prob − market_prob\| ≥ `polymarket_divergence_extreme` (0.20) |
| h | **GDELT pulse** | `LIVE` if best-region Welford count ≥ `gdelt_min_history` (20) AND fresh within `GDELT_FRESH_DAYS` (4), else `WARMING-UP`; reports `N/20` + last-pulse age | per-region vol z ≥ `gdelt_anomaly_z` (2.5) with negative tone |
| f | **Catalyst window** | `LIVE` if the static calendar loads (a window/gate — **no weight, not independent**) | next FOMC/CPI/NFP ≤ `catalyst_window_days` (10) |

**Honesty note (vol):** the Phase-1 IV snapshot collector only persists the
`strategy_universe.iv_snapshot_universe()` set. Of the Hunter's mapped ETFs only
**SPY / TLT / GLD** fall inside it; **USO, IEF, FXE, ITA, EWG, EWJ, EWZ** never
accrue IV history, so the vol component is a **structural 0-weight** for them. The
readout states this plainly rather than implying they will warm up.

### Firing posture

- **`RARE`** — `live_independent_count ≥ min_components` (currently 4 ≥ 3): the
  Hunter *can* technically fire, but only when ≥ 3 independent components hit
  extremes **and agree on direction**. Most runs emit zero (silence is a feature).
- **`DORMANT`** — fewer than `min_components` are LIVE: the Hunter cannot fire at
  all until more components come online.

The three deterministic market-wide components (regime / cross-asset / sentiment)
are LIVE from day one; **COT crowding (a) is also LIVE** as of v6.10.0 — the
one-time backfill (`scripts/backfill_cot_history.py`) made the CFTC feed's
52-week history warm immediately. The remaining data-accruing components (vol,
polymarket, gdelt) move the posture from "rare" toward "regularly possible" as
their own history accrues.

---

## 2. Grading-clock wiring (the scoreboard accrual is confirmed)

### Dossiers accrue to `ai_proposals`

Already wired and verified end-to-end:

- **Phase 1** runs `asymmetry_hunter.hunt()` and persists the watchlist to
  `reports/dossiers/latest_watchlist.json`
  (`phase1_intelligence.py`).
- The **orchestrator** logs each candidate via
  `AIScoreboard.log_asymmetry_dossiers(candidates, weights_hash=…)`
  (`orchestrator.py`), writing one row per dossier with
  `source='asymmetry_hunter'`, `prompt_hash = weights_hash`, `phase='asymmetry'`,
  conviction = `composite/10`, `position_size_pct = max_premium_pct`.
- `score_due_proposals()` scores those rows against SPY at **2/4/8/12 weeks** — the
  same machinery, and same bar (`MIN_PROPOSALS_FOR_VERDICT = 60`), as the
  MasterPrompt.

### Separate Soros-layer verdict

`AIScoreboard.compute_asymmetry_scoreboard()` filters to
`source='asymmetry_hunter'` and renders the verdict with Soros-appropriate wording
(`layer_label="Asymmetry Hunter (Soros layer)"`, `simplify_hint="retire the Soros
overlay"`). `python main.py scoreboard` prints it as a **separate** block below the
MasterPrompt verdict, and `render_report_markdown()` adds a
`## 🦅 Asymmetry Hunter (Soros layer)` section with its own horizon table.

### The clock in the header

`AIScoreboard.asymmetry_grading_clock()` returns the factual clock, surfaced in
both the CLI and the monthly report header (`## ⏱️ Asymmetry Hunter — Grading
Clock`):

- **Components currently LIVE** — delegated to `compute_hunter_readiness()` (DRY).
- **Dossiers logged** and **dossiers scored at 12w / 60 required**.
- **Estimated weeks to first verdict** — a best-effort estimate from the logging
  cadence plus the 12-week maturation floor. Because candidates are rare by design,
  this is typically **data-limited, not time-limited**, and the note says so.

Example CLI block:

```
----------------------------------------------------------------------
🦅 ASYMMETRY HUNTER (Soros layer)
----------------------------------------------------------------------
Components currently LIVE: 4 / 3 required
Dossiers logged: 0; scored at 12w: 0 / 60 required for a verdict
(Indeterminate — too few dossiers logged to estimate a cadence. Dossiers are
 rare by design; the clock is data-limited, not time-limited.)

Soros-layer verdict: INSUFFICIENT DATA — need 60+ scored proposals …
```

---

## 3. Daily-digest integration

`reports/paper/daily_digest.md` (overwritten every paper run by
`paper_runner.generate_daily_digest`) gains a one-screen **`## Asymmetry Hunter`**
block so the operator reads ONE file and knows the Hunter's state
(`_hunter_digest_block()`):

```
## Asymmetry Hunter

- Components LIVE: **4 / 7** (candidate rule needs ≥3 independent)
- Dossiers this run: **0** (silence is a feature; most runs emit zero)
- Convexity sleeve used: **0.0% / 5.0%** cap
- _Full readout: `python main.py hunter-status`_
```

Sleeve usage is the sum of the last watchlist's `max_premium_pct` against the
`RiskEnforcer` convexity-sleeve cap (5%). The block always renders, degrading to a
single "unavailable" line rather than ever breaking the digest.

---

## 4. What "done" means

- `hunter-status` truthfully reports per-component readiness (LIVE / WARMING-UP
  with `N/threshold` / MOCKED-OFF) with the exact gate for each — **verified**.
- Dossiers log to `ai_proposals` with `source='asymmetry_hunter'` + the current
  `weights_hash`, and `scoreboard` surfaces a **separate Soros-layer verdict** once
  ≥ 60 are scored — **verified**.
- The daily digest shows the Hunter's state — **verified**.

The verdict on the Soros layer is **earned over ~60 scored dossiers, the same
standard as the MasterPrompt** — not asserted, not scheduled.

---

## 5. Code map

| Concern | Location |
|---------|----------|
| Live readiness (pure) | `modules/analytics/asymmetry_hunter.py` → `compute_hunter_readiness`, `format_hunter_readiness` |
| CLI command | `main.py` → `hunter-status` branch |
| Warehouse freshness reads | `modules/data/warehouse.py` → `latest_polymarket_date`, `latest_gdelt_date`, `max_gdelt_history`, `count_distinct_polymarket_markets`, `count_iv_snapshots` |
| Soros verdict + clock | `modules/analytics/ai_scoreboard.py` → `compute_asymmetry_scoreboard`, `asymmetry_grading_clock`, `_render_asymmetry_clock_md` |
| Scoreboard CLI | `main.py` → `scoreboard` branch |
| Digest block | `modules/execution/paper_runner.py` → `_hunter_digest_block`, `_render_digest_markdown` |
| Tests | `tests/test_hunter_status.py`, `tests/test_ai_scoreboard.py`, `tests/test_paper_runner.py` |
