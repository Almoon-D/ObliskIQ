# Findings carried over from the legacy repositories

What the three earlier repositories measured or concluded that a current rule still rests on,
condensed from their own documents when the `legacy/` snapshot was deleted (Session 20). Every
figure below was produced by the legacy project named, on the date its document gives; none was
re-run here. A legacy backtest is evidence about that project's method and data, never a return
claim for this one (DECISIONS D62).

## Where the snapshot is

`legacy/` held a verbatim copy of the files MASTER_PLAN §6 names, with a manifest of every file.
Release `v0.28.0` is the last tree that contains it:

```
git fetch origin tag v0.28.0 --depth 1
git show v0.28.0:legacy/MANIFEST.md
git show v0.28.0:legacy/old/ObliskIQ/modules/analytics/asymmetry_hunter.py
```

| Tree | Repository | Commit | Licence | Files |
|---|---|---|---|---|
| `legacy/lite/` | `Almoon-D/ObliskIQ-lite-MCP` | `b1ebd490b439ce22e03de0ca25085a81d4ccd840` | AGPL-3.0 | 314 |
| `legacy/old/` | `Almoon-D/Inversor-Inteligente` | `68280e13ad8d935600a593976fb8cce1bda53bfe` | Apache-2.0 | 81 |
| `legacy/pulse/` | `Almoon-D/market-pulse-bot` | `900dbf404320917f41beac5ac56865c238aebbbe` | AGPL-3.0 | 31 |

Code ported from them keeps its attribution in the module that holds it (a docstring names the
source file); the Apache-2.0 files are used under that licence, which is compatible with this
repository's AGPL-3.0-or-later. A path such as `legacy/lite/config.yaml:134-144` in
`docs/DECISIONS.md` or `docs/SESSION_PROMPTS.md` is an address in that tag.

## Inversor-Inteligente (`legacy/old/`)

### The deterministic strategy failed its own pre-registered gate, twice

`ObliskIQ/docs/STRATEGY_V1_1_VERDICT.md` (run 2026-06-12) and `STRATEGY_V2_DESIGN.md` §1.1.
Walk-forward, 12 months of training and one of test rolled monthly, out of sample 2016-01 to
2026-05 (125 windows), 102 US large caps, costs on.

| Run | OOS Sharpe | SPY Sharpe | Max drawdown | Signal IC (Spearman, n = 989) | Verdict |
|---|---:|---:|---:|---|---|
| v1 | 0.805 | 0.858 | −20.79 % | 0.0611, p 0.0546 | fail |
| v1.1 | 0.801 | 0.858 | −19.32 % | 0.0395, p 0.2151 | fail |

- v1.1 changed execution only, each change registered before the run: orders for the difference
  between consecutive targets (costs fell from $39,817 to $15,370 on $100,000; turnover 57.5 % a
  month against about 136 %), idle cash in T-bills (+$18,382), and a point-in-time regime feed
  from ALFRED vintages (it de-risked 45 of 125 months). The Sharpe gate still failed, so the
  deficit was in the signal and the overlay, not in execution realism. v1.1: CAGR 11.05 % against
  SPY's 15.31 % and 9.21 % for 60/40; beta 0.37.
