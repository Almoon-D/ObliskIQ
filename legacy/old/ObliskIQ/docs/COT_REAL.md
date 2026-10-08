# COT_REAL — Making the Crowding Signal Live (v6.10.0)

**Status:** Delivered · **Branch:** `claude/cot-crowding-signal-rz3rax`

## 1. Why

The Asymmetry Hunter's crowding component **(a)** — the most Soros-central signal —
**self-zeroed in production**. `modules/market/cftc_cot_analyzer.py` parsed real
CFTC fields but computed the crowding percentile via `_calculate_mock_percentile()`
(*"In production, this would compare to 3 years of historical data"*), and
`fetch_latest_cot_report()` raised `NotImplementedError` unless
`ENABLE_MOCK_COT_DATA=true`. So live runs had **no COT data**, the payload status
was never `success`, and `_crowding_component` returned 0 weight.

This is the **same bug class** fixed for IV rank in v6.1.0 (a percentile that needs
accumulated history, faked from a placeholder). We applied the **IV-collector
pattern** to COT. Unlike IV, CFTC history *is* downloadable, so a one-time backfill
makes percentiles meaningful on day one.

## 2. History-store design (mirrors the IV snapshot pattern)

New warehouse table `cot_history` (`database/schema.sql`, also PostgreSQL schema),
idempotent per `(contract_code, report_date)`:

| column | meaning |
|---|---|
| `contract_code` | CFTC `CFTC_Contract_Market_Code` (e.g. `067651`) |
| `contract_name` | canonical name (e.g. `Crude Oil`) — what the Hunter matches |
| `report_date` | COT "as of" Tuesday |
| `large_spec_net` | Non-Commercial long − short |
| `commercial_net` / `small_spec_net` | hedger / nonreportable nets |
| `open_interest` | OI (All) |
| `net_spec_pct_oi` | `large_spec_net / open_interest × 100` (the percentile metric) |

Warehouse helpers added (`modules/data/warehouse.py`), 1:1 with the IV helpers:

| IV | COT |
|---|---|
| `store_iv_snapshot` | `store_cot_snapshot(rows)` — `INSERT OR REPLACE` |
| `count_iv_snapshots` | `count_cot_snapshots(code, weeks=156, as_of=None)` |
| `_calculate_iv_rank` | `_calculate_cot_percentile(code, net_pct_oi, as_of=None)` |
| `iv_rank_with_status` (N/60) | `cot_percentile_with_status` → **N/52 weeks** |
| `MIN_IV_HISTORY_DAYS=60` | `MIN_COT_HISTORY_WEEKS=52`, `COT_PERCENTILE_WEEKS=156` |
| — | `latest_cot_date` / `count_cot_history` (hunter-status freshness) |

`cot_history` is a *new table*, so `CREATE TABLE IF NOT EXISTS` (run on every
warehouse open) covers existing DBs automatically — no column migration needed.

## 3. Data source & backfill

- **Feed:** CFTC **legacy futures-only annual archives** — one file covers all our
  tracked contracts (commodities *and* financials) and its `Non-Commercial` field
  matches the analyzer's existing `large_spec_*` schema.
  - Per-year: `https://www.cftc.gov/files/dea/history/deacot{YEAR}.zip` (1986–present)
  - Each ZIP holds one `annual.txt` CSV. The parser
    (`parse_cot_annual_zip`) is **shared** by the live Phase-1 fetch and the
    backfill (DRY), tolerant of header whitespace, selecting columns by name.
- **Live path:** `fetch_latest_cot_report` downloads the current-year ZIP (prior
  year fallback in early January), parses it, returns the latest weekly row per
  tracked contract. Phase 1 UPSERTs that week into `cot_history` *before* computing
  percentiles (so the current obs is in its own rank), then analyzes with
  `as_of=today`. Network/parse failure degrades to `unavailable` — never aborts.
