# STRATEGY_V2_DESIGN — Strategy v2 Pre-Registration (design only)

**Status: PRE-REGISTERED — frozen at merge. No strategy code exists yet.**
**Doc version:** 1.0 · **Date:** 2026-07-04 · **Registered at repo:** v6.14.0
**Produced by:** Playbook PROMPT 6 (`docs/PLAYBOOK_FINAL_2026-07-02.md` §Prompt 6)
**Executed by:** Playbook PROMPT 7 (branch `research/strategy-v2-verdict`), which writes
`reports/STRATEGY_V2_VERDICT.md` (paths ObliskIQ-relative)
**Verdict stamp (Prompt 7 writes exactly one of these here, in its own commit):**
`PENDING` <!-- PASS | FAIL → PARKED -->

---

## §0 Registration rules (read first)

1. **Frozen at merge.** Every hypothesis, constant, gate, and span in this document is
   fixed as of the merge commit of this file — before any v2 backtest code exists.
2. **Amendment rule.** Any edit to §2–§6 before the verdict run voids this registration.
   A voided registration must be re-issued as doc version 2.0 with a new constants hash
   (§6), and the void must be recorded in the verdict report. Post-verdict, the only
   permitted edit is stamping the verdict line above.
3. **Single-shot rule.** The first Prompt 7 run whose data audit (§3.4) passes produces
   THE verdict. Infrastructure crashes and data-audit failures (the `DATA_GAPS` path,
   precedent `run_strategy_v1_verdict.py:857-903`) may be fixed and re-run — those runs
   never produced gate metrics. Once gate metrics are produced, they are final.
4. **No-iteration commitment.** See §4. If v2 fails any gate, it is parked, not tuned.
5. **Scope.** v2 is the deterministic strategy layer (successor to
   `modules/strategy/engine.py` v1). It does not constrain or replace the AI pipeline
   (Phases 1–4), the Asymmetry Hunter, or the paper-trading gate. Zero code in this
   session; all implementation happens in Prompt 7, in research-script scope only (§7).

---

## §1 Why v2 exists — the verdict history this design must answer

### 1.1 The record (verified against the verdict docs)

| Run | Verdict | OOS Sharpe | Benchmark Sharpe (SPY) | MaxDD | Signal IC (Spearman) | Windows |
|-----|---------|-----------:|-----------------------:|------:|----------------------|---------|
| v1 (`docs/STRATEGY_V1_VERDICT.md`) | **FAIL** | 0.805 | 0.858 | −20.79% | 0.0611, p 0.0546, n 989 | 125 monthly |
| v1.1 (`docs/STRATEGY_V1_1_VERDICT.md`) | **FAIL** | 0.801 | 0.858 | −19.32% | 0.0395, p 0.2151, n 989 | 125 monthly |

Both runs: 12m train / 1m test, monthly roll, OOS 2016-01 → 2026-05, 102 US mega-caps,
costs on, pre-registered gates. Both failed only the Sharpe-vs-benchmark gate; drawdown
and degradation gates passed. v1.1 (delta execution, BIL sleeve, point-in-time regime
feed) proved the deficit is **not execution realism**: costs fell from $39,817 to
$15,370, the BIL sleeve added $18,382, the regime overlay fired in 45 windows — and the
Sharpe gate still failed. Per the v1.1 verdict: *"The deficit is therefore in the SIGNAL
and the OVERLAY's judgment, not in execution realism."* v1.1 stands as an accepted
**defensive chassis** (settled decision) — risk-disciplined, alpha unproven.

### 1.2 The four salvage hypotheses v2 operationalizes

Quoted from `docs/STRATEGY_V1_1_VERDICT.md` §"v2 HYPOTHESES" (lines 258–266); the
mapping to v2 hypotheses is this document's §2:

1. *"Risk-off sleeve as a directional position (TLT)… a BIL/TLT split conditioned on the
   inflation component of the regime feed."* → **H2**
2. *"Regime modulates the TREND GATE's strictness rather than halving gross outright,
   and/or requires credit-spread confirmation (… FRED no longer serves ICE BofA
   history)."* → **H2** (with a pre-registered long-history substitute series)
3. *"Faster or dual-horizon trend gate (… 50d/200d combination …)."* → **H2**
4. *"Broader point-in-time universe … keeping confidence pure-momentum (regime affecting
   gross only, not the IC-bearing signal)."* → **H1**

(Seed 5, the Fama-French parser fix, was tooling; the Playbook §2 update note records it
as fixed in Prompt 1, so factor attribution is a required diagnostic in §3.6.)

### 1.3 Benchmark change: SPY → ACWI (declared before any result exists)