- Biases the verdict disclosed: a current-constituent universe (survivorship, estimated +1 to
  2 % a year in the strategy's favour, and membership look-ahead with it); entries at the signal's
  own close; monthly marks, which understate drawdowns within a month; FRED serves the ICE BofA
  credit spreads only from 2023-06, so the credit input ran on the yield curve alone before that.
- v2 was pre-registered (`STRATEGY_V2_DESIGN.md`, 2026-07-04: global dual momentum, a regime
  gate, a COT crowding haircut, quarter-Kelly sizes; benchmark ACWI; frozen constants; one run;
  park on any failed gate, no v2.x on the same decade) and its verdict stamp was still `PENDING`
  at the snapshot. Its annualisation section exists because an earlier report annualised monthly
  figures with √252.

Rests on it: D62 to D64 (pre-registration, one confirmation run, no re-tuning on a seen sample),
the research harness's delta execution and elapsed-time annualisation, and the plan's refusal to
port the regime classifier.

### The pattern library: 0 of 31 trusted on honest sample sizes

`docs/PROVENANCE_PURGE_REPORT.md` (2026-06-10). A generator multiplied each pattern's sample by
ten with a floor of 400, so 30 of 31 patterns cleared a gate that needs a 95 % interval no wider
than 0.20 (about n ≥ 250). With the real counts (7 to 200 observations) none does: 19 fail on
n < 30, 11 on the interval's width, one on p ≥ 0.05; the narrowest interval is 0.249. The stated
ICs are the papers' own, never re-derived out of sample, and the report expects 20 to 50 % less.
Benjamini–Hochberg across the 31 was added to the record.

Rests on it: `research/pattern_registry.yaml` keeps the 31 definitions and discards every stated
IC, n and hit rate (D-entries on the registry, MASTER_PLAN §6.4), and the false-discovery control
in `research/patterns.py`.

### What that project never did

`docs/KNOWN_LIMITATIONS.md` (2026-10-02). No live run ever completed; nothing in it shows a
predictive edge; Yahoo was the single source of prices; the paper book refused non-USD listings;
measured line coverage was 62.9 %. It describes itself as an execution and risk chassis, not a
strategy with a demonstrated edge.

### The Asymmetry Hunter as built there

`ObliskIQ/docs/ASYMMETRY_HUNTER.md`, `HUNTER_READINESS.md`, `GEO_INPUTS.md`, `COT_REAL.md`.

- **Rule.** Deterministic; an asset is a candidate only when at least three independent
  components are at an extreme and agree on direction; the composite is the weighted mean of the
  contributors alone; at most five candidates; most runs emit none. A component on mocked,
  approximate or immature data carries zero weight and says so.
- **Components and weights (sum 1).** Crowding from CFTC positioning 0.19 (large-speculator
  percentile ≥ 85, or ≤ 15, contrarian); volatility mispricing 0.19 (IV rank ≥ 90 or ≤ 10 after
  60 daily snapshots; 25-delta skew ≥ 3.0); regime fragility 0.17 (composite ≥ 60 and rising);
  cross-asset divergence 0.17 (|high-yield spread z − VIX z| ≥ 2.0); sentiment 0.15 (≥ 90th or
  ≤ 10th percentile); Polymarket divergence 0.07 (|own probability − market's| ≥ 0.20); GDELT
  pulse 0.06 (regional volume z ≥ 2.5 with tone ≤ 0, at least 20 observations). A catalyst within
  ten days is a window, not a vote.
- **Expression and rails.** A long call or put at delta 0.60 to 0.70, three to six months out,
  never under 30 days, open interest above 1,000; 2 % of the portfolio per idea and 5 % across all.
- **Its replay fired nothing.** Over 2018Q4, 2020-02/03, 2022 and 2025 the replay produced zero
  candidates in 129 steps. Its own note: a data gap, not a signal. The warehouse held no
  point-in-time history for the cross-asset and regime components, so three agreeing components
  could not be reached from positioning alone. Sentiment's Fear & Greed and put/call inputs were
  placeholders, the commodity term structure a moving-average approximation.
- **Positioning data.** The crowding percentile had been computed from a placeholder and the
  fetch raised unless a mock flag was set, so the component was zero in production until the
  CFTC's futures-only annual archives were loaded (`cftc.gov/files/dea/history/deacot{YEAR}.zip`,
  one `annual.txt` each, 1986 onward; 599 weeks per contract from 2015). The percentile is the
  inclusive rank of net speculative positions over open interest in a 156-week window, valid
  after 52 weeks. One contract code was wrong: `043607` is the Ultra 10-year, and the 30-year
  bond is `020601`.
- **Polymarket and GDELT.** Gamma API by market slug, the "Yes" price as a probability, a daily
  snapshot per market; GDELT 2.0 DOC API timelines of volume and tone per region over seven days,
  z-scored against a running mean and variance that exclude the day's own point.

Rests on it: MASTER_PLAN §5.5's event sleeve (components, measured independence, zero weight for
immature inputs), `data/positioning/cot.py`, `data/events/`, and Session 15's prompt.

### Grading an AI's calls, and what a paper gate cannot prove

`ObliskIQ/docs/AI_SCOREBOARD.md`, `PAPER_TRADING_GATE.md` (both v6.16.0).

- The first scoreboard tested "beat SPY" as a proportion over every logged row. It could not
  work: a name proposed on consecutive runs was counted several times; half is not the no-skill
  rate of a long-biased book; SPY is the wrong yardstick for a foreign listing; 60 rows detect
  only a hit rate near 68 %; layers were pooled. Detecting a rank IC of 0.05 on proposals needs
  about 3,100 independent observations.
- Its replacement, registered before data: one observation per layer, name, direction and ISO
  week; a four-week horizon fixed in advance; weekly cohorts with Newey–West errors (three lags);
  at least 26 cohorts and 30 observations; excess over the proposal's regional benchmark in the
  base currency; verdicts `adds_value`, `anti_predictive`, `decoration` (the interval excludes an
  edge of +0.5 points per four weeks) or `inconclusive`.
- The weekly forecast panel is the powered test: the same question on a fixed 188-name universe
  once a week (the probability of beating ACWI over four weeks, and the expected excess), with
  12-1 momentum logged beside each forecast so that incremental IC is measured. One cohort's rank
  IC has a standard error near 0.073; a year of overlapping cohorts resolves an IC of about 0.06
  at 80 % power, and one below 0.05 is invisible at this size.
