# Strategy v1.1 — The Verdict Run

**Run date:** 2026-06-12T19:28:25  
**Strategy:** Engine v1 constants UNCHANGED + pre-registered fixes H1 (delta execution), H2 (BIL cash sleeve), H3 (point-in-time regime feed) — see "v1.1 pre-registered fixes"  
**OOS test span:** 2016-01 → 2026-05 (125 monthly windows, 12m train / 1m test, rolled monthly — same as v1)  
**Costs:** ON — PaperTrader constants (slippage 0.1%/leg + IBKR commission schedule), charged on traded DELTAS only (H1)  
**Universe:** 102 large caps (current-constituent snapshot — see bias disclosure)

## VERDICT: **FAIL**

| Gate | Required | Actual | Result |
|------|----------|--------|--------|
| oos_sharpe_beats_spy | OOS Sharpe > SPY Sharpe (0.858) | 0.801 | ❌ FAIL |
| max_drawdown_under_25pct | abs(MaxDD) < 25% | -19.32 | ✅ PASS |
| degradation_under_30pct | IS→OOS degradation < 30% | -12.52 | ✅ PASS |

**Cause analysis (FAIL):** the strategy's OOS Sharpe (0.801) did not beat SPY (0.858) DESPITE the pre-registered v1.1 fixes (delta execution charging ~58%/mo turnover instead of v1's ~136%, a BIL-yielding cash sleeve for the 22 defensive months, and an ACTIVE macro regime overlay) — most likely cause: 2016–2026 was a mega-cap-led bull decade in which cross-sectional momentum on large caps plus a laggy monthly trend gate added no risk-adjusted edge over simply holding the index. Per the pre-registration this is a hard stop: NO further fixes or parameter tweaks against this same decade (Bailey et al. 2014). Salvage analysis belongs ONLY in the verdict doc's final section as v2 HYPOTHESES (e.g., TLT-in-risk-off as a directional sleeve, faster trend gates) to be designed fresh and pre-registered before any new run.

## Aggregate results (all net of transaction costs)

| Portfolio | Total return | CAGR | Sharpe | Sortino | MaxDD | % positive months |
|-----------|-------------:|-----:|-------:|--------:|------:|------------------:|
| **Strategy Engine v1** | 197.8% | 11.05% | 0.801 | 1.374 | -19.32% | 64.0% |
| SPY buy-and-hold | 340.7% | 15.31% | 0.858 | 1.289 | -23.94% | 69.6% |
| 60/40 SPY/TLT | 150.3% | 9.21% | 0.653 | 1.124 | -26.26% | 67.2% |

