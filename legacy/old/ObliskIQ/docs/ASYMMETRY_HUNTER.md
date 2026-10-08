# Asymmetry Hunter v1 — The Soros Overlay

**Version:** 6.5.0 (updated v6.11.1) · **Status:** deterministic, graded, code-enforced

> George Soros: *"Markets are driven by perception. Find where perception will break."*

## 1. Purpose & philosophy

The Asymmetry Hunter finds asymmetric, episodic opportunities across markets —
Soros-style — **without letting an LLM hallucinate reflexivity twice a day**. It
is **deterministic**: it fuses signals the pipeline *already computes* into a
scored watchlist. The AI may later *narrate* the candidates the math surfaced;
it **never invents candidates**.

- Runs at the **end of Phase 1**; output attached to the Phase 1 report and
  injected into the Phase 2 prompt as a clearly-labeled **ASYMMETRY WATCHLIST**
  section (with an explicit *"do NOT invent candidates"* instruction).
- An asset becomes a candidate **only when ≥ 3 INDEPENDENT components agree on the
  same direction**. Most runs emit **ZERO** — *silence is a feature*. Soros trades
  are episodes, not a daily quota.
- Every dossier is **graded** by the AI Scoreboard on the same standard as the
  MasterPrompt, so in ~6 months we know whether the Soros layer adds value, is
  decoration, or is anti-predictive.

Entry point: `modules/analytics/asymmetry_hunter.py::hunt(phase1_data) -> dict`.
It is pure, synchronous, and **never raises** (degrades to `{"status": "error"}`).

## 2. The components

Each component reads an already-computed Phase 1 key and emits a `ComponentScore`
(0–100 magnitude + direction). A component backed by mocked / approximate /
immature data **degrades to 0 weight** with an honest `status` flag — never a
fabricated number (CLAUDE.md anti-fabrication rule). Components **a–e** plus the
two v6.5.0 external evidence streams **g–h** are weighted votes; **f** is a
window/gate.

| # | Component | Source (Phase 1 key) | Extreme rule | Honest degradation |
|---|-----------|----------------------|--------------|--------------------|
| a | **Crowding** (`crowding`) | `cot_positioning` | large-spec percentile ≥ `crowding_extreme_pctl` (crowded long → contrarian short) **or** ≤ `100 − crowding_extreme_pctl` (crowded short → contrarian long) — both tails fire | **REAL** CFTC feed + 3y+ `cot_history` (v6.10.0); percentile null until 52 weekly obs → `warming_up`, 0 weight; feed down → `unavailable` |
| b | **Vol mispricing** (`vol_mispricing`) | warehouse `get_iv_history` | IV rank ≥ `iv_rank_high` (stress) / ≤ `iv_rank_low` (cheap convexity); `|25Δ skew|` ≥ `skew_extreme` | `iv_rank` null until 60 snapshots → `warming_up`, 0 weight |
| c | **Regime fragility** (`regime_fragility`) | `regime_transition` (detector) | composite ≥ `regime_fragility_extreme` **and** trend rising → risk-off | detector error → `error`, 0 weight |
| d | **Cross-asset divergence** (`cross_asset`) | `cross_asset_divergence` (HY OAS z vs VIX z) **+** `commodity_futures` term structure | `|hy_z − vix_z|` ≥ `cross_asset_z_div`; strong backwardation/contango flip | commodity curves are MA-approximations → `approximation` |
| e | **Sentiment extreme** (`sentiment_extreme`) | `sentiment` / `regional_sentiment` (real matrix/social) | score ≥ `sentiment_high_pctl` (euphoria → contrarian risk-off) / ≤ `sentiment_low_pctl` (panic → contrarian risk-on) | hardcoded Fear&Greed (=50) and put/call (=0.85) are placeholders → excluded, `placeholder` |
| f | **Catalyst proximity** | `config/event_calendar.json` | days to next FOMC/CPI/NFP ≤ `catalyst_window_days` | a **window/gate**, NOT a weighted vote; missing file → `error` |
| g | **Polymarket divergence** (`polymarket_divergence`) | `polymarket_divergence` (curated Gamma markets vs our data stance) | `|our_prob − market_prob|` ≥ `polymarket_divergence_extreme` → risk-off when we're more bearish than the crowd | feed unreachable / no data-anchored market → `unavailable`, 0 weight |
| h | **GDELT pulse** (`gdelt_pulse`) | `gdelt_pulse` (per-region volume/tone + Welford z) | region volume z ≥ `gdelt_anomaly_z` **and** tone ≤ `gdelt_tone_max` → stance from the static `config/gdelt_regions.json` map | Welford immature → `warming_up`; feed down → `unavailable` |

See **`docs/GEO_INPUTS.md`** for the full g/h methodology (APIs, configs,
mappings, velocity, component math).