- **Backfill:** `scripts/backfill_cot_history.py` (mirrors `backfill_fred_data.py`).
  Downloads `deacot{year}.zip` over a year range (**default 2015→present**: ≥3y for
  live percentiles *and* enough prior history for the replay's 2018Q4 window),
  reassembling via HTTP **Range** requests so transfers survive proxy/body caps.

**Verified backfill coverage (2015→2026, run 2026-06-29):**

```
Total COT rows: 6,589  ·  latest: 2026-06-23
Crude Oil (067651)        599 wk  WARM   2015-01-06 → 2026-06-23
Gold (088691)             599 wk  WARM   2015-01-06 → 2026-06-23
S&P 500 (13874+)          599 wk  WARM   2015-01-06 → 2026-06-23
10-Year Treasury (043602) 599 wk  WARM   2015-01-06 → 2026-06-23
30-Year Treasury (020601) 599 wk  WARM   2015-01-06 → 2026-06-23
Euro (099741)             599 wk  WARM   2015-01-06 → 2026-06-23
(+ Nasdaq, Natural Gas, Silver, Copper, US Dollar Index — also 599 wk each)
```

### Contract-code verification (prompt item 3)

Codes were verified against the real CFTC 2024 archive. **One bug fixed:**
`US30Y` was mapped to `043607`, which is actually **"ULTRA UST 10Y"** — the wrong
contract. The classic 30-year CBOT **"UST BOND"** futures is `020601`; the alias map
was corrected. All six Hunter-mapped contract *names* exactly match the
`AssetSpec.cot_aliases` (Crude Oil, Gold, 10-Year Treasury, 30-Year Treasury, Euro,
S&P 500), so `_crowding_component` matches without changes.

## 4. Percentile methodology

`large_spec_percentile` = inclusive rank of the current `net_spec_pct_oi` within the
trailing **156-week (3-year)** window of `cot_history` rows with
`report_date <= as_of`:

```
percentile = 100 × (# stored net_spec_pct_oi values ≤ current) / (# values in window)
```

- **3-year window** matches `CFTCCOTAnalyzer.lookback_weeks=156` and the COT
  literature's 3y convention.
- **Warm-up:** until ≥ **52** weekly observations exist in the window,
  `cot_percentile_with_status` returns `(None, N, "COT percentile warming up:
  N/52 weeks")` and the contract casts **no** vote — honest, like IV's N/60 rule.
  The backfill clears this immediately for the major contracts; the fallback covers
  thin/new contracts.
- `as_of` is threaded everywhere, so the **replay is point-in-time honest** (ranks
  only against history dated ≤ the step date — no look-ahead).

The Hunter gate is now **symmetric**: high percentile (crowded long) → contrarian
short; low percentile ≤ `100 − crowding_extreme_pctl` (crowded short) → contrarian
long. Previously only the high tail could fire.

## 5. Before / after — replay COT coverage

`scripts/replay_asymmetry_hunter.py` reconstructs a point-in-time `cot_positioning`
payload from `cot_history` and ranks it as-of each step. **It is a firing-frequency
sanity check, NOT a PnL/backtest claim.**

| Window | COT cov. (before → after) | Crowding vote (real extremes) |
|---|---|---|
| 2018Q4 (Volmageddon) | gap/skipped → **14/14** | **11/14** steps |
| 2020-02/03 (COVID) | gap/skipped → **9/9** | **9/9** steps |
| 2022 (rate-shock bear) | gap/skipped → **53/53** | **51/53** steps |
| 2025 (recent) | gap/skipped → **53/53** | **44/53** steps |

- **Before:** COT was a documented coverage gap ("mock != history") — the replay
  skipped crowding entirely.
- **After:** full point-in-time COT coverage, and the crowding component cast a
  directional contrarian vote in the large majority of stress-window steps —
  exactly where positioning extremes cluster.
- **Crowding *contributing to a full firing* = 0** in this measurement because a
  COT-only warehouse can't satisfy the ≥3-independent-component rule (the replay's
  macro/IV point-in-time series weren't populated in that run). With a fully
  populated warehouse, crowding now counts toward the ≥3.

## 6. Confirmation: the Hunter receives real crowding

`python main.py hunter-status` against the backfilled warehouse:

```
Components currently LIVE: 4 of 7  (crowding, regime_fragility, cross_asset, sentiment_extreme)
  ✅ [LIVE] (a) Crowding (CFTC COT)  Crude Oil 156/52, Gold 156/52, 10-Year Treasury 156/52,
            30-Year Treasury 156/52, Euro 156/52, S&P 500 156/52
     No COT coverage (structural 0-weight): ITA, EWG, EWJ, EWZ. Latest COT report: 2026-06-23.
```

Crowding moved from `MOCKED-OFF` → `LIVE`. The four regional/defense ETFs with no
CFTC contract are flagged as an honest structural 0-weight.

## 7. Grading continuity

No component **weight** changed, so `AsymmetryConfig.weights_hash()` is unchanged
and the AI Scoreboard grading contract stays continuous — crowding simply
transitions to a live, contributing component (surfaced in the grading clock and
`hunter-status`).

## 8. Tests (offline, `pytest -m "not live"`)

`tests/test_cftc_cot_history.py` + a recorded CFTC slice in
`tests/fixtures/cot_annual_sample.csv` (no live calls in CI):

- parser field extraction + the corrected 30Y code (`020601`);
- percentile-from-history rank correctness;
- 52-week warm-up fallback;
- point-in-time `as_of` ranking;
- contrarian direction mapping (extreme long → short, extreme short → long);
- the symmetric Hunter gate (both tails fire; warming-up self-zeros).

## 9. Operational notes

- One-time setup: `python scripts/backfill_cot_history.py --start-year 2015`
  (the script backfills *and* prints the verification report in one pass; the
  separate `--verify` flag is report-only and skips fetching — use it later to
  re-check coverage without re-downloading).
- Thereafter every pipeline run UPSERTs the latest week (idempotent; CFTC is weekly).
- `ENABLE_MOCK_COT_DATA=true` remains only as an explicit offline-dev fallback (its
  mock still degrades to 0 weight by design).