- **IS→OOS degradation:** -12.52% (avg IS 0.826%/mo vs avg OOS 0.930%/mo; formula: (IS−OOS)/IS×100, canonical convention)
- **Windows profitable:** 80 / 125 (64.0%)
- **Defensive-sleeve months (no equity BUYs, SPY<200d SMA — cash parked in BIL):** 22 (2016-01, 2016-02, 2016-03, 2018-11, 2019-01, 2019-02, 2019-06, 2020-03, 2020-04, 2020-05, 2022-03, 2022-05, 2022-06, 2022-07, 2022-08, 2022-09, 2022-10, 2022-11, 2023-01, 2025-04, 2025-05, 2026-04)
- **Total transaction costs paid (strategy):** $15,370 on $100,000 initial (SPY B&H: $547; 60/40: $1,022)
- **Avg monthly turnover (delta-rebalance model, H1):** 57.5% of portfolio (month-over-month holdings overlap 72%; v1's full-liquidation model charged ~136%/mo on the same strategy)
- **Signal IC (engine confidence vs realized monthly return, Spearman):** IC 0.0395, p 0.2151, n 989 (Simons gate: IC > 0.05, p < 0.05 → FAIL)
- **Beta vs SPY:** 0.37 · **Information ratio vs SPY:** -0.32

## Per-year OOS returns

| Year | Strategy | SPY B&H | 60/40 |
|------|---------:|--------:|------:|
| 2016 | +19.4% | +12.0% | +8.1% |
| 2017 | +23.4% | +22.6% | +16.6% |
| 2018 | +5.4% | -5.2% | -2.7% |
| 2019 | +3.1% | +32.3% | +25.7% |
| 2020 | +21.5% | +17.2% | +19.3% |
| 2021 | +2.4% | +28.7% | +14.5% |
| 2022 | -12.4% | -18.2% | -23.4% |
| 2023 | +16.2% | +25.5% | +15.8% |
| 2024 | +33.0% | +25.3% | +11.5% |
| 2025 | +4.4% | +18.2% | +12.4% |
| 2026 | +5.7% | +11.4% | +6.9% |

## v1.1 pre-registered fixes (measured this run)

### H1 — Turnover-realistic execution (cost drag before/after)

| Cost model | Total costs | Avg monthly turnover |
|------------|------------:|---------------------:|
| v1 full liquidation (counterfactual, same targets) | $51,309 | — |
| **v1.1 delta rebalance (actual)** | **$15,370** | **57.5%** |

- Cost drag removed by H1: **$35,938** over the OOS span (counterfactual computed in the same pass on identical targets, BIL included).
- Historical reference: v1 charged ~136%/mo turnover ($39,817 total) under full liquidation vs 72% measured holdings overlap (docs/STRATEGY_V1_VERDICT.md, 2026-06-10).

### H2 — Cash sleeve yield (BIL instead of 0%)

- Sleeve held 125 of 125 months (avg weight when held 45.2%).
- Gross sleeve P&L **$22,252**, sleeve trading costs $3,870 → net contribution **$18,382** (+18.38% of initial capital; v1: $0).
- T-bills are the parking spot; TLT-in-risk-off is a directional duration bet, explicitly NOT v1.1 (v2 hypothesis).

### H3 — Macro regime overlay (active for the first time)

- Source: warehouse.regime_history point-in-time (ALFRED vintages, scripts/build_regime_history.py) — per-series point-in-time treatment documented in reports/validation/regime_feed_<date>.md.
- Input coverage caveat: FRED serves ICE BofA credit spreads (BAMLC0A4CBBB, BAMLH0A0HYM2) only from 2023-06 (licensing truncation; no ALFRED archive exists) — before that the credit component runs on the yield curve alone. Per-month treatment in the regime feed report.
- Regime distribution across OOS windows: bear: 45 · choppy: 80.
- **Regime-driven derisk months: 45** (2020-05, 2020-06, 2020-07, 2020-08, 2020-09, 2020-10, 2020-11, 2020-12, 2021-01, 2021-02, 2021-03, 2021-04, 2021-05, 2021-06, 2022-08, 2022-09, 2022-10, 2022-11, 2022-12, 2023-01, 2023-02, 2023-03, 2023-04, 2023-05, 2023-06, 2023-07, 2023-08, 2023-09, 2023-10, 2023-11, 2023-12, 2024-01, 2024-02, 2024-03, 2024-04, 2024-05, 2024-06, 2024-07, 2024-08, 2024-09, 2024-10, 2025-01, 2025-02, 2025-06, 2026-04).
- Effect when risk-off: risk-off ⇒ gross halved to 42.5% and cash floor raised to 50% by the engine's existing compute_position_size_pct.

## Honest bias disclosure

1. **Survivorship bias (UPWARD, est. +1–2%/yr on strategy CAGR).** The universe is a current-day large-cap snapshot; yfinance serves no delisted tickers, so every 2014–2026 blow-up/delisting the strategy might have held is absent. Literature on current-constituent vs point-in-time universes (Elton, Gruber & Blake 1996; Shumway 1997 delisting bias) puts the inflation at roughly 1–2%/yr for large-cap long-only selections — and momentum specifically tends to ride names that later collapse. SPY/60-40 benchmarks are immune (the ETF return is what it actually was), so the strategy-vs-SPY comparison is biased IN THE STRATEGY'S FAVOR by roughly that amount: a marginal PASS on the Sharpe gate must be discounted accordingly.
2. **Index-membership lookahead (UPWARD, inseparable from #1).** Using today's membership leaks knowledge of which firms grew large enough to be included. Point-in-time entry (252-trading-day history + $50M ADV, both trailing) limits but does not remove this.
3. **Full-liquidation cost model — FIXED in v1.1 (H1).** The strategy now trades only the delta between consecutive target portfolios (rebalance_mode='delta', PaperTrader parity; final window liquidates fully like the benchmarks). Strategy and benchmarks finally share one execution model and one per-leg cost model; the residual counterfactual is quantified in the v1.1 fixes section. Run B's terminal window also liquidates, placing one exit-leg charge in the 2015-12 IS context month (immaterial; v1 charged exits every month).
4. **Trade-at-signal-close.** Signals use closes up to train_end; entries fill at the last close ≤ test_start, which is that same close (standard monthly-rebalance convention; 0.1%/leg slippage is charged against it).
5. **Monthly marks.** Equity is marked at monthly boundaries, so MaxDD understates intramonth drawdowns for strategy AND benchmarks alike; the proposed -8% stop-loss is NOT simulated.
6. **Idle cash earns 0% — FIXED in v1.1 (H2).** ALL strategy cash (the defensive sleeve and the 15%/risk-off cash floors) is now held in BIL with the same per-leg cost constants on its deltas. Ingest uses adjusted closes, so the T-bill yield arrives as total-return price drift. The 0.1%/leg slippage constant OVERSTATES real BIL spreads (~1bp) — kept per pre-registration; conservative against the strategy.
7. **Macro regime overlay inactive — FIXED in v1.1 (H3).** The warehouse regime_history table now carries a point-in-time monthly regime feed reconstructed from ALFRED as-published vintages (scripts/build_regime_history.py; per-series treatment, including the publication-lag fallback where vintage coverage is missing, is documented in reports/validation/regime_feed_<date>.md). No same-month macro data can influence a month's regime. 'choppy' maps to the neutral 0.5 score by the engine's existing logic.
8. **Single configuration, zero tuning.** Engine constants are the unchanged ex-ante academic defaults (12-1 momentum, 200d SMA, top decile, $50M ADV). v1.1's three fixes were PRE-REGISTERED before this run (execution realism and data plumbing only — no parameter search, no peeking-and-tweaking); per Bailey et al. (2014) this is one additional non-parametric trial on the same decade, so treat any marginal PASS with that one-extra-test discount in mind.
9. **Delta-mode trade-metric semantics (reporting note, not a return bias).** Per-trade records now carry the position's GROSS monthly price return (costs are settled in cash), so per-trade win rate / profit factor / signal-IC measure signal vs gross return, and the BIL sleeve adds cost-free monthly records (excluded from IC via signal=None). Period returns — and therefore ALL three gates — remain fully net of costs.

## Data audit summary

- Reference calendar (SPY): 3278 trading days
- FULL history tickers: 103; PARTIAL (late listing, point-in-time gated): PLTR, PYPL; excluded (missing/stale/gappy): none
- Positions skipped for missing execution prices across all windows: 0

## Per-window appendix

| Window | IS %/mo | OOS % | Buys | Skipped | Costs $ | Turnover % | SPY>SMA | Regime |
|--------|--------:|------:|-----:|--------:|--------:|-----------:|---------|--------|
| 2016-01 | +0.26 | -0.13 | 0 | 0 | 107 | 100 | False | choppy |
| 2016-02 | +0.22 | +0.02 | 0 | 0 | 0 | 0 | False | choppy |
| 2016-03 | -0.32 | +0.02 | 0 | 0 | 0 | 0 | False | choppy |
| 2016-04 | -0.08 | -1.35 | 8 | 0 | 218 | 151 | True | choppy |
| 2016-05 | -0.06 | +3.80 | 6 | 0 | 89 | 75 | True | choppy |
| 2016-06 | +0.05 | +1.51 | 10 | 0 | 127 | 85 | True | choppy |
| 2016-07 | +0.26 | +4.77 | 8 | 0 | 49 | 37 | True | choppy |
| 2016-08 | +0.61 | +3.12 | 10 | 0 | 116 | 86 | True | choppy |
| 2016-09 | +1.13 | +0.45 | 10 | 0 | 98 | 71 | True | choppy |
| 2016-10 | +1.19 | -3.32 | 10 | 0 | 54 | 36 | True | choppy |
| 2016-11 | +0.91 | +3.46 | 9 | 0 | 54 | 35 | True | choppy |
| 2016-12 | +0.91 | +5.95 | 7 | 0 | 120 | 87 | True | choppy |
| 2017-01 | +1.49 | +2.90 | 10 | 0 | 97 | 70 | True | choppy |
| 2017-02 | +1.74 | +4.12 | 9 | 0 | 145 | 103 | True | choppy |
| 2017-03 | +2.09 | -0.61 | 10 | 0 | 65 | 38 | True | choppy |
| 2017-04 | +2.03 | -2.89 | 10 | 0 | 41 | 21 | True | choppy |
| 2017-05 | +1.90 | +6.22 | 10 | 0 | 89 | 56 | True | choppy |
| 2017-06 | +2.09 | -0.31 | 10 | 0 | 47 | 23 | True | choppy |
| 2017-07 | +1.94 | +5.20 | 10 | 0 | 65 | 39 | True | choppy |
| 2017-08 | +1.97 | -1.04 | 10 | 0 | 117 | 71 | True | choppy |
| 2017-09 | +1.63 | +2.31 | 10 | 0 | 63 | 36 | True | choppy |
| 2017-10 | +1.78 | +2.31 | 10 | 0 | 92 | 54 | True | choppy |
| 2017-11 | +2.26 | +1.91 | 9 | 0 | 122 | 71 | True | choppy |
| 2017-12 | +2.13 | +1.47 | 9 | 0 | 125 | 70 | True | choppy |
| 2018-01 | +1.77 | +8.50 | 10 | 0 | 68 | 37 | True | choppy |
| 2018-02 | +2.22 | -0.63 | 10 | 0 | 78 | 38 | True | choppy |
| 2018-03 | +1.82 | -5.24 | 10 | 0 | 70 | 35 | True | choppy |
| 2018-04 | +1.42 | +4.66 | 10 | 0 | 16 | 4 | True | choppy |
| 2018-05 | +2.05 | +8.65 | 10 | 0 | 45 | 22 | True | choppy |
| 2018-06 | +2.24 | -1.54 | 10 | 0 | 108 | 55 | True | choppy |
| 2018-07 | +2.14 | +1.16 | 10 | 0 | 47 | 21 | True | choppy |
| 2018-08 | +1.81 | +6.08 | 10 | 0 | 77 | 37 | True | choppy |
| 2018-09 | +2.40 | +2.18 | 10 | 0 | 122 | 54 | True | choppy |
| 2018-10 | +2.39 | -12.34 | 10 | 0 | 155 | 70 | True | choppy |
| 2018-11 | +1.08 | -0.02 | 0 | 0 | 302 | 166 | False | choppy |
| 2018-12 | +0.91 | -4.09 | 6 | 0 | 181 | 102 | True | choppy |
| 2019-01 | +0.44 | +0.09 | 0 | 0 | 167 | 98 | False | choppy |
| 2019-02 | -0.23 | +0.19 | 0 | 0 | 0 | 0 | False | choppy |
| 2019-03 | -0.16 | +2.83 | 10 | 0 | 286 | 170 | True | choppy |
| 2019-04 | +0.52 | -2.09 | 10 | 0 | 101 | 54 | True | choppy |
| 2019-05 | -0.04 | -2.06 | 10 | 0 | 127 | 71 | True | choppy |
| 2019-06 | -0.90 | +0.00 | 0 | 0 | 282 | 169 | False | choppy |
| 2019-07 | -0.77 | -0.29 | 9 | 0 | 253 | 153 | True | choppy |
| 2019-08 | -0.89 | +1.02 | 10 | 0 | 73 | 38 | True | choppy |
| 2019-09 | -1.29 | -3.24 | 10 | 0 | 95 | 52 | True | choppy |
| 2019-10 | -1.74 | -0.40 | 10 | 0 | 69 | 37 | True | choppy |
| 2019-11 | -0.69 | +1.48 | 9 | 0 | 116 | 68 | True | choppy |
| 2019-12 | -0.57 | +5.82 | 10 | 0 | 172 | 103 | True | choppy |
| 2020-01 | +0.25 | +1.68 | 10 | 0 | 110 | 54 | True | choppy |
| 2020-02 | +0.38 | -0.18 | 10 | 0 | 77 | 39 | True | choppy |
| 2020-03 | +0.35 | -0.01 | 0 | 0 | 306 | 170 | False | choppy |
| 2020-04 | +0.12 | -0.04 | 0 | 0 | 0 | 0 | False | choppy |
| 2020-05 | +0.29 | +0.01 | 0 | 0 | 0 | 0 | False | bear |
| 2020-06 | +0.47 | +4.41 | 10 | 0 | 156 | 85 | True | bear |
| 2020-07 | +0.83 | +5.01 | 10 | 0 | 82 | 39 | True | bear |
| 2020-08 | +1.27 | +8.04 | 10 | 0 | 70 | 31 | True | bear |
| 2020-09 | +1.83 | -3.92 | 10 | 0 | 44 | 17 | True | bear |
| 2020-10 | +1.77 | -1.98 | 10 | 0 | 36 | 13 | True | bear |
| 2020-11 | +1.64 | +5.94 | 10 | 0 | 30 | 10 | True | bear |
| 2020-12 | +2.01 | +1.34 | 10 | 0 | 58 | 23 | True | bear |
| 2021-01 | +1.64 | +1.75 | 10 | 0 | 33 | 12 | True | bear |
| 2021-02 | +1.64 | -1.15 | 10 | 0 | 53 | 20 | True | bear |
| 2021-03 | +1.56 | +1.03 | 10 | 0 | 35 | 12 | True | bear |
| 2021-04 | +1.65 | +2.17 | 10 | 0 | 125 | 53 | True | bear |
| 2021-05 | +1.83 | +1.61 | 10 | 0 | 52 | 20 | True | bear |
| 2021-06 | +1.97 | -0.84 | 10 | 0 | 74 | 29 | True | bear |
| 2021-07 | +1.53 | -1.20 | 10 | 0 | 226 | 102 | True | choppy |
| 2021-08 | +1.02 | +2.90 | 10 | 0 | 51 | 20 | True | choppy |
| 2021-09 | +0.61 | -1.97 | 9 | 0 | 125 | 53 | True | choppy |
| 2021-10 | +0.78 | +5.10 | 9 | 0 | 122 | 53 | True | choppy |
| 2021-11 | +1.36 | -6.51 | 10 | 0 | 244 | 104 | True | choppy |
| 2021-12 | +0.31 | -0.02 | 9 | 0 | 151 | 66 | True | choppy |
| 2022-01 | +0.20 | -1.46 | 10 | 0 | 190 | 88 | True | choppy |
| 2022-02 | -0.07 | -5.60 | 10 | 0 | 168 | 77 | True | choppy |
| 2022-03 | -0.45 | -0.16 | 0 | 0 | 335 | 168 | False | choppy |
| 2022-04 | -0.55 | -5.09 | 9 | 0 | 305 | 153 | True | choppy |
| 2022-05 | -1.16 | -0.12 | 0 | 0 | 285 | 151 | False | choppy |
| 2022-06 | -1.30 | +0.05 | 0 | 0 | 0 | 0 | False | choppy |
| 2022-07 | -1.23 | +0.06 | 0 | 0 | 0 | 0 | False | choppy |
| 2022-08 | -1.12 | +0.21 | 0 | 0 | 0 | 0 | False | bear |
| 2022-09 | -1.34 | +0.18 | 0 | 0 | 0 | 0 | False | bear |
| 2022-10 | -1.16 | +0.20 | 0 | 0 | 0 | 0 | False | bear |
| 2022-11 | -1.55 | +0.31 | 0 | 0 | 0 | 0 | False | bear |
| 2022-12 | -0.97 | -1.46 | 10 | 0 | 166 | 85 | True | bear |
| 2023-01 | -1.09 | +0.24 | 0 | 0 | 160 | 83 | False | bear |
| 2023-02 | -0.95 | -0.01 | 9 | 0 | 147 | 77 | True | bear |
| 2023-03 | -0.48 | +0.34 | 8 | 0 | 74 | 34 | True | bear |
| 2023-04 | -0.43 | +1.69 | 9 | 0 | 59 | 27 | True | bear |
| 2023-05 | +0.14 | +2.91 | 10 | 0 | 76 | 35 | True | bear |
| 2023-06 | +0.39 | +2.41 | 8 | 0 | 82 | 37 | True | bear |
| 2023-07 | +0.58 | +1.70 | 10 | 0 | 79 | 34 | True | bear |
| 2023-08 | +0.72 | +1.11 | 10 | 0 | 50 | 20 | True | bear |
| 2023-09 | +0.79 | -1.66 | 10 | 0 | 35 | 12 | True | bear |
| 2023-10 | +0.64 | +1.39 | 9 | 0 | 45 | 17 | True | bear |
| 2023-11 | +0.74 | +3.89 | 10 | 0 | 64 | 27 | True | bear |
| 2023-12 | +1.03 | +1.20 | 10 | 0 | 55 | 21 | True | bear |
| 2024-01 | +1.26 | +5.19 | 10 | 0 | 54 | 20 | True | bear |
| 2024-02 | +1.66 | +6.25 | 10 | 0 | 83 | 32 | True | bear |
| 2024-03 | +2.18 | -1.21 | 10 | 0 | 88 | 33 | True | bear |
| 2024-04 | +2.05 | -1.33 | 10 | 0 | 33 | 10 | True | bear |
| 2024-05 | +1.79 | +2.93 | 10 | 0 | 39 | 13 | True | bear |
| 2024-06 | +1.79 | +3.17 | 9 | 0 | 100 | 38 | True | bear |
| 2024-07 | +1.86 | -4.26 | 10 | 0 | 117 | 43 | True | bear |
| 2024-08 | +1.35 | +4.42 | 9 | 0 | 45 | 15 | True | bear |
| 2024-09 | +1.62 | +1.59 | 10 | 0 | 94 | 34 | True | bear |
| 2024-10 | +1.89 | +0.40 | 10 | 0 | 80 | 28 | True | bear |
| 2024-11 | +1.81 | +9.87 | 10 | 0 | 259 | 101 | True | choppy |
| 2024-12 | +2.29 | +2.60 | 10 | 0 | 124 | 42 | True | choppy |
| 2025-01 | +2.40 | +2.67 | 9 | 0 | 320 | 111 | True | bear |
| 2025-02 | +2.20 | -1.34 | 9 | 0 | 141 | 45 | True | bear |
| 2025-03 | +1.57 | -4.70 | 9 | 0 | 254 | 87 | True | choppy |
| 2025-04 | +1.26 | +0.20 | 0 | 0 | 413 | 151 | False | choppy |
| 2025-05 | +1.39 | +0.32 | 0 | 0 | 0 | 0 | False | choppy |
| 2025-06 | +1.18 | +0.48 | 10 | 0 | 240 | 85 | True | bear |
| 2025-07 | +0.96 | +1.10 | 9 | 0 | 285 | 101 | True | choppy |
| 2025-08 | +1.41 | +0.56 | 10 | 0 | 164 | 57 | True | choppy |
| 2025-09 | +1.10 | +5.14 | 10 | 0 | 251 | 87 | True | choppy |
| 2025-10 | +1.39 | +2.14 | 10 | 0 | 177 | 58 | True | choppy |
| 2025-11 | +1.53 | -2.89 | 9 | 0 | 315 | 103 | True | choppy |
| 2025-12 | +0.49 | +1.00 | 10 | 0 | 309 | 105 | True | choppy |
| 2026-01 | +0.36 | +3.50 | 10 | 0 | 122 | 39 | True | choppy |
| 2026-02 | +0.43 | -3.43 | 9 | 0 | 173 | 55 | True | choppy |
| 2026-03 | +0.25 | -0.12 | 9 | 0 | 77 | 24 | True | choppy |
| 2026-04 | +0.64 | +0.11 | 0 | 0 | 453 | 153 | False | bear |
| 2026-05 | +0.63 | +5.76 | 10 | 0 | 815 | 276 | True | choppy |

## Non-blocking analyses (informational — do NOT affect the verdict)

### Monte Carlo confidence intervals

- Method: i.i.d. bootstrap of OOS monthly returns (research/validation/monte_carlo.py) — 2,000 sims
- OOS Sharpe: point **0.801**, 90% CI [0.442, 1.559], median 1.002 (monte_carlo.py uses sqrt(252) annualization; rescaled by sqrt(12/252)=0.216 to the headline convention)
- P(Sharpe > 0): 100.0% · P(profitable OOS): 99.9%
- Total-return 90% CI [59.14%, 443.45%] · MaxDD 90% CI [-28.62%, -8.9%]

### Historical stress replay

| Window | Months | Strategy ret | Strategy MaxDD | SPY ret | SPY MaxDD |
|--------|-------:|-------------:|---------------:|--------:|----------:|
| 2018Q4 — Q4 vol-pocalypse | 3 | -15.9% | -4.1% | -13.7% | -8.7% |
| 2020 Feb–Mar — COVID crash | 2 | -0.2% | -0.0% | -21.3% | -18.1% |
| 2022 — rate-hike bear | 12 | -12.4% | -11.1% | -18.2% | -21.0% |

_actual strategy OOS returns sliced to each window (our data)._

Hypothetical instantaneous shocks (equal-weight sector book):

| Scenario | Portfolio loss | Margin call | Forced liquidation |
|----------|---------------:|:-----------:|:------------------:|
| 2020_covid | -28.4% | no | no |
| 2008_gfc | -57.8% | yes | yes |
| 1987_crash | -19.6% | no | no |

_instantaneous one-shot shocks (research/validation/stress_tester.py) on an equal-weight sector book — NOT a replay._

### Fama-French factor attribution (Mkt-RF, SMB, HML, MOM)

- _Unavailable: factor download returned no data_

## v2 HYPOTHESES (salvage analysis — design fresh, pre-register, then run)

The three v1.1 fixes did exactly what they claimed mechanically — costs down $35,938 vs the full-liquidation model, the cash sleeve added $18,382, the regime overlay fired in 45 windows — and the Sharpe gate still failed. The deficit is therefore in the SIGNAL and the OVERLAY's judgment, not in execution realism. Candidate hypotheses for v2 (each requires its own ex-ante design; none may be retrofitted to this decade):

1. **Risk-off sleeve as a directional position (TLT).** v1.1 parks cash in T-bills by design; long duration in risk-off is a separate, directional bet that pays exactly when the equity sleeve is out (2018Q4, 2020-03, 2022 is the counter-case). Hypothesize a BIL/TLT split conditioned on the inflation component of the regime feed.
2. **Regime overlay gates gross too bluntly.** The point-in-time feed classified 45 OOS months risk-off — including long stretches of 2020-21 and 2023-24 while SPY compounded — costing more return than the overlay saved in drawdown (MaxDD improved only ~1.5pts vs v1). Hypothesize: regime modulates the TREND GATE's strictness rather than halving gross outright, and/or requires credit-spread confirmation (which also means restoring pre-2023 spread inputs from a licensed source — FRED no longer serves ICE BofA history).
3. **Monthly 200d-SMA gate is too laggy.** The 22 defensive months and the 2018Q4/2020/2022 whipsaw exits/re-entries persist unchanged from v1. Hypothesize a faster or dual-horizon trend gate (weekly evaluation, 50d/200d combination, or breadth confirmation).
4. **Cross-sectional 12-1 momentum on ~100 mega caps adds no edge over a mega-cap-led index.** The signal IC degraded to 0.039 (p 0.22) with the regime term active. Hypothesize: broader point-in-time universe, sector-neutral momentum ranks, and keeping confidence pure-momentum (regime affecting gross only, not the IC-bearing signal).
5. **Tooling for Prompt 2:** fix the Fama-French momentum-file parser in research/future/french_factors_downloader.py (the 5-factor file downloads; the momentum file fails to parse), so the factor attribution can run on the stored monthly returns in this verdict's JSON artifact.

## Reproduce

```bash
pip install -r requirements.txt  # plus a current yfinance for the ingest stage
export FRED_API_KEY=...                   # free; ALFRED uses the same key
python scripts/build_regime_history.py    # H3 point-in-time regime feed
python scripts/run_strategy_v1_verdict.py
```

Full machine-readable artifact: `reports/validation/strategy_v1_1_verdict_20260612.json`