**Direction convention.** Market-wide signals (c, d-divergence, e, **g**) map to a
per-asset direction via the asset's **risk polarity**: a *risk-off* read is SHORT
for risk-on assets (SPY, USO, FXE, ITA, EWG/EWJ/EWZ) and LONG for safe havens
(GLD, IEF, TLT); a *risk-on* read is the opposite. Per-asset signals (a crowding,
b vol, d-commodity) carry their own direction. Component **h** is mapped per-asset
by the static **region → asset → stance** table (e.g. MIDDLE_EAST → USO/GLD/ITA
long, SPY short), independent of polarity. Component **f** annotates the catalyst
window only.

## 3. Candidate rule

1. Components **(a)–(e)** and the evidence streams **(g)–(h)** vote per asset;
   **(f)** is a window/gate. g and h are *evidence, not standalone triggers* —
   they still require ≥ `min_components` agreement like every other component.
2. Keep only components that are `available`, `is_extreme`, and cast a directional vote.
3. Bucket by direction. A candidate requires **≥ `min_components` (default 3)**
   agreeing on the **same** direction. Ties (LONG == SHORT) fail consistency.
4. **Composite** = weighted mean of the *contributing* components, weights
   **renormalised over contributors only** so degraded components never dilute.
5. Rank by composite, take the top **`max_candidates` (default 5)**.

## 4. Weights & thresholds (`AsymmetryConfig` in `modules/config/settings.py`)

| Weight (a–e + g/h, sum = 1.0) | Default |   | Threshold | Default |
|-------------------------------|--------:|---|-----------|--------:|
| `crowding_weight`       | 0.19 |   | `crowding_extreme_pctl` | 85 |
| `vol_mispricing_weight` | 0.19 |   | `iv_rank_high` / `iv_rank_low` | 90 / 10 |
| `regime_fragility_weight` | 0.17 | | `skew_extreme` | 3.0 |
| `cross_asset_weight`    | 0.17 |   | `regime_fragility_extreme` | 60 |
| `sentiment_extreme_weight` | 0.15 || `cross_asset_z_div` | 2.0 |
| `polymarket_divergence_weight` | 0.07 || `sentiment_high/low_pctl` | 90 / 10 |
| `gdelt_pulse_weight`    | 0.06 |   | `catalyst_window_days` | 10 |
|                         |      |   | `polymarket_divergence_extreme` | 0.20 |
|                         |      |   | `gdelt_anomaly_z` / `gdelt_tone_max` | 2.5 / 0.0 |
|                         |      |   | `gdelt_min_history` | 20 |
| Rule | `min_components` 3 · `max_candidates` 5 · `min_iv_history_days` 60 | | | |

`AsymmetryConfig.weights_hash()` is a stable 16-char SHA-256 of the weights — the
**prompt-hash-equivalent** for the grading contract (§7). Change a weight, change
the hash, and historical grades stay attributable to the config that produced them.
**v6.5.0** renormalized the original five down and added g/h, so the hash rolled
over by design: pre-6.5.0 grades stay tied to the old hash.

## 5. Asset mapping (contract → liquid ETF)

| Contract / COT name | ETF | Class | Risk polarity | Commodity key |
|---------------------|-----|-------|---------------|---------------|
| Crude Oil (CL)      | USO | crude | risk-on (+1)  | `CL=F` |
| Gold (GC)           | GLD | gold  | safe-haven (−1) | `GC=F` |
| 10-Year Treasury    | IEF | rates | safe-haven (−1) | — |
| 30-Year Treasury    | TLT | rates | safe-haven (−1) | — |
| Euro (EUR)          | FXE | fx    | risk-on (+1)  | — |
| S&P 500 (SP500)     | SPY | equity| risk-on (+1)  | — |
| Defense (—)         | ITA | defense | risk-on (+1) | — |
| Germany (—)         | EWG | regional_equity | risk-on (+1) | — |
| Japan (—)           | EWJ | regional_equity | risk-on (+1) | — |
| Brazil (—)          | EWZ | regional_equity | risk-on (+1) | — |

The bottom four (v6.5.0) are the **GDELT-mapping universe**: they carry no COT /
commodity coverage, so crowding (a) and commodity (d) self-degrade for them — they
earn candidacy from the market-wide signals plus g and h. Secondary mappings
(documented, not primary): crude → XLE, long-end → TLT, NASDAQ → QQQ, silver →
SLV, copper → CPER, nat-gas → UNG, dollar → UUP.

## 6. The Asymmetry Dossier

Each candidate produces a markdown + structured dossier with:
`consensus_belief`, `why_fragile`, `feedback_loop` (one sentence),
`catalyst_window`, `invalidation_level` (a price/level that kills the thesis —
the 60-day swing extreme when priced, else qualitative), and a **defined-risk
expression**:

- **Instrument:** long ITM call (LONG) / put (SHORT) on the mapped ETF if
  optionable, else the ETF itself.
- **Delta:** target **0.60–0.70** (we emit 0.65; select the strike with
  `bs_delta` from `iv_snapshot_collector.py`).
- **Expiry:** **3–6 months** (we emit ~4mo / 120 DTE), always **> 30 DTE**.
- **Liquidity:** open-interest gate **> 1,000** via
  `transaction_costs.option_open_interest_ok`.
- **Max premium:** the dossier's **share of the convexity sleeve** =
  `min(per_dossier_cap, total_sleeve / n_candidates)`.

## 7. Risk rails (HARD, code-enforced in `RiskEnforcer`)

Defined in `schemas.RiskLimits` (runtime) and mirrored in `config/risk_limits.py`:

| Rail | Limit | Enforcing rule |
|------|-------|----------------|
| Total convexity sleeve | **5%** of portfolio across ALL open expressions | `CONVEXITY_SLEEVE_TOTAL` |
| Per-dossier premium | **2%** | `CONVEXITY_PER_DOSSIER_CAP` |
| Option time-to-expiry | never **< 30 DTE** | `OPTION_MIN_DTE` |

`RiskEnforcer.validate_convexity_sleeve()` greedily admits dossiers in
descending composite order; the highest-conviction asymmetries win the limited
sleeve, the rest are **rejected (not resized)**. The hunter **NEVER** sizes up a
Phase 4 position — the convexity sleeve is entirely separate.

## 8. Grading contract

Every dossier is logged to the AI Scoreboard
(`AIScoreboard.log_asymmetry_dossiers`) with `source='asymmetry_hunter'` and
`prompt_hash = weights_hash`. The same `score_due_proposals` / verdict machinery
that grades the MasterPrompt scores these against SPY at 2/4/8/12 weeks. After
≥ 60 scored proposals, `python main.py scoreboard` returns a verdict —
**adds_value · decoration · anti_predictive** — for the Soros layer, isolated by
source/hash. *Same standard, no special pleading.*

## 9. Firing-frequency replay

`scripts/replay_asymmetry_hunter.py` reconstructs components point-in-time over
**2018Q4, 2020-02/03, 2022, 2025** and reports how often the hunter would have
fired. **It is a sanity check, NOT a backtest or PnL claim.** Expectation: a
handful per stress episode, near-zero in calm months.

| Window | Steps fired | Total candidates | Notes |
|--------|-------------|------------------|-------|
| 2018Q4 (Volmageddon) | 0 / 14 | 0 | COT cov. 14/14, crowding voted 11/14 steps; cross-asset/regime coverage 0/14 (gap) |
| 2020-02/03 (COVID) | 0 / 9 | 0 | COT cov. 9/9, crowding voted 9/9 steps; cross-asset/regime coverage 0/9 (gap) |
| 2022 (rate-shock bear) | 0 / 53 | 0 | COT cov. 53/53, crowding voted 51/53 steps; cross-asset/regime coverage 0/53 (gap) |
| 2025 (recent) | 0 / 53 | 0 | COT cov. 53/53, crowding voted 44/53 steps; cross-asset/regime coverage 0/53 (gap) |

> Reproduced 2026-07-01 against a warehouse backfilled via
> `scripts/backfill_cot_history.py` (2015→present); numbers match the checked-in
> `reports/validation/asymmetry_replay_2026-06-29.md` exactly. **COT now has full
> point-in-time coverage** (v6.10.0) and casts a real contrarian vote in the large
> majority of stress-window steps — exactly where positioning extremes cluster.
> Zero firings above is a genuine **data gap, not a signal**: this replay's
> warehouse has no point-in-time `cross_asset` (HY OAS/VIX) or macro regime
> history, so those two components report 0 coverage and the ≥3-component
> candidate rule can't be reached from COT alone. Re-run as that history accrues
> — see `docs/COT_REAL.md` §5 for the full before/after.

## 10. Limitations & honesty

- **COT crowding is LIVE** (v6.10.0). Component (a) reads a REAL CFTC feed backed
  by a 3y+ backfilled `cot_history` store and a documented 52-week warm-up rule —
  it now **contributes** to candidacy (both tails: crowded long → contrarian short,
  crowded short → contrarian long). It self-zeros only when a contract is still
  warming up (<52 weeks) or the feed is unreachable. See `docs/COT_REAL.md`.
- **Commodity curves are MA-approximations**, flagged `approximation`.
- **IV rank warms up** for 60 snapshots → component (b) is 0 weight until then.
- **No Fear&Greed index**; Phase 1's `fear_greed` (=50) and `put_call_ratio`
  (=0.85) are placeholders and are **excluded** — sentiment uses only the real
  matrix / social readings.