The settled global-mandate decision (Playbook, Session Context Header: *"Benchmark for
global strategy work = ACWI, not SPY"*) applies to v2, whose universe is global (§2.1).
To make benchmark-shopping impossible to allege: the change is registered **here, before
any v2 backtest exists**, and the verdict must additionally **report** (non-gating) the
same strategy against SPY buy-and-hold, so the reader can see what the old gate would
have said. The gated comparison is ACWI only (§3.3).

### 1.4 What v2 is NOT

- Not a revival of the demoted pattern library (0/31 trusted — `docs/SYSTEM_CENSUS.md`);
  no pattern-library signal is used anywhere in v2.
- Not an options, shorting, leverage, or intramonth strategy. Long-only ETFs, monthly
  rebalance, `max_leverage = 1.0` (`modules/config/risk_limits.py:285`).
- Not dependent on any forward-only data stream (IV, Polymarket, GDELT, sentiment,
  Fear&Greed, put/call — §5.3). Every input has honest history over the full window.
- Not a consumer of paid APIs. yfinance, FRED/ALFRED, and CFTC public archives only.
- Not FX-converting: all vehicles are USD-listed (the execution path has no FX
  conversion — verified absent; `global_vehicles.trading_currency_for` is informational
  only).

---

## §2 Hypotheses

Four named hypotheses, one per mandated role. Each states: the mechanism, the causal
story for why the edge exists, why it should persist, and what falsifies it. Design
principles binding all four:

- **Ex-ante constants only.** Every parameter is an academic default or an existing house
  constant (provenance table in §2.6). Nothing was searched against the test decade.
- **Signal purity** (seed 4): the regime/gating machinery (H2) and the risk overlays
  (H3, H4) never touch the H1 ranking. The IC gate (§3.3 G5) is measured on the pure
  signal.
- **Honest degradation:** any input that is missing or warming up degrades to the
  defensive default, never to a fabricated value (house rule; e.g.
  `warehouse.cot_percentile_with_status`, `modules/data/warehouse.py:840-867`).

### 2.1 H1 — Simons (pattern evidence): global cross-sectional dual momentum

**Mechanism.** Rank a fixed global ETF universe by 12-1 momentum each month; hold the
top quartile, each name additionally required to trade above its own 200-day SMA
("dual momentum": relative strength + absolute trend).

**Universe (48 USD-listed ETFs — frozen).** The union of ETF lists that already exist in
config for other purposes — chosen so v2 introduces **zero universe-selection freedom**:
`REGION_ETFS` (all 10 regions, `modules/config/global_vehicles.py:229-240`) ∪
`CORE_ETFS` ∪ `SECTOR_ETFS` (`modules/config/strategy_universe.py:65-70`).

| Bucket (for the §2.4 cap) | Tickers |
|---|---|
| NA | SPY, QQQ, IWM, EWC, XLK, XLE, XLF, XLV, XLI, XLY, XLP, XLU, XLB, XLRE, XLC |
| Europe | VGK, EZU, EWG, EWQ, EWU, EWL |
| China | MCHI, FXI, KWEB, ASHR |
| Japan | EWJ, DXJ |
| India | INDA, EPI, SMIN |
| Asia | AAXJ, EWY, EWT, EWS, EWM |
| LatAm | ILF, EWZ, EWW |
| Middle_East | KSA, EIS, TUR |
| Oceania | EWA, ENZL |
| Africa | EZA, AFK, NGE |
| Commodity | GLD |
| Duration | TLT |

BIL is the cash vehicle (never ranked); ACWI is the benchmark (never held). 48 tickers
total (35 regional + GLD + TLT + 11 sectors; SPY/QQQ/IWM appear in both source lists).

**Signal.** `momentum_12_1 = close[t−21] / close[t−252] − 1` — the identical v1 formula
(`modules/strategy/engine.py:132-157`; Jegadeesh & Titman 1993), computed on warehouse
adjusted (total-return) closes.

**Eligibility (point-in-time, per rebalance date t).** An ETF is eligible iff:
(a) a warehouse close exists (7-calendar-day lookback, `_get_price_at_date` convention)
at t and at each of the 37 prior monthly boundaries — i.e. 36 monthly returns are
computable (required by H4); and (b) trailing 63-trading-day average dollar volume
(mean of close × volume) ≥ $50,000,000 (`RISK_LIMITS.min_liquidity_volume`,
`modules/config/risk_limits.py:292`), with ≥ 63 volume observations. Deviation from
v1's 252-trading-day entry rule, forced by H4's estimator, disclosed in §5.4.

**Selection.** Sort eligible ETFs by momentum descending, ties broken by symbol
ascending (engine determinism convention, `engine.py:188-189`). Select
`count = max(1, int(0.25 × n_eligible))` (the v1 `select_top_decile` counting rule,
`engine.py:196-199`, with `top_fraction = 0.25`). Then **drop** any selected ETF whose
last close is not strictly above its own 200-day SMA (`is_above_sma` semantics,
`engine.py:167-178`; insufficient history → drop, fail-safe). **No backfilling** from
lower ranks — a thin month holds fewer names and more BIL.

**Causal story.** Cross-sectional momentum across countries and asset classes is one of
the most replicated return patterns in the literature (Rouwenhorst 1998, international
equities; Asness, Moskowitz & Pedersen 2013, "Value and Momentum Everywhere") and is
attributed to investor underreaction, flow herding, and home-bias limits to arbitrage —
frictions that do not disappear because they are published, since exploiting them
across borders requires mandate freedom most institutional capital lacks. v1's specific
failure mode — cross-sectional momentum *inside one mega-cap index* ≈ the index itself,
minus costs (v1 verdict cause analysis) — is structurally absent when the cross-section
spans regions with wide return dispersion (China vs Japan vs LatAm vs US sectors vs
duration vs gold).

**Why the top quartile, not the decile.** Asset-class/country momentum portfolios in the
literature hold the top third-to-quartile of small cross-sections (Faber 2007 GTAA-class
specs; AMP 2013 country portfolios) — a decile of 48 is 4 names, un-diversifiable. This
is an ex-ante convention choice, not a fitted parameter.

**Falsification.** G5 (IC of the pure signal) and ablation A2/A3 attribution. If global
cross-sectional momentum has no predictive power on this universe, the IC gate kills v2
regardless of portfolio-level luck.

### 2.2 H2 — Dalio (regime risk discipline): regime modulates gate strictness; BIL/TLT risk-off split

**Mechanism.** A portfolio-level trend gate on the benchmark asset decides whether the
equity sleeve is ON this month. Regime changes the gate's **strictness** — never gross
exposure, never the signal:

| Regime (point-in-time feed) | Equity sleeve ON iff |
|---|---|
| `bull` | soft **OR** hard |
| `choppy` / missing row | soft |
| `bear`, credit stress **absent** | soft |
| `bear`, credit stress **present** | soft **AND** hard |

where **soft** = ACWI last close > its 200-day SMA and **hard** = ACWI last close > its
50-day SMA (both `is_above_sma` semantics on warehouse closes as of t; insufficient
history → gate cannot pass, fail-safe per `engine.py:69-71`).

- **Regime feed:** latest `regime_history` row with date ≤ t (built by
  `scripts/build_regime_history.py` from ALFRED as-published vintages; labels are
  exactly `bull`/`bear`/`choppy`, `modules/macro/analyzer.py:335,348,355`). Row-gap
  audit: ≤ 45 days (`REGIME_FEED_MAX_GAP_DAYS` precedent,
  `run_strategy_v1_verdict.py:143`).
- **Credit stress** (the seed-2 confirmation, with the pre-registered long-history
  substitute for the truncated ICE BofA series): FRED series **`BAA10Y`** (Moody's
  Seasoned Baa Corporate Bond Yield Relative to 10-Year Treasury Constant Maturity;
  daily; verified live on FRED as of 2026-07). Apply a +1-business-day publication lag
  (the regime builder's convention for daily unrevised series,
  `scripts/build_regime_history.py:88-93`), then: credit stress is **present** at t iff
  `BAA10Y[t_lagged] − BAA10Y[t_lagged − 63 observations] > +0.50` percentage points.
  Missing observations after a 5-calendar-day lookback → stress **present** (fail-safe
  defensive).
- **Risk-off allocation (sleeve OFF):** if the latest regime row's `inflation_rate`
  (YoY CPI, ALFRED-vintage; schema `database/schema.sql:231`) > 3.0% → **100% BIL**;
  else → **50% TLT / 50% BIL** (seed 1: duration is a directional bet that pays in
  deflationary risk-off and loses in inflationary risk-off). `inflation_rate`
  missing/NULL → 100% BIL (fail-safe: no duration bet without data).
- **No gross halving anywhere.** v1's `compute_position_size_pct` risk-off halving
  (`engine.py:270-275`) is deliberately absent from v2.

**Causal story.** Macro regimes persist for quarters because policy transmission and
credit cycles unfold slowly; equity drawdowns cluster in contractionary regimes. But
v1.1 measured the cost of acting on regime *bluntly*: 45/125 OOS months de-risked
(including long stretches of 2020-21 and 2023-24 while equities compounded) bought only
~1.5 points of MaxDD improvement. Modulating the *evidence threshold* for staying
invested preserves participation while keeping crash protection where it demonstrably
worked (2018Q4, 2020-03: trend gate + genuine credit widening). The BAA10Y spread is a
mechanical, unrevised, decades-deep market price of credit stress — it confirms real
funding stress and ignores pure price whipsaw. The inflation-conditioned TLT split
encodes the one regime distinction duration actually respects (2008/2020 vs 2022).

**Persistence.** The inputs are slow-moving official statistics and a market spread;
the edge claimed is not alpha but *avoided beta in the left tail* — it persists as long
as recessions and credit crunches remain drawn-out affairs rather than instantaneous
jumps.

**Falsification.** Ablations A3 (static gate) and A4 (no TLT split): if the modulated
gate does not beat the static gate on the gated metrics, H2's judgment layer added
nothing.

### 2.3 H3 — Soros (reflexivity asymmetry): COT crowding haircut

**Mechanism.** When speculative positioning in a selected asset's underlying futures
contract is at a top-decile extreme, halve that asset's weight. Long-only risk
modulation — no contrarian entries, no shorts.

- **Mapping (frozen; only where the tracked contract IS the asset's direct underlying,
  codes from `TRACKED_CONTRACTS`, `modules/market/cftc_cot_analyzer.py:258-281`):**
  SPY ← SP500 `13874+` · QQQ ← NASDAQ `20974+` · TLT ← US30Y `020601` · GLD ← GC
  `088691`. The other 44 ETFs have no direct CFTC contract and are structurally
  unmodulated (disclosed, §5.4 — this is a narrow overlay, not a return engine).
- **Rule (per rebalance t, per mapped selected asset):** with
  `as_of_cot = t − 7 calendar days` (publication-lag honesty: CFTC releases the Tuesday
  report on Friday; the stored `report_date` is the as-of Tuesday), take the latest
  `cot_history` row with `report_date ≤ as_of_cot`; feed its `net_spec_pct_oi` to
  `warehouse.cot_percentile_with_status(code, value, as_of=as_of_cot)`
  (`modules/data/warehouse.py:840-867`: trailing 156-week window, 52-week warm-up,
  inclusive rank, point-in-time). **Percentile ≥ 90 → weight × 0.5** (the analyzer's
  EXTREME_LONG threshold). Percentile `None`/warming → no action (honest degradation).
- **Data:** genuinely backfillable — CFTC legacy futures-only annual archives 1986–
  present (`cftc_cot_analyzer.py:286-287`); backfill pre-registered from **2010**
  (`scripts/backfill_cot_history.py --start-year 2010`, the script's own documented
  example, line 24), so trailing-156-week percentiles are fully warm from 2013 — before
  the first walk-forward decision.

**Causal story.** COT net-speculative extremes measure how completely the crowd already
owns a narrative. Reflexivity (Soros 1987/2013): price → narrative → positioning feeds
back until the marginal buyer is exhausted; at the extreme, the same loop runs in
reverse, convexly, because exits are correlated. Halving weight at the ≥90th percentile
buys cheap protection against exactly the unwind leg while keeping most of the momentum
exposure. Momentum selection *systematically* walks into crowded trades — H3 exists
because H1 does.

**Persistence.** Positioning data is public weekly, yet the extreme persists as a signal
because acting on it requires *reducing* a winning position — behaviorally and
institutionally the hardest trade to do.

**Falsification.** Ablation A1: if removing the haircut does not worsen the gated
Sharpe/MaxDD trade-off, H3 added nothing and must not survive into any future design.

### 2.4 H4 — Thorp (fractional-Kelly sizing): quarter-Kelly replaces equal weight

**Mechanism.** Position weights come from each asset's own estimated edge and odds via
the existing Kelly implementation — not from `gross / n`.

Per surviving selected asset at rebalance t, from its trailing **36 monthly
boundary-to-boundary returns** (the same 37 boundary closes eligibility guaranteed):

- `p` = fraction of the 36 returns > 0; `wins` = returns > 0; `losses` = returns < 0
- `avg_win = mean(wins)`; `avg_loss = |mean(losses)|`
- Call `ScreeningEngine.calculate_kelly_position_size(win_prob=p, avg_win, avg_loss,
  max_position=0.10, kelly_fraction=0.25)`
  (`modules/analytics/screening_engine.py:380-434`): `b = avg_win/avg_loss`,
  `f_full = (b·p − q)/b`, `f_i = 0.25 × f_full`, clamped to [0, 0.10]. Degenerate inputs
  (p ≤ 0, p ≥ 1, avg_loss = 0) return 0 by the function's own semantics — the asset is
  dropped (an all-positive-months asset has no loss estimate; disclosed).
- **Drop** the asset if `f_full ≤ 0` (negative stated edge → no position; the Phase-4
  quarter-Kelly-clamp precedent) or if `f_i < 0.02` (`min_position_pct`,
  `risk_limits.py:273` — below-minimum positions are not tradable).

Then, in order: apply the H3 haircut; apply the **region-bucket cap** — for each §2.1
bucket with Σ weights > 0.40 (`max_sector_pct`, `risk_limits.py:274`, repurposed as the
regional concentration cap for an ETF book), scale that bucket's weights by
`0.40 / Σ_bucket`; finally **gross cap**: if Σ all weights > 0.85
(1 − `DRAWDOWN_LIMITS.cash_normal`, the v1 normal-gross convention, `engine.py:270`),
scale all weights by `0.85 / Σ`. Weights are **never scaled up** — Kelly output is a
ceiling, not a target. The remainder (1 − Σ) is held in BIL. Zero survivors with the
sleeve ON → 100% BIL (the TLT split fires only on sleeve OFF).

**Constants:** `kelly_fraction = 0.25` is the house constant
(`RISK_LIMITS.fractional_kelly`, `risk_limits.py:303`; also the Phase-4 clamp). Caps
0.02/0.10 are the house position limits. The 36-month window is the shortest that gives
a monthly win-rate estimate with n > 30.

**Causal story.** Kelly (1956) maximizes long-run geometric growth; equal weighting
ignores dispersion in both edge and odds across a universe spanning Nigeria ETFs and
T-bonds. Fractional (quarter) Kelly is Thorp's own practice and the
MacLean–Thorp–Ziemba prescription for estimation error: sample-estimated `p` and `b`
overstate precision, and quarter-Kelly keeps the sizing on the safe side of the growth
curve where errors cost little. The claimed edge is a *geometric-growth and drawdown*
improvement at equal signal quality.

**Falsification.** Ablation A2 (equal-weight at the same gross): if quarter-Kelly does
not improve Sharpe or MaxDD versus equal weight, H4 added nothing.

### 2.5 Composite decision procedure (exact order of operations)

At every monthly boundary t:

1. Read regime row (≤ t), ACWI soft/hard SMA states, credit-stress state → **H2 gate**.
2. Gate OFF → allocation is 100% BIL or 50/50 TLT/BIL by the inflation split. Done.
3. Gate ON → compute eligibility set (§2.1). Audit: ≥ 25 eligible (else DATA-AUDIT FAIL).
4. Momentum-rank eligibles; select top quartile; drop names below own 200d SMA (**H1**).
5. Quarter-Kelly weight each survivor; drop `f_full ≤ 0` and `f_i < 0.02` (**H4**).
6. Apply COT crowding haircut ×0.5 where percentile ≥ 90 (**H3**).
7. Apply region-bucket cap (0.40), then gross cap (0.85). Never scale up.
8. Remainder → BIL. Emit target weights; the backtester's delta mode trades the
   difference (no-trade band 0.005, `BacktestConfig.delta_no_trade_band_pct` default).

### 2.6 Parameter provenance (every constant is ex-ante)

| Constant | Value | Source |
|---|---|---|
| Momentum lookback / skip | 252 / 21 d | v1 unchanged (`engine.py:107-108`; Jegadeesh & Titman 1993) |
| Own-asset trend filter | 200d SMA | v1 unchanged (`engine.py:111`; Moskowitz-Ooi-Pedersen 2012) |
| Top fraction | 0.25 | Asset-class-momentum convention (Faber 2007; AMP 2013); v1 counting rule `engine.py:198` |
| Eligibility | 37 monthly closes + $50M ADV (63d) | H4 estimator requirement + `risk_limits.py:292` |
| Gate SMAs | 200d / 50d | Seed 3 (dual horizon); both are the standard technical horizons |
| Credit series / threshold | BAA10Y, +0.50pp per 63 obs | Long-history FRED substitute (seed 2); ~half-point-per-quarter widening is the documented scale of 2008/2011/2015/2020/2022 episodes |
| Inflation split | 3.0% YoY | Fed target + buffer, round ex-ante number |
| Risk-off split | 50% TLT / 50% BIL | Neutral prior (seed 1) |
| COT thresholds | ≥ 90th → ×0.5; 156w window; 52w warm-up; 7d pub lag | Existing analyzer/warehouse constants (`warehouse.py:38,42`); haircut = one halving, neutral prior |
| Kelly | 0.25 × full; 36m window; caps 0.02/0.10 | `risk_limits.py:303,273,272`; MacLean-Thorp-Ziemba |
| Region cap / gross | 0.40 / 0.85 | `risk_limits.py:274`; `1 − cash_normal` (v1 convention, `engine.py:270`) |
| Protocol / rf / capital | 12m/1m monthly, delta mode, 2%, $100k | v1/v1.1 precedent (`run_strategy_v1_verdict.py:107-129`) |

---

## §3 Pre-registered verdict criteria

Registered before any v2 backtest code exists. Prompt 7 evaluates these mechanically;
nothing here is negotiable at run time.

### 3.1 Benchmarks

- **Gated:** ACWI (iShares MSCI ACWI ETF, US-listed, inception 2008-03) buy-and-hold —
  bought at the first monthly boundary, sold at the last, both legs costed
  (`simulate_buy_and_hold` mechanics, `run_strategy_v1_verdict.py:603-623`). Ingested
  through the identical `yf.download(auto_adjust=True)` → warehouse path as every other
  ticker (total-return closes).
- **Reported, non-gating:** SPY buy-and-hold (anti-benchmark-shopping disclosure, §1.3)
  and 60/40 ACWI/TLT monthly-rebalanced (`simulate_60_40` mechanics with the equity leg
  switched to ACWI).

### 3.2 Protocol and spans

Identical machinery to v1/v1.1 — canonical backtester
(`modules/testing/walk_forward_backtester.py`), 12m train / 1m test, monthly roll,
`rebalance_mode="delta"`, canonical costs (0.1%/leg slippage `DEFAULT_SLIPPAGE_PCT` +
IBKR commission schedule, `modules/analytics/transaction_costs.py:45,52`), initial
capital $100,000, risk-free rate 0.02 — so a pass/fail is attributable to the
hypotheses, not to infrastructure changes.

| Span | Value | Why |
|---|---|---|
| `INGEST_START` | **2012-06-01** | 37-month Kelly/eligibility windows and the ACWI 200d SMA must be warm at the first decision (2015-01); v1 used 2013-06-02 which cannot support H4 |
| IS-support run | 2014-01-01 → 2016-01-02 | RUN_B precedent (supplies 2015 IS context; no parameters fitted) |
| Headline run | 2015-01-01 → last complete month at run time | RUN_A precedent |
| **OOS test months** | **2016-01 → last complete month (≥ 2026-06)** | ≥ 126 monthly windows; same OOS start as v1/v1.1 |

The window deliberately does **not** extend before 2015: the point-in-time regime feed
is honest only from ~2013-12 (`build_regime_history.py:98-103` — ALFRED
`REALTIME_START=2012`, `DEFAULT_START=2013-12-31`; anything earlier is lagged-revised
data with lookahead in the levels), and pre-registration on dishonest data is worthless.
The resulting single-decade limitation is disclosed in §5.4.

### 3.3 Gates — ALL blocking; PASS requires all five; any failure → FAIL → §4

All gates evaluate the **OOS test-month series, net of costs**, in USD.

| # | Gate | Requirement (exact) |
|---|---|---|
| **G1** | Sharpe vs benchmark | OOS Sharpe(v2) **>** OOS Sharpe(ACWI B&H), raw float comparison, both computed by the §3.5 conventions |
| **G2** | Drawdown | \|MaxDD(v2)\| **< 25.0%** on monthly marks (expanding-peak method, `walk_forward_backtester.py:1626-1637`) |
| **G3** | IS→OOS degradation | `(avg_IS − avg_OOS) / avg_IS × 100 < 30.0`, computed exactly as `run_strategy_v1_verdict.py:1505` over the per-window table (`build_window_table` IS semantics, `:731-792`). **Pre-registered edge tightening:** if `avg_IS ≤ 0`, the gate **FAILS** (the v1 code maps this to 0.0/pass; a strategy that loses money in-sample has no valid degradation baseline) |
| **G4** | Expectancy | Monthly active returns `a_m = r_v2,m − r_ACWI,m` on identical boundaries: require `mean(a) > 0` **AND** one-sided one-sample t-statistic `t = mean(a) / (sd_sample(a) / √n) ≥ 1.66` (n ≥ 125 ⇒ critical value 1.657 at α = 0.05; 1.66 is the registered, slightly conservative constant) |
| **G5** | Signal validity (Simons house law: IC > 0.05, p < 0.05) | Pooled Spearman rank correlation between the **pure H1 momentum value** (pre-gate, pre-haircut, pre-Kelly) of every **eligible** asset at every OOS boundary and that asset's next-test-month total return: require **IC > 0.05 AND p < 0.05** (p as returned by `scipy.stats.spearmanr`, two-sided), with the honest pooled pair count n reported. Registered deviation from v1 (which measured IC on executed buys only, n 989): the all-eligible pooled test is the purer falsification of H1 and has ~5,000 expected pairs |

No result may be reported with any sample-size adjustment, extension, or augmentation of
any kind (`docs/PROVENANCE_PURGE_REPORT.md` is binding precedent: raw observation counts
only).

### 3.4 Minimum samples & data audit (audit failure = DATA_GAPS report, not a verdict)

| Check | Minimum |
|---|---|
| OOS monthly windows | ≥ 125 |
| Eligible assets at **every** OOS rebalance | ≥ 25 |
| G5 pooled IC pairs | ≥ 3,000 |
| ACWI closes | continuous coverage 2012-06 → run end (reference-calendar role) |
| BAA10Y coverage | from ≤ 2013-06-01 through run end |
| Regime feed | monthly rows from ≤ 2014-12-31, max gap 45 days (v1.1 audit precedent) |
| COT (4 mapped contracts) | ≥ 156 weekly rows trailing at 2014-01-01 (fully warm percentiles) |

Any miss → write `reports/validation/DATA_GAPS_<stamp>.md` (precedent
`run_strategy_v1_verdict.py:857-903`), exit non-zero, fix the **data**, re-run. The
design is never edited to fit the data (§0.2).

### 3.5 Annualization conventions (exact factors — the 4.6× bug guard)

The v5.35.1 bug (monthly walk-forward returns annualized with the daily √252 factor,
inflating Sharpe/Sortino by √(252/12) = √21 ≈ **4.583×**) was purged
(`CHANGELOG.md` [5.35.1]; fix at `walk_forward_backtester.py:1654-1666`). Nothing
resembling it may be reintroduced. The registered conventions:

| Quantity | Convention |
|---|---|
| Periods per year (ppy) | Inferred from the equity curve: `ppy = 365.25 / median point spacing in days` (`_periods_per_year`, `walk_forward_backtester.py:1607-1624`) — ≈ **12.0** for this monthly protocol |
| Sharpe | `√ppy × mean(r_period − rf_period) / std(r_period − rf_period)` with `rf_period = 0.02 / ppy` — factor **√12 ≈ 3.4641** here |
| Sortino | `√ppy × mean(r) / downside_std(r)` (same factor) |
| Annualized return | Geometric: `(1 + total_return)^(1/years) − 1`, `years = calendar_days / 365.25` — **never** a ×252 or ×12 multiple of a mean |
| Max drawdown | Expanding-peak on the monthly equity curve |
| Daily series (diagnostics only) | factor **√252 ≈ 15.8745**, `rf 0.02/252` |
| **Forbidden** | Any 252-based factor applied to monthly aggregates (= ×4.583 inflation), and any mixing of frequencies within one statistic |

Required guards in the Prompt 7 script (both are v1.1 precedents):
1. The script's own metric mirror must equal the canonical backtester's Sharpe within
   1e-6 (integrity assert, `run_strategy_v1_verdict.py:1491-1495`).
2. `research/validation/monte_carlo.py` annualizes daily (√252); its outputs **must** be
   rescaled by `√(ppy/252)` before reporting (`run_strategy_v1_verdict.py:1205-1208`).
3. **Never call `_calculate_comparison_metrics`**
   (`walk_forward_backtester.py:1920-1934`) on monthly data — it hardcodes √252 for a
   documented daily-returns path and is the one surviving √252 in the canonical file.

### 3.6 Required diagnostics (reported, non-gating)

Per-window table (IS/OOS returns, turnover, costs, gate state, regime, buys); total and
counterfactual costs; windows-profitable %; defensive (sleeve-OFF) month count with the
BIL/TLT split taken; beta and information ratio vs ACWI; per-trade expectancy table
(target n ≥ 100 round trips); Monte Carlo (2,000 i.i.d. bootstrap sims, rescaled per
§3.5); stress replay (COVID_2020 / GFC_2008 / 1987 one-shot shocks, `_safe_section`
precedent); Fama-French attribution (Mkt-RF/SMB/HML/MOM on the stored monthly returns —
the parser was fixed in Prompt 1; degrade gracefully if it breaks again); and the four
ablations:

| Ablation | Change vs full v2 | Attributes |
|---|---|---|
| A1 | COT haircut removed | H3 |
| A2 | Equal-weight `min(0.85/k, 0.10)` instead of quarter-Kelly | H4 |
| A3 | Static gate (choppy rules always: soft only; no regime, no credit), risk-off = 100% BIL | H2 gate modulation |
| A4 | Full gate logic, but risk-off = 100% BIL always (no TLT split) | H2 duration bet |

Ablations are diagnostics for the post-mortem and for any future v3 design. **They
cannot rescue a FAIL** — the gates bind on the full composite only.

---

## §4 Kill criteria and the park commitment

1. **Kill trigger:** any of G1–G5 fails, on the single §0.3 verdict run.
2. **Consequence:** Strategy v2 is **PARKED**. Prompt 7 writes the FAIL verdict plainly
   in `reports/STRATEGY_V2_VERDICT.md` and stops — no tweaks, no "what if" reruns, no
   parameter suggestions in that session (Playbook Prompt 7 wording is binding). The
   verdict line in this document's header is stamped `FAIL → PARKED`.
3. **No-iteration commitment:** no v2.x variant — no constant change, lookback change,
   threshold change, universe swap, or gate re-benchmarking — may be run against this
   same OOS decade. Tuning parameters against a test set you already saw is the exact
   overfitting mechanism the house standard forbids (Bailey, Borwein, López de Prado &
   Zhu 2014, cited since the v1 verdict).
4. **What a future v3 must be:** materially different hypotheses — at least one of
   (a) a different signal family (not a momentum-rank variant), (b) a data source with
   honest history not used here, or (c) a structurally different universe-construction
   principle — each with its own fresh pre-registration document. Re-tuned v2 constants
   do not qualify.
5. **If v2 PASSES:** it earns exactly what v1 would have earned — promotion to a
   candidate engine behind the existing version plumbing
   (`strategy_engine_version` config, `settings.py:801-803`; `create_strategy_engine`
   factory), a separate implementation prompt, and the full 90-day paper-trading gate
   (`docs/PAPER_TRADING_GATE.md`). A backtest PASS authorizes paper trading, not live
   capital.

---

## §5 Data: what the pipeline already provides vs what needs building

Verified against the codebase at v6.14.0 (not assumed). Citations are file:line.

### 5.1 Already provided (honest history over the full window)

| Capability | Where | Notes |
|---|---|---|
| Price spine, total-return | `run_strategy_v1_verdict.py:183-262` ingest (`yf.download(auto_adjust=True)`) → `warehouse.ticker_prices` → `PointInTimeStore` (:404) strict as-of slices | yfinance serves every universe ETF and ACWI to inception; adjusted closes = dividends folded in |
| Canonical walk-forward backtester | `modules/testing/walk_forward_backtester.py` | delta mode (v6.2.0), no-lookahead asserts (:99-119), ppy-inferred annualization (:1607-1624), per-period cost accounting |
| Canonical cost model | `modules/analytics/transaction_costs.py:45,52` + settings :222-250 | 0.1%/leg slippage + IBKR commission; single source shared with PaperTrader |
| Point-in-time regime feed | `scripts/build_regime_history.py` (11-series ALFRED registry :80-94) → `warehouse.regime_history` (store/get :369-430; schema `database/schema.sql:223-236` incl. `inflation_rate`) | Honest from ~2013-12; bull/bear/choppy labels |
| COT history + PIT percentile | `scripts/backfill_cot_history.py` (CFTC annual ZIPs 1986+; default 2015, supports 2010) → `warehouse.cot_history`; `cot_percentile_with_status(as_of=)` `warehouse.py:840-867` | 12 tracked contracts incl. the 4 mapped ones; 52-week warm-up; 156-week window |
| Kelly implementation | `ScreeningEngine.calculate_kelly_position_size`, `screening_engine.py:380-434` | Exact formula H4 uses; degenerate-input semantics defined |
| Risk limits (house constants) | `modules/config/risk_limits.py:272-303` | 0.10/0.02 position caps, 0.40 sector/region cap, `fractional_kelly = 0.25`, leverage 1.0 |
| Universe source lists | `global_vehicles.REGION_ETFS` (:229-240); `strategy_universe.CORE_ETFS/SECTOR_ETFS/CASH_ETF/BOND_ETF` (:46-70) | §2.1 is their frozen union — no new list is invented |
| Verdict-runner architecture | `scripts/run_strategy_v1_verdict.py` | In-script adapter pattern, B&H/60-40 benchmark sims, `series_metrics` mirror + 1e-6 assert, `decide_verdict`, DATA_GAPS writer, `_safe_section` degradation, artifact writers — all to be cloned, not rewritten |
| FRED client | `modules/macro/fred_data.py` (`fetch_series_with_history` :182 for arbitrary series ids) | BAA10Y is an unrevised daily market series → latest vintage = as-published; +1BD lag applied in-script |

### 5.2 Needs building (all inside Prompt 7's research script; zero `modules/` edits)

| Item | Effort | Note |
|---|---|---|
| ACWI ingest | add ticker to the in-script ingest list | flows through the existing path unchanged |
| v2 universe constant + bucket map | in-script tuple/dict, copied verbatim from §2.1 | do NOT edit `strategy_universe.py` (its `BENCHMARK="SPY"` etc. serve the live v1 path) |
| v2 decision adapter (§2.5) | in-script class, `V1StrategyAdapter` pattern | imports engine pure functions (`momentum_12_1`, `is_above_sma`), warehouse, Kelly |
| H2 credit input | in-script BAA10Y fetch + lag + 63-obs differencing | via `fred_data` |
| G4/G5 gate code | in-script (t-test; pooled `scipy.stats.spearmanr`) | new, small, fully specified in §3.3 |
| COT backfill execution | `python scripts/backfill_cot_history.py --start-year 2010 --verify` | script exists; running it is Prompt-7-authorized offline research |
| Regime feed build execution | `python scripts/build_regime_history.py` (needs `FRED_API_KEY`) | script exists |
| Verdict writer | clone v1 writers; per-gate table incl. G4/G5; constants-hash stamp (§6) | outputs in §7.3 |

### 5.3 Not usable — and deliberately not used (scope-creep block)

These are forward-only or unpersisted and cannot be reconstructed for 2015–2026; v2 uses
none of them, and Prompt 7 must not "helpfully" add them: IV rank/skew
(collector is forward-only, 60-day warm-up), Polymarket (current probability only),
GDELT (Welford accumulator, forward-only), Reddit/Twitter/RSS sentiment (live-only),
CNN Fear&Greed and CBOE put/call (fetched live, not even persisted), Google Trends
(history exists at source but unpersisted and re-based per query window), SEC 13F
(EDGAR history exists but no warehouse table), FX conversion (no execution-path support
— hence USD-listed vehicles only).

### 5.4 Known biases and honest disclosures (to be reproduced in the verdict)

1. **Universe-snapshot survivorship (UPWARD):** §2.1 is a 2026 snapshot of config lists;
   country ETFs that closed during 2015–2026 are absent. Materially weaker than v1's
   single-stock survivorship (broad ETFs rarely delist and don't "grow into" an index),
   but non-zero and disclosed.
2. **Late-inception ETFs:** KSA (2015-09) and peers enter via the point-in-time
   eligibility rule only once 37 monthly closes exist — no backfilled membership.
3. **Monthly marks:** MaxDD understates intramonth drawdown; no intramonth stop-loss is
   simulated (v1 disclosure carried forward).
4. **Trade-at-boundary-close** with the delta no-trade band; same idealization as v1.1.
5. **Eligibility deviation:** 37 months vs v1's 252 trading days (forced by H4's
   estimator) — a *stricter* entry rule, so it cannot manufacture extra early trades.
6. **Benchmark switch:** registered ex-ante (§1.3); SPY comparison reported non-gating.
7. **COT publication lag** handled with a conservative 7-calendar-day `as_of` shift;
   regime rows lag month-end by construction; BAA10Y lagged +1 business day.
8. **Single decade:** OOS is 2016–2026 only (regime-feed honesty floor, §3.2). A PASS
   is evidence within one macro era, not a universal law — the paper-trading gate
   exists precisely to test the next era out-of-sample.
9. **Single configuration, zero tuning:** exactly one parameterization (§6) is ever
   run. There is no second chance by design (§0.3, §4.3).

---

## §6 Frozen constants (the machine-readable registration)

Prompt 7 must extract the JSON between the fence markers below (strip leading/trailing
whitespace, UTF-8), verify `sha256(bytes)` equals the stamped hash, embed both the hash
and its recomputation in the verdict report, and load every runtime constant from this
block (or assert equality where constants are imported from house modules).

```json
{
  "registration": {"doc": "STRATEGY_V2_DESIGN.md", "doc_version": "1.0", "date": "2026-07-04", "repo_version_at_registration": "6.14.0"},
  "universe": {
    "buckets": {
      "NA": ["SPY", "QQQ", "IWM", "EWC", "XLK", "XLE", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"],
      "Europe": ["VGK", "EZU", "EWG", "EWQ", "EWU", "EWL"],
      "China": ["MCHI", "FXI", "KWEB", "ASHR"],
      "Japan": ["EWJ", "DXJ"],
      "India": ["INDA", "EPI", "SMIN"],
      "Asia": ["AAXJ", "EWY", "EWT", "EWS", "EWM"],
      "LatAm": ["ILF", "EWZ", "EWW"],
      "Middle_East": ["KSA", "EIS", "TUR"],
      "Oceania": ["EWA", "ENZL"],
      "Africa": ["EZA", "AFK", "NGE"],
      "Commodity": ["GLD"],
      "Duration": ["TLT"]
    },
    "n_tickers": 48,
    "cash_etf": "BIL",
    "risk_off_bond_etf": "TLT",
    "benchmark_gated": "ACWI",
    "benchmarks_reported": ["SPY_buy_and_hold", "60_40_ACWI_TLT"]
  },
  "spans": {
    "ingest_start": "2012-06-01",
    "run_b_start": "2014-01-01",
    "run_b_end": "2016-01-02",
    "run_a_start": "2015-01-01",
    "run_a_end_rule": "last_complete_calendar_month_at_run_time_not_before_2026-06-30",
    "oos_first_test_month": "2016-01"
  },
  "protocol": {
    "training_months": 12,
    "testing_months": 1,
    "rebalance_frequency": "monthly",
    "rebalance_mode": "delta",
    "delta_no_trade_band_pct": 0.005,
    "initial_capital": 100000.0,
    "risk_free_rate_annual": 0.02,
    "long_only": true,
    "max_leverage": 1.0
  },
  "h1_momentum": {
    "lookback_days": 252,
    "skip_days": 21,
    "top_fraction": 0.25,
    "selection_count_rule": "max(1, int(0.25 * n_eligible))",
    "tie_break": "symbol_ascending",
    "own_trend_sma_days": 200,
    "eligibility_min_monthly_boundary_closes": 37,
    "eligibility_min_adv_usd": 50000000.0,
    "adv_window_trading_days": 63
  },
  "h2_regime_gate": {
    "gate_asset": "ACWI",
    "sma_soft_days": 200,
    "sma_hard_days": 50,
    "gate_rule": {"bull": "soft_or_hard", "choppy_or_missing": "soft", "bear_no_credit_stress": "soft", "bear_credit_stress": "soft_and_hard"},
    "credit_series": "BAA10Y",
    "credit_publication_lag_business_days": 1,
    "credit_widening_threshold_pp": 0.50,
    "credit_window_observations": 63,
    "credit_missing_data_rule": "stress_present",
    "inflation_split_yoy_pct": 3.0,
    "risk_off_above_split": {"BIL": 1.0},
    "risk_off_at_or_below_split": {"TLT": 0.5, "BIL": 0.5},
    "inflation_missing_rule": {"BIL": 1.0},
    "gross_halving": "none"
  },
  "h3_cot_haircut": {
    "map": {"SPY": "13874+", "QQQ": "20974+", "TLT": "020601", "GLD": "088691"},
    "extreme_long_percentile_min": 90.0,
    "haircut_multiplier": 0.5,
    "publication_lag_calendar_days": 7,
    "percentile_window_weeks": 156,
    "warmup_weeks": 52,
    "warming_or_missing_rule": "no_action",
    "backfill_start_year": 2010
  },
  "h4_kelly_sizing": {
    "kelly_fraction": 0.25,
    "estimation_window_monthly_returns": 36,
    "win_definition": "monthly_return_gt_0",
    "drop_if_full_kelly_le_0": true,
    "min_weight": 0.02,
    "max_weight": 0.10,
    "region_bucket_cap": 0.40,
    "gross_cap": 0.85,
    "scale_up": "never",
    "residual": "BIL"
  },
  "gates": {
    "g1_sharpe": "oos_sharpe_v2_strictly_greater_than_oos_sharpe_acwi_bh",
    "g2_max_abs_drawdown_pct": 25.0,
    "g3_max_degradation_pct": 30.0,
    "g3_avg_is_le_0_rule": "FAIL",
    "g4_mean_active_gt_0": true,
    "g4_one_sided_t_min": 1.66,
    "g5_ic_min": 0.05,
    "g5_p_max": 0.05,
    "g5_ic_population": "all_eligible_assets_pooled_pre_gate_pre_haircut"
  },
  "minimums": {
    "oos_windows": 125,
    "eligible_assets_every_oos_rebalance": 25,
    "ic_pairs": 3000,
    "baa10y_coverage_from": "2013-06-01",
    "regime_first_row_by": "2014-12-31",
    "regime_max_gap_days": 45,
    "cot_fully_warm_by": "2014-01-01"
  },
  "annualization": {
    "ppy_rule": "365.25_div_median_equity_point_spacing_days",
    "sharpe_factor_monthly": "sqrt(ppy)~=sqrt(12)~=3.4641",
    "sharpe_factor_daily_diagnostics_only": "sqrt(252)~=15.8745",
    "forbidden": "any_252_based_factor_on_monthly_aggregates",
    "cagr": "geometric_(1+total)^(1/years)-1_years=days/365.25",
    "monte_carlo_rescale": "sqrt(ppy/252)",
    "mirror_assert_tolerance": 1e-06
  }
}
```

**SHA-256 of the JSON block above:**
`ab070e26a744b1ce5c5ad1e936a485c285df51a58852428147da7c23e07d94ce`

*(Stamped by the Prompt 6 session immediately after writing the block; Prompt 7 must
recompute and match before running anything. A mismatch means this registration was
edited — void, per §0.2.)*

---

## §7 Prompt 7 mechanical execution checklist

Everything below is decided; Prompt 7 implements, it does not design.

### 7.1 Preconditions and command sequence

```bash
# Branch: research/strategy-v2-verdict  (per the Playbook model map)
export FRED_API_KEY=...                                   # free key; ALFRED uses the same
pip install -r requirements.txt                            # + current yfinance for ingest
python scripts/backfill_cot_history.py --start-year 2010 --verify
python scripts/build_regime_history.py                     # point-in-time regime feed
python scripts/run_strategy_v2_verdict.py                  # NEW — the only new file
```

All three runs are offline research on historical data (freeze-compatible, the class of
work Prompt 7 explicitly authorizes). No live pipeline, no paper trading, no `main.py`.

### 7.2 Implementation constraints

- **One new file:** `scripts/run_strategy_v2_verdict.py`, cloned from the v1 runner's
  architecture (ingest → audit → Run B → Run A → benchmarks → gates → artifacts), with
  the §2.5 adapter and §2.1 universe defined **in-script**.
- **Zero edits to `modules/`** (Prompt 7 acceptance: "zero changes to live pipeline
  code"). Import and reuse: `momentum_12_1`, `is_above_sma`,
  `simple_moving_average` (`modules/strategy/engine.py`), the canonical backtester +
  `BacktestConfig(rebalance_mode="delta", benchmark_ticker="ACWI", ...)`,
  `DataWarehouse` (COT percentile, regime history), `ScreeningEngine` Kelly,
  transaction-cost constants, `fred_data` for BAA10Y.
- **Do not touch:** `strategy_universe.BENCHMARK`/`ALL_TICKERS` (live v1 path);
  `_calculate_comparison_metrics` (`walk_forward_backtester.py:1920-1934`, the √252
  daily path); the pattern library; anything under `modules/execution/`.
- Reproduce the 1e-6 mirror assert and the Monte-Carlo rescale (§3.5).
- Constants: load/echo the §6 JSON; verify its SHA-256 against the stamp; where a value
  duplicates a house constant (e.g. `fractional_kelly`), assert equality at startup.

### 7.3 Required artifacts (paths ObliskIQ-relative)

| Artifact | Path |
|---|---|
| Verdict report (human) | `reports/STRATEGY_V2_VERDICT.md` (Playbook-specified location) |
| Machine payload | `reports/validation/strategy_v2_verdict_<stamp>.json` |
| Data-audit failure (only on audit fail) | `reports/validation/DATA_GAPS_<stamp>.md` |

The verdict report must contain, in order: run metadata + constants hash (recomputed);
the **per-gate table** (gate · required · actual · PASS/FAIL) for G1–G5; the §3.4
minimums table with actuals; aggregate metrics for v2, ACWI B&H, SPY B&H, 60/40
ACWI/TLT; the §3.6 diagnostics incl. all four ablations; the §5.4 disclosures with
run-specific numbers; and the one-line verdict. If any gate fails: state
**FAIL — Strategy v2 is PARKED per STRATEGY_V2_DESIGN.md §4**, stamp this design doc's
header line, and stop — no tweak proposals in that session.

### 7.4 Failure paths

- Data audit fails → DATA_GAPS artifact, exit 2, fix data, re-run (§0.3 — no verdict was
  produced).
- Any crash after gate metrics exist → the metrics stand; re-running to "get a better
  draw" is prohibited (deterministic pipeline; a re-run must reproduce them within
  float tolerance and must be reported as a reproduction, not a fresh verdict).

---

## §8 References

- Jegadeesh & Titman (1993), *Returns to Buying Winners and Selling Losers* — 12-1
  momentum. Rouwenhorst (1998), *International Momentum Strategies*. Asness, Moskowitz &
  Pedersen (2013), *Value and Momentum Everywhere*. Moskowitz, Ooi & Pedersen (2012),
  *Time Series Momentum* — trend filters. Faber (2007), *A Quantitative Approach to
  Tactical Asset Allocation* — asset-class trend/momentum conventions. Antonacci (2014),
  *Dual Momentum Investing*.
- Kelly (1956), *A New Interpretation of Information Rate*. Thorp (2006), *The Kelly
  Criterion in Blackjack, Sports Betting, and the Stock Market*. MacLean, Thorp & Ziemba
  (2011), *The Kelly Capital Growth Investment Criterion* — fractional Kelly under
  estimation error.
- Soros (1987/2013), *The Alchemy of Finance* / *Fallibility, Reflexivity, and the Human
  Uncertainty Principle* — positioning/narrative feedback.
- Bailey, Borwein, López de Prado & Zhu (2014), *Pseudo-Mathematics and Financial
  Charlatanism* — the no-iteration commitment's basis.
- Internal: `docs/STRATEGY_V1_VERDICT.md`, `docs/STRATEGY_V1_1_VERDICT.md`,
  `docs/PROVENANCE_PURGE_REPORT.md` (root docs/), `docs/SYSTEM_CENSUS.md`,
  `docs/PAPER_TRADING_GATE.md`, `docs/PLAYBOOK_FINAL_2026-07-02.md` (root docs/),
  `docs/COT_REAL.md`.