- The paper gate (Sharpe above 0.5, drawdown under 20 %, win rate above half of at least 30
  closed round trips, 90 calendar and 126 trading days, excess over ACWI not demonstrably
  negative) protects capital from an unstable book and proves no skill: a daily Sharpe over 90 to
  126 days has a standard error near 2, so a strategy with none passes about 40 % of the time.

Rests on it: `scoreboard/grading.py`, `scoreboard/forecast_panel.py`, the promotion gate's
operational criteria (D63, D169 to D171) and the rule that software tests and paper results are
not evidence of returns.

### Free LLM tiers, as checked in 2026-10

`ObliskIQ/docs/AI_PROVIDERS.md`. Gemini's free tier serves Flash models only (Pro paid-only since
April 2026), with limits per project, not per key. OpenRouter's `:free` models allow 20 requests
a minute and 50 a day (1,000 after any purchase of $10 of credit) and their ids rotate. NVIDIA's
developer tier allows about 40 requests a minute. Groq's free tier (about 8,000 tokens a minute)
is smaller than one of that project's prompts. A fallback that answers is graded under its own
`provider:model`. Current provider facts are in `macro-sources.md`.

## ObliskIQ-lite-MCP (`legacy/lite/`)

Its broker, macro and commodity references are condensed in `ibkr.md`, `ibkr-orders.md`,
`macro-sources.md` and `commodities.md`; its deployment walkthroughs in `docs/DEPLOY.md`. What
follows is what those files do not hold.

### Decisions of lite's that current rules rest on

From `docs/DECISIONS.md` @b1ebd49; `docs/DECISIONS.md` D181 lists them.

- **The option sleeve was viable at the account's size.** Counted against live chains: ten
  contracts cleared every gate at once against a floor of three, and three still cleared on the
  reduced budget. Concentration, not diversification, because splitting does not shrink the
  penalty.
- **The Client Portal API is the collection route of record**; IBKR's connector is a fallback
  that needs a person present. The first live Client Portal run (2026-09-10) collected 3,832
  chain rows over 21 underlyings.
- **A cell no collector can ever fill is `inapplicable`, declared where it is defined**, not
  missing: before it, coverage counted unmapped leaves and every build read as degraded.
- **A health verdict is about age.** A status describes one run; the silence since is a separate
  concern, measured against the `as_of` asked about. A collector whose timer had stopped three
  months earlier read as healthy until age became a named concern.
- **A guard is verified by mutation, not by being green.** A re-audit broke three stated
  invariants on a copy of the tree and all 1,770 tests still passed; a test that pins an
  invariant is finished only when the invariant has been broken once and the test failed.
- **A collector reads the clock once, at the top of its run**, and every date below comes from
  the run, or the run cannot be replayed.

### marketdata.app, a vendor of US option chains

`references/marketdata-playbook.md`; tags `[MD ✓ <date>]` are lite's live calls. Not used here
(`MARKETDATA_API_TOKEN` is an optional source, MASTER_PLAN §5.18); recorded for the session that
prices option history.

- `GET /v1/options/chain/{symbol}/`: the live chain returns bid, ask, sizes, last, volume, open
  interest, the underlying's price, implied volatility (a decimal, 0.1495) and all greeks
  `[MD ✓ 2026-08-28]`. Answers arrive as HTTP **203** with a complete body, in parallel arrays;
  timestamps are epoch seconds in US Eastern time, and `date=` names a US session.
- **Historical chains carry no implied volatility and no greeks** `[MD ✓ 2026-09-09]`: 608 SPY
  contracts for one past session, `iv` and `delta` null on every one, prices and open interest
  complete. The pricing page's "IV & Greeks included" is true of the live chain only. Implied
  volatility must be solved from the mid, which needs a rate at each maturity and a dividend
  yield.
- Billing: the live tier costs one credit per option symbol returned; a dated request costs one
  credit per 1,000 symbols (608 contracts, one credit). Usage is in the `x-api-ratelimit-*`
  headers. A new free token showed 10,000 credits a day against a documented 100.
- **One IP address at a time**, enforced with HTTP 403 and a five-minute block that costs no
  credit: unusable from a cloud sandbox whose egress address rotates; collect from a host with a
  fixed address.
- Historical coverage is per symbol: XLP answered `no_data` with no neighbouring dates while SPY,
  QQQ, XLF and XLE were served for the same session. European listings are not covered.

## market-pulse-bot (`legacy/pulse/`)

Its venue windows, seasonal closes and synthetic calendars are `config/defaults/venues.yaml`
(each entry's `origin` names it, with what was and was not re-verified) and `calendars.md`. It
showed SGX's regular session straight through the lunch break (an expected failure in its own
tests) and measured elapsed durations on local clock time across a daylight-saving change;
`calendars/` does neither.