- Because three trustworthy *independent* components must agree, production firings
  stay **rare** by design — crowding being live raises the per-asset component count
  but still needs ≥3 to align. That rarity is intended.

> **Update (v6.7.0):** Fear & Greed (CNN) and put/call (CBOE) are now **real,
> circuit-broken feeds**, no longer the `=50` / `=0.85` placeholders described
> above. The sentiment component reads the regional matrix + social composite and
> still excludes any feed that degrades to a placeholder.
>
> **Update (v6.10.0):** COT crowding (a) is **no longer mocked** — it is a real,
> backfilled CFTC feed with a 52-week warm-up. The `weights_hash` is unchanged
> (no weight moved), so the AI Scoreboard grading contract stays continuous.
>
> **Update (v6.11.1):** the v6.10.0 COT-Real release shipped *after* the v6.9.0
> Hunter Readiness doc and the v6.8.0 operator dry-run capture, so both had
> baked in "COT is mocked" as fact. §9 and §11 below still described crowding as
> `MOCKED-OFF` / `_to populate_`. Both are now updated to the live, reproduced
> state — see `docs/V9_RECONCILE_NOTES.md`.

## 11. Readiness & Grading Clock

The Hunter is honest about whether it can fire *yet*. Two surfaces make that
state visible to a non-coder operator.

### `python main.py hunter-status` — the readiness readout

A **pure status query**: no pipeline run, no network. It inspects the current
warm-up state of every component directly (warehouse snapshot counts, config
gates, the static calendar) via `compute_hunter_readiness()` and prints, per
component, **LIVE / WARMING-UP (N/threshold) / MOCKED-OFF** with the *exact gate*:

| Component | Gate | Typical state today |
|-----------|------|---------------------|
| (a) Crowding (COT) | real CFTC feed + ≥52-wk history → pctl ≥ 85 | ✅ **LIVE** (real CFTC feed; ITA/EWG/EWJ/EWZ have no COT contract → structural 0-weight) |
| (b) Vol mispricing | ≥ 60 IV snapshots/asset → IV rank ≥ 90 / ≤ 10 | ⏳ **WARMING-UP** (`SPY/TLT/GLD N/60`; the other Hunter ETFs are outside the IV-snapshot universe → structural 0-weight) |
| (c) Regime fragility | transition composite ≥ 60 & rising | ✅ **LIVE** (deterministic) |
| (d) Cross-asset | \|HY-OAS z − VIX z\| ≥ 2.0σ | ✅ **LIVE** (commodity term-structure sub-signal flagged `APPROXIMATION`) |
| (e) Sentiment extreme | composite ≥ 90th / ≤ 10th pctl | ✅ **LIVE** (Reddit/Twitter/CNN F&G/CBOE put-call — placeholders excluded) |
| (g) Polymarket | curated markets snapshotted + \|Δprob\| ≥ 20% | ⏳ **WARMING-UP** until snapshots accrue (reports last-snapshot age) |
| (h) GDELT pulse | per-region Welford count ≥ 20 + vol z ≥ 2.5, neg tone | ⏳ **WARMING-UP** until the accumulator fills (reports `N/20` + last-pulse age) |
| (f) Catalyst window | next FOMC/CPI/NFP ≤ 10d | window flag (gate, not a weighted vote) |

It closes with: **`Components currently LIVE: X of 7`**, the candidate rule
(`≥ 3 independent`), and the **expected firing posture** (`RARE` while only a
handful of components are live; `DORMANT` if fewer than 3). As of the v6.10.0
COT-Real backfill, a live run shows **4 of 7** (`crowding`, `regime_fragility`,
`cross_asset`, `sentiment_extreme`) — confirmed by re-running
`python main.py hunter-status` on 2026-07-01. A footer shows the last persisted
watchlist (candidates + convexity-sleeve usage).

### The grading clock — earned over ~60 scored dossiers

Every emitted dossier is logged to `ai_proposals` with
`source='asymmetry_hunter'` and the current `weights_hash` (orchestrator →
`log_asymmetry_dossiers`), then scored against SPY at 2/4/8/12w by the **same**
machinery as the MasterPrompt. `python main.py scoreboard` surfaces a **separate
Soros-layer verdict** (`compute_asymmetry_scoreboard()`) once **≥ 60** dossiers are
scored — the identical bar to the MasterPrompt, isolated by source/hash.

The scoreboard report header and CLI both show the clock
(`asymmetry_grading_clock()`): components currently LIVE, dossiers logged, dossiers
scored / 60 required, and a best-effort *weeks-to-first-verdict* estimate. Because
candidates are rare by design, the clock is usually **data-limited, not
time-limited** — the verdict is earned, not scheduled.

See `docs/HUNTER_READINESS.md` for the full output spec, the grading-clock wiring
and the daily-digest integration.
