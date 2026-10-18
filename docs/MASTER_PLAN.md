# ObliskIQ — Master Plan

Committed by Session 1 as `docs/MASTER_PLAN.md`. Based on a full read of
`Almoon-D/Inversor-Inteligente` @ `68280e1`, `Almoon-D/ObliskIQ-lite-MCP` @ `b1ebd49` and
`Almoon-D/market-pulse-bot` @ `900dbf4`. Owner: Álvaro — one IBKR account, EUR base,
tax-resident in Spain (changeable).

This file says **what is being built and why**. Numbers it quotes are defaults: `config/`
owns every value, and changing one there needs no edit here. Progress lives in
`docs/ROADMAP.md`. A decision recorded here
changes only through a `docs/DECISIONS.md` entry that names the evidence, followed by an
edit to this file in the same PR. Sessions do not re-derive the plan.

---

## 0. How to read this

| You need | Read |
|---|---|
| What to build in this session | `docs/SESSION_PROMPTS.md`, your session's section |
| How to behave in this repo | `AGENTS.md` (loaded through `CLAUDE.md`) |
| Why a rule exists, what was rejected | `docs/DECISIONS.md` |
| Observed broker and data-source behaviour | `docs/knowledge/*.md` |
| What is done and what is open | `docs/ROADMAP.md` |
| The design | this file |

---

## 1. Mission

A global, autonomous portfolio manager for one IBKR account that:

1. **Survives first.** A diversified, regime-agnostic core that cannot blow up: no
   leverage, no short premium, no shorting, nothing the owner's jurisdiction forbids.
2. **Compounds second.** The core carries documented premia — multi-asset exposure and
   systematic trend following — and adapts its allocation within the owner's limits through
   rules that pass a pre-registered test; until one does, it holds the variant the owner's
   tolerance picks from the after-tax frontier that the objective below scores (§5.4,
   DECISIONS D89).
3. **Hunts asymmetry third.** A satellite that searches every market IBKR reaches for the
   few stocks, sectors and assets that move far more than the market — dislocations,
   reflexive loops, crowded positioning, forced selling, informed buying — corroborates them
   with independent data and expresses them with capped loss: shares with volatility-scaled
   stops, or long vanilla options when they beat the shares for the same signal.
4. **Measures itself.** Every hypothesis is pre-registered and tested on point-in-time
   history with a decision rule that says sufficient, insufficient, inconclusive or not
   evaluable (§5.9); every proposal, from a detector, a pattern or an LLM, is logged and
   graded forward. **Capital follows evidence**: a layer is funded by a test it passed, and
   its live results are monitored against that evidence (§5.19).

**Objective.** Maximise the long-run growth of after-tax wealth: the annualised log growth of
the account's liquidation value, net of costs, within the owner's drawdown limit (§11,
DECISIONS D60). Tax is simulated as it arises — realised gains through the lot engine, each
year's tax paid at the profile's payment date — and at the horizon the remaining positions are
sold and their tax paid. Research reports a frontier across drawdown limits; the owner's
tolerance picks the point that receives capital. Two references are scored under the same
rule, in the base currency: a global accumulating UCITS equity ETF bought and held,
and a global 60/40. They are yardsticks and a possible outcome, not the destination. No
system knows with high certainty what markets will do: returns come from documented premia
and from asymmetric bets sized by evidence, with costs and taxes treated as costs.

---

## 2. Mandate and owner constraints

**Nothing is hardcoded.** Every value below lives in configuration. Jurisdiction rules
(what may be bought, how gains are taxed) live in swappable profiles (§5.16), so a change
of residence is a profile switch plus, at most, a new policy module.

**Defaults come from ObliskIQ-lite-MCP.** Its `config.yaml` blocks `instruments`,
`positions`, `equity_risk`, `portfolio_circuit_breaker`, `option_sleeve`, `tenor`,
`zombie_test`, `discipline`, `fx_inflation` and `commodities` are ported as
`config/defaults/` values and extended here (bonds, mutual funds, the core). Where a lite
limit meets a mechanism it never had (for example, its minimum position size and a probe
entry), the resolution is recorded in `DECISIONS.md` and the stricter risk outcome wins.

| Constraint | Rule | Where it lives |
|---|---|---|
| Base currency | Selectable (EUR today). Caps, budgets and benchmarks are in it. Other currencies may be held as working balances. | `config/owner.yaml` |
| Universe | Global: every venue and asset class IBKR offers that the account is permitted to trade (§5.3). | `config/universe.yaml` |
| Eligibility | Computed per instrument by the jurisdiction profile. Profile `es` (EU PRIIPs): single stocks, bonds, UCITS funds and ETFs, eligible ETCs and futures-tracking ETCs (lite's contango rule applies); no US-domiciled ETFs/ETPs in cash; listed options on them allowed only if closed before expiry. **Reading any instrument as data is always allowed.** | `config/jurisdictions/<id>.yaml` + policy module |
| Prohibited | Futures and futures options (curves are data only), leveraged/inverse ETPs (rejected at ingestion by resolved description), CFDs, FX forwards, any written option. | universe ingestion + gates |
| Leverage | None. Gross ≤ 100% of managed capital; no negative cash balance in any currency (a negative balance is a loan). Spot FX conversion to fund a trade is allowed. | pre-trade checks + whatif |
| Short exposure | None. Bearish = underweight or long put. | expression selector |
| Positions | An absolute minimum position derived from commissions (DECISIONS D66); lite's maximum initial size, sector, country and single-foreign-currency limits, restated per sleeve and read on look-through exposure (D69); exposure caps by asset class are optional keys, none set (D88); winners are not trimmed to fit; no averaging down without a re-underwrite carrying a new dated catalyst. | `config/defaults/positions.yaml`, `config/owner.yaml` |
| Option sleeve | Lite rules: shared premium pool, continuous penalty balance, concurrency cap, minimum premium, catalyst-driven tenor window, zombie test on two sessions, computed mandatory close; options preference: single-stock options, then UCITS ETF options, then US ETF options with mandatory close. | `engines/options_sleeve` + gates |
| Data | Free sources (several free keys allowed) plus the IBKR market data the owner's main username subscribes to and shares with its paper account. Quote policy per asset class (§5.13). | `config/sources.yaml`, `market_data.*` |
| Orders | Limit-capped only: patient limits, IBKR algos with a limit cap, stop-limit protective exits. Never a plain market order. | execution policy |
| AI | Modular providers and models (§5.10) within a monthly budget. The LLM never sizes, never places orders, never overrides a gate. | `config/llm.yaml` + import contracts |
| Account size | Any size. Minimum trade configurable as an absolute amount, a % of managed capital, or both combined; never below the commission-derived floor. | `config/owner.yaml: trading.min_trade` |
| Activation | Every capability runs when a condition computed from configuration holds — evidence (§5.9), capital, a verified broker capability — never above a NAV threshold key; the bond rule (§5.3) is the model. What pays only at larger capital is built in Session 19a (DECISIONS D66). | activation module + `obliskiq capabilities status` |
| Capital flows | The owner announces deposits and dated withdrawals; the bot plans for them at once and honours them without breaking the strategy (§5.15). | cash-flow ledger |
| Taxes | Pluggable tax policy per jurisdiction (§5.16). Not tax advice. | profile + `jurisdiction/` |
| Autonomy | Paper trading is fully autonomous across all sleeves. Live autonomy per sleeve is set by the owner at promotion. An approval queue exists, off by default. | `execution.modes` |
| Existing holdings | Everything the bot did not open is `unmanaged`: counted in NAV and risk, never traded until the owner releases it. | holdings registry |

---

## 3. Starting point

### 3.1 Inversor-Inteligente (~106k lines plus ~67k test lines)

A four-phase pipeline (free data → LLM proposes tickers → deterministic analysis → LLM
decides → human executes). Its own records show:

- Strategy v1 and v1.1 failed their pre-registered walk-forward gate against SPY (OOS
  Sharpe 0.801 vs 0.858; signal IC 0.0395, p 0.22); the drawdown and degradation gates
  passed.
- The regime classifier never produced "bull": 125 monthly windows 2016–2026 gave 45
  "bear", 80 "choppy" (`modules/macro/analyzer.py` needs three conditions for bull, any one
  for bear), so it de-risked through 2020–2024.
- No live run ever completed; the universe was US ETFs an EU retail account cannot buy.
- The pattern library was never tested on data. Its IC, hit-rate and sample-size figures
  are attributed to papers rather than measured, and most cited studies report no IC at
  all. After fabricated sample sizes were purged, 7 of 31 patterns still pass
  Benjamini–Hochberg on the stated numbers, but all fail the strict precision gate (CI
  width ≤ 0.20 needs n ≈ 250). They are **unverified**, not false (§6.4).

Kept: anti-fabrication discipline; the Asymmetry Hunter (deterministic, candidate only
when several independent components agree, honest zero-weight degradation); the
scoreboard methodology (pre-registered verdicts, Newey–West on overlapping horizons, Brier
skill, calibration); real CFTC COT history; GDELT and Polymarket clients; the LLM router
with failover; the continuity guard; the cost model; the crisis database (stress scenarios
only); the risk-parity optimizer once its objective is fixed.

The repository is archived on GitHub after Session 1 snapshots what §6.2 lists.

### 3.2 ObliskIQ-lite-MCP (~34k lines plus ~32k test lines, AGPL-3.0)

An MCP server plus a skill, used interactively. It is the engineering base:

- IBKR Client Portal Web API over OAuth 1.0a, **verified live and headless** from a cloud
  session (2026-09-10: handshake end to end, 3,832 chain rows across 21 underlyings).
  `references/ibkr-playbook.md` (~2,300 lines of observed behaviour) is the most valuable
  document in either repository.
- Point-in-time macro with release dates; FRED/ALFRED, ECB, CFTC, EIA, ONS and StatCan
  verified live.
- The grid (market × sector × 15 indicator families) with honest statuses, trajectories,
  cross-asset correlations and implied-vol level, skew and term structure; a vectorised
  BSM and IV solver; option-sleeve economics, measured operationally viable at an ~€11k
  account (ten contracts cleared every gate at once against live chains — a statement about
  the gates, not about returns); a frozen-thesis ledger; the mandate's asset and
  limit rules (§2).
- No order placement, and no unattended machine ever ran.

### 3.3 market-pulse-bot (~3.3k lines, AGPL-3.0, Python 3.14)

The owner's exchange-status monitor for 26 venues: `exchange_calendars` sessions plus
source-checked phase windows (pre-market, opening and closing auctions, lunch breaks,
post-market), seasonal closing times, synthetic calendars for venues the library lacks,
and a conservative incident store for halts and exceptional closures (manual entries for
market-wide events, because no trustworthy free market-wide feed exists for most venues;
structured feeds for single-stock halts). It becomes the calendar layer (§5.12).

### 3.4 The lesson

Both investing projects spent most of their effort on documentation and process and
neither placed an automated order; the one that tested its strategy learned it had no edge
only at the end. ObliskIQ inverts both: evidence comes first — a research harness, the
core's after-tax verdict and one selection hypothesis run before the execution mechanics are
finished (§10) — and a walking skeleton trades on the IBKR paper account by Session 6, on
the deployment machine the owner brings up first (T1). Every later session replaces a stub
with a real engine, built for what the evidence supports. Docs stay lean: this file, a short
`DECISIONS.md`, a `ROADMAP.md` session log, knowledge files.

---

## 4. Design principles and rejected alternatives

| Idea | Decision | Why |
|---|---|---|
| "With high certainty about the future, investing is easy." | Edge = calibrated probability × asymmetric payoff. | Certainty is not available; overstated edges are what ruin a Kelly-sized account. |
| Simons as microstructure trading: market making, intraday arbitrage, order micro-blocks, multi-asset option baskets. | Rejected. Simons here is statistical discipline: validation before trust, cost-aware execution, evidence-based sizing, a hard risk engine, the scoreboard. | Not achievable on a REST API at retail size; the atomic unit of an option position at this size is one contract. |
| A daily macro regime model setting the allocation. | Rejected. The core is regime-agnostic; its dynamic element is trend following. | The legacy classifier was wrong for a decade; trend has the stronger evidence. |
| A core that only follows bands, no trend. | Kept as a variant (A); the after-tax verdict decides, and variants it cannot tell apart resolve to the simplest. | Bands alone give no crash protection; an unlevered risk-balanced core with long-duration bonds fell sharply in 2022, when stocks and bonds dropped together. The after-tax backtest decides (§5.4). |
| Trend as an on/off switch at the 200-day average. | Rejected. Trend is a partial, slow, hysteretic tilt with tax-aware execution. | Whipsaw and realised gains are costly, especially in Spain, where ETF switches are taxable. |
| Adding to long options after the move confirms. | Rejected. Options are bought in one block when implied vol is cheap; exits are scaled. Pyramiding applies to shares only. | Adding later pays a higher premium for the same convexity. |
| Kelly with probabilities shrunk toward 0.5. | Rejected. Probabilities shrink toward the **base rate** (market-implied or random-walk), and until a layer is calibrated it sizes by a fixed risk budget (§5.11). | A 3:1 target is rarely a coin flip; anchoring at 0.5 overstates the edge, while anchoring at the base rate with no evidence gives Kelly ≈ 0 and would freeze the satellite. |
| A fixed account-size threshold for direct bonds. | Rejected in favour of a computed rule: a bond qualifies only when its minimum increment and per-order minimum commission fit the role's amount and it beats the best fund on all-in cost (§5.3). | The rule excludes bonds at small sizes and admits them as capital grows, without a magic number. |
| Swap partners on the same index from another issuer. | Off by default; swap partners track a different, highly correlated index (S&P 500 ↔ MSCI USA, MSCI World ↔ FTSE Developed). | Different issuers are not homogeneous under art. 8 RIRPF, which requires "un mismo emisor"; a different index also removes any argument under the general anti-avoidance rule at negligible tracking cost. |
| Free-threaded Python (3.14t) in production. | Standard CPython 3.14; parallel computation runs through a process pool (`compute.workers` in config); code kept thread-safe; 3.14t is reconsidered only if profiling shows CPU-bound parallel work that a process pool cannot serve. | Simultaneous calculation does not need 3.14t: numpy releases the GIL inside its kernels and a process pool parallelises Python-level loops today. 3.14t is slower single-threaded and not every scientific wheel ships a free-threaded build. |
| Delayed quotes for everything. | Rejected for options (real-time quotes on the option and its underlying are required); accepted for stocks, ETFs, funds and bonds with safeguards (§5.13). | Option spreads are wide and stale mids misprice. |
| One authentication route only. | Two transports behind one client: the Client Portal Gateway with automated login for paper, OAuth 1.0a for the live account where IBKR grants it (§5.12, DECISIONS D42). | IBKR told the owner (2026-10-08) that OAuth 1.0a is for institutional clients and retail accounts use the Gateway, although lite ran OAuth live on 2026-09-10. Paper logins have no second factor, so the Gateway can run unattended in paper. |
| "A grid by region or by sector." | One long-format measurement store with three views (§5.7). | Different questions need different axes; one store keeps them consistent. |
| Assuming the satellite will pick winners most of the time. | The satellite is built to find them; pre-registered evidence decides how much capital it earns (§5.9, §5.19). | 58% of US stocks returned less than one-month T-bills over their lifetimes and 4.3% of firms created all the market's net wealth 1926–2016 (Bessembinder 2018): winners exist but are rare, so a selection that misses them lags the index, and skill must be shown before it is funded. |
| Mining many patterns. | Rejected. Hypotheses come with an economic mechanism, are pre-registered, tested point-in-time out of sample with multiple-testing control, then shadow-graded live before receiving weight (§5.9). | The factor zoo (Harvey, Liu & Zhu 2016); published anomalies decay (McLean & Pontiff 2016). |
| Social-media sentiment. | Dropped. Market-implied sentiment, positioning and graded LLM event extraction instead; VADER only as an offline fallback. | Paid or restricted APIs, noise. |
| "The AI chooses sectors, then companies, then decides." | "LLM nominates, data disposes": the LLM may nominate; deterministic corroboration and scoring decide; rules size and gate. | Keeps the owner's original idea without letting the LLM own risk. |
| A risk-balanced core chosen for its Sharpe ratio. | Rejected as the default. The core's variant is picked from an after-tax frontier scored by the objective (§1), among risk-balanced, growth-tilted and equity-dominant variants (§5.4, DECISIONS D60). | Without leverage, risk balance weights by risk rather than growth (two assets of 16 % and 5 % volatility at equal risk hold about 24 % equity); a variant can beat a 60/40 on Sharpe and still compound less than the equity reference. |
| Live returns as the proof of skill ("capital follows demonstrated skill"). | Rejected. Capital follows pre-registered research outcomes; live results are monitored against them through a twin (§5.19, D65). | t ≈ IR·√years: a live record confirms a moderate edge only after a decade or more, but it can flag decay or an implementation fault much sooner. |
| Testing each component alone before any combination. | Rejected. A few combinations with a stated mechanism are pre-registered and tested beside their components, every trial counted (§5.9, D63). | An edge may exist only conditionally; an unregistered search for combinations would multiply false discoveries. |
| A market order to escalate a protective exit after a gap. | Rejected. Sizing by gap-adjusted loss, a stop spec that follows volatility and gaps, and a triggered stop repriced through the gate (§5.11, §5.12, D67). | AGENTS.md forbids plain market orders; a stop-limit's loss at the stop is not a maximum loss. |
| Capabilities gated by account size. | Rejected. Each runs when a computed activation condition holds (§2); what pays only at larger capital is built in Session 19a (D66). | No magic NAV threshold; one codebase serves a small account and a large one. |

---

## 5. Architecture

### 5.1 Overview

```
 collectors (async; per venue and per source cadence)
   IBKR: bars, snapshots, chains, curves, contract rules, account, positions, NAV
   Macro (point-in-time): FRED/ALFRED, ECB, BoJ, BIS, ONS, StatCan, EIA, Fed EBP
   Positioning: CFTC COT · Insiders: SEC Form 4 · Events: Polymarket, Kalshi, GDELT, RSS
          ▼
 SQLite store (WAL, forward-only migrations) — one long-format measurement table
          ▼
 compute (no network)
   views: equity grid · macro board · option surface · correlations · fx · curves
          ▼                                   ▼
 CORE                                  SATELLITE (Asymmetry Hunter)
 risk balance across four              components + patterns + LLM nominations →
 growth/inflation environments         measured-independence corroboration →
 + tax-aware trend tilt                frozen thesis → entry → monitor → exit
          └──────────────┬────────────────────┘
                         ▼
 DECISION: vehicle selector · expression selector · evidence-based sizing ·
 sleeve budgets · whole-book risk · breakers · tax and turnover budgets ·
 cash-flow reservations · inertia → OrderIntents
                         ▼
 EXECUTION: calendars and phases · quote policy · currency manager · pre-trade checks ·
 whatif · limit-capped orders · resting stop-limit exits · reply policy · cOID
 idempotency · reconciliation · modes (shadow | paper | live-small | live) · kill switch
                         ▼
 SCOREBOARD AND COCKPIT: TWR attribution · TCA · grading · calibration · pattern
 registry · capital allocation · promotion · Telegram, MCP cockpit and daily dashboard
```

### 5.2 Investment pillars

| Pillar | Role | Why it earns a place |
|---|---|---|
| **Risk balance** (Dalio, All Weather) | Core allocation: equal risk to growth↑, growth↓, inflation↑, inflation↓. | Survives regimes without forecasting them. |
| **Trend following** (the CTA tradition: Dennis and the Turtles, Seykota, Harding) | A slow, tax-aware tilt on the core; trend confirmation gates additions to share positions in the satellite. | The most robust systematic edge documented across a century of markets (Moskowitz, Ooi & Pedersen 2012; Hurst, Ooi & Pedersen 2017), and the cheapest crisis protection. |
| **Reflexivity** (Soros) | The satellite: dislocations, crowded positioning, policy breaks, expressed with capped loss, graded. | The source of asymmetric upside; also the least systematic, hence evidence-gated capital and a monitoring rule that returns it to shadow (§5.19). |
| **Statistical discipline** (Simons) | Validation, pattern research, cost-aware execution, evidence-based sizing, the risk engine, the scoreboard. | Not a strategy here; the reason the other three can be trusted. |

Not pillars: value investing (kept as a quality/valuation factor in stock selection),
permanent tail hedging (systematic put buying bleeds the variance risk premium; trend
protects more cheaply), FX carry (needs forwards).

### 5.3 Universe and vehicles

**Every instrument can be data; only eligible ones can be bought.** The universe declares
instruments with roles (`data`, `tradable_candidate`, `option_underlying`), attributes
(asset class, domicile, UCITS/KID status, share class and its currency hedge, currency,
listing venue, lot size, fees) and the exposures they represent (core roles, grid cells,
macro-board entities), read through to the holdings' country, sector and currency where the
issuer publishes them (DECISIONS D69).

| Asset class | Use | Notes |
|---|---|---|
| Single stocks, any IBKR venue the account is permitted on | satellite (the main hunting ground); stock baskets for a core equity role when the basket rule's computed condition holds (Session 19a) | lot sizes (e.g. Japan 100 shares, Hong Kong board lots), transaction taxes, trading hours |
| ETFs and ETCs, UCITS (incl. physically backed and futures-tracking ETCs) | core roles; satellite sector, country and commodity expressions | listed mostly in Europe and London, with exposure anywhere; futures-tracking wrappers subject to lite's contango limit |
| Government bonds and bills | core duration and cash roles when the bond rule passes | rule: minimum increment ≤ a configured share of the role's amount, per-order minimum commission (IBKR: US Treasuries 0.002% of face, minimum USD 5; EUR bonds 0.1% up to EUR 8,500, minimum EUR 1) within the cost band, enough bonds to hold the duration target, and all-in cost below the best fund; Web API trading verified in Session 19a |
| Mutual funds (UCITS) | core roles where cheaper, or where trend runs inside the fund | daily NAV, cut-off times, cash-quantity orders; Web API support verified in Session 19a; Spanish tax-free fund switches apply only if IBKR supports the regime (unverified; assumed not) |
| Long vanilla options | satellite convexity, when the options sleeve's activation condition holds (§5.6) | real-time quotes on the option and its underlying required |
| US-listed ETFs, index contracts, futures curves | data and option underlyings | never bought in cash under `es` |

**Finding the stocks.** Single-stock candidates per grid cell come from the published
holdings of the cell's representative ETFs (issuer files, free) and IBKR's market scanner.
They are resolved through contract search and pinned when a thesis needs them, so the
hunting ground covers every permitted venue without a hand-kept ticker list.

Global constraints the system models explicitly: trading permissions per venue in the
account; market-data cost and availability per venue for the subscribing username; FX conversion
cost; lot sizes and minimum tickets; per-venue transaction taxes (e.g. UK stamp duty,
French, Italian and Spanish financial-transaction taxes, Hong Kong stamp duty); dividend
withholding by issuer country and fund domicile; trading hours and phases versus our
cycles. All of these live in config with a source per value.

### 5.4 Core engine

- **Roles.** Global developed equity, emerging-market equity, euro government bonds
  (intermediate and long duration), inflation-linked government bonds, US Treasuries,
  gold, broad commodities, cash. Granularity is configurable (a compact set for small
  accounts; regional equity roles for larger ones). The vehicle selector fills each role; a
  foreign role may be filled by a currency-hedged share class, if the owner rules that a
  hedge inside a UCITS fund fits the mandate (§11).
- **Variants.** The engine builds the variants Session 4c registers and runs the one the
  owner picks from its frontier (`core.variant`): A static risk balance with wide bands; B A + the trend
  tilt; C a growth-tilted risk budget + the tilt; D an equity-dominant book with the tilt as
  an overlay to cash, sized to each drawdown limit of the pre-registered grid. The two
  references of §1 are on the frontier too, so either can be the owner's pick (DECISIONS
  D60, D79).
- **Adaptation** (DECISIONS D89). The allocation adapts within the owner's limits: the
  neutral allocation for the owner's risk level, plus each adaptation a pre-registered test
  found sufficient, weighted by its evidence; one not yet sufficient runs in shadow. The
  static variants are the fallback and the benchmark. Mechanisms are tested one at a time —
  total-risk scaling on a re-estimated volatility (Session 4e), re-estimated risk-balance
  weights, return timing (trend, value, carry, macro) — each with its parameters in
  configuration. An adaptation is sufficient on the objective (§1) against static A at the
  owner's point, never on a forecast's accuracy (DECISIONS D92).
- **Risk balance** (A–C). 25% of risk per environment in A and B; equal risk contribution
  inside a basket. No leverage, so risk comes from duration, not borrowing.
- **Covariance.** The variants 4c registered hold static risk-balance weights computed from a
  declared long-run covariance in the base currency (DECISIONS D80, D90). Session 4e scales
  total risk with an estimate (EWMA blended with a multi-year estimate, Ledoit–Wolf shrinkage
  toward the declared matrix), fully specified in its pre-registration; re-estimated
  risk-balance weights are a later test (DECISIONS D92).
- **Optimizer.** The legacy `_risk_parity` compared absolute risk contributions to 1/n
  without dividing by portfolio volatility. It is fixed (risk-contribution shares) and
  solved with a convex formulation, verified against closed forms (two assets; diagonal
  covariance gives inverse volatility) and an independent library in tests.
- **Trend tilt.** Per asset, 12-month time-series momentum and a 10-month moving average
  with a hysteresis buffer, evaluated monthly; a negative trend scales the asset down
  (never to zero by default) and the freed weight goes to cash or short duration.
- **Tax-aware execution.** Realised losses are taken freely (they offset gains); a trend
  reduction that would realise a large gain is funded first from cash flows and other
  lines, and executed only when its pre-registered benefit exceeds the tax cost.
  Re-entry after a loss sale uses a swap partner (a vehicle on a different, highly
  correlated index from another issuer) so the two-month rule does not defer the loss.
  Spanish rules are in §5.16.
- **Rebalancing.** Monthly evaluation, daily drift check, wide inner/outer bands, minimum
  trade, integer shares, cash flows first, sells beyond the outer band only.
- **Exits.** Core positions carry no stop orders: their downside control is risk balance
  and the trend tilt. Resting stop-limit exits protect satellite share positions (§5.12).
- **Validation.** Session 4c's pre-registered walk-forward test, **after tax** under the
  active profile and scored by the objective (§1): the two references and variants A–D, tax
  paid as it arises and the rest at liquidation; base-currency index proxies with documented
  bias; the decision rule of §5.9. It reports a frontier: for each drawdown limit on a
  pre-registered grid, the variant with the highest median after-tax log growth that
  respects it; variants the test cannot tell apart resolve to the simplest, lowest-turnover
  one. The owner's drawdown tolerance picks the point that goes to config (an
  authorisation, D60).

### 5.5 Satellite engine — the Asymmetry Hunter

1. **Components**, typed and point-in-time: crowding (COT percentile and velocity); vol
   mispricing (IV percentile, IV versus a realised-vol forecast); IV term-structure
   inversion; skew extremes; regime fragility (credit-spread level and velocity, the
   excess bond premium, curve, vol term structure, and the low-weight macro patterns of
   §6.4); cross-asset divergence (correlation shifts, trajectories); trend inflection;
   prediction-market divergence; GDELT pulse; insider-buying clusters (SEC Form 4);
   central-bank surprise (policy path versus the curve); catalyst window; promoted
   patterns (§5.9); LLM nomination (one vote at most, never a hard input).
2. **Measured independence.** Components whose signals have historically co-moved count
   as one vote. A candidate needs at least `k` effective agreeing votes, one of them a
   hard market input (price, positioning or volatility). Most runs produce no candidate.
   The agreement rule is a registered combination: it is tested beside its components, and
   a component or the rule earns weight only through the research pipeline (§5.9). Until
   then components run in shadow and their outputs are graded.
3. **Exposures, not tickers.** A candidate targets a grid cell, a macro-board entity or an
   instrument; the vehicle selector and the expression selector pick what to buy.
4. **Entry.** Shares: a probe position, scaled up only on trend confirmation, cut on any
   invalidation or at the time stop. Options: one block when implied vol is cheap, no
   additions; exits scaled (§5.6). A catalyst window or a confirmation rule is mandatory,
   because being early is being wrong.
5. **Thesis lifecycle.** Candidate → approved (deterministic gate with reason codes) →
   active → closed. Thesis text and triggers are immutable (SQL trigger); changes append to
   a re-underwrite log. The dossier pre-registers expected payoff, horizon and
   invalidations; the scoreboard grades exactly that.
6. **Triggers** (a typed DSL, evaluated every heartbeat, logged): `price_cross`,
   `series_threshold`, `grid_cell`, `positioning`, `event_prob`, `date_reached`,
   `drawdown_from_entry`, `trend_state`.
7. **Learning.** Component weights update by empirical-Bayes shrinkage only after a
   pre-registered minimum sample, counted in effective observations (§5.9); every weight
   set is versioned by its hash.
8. **Company selection** inside a sector or country thesis, and the selection hypotheses
   the research pipeline found sufficient (none yet: Session 4d's country momentum and its
   stop were insufficient, DECISIONS D87; insider-purchase clusters and the stock-level
   composite wait for survivorship-free prices, D84): liquidity, 12-1
   momentum with a momentum-crash guard, quality and valuation where data exists, promoted
   patterns, honest degradation.

### 5.6 Options sleeve

Lite rules: shared premium pool, penalty balance booked at detection and repaid by
realised gains on qualifying exits, concurrency cap, minimum premium, strike-smile N(d₂)
zombie test over two sessions, catalyst-driven tenor window, mandatory close computed by
calendar for options on ETPs the jurisdiction forbids holding. The sleeve trades only when
its activation condition holds (§2): H8 — an option against a share with a stop, for a
signal the research pipeline found sufficient — is sufficient, and the budget buys the
pre-registered number of median premiums within the friction gate. Until then it runs in
shadow, and the chain collector stores option surfaces forward for H8 (DECISIONS D66, D71).
Additions:

- **Integer contracts.** At small NAV the budget buys one or two contracts; concurrency
  follows from the budget rather than a fixed rule.
- **Friction gate.** Round-trip commission plus any FX conversion cost must stay below a
  configured share of the premium; a position allows a limited number of adjustments.
- **One-block entry, scaled exit.** When the premium reaches a configured multiple, a
  configured fraction is sold to recover the premium and refill the pool; the rest runs
  under the target, zombie and mandatory-close rules.

### 5.7 Measurement store and views

One long-format table — `(entity, indicator, as_of, value, status, reason, n_obs, window,
units, source)` — where an entity is a market × sector cell, a macro-board entity or an
instrument. Views are queries:

| View | Axes | Answers |
|---|---|---|
| Equity grid | market × sector × 15 indicator families (lite semantics) | where equity moves and dislocations are |
| Macro board | region × asset class (equity index, 2y, 10y, curve, real rate, FX vs base, credit, vol index, commodities) | where cross-asset stress and policy breaks are |
| Option surface | per underlying: delta × tenor × IV, percentile, skew, term slope, liquidity, theta | how to express a view with defined risk |

Carried from lite: explicit `ok | n/a | stale | inapplicable` statuses with reasons,
declared unmapped cells, per-input freshness, `as_of`-bounded reads, trajectories.

### 5.8 Vehicle selector

Given an exposure, rank the eligible vehicles by: eligibility; all-in cost (fund fee,
spread, commission, transaction tax, FX conversion, market-data cost for the venue);
liquidity; tracking quality against the exposure's data series; tax treatment under the
active profile (accumulating vs distributing, fund domicile and withholding); currency;
trading-window overlap with our cycles. It also maintains swap-partner groups. Output:
ranked vehicles with reasons. The core uses it for roles; the satellite for theses.

### 5.9 Research pipeline

Research is a standing capability and comes before the machinery it would justify. Every
hypothesis — a core variant, a selection signal, a Hunter component or combination, an
expression, a pattern — lives in `research/pattern_registry.yaml` with a lifecycle:
`idea → pre-registered → tested → shadow → active → retired`. It is tested by one harness,
`src/obliskiq/research/` (Session 4b), under every rule of `src/`; exploratory scripts in
`research/explore/` are never evidence (DECISIONS D64).

- **Entry.** A hypothesis needs an economic mechanism and a citation or a stated
  rationale. Ideas may come from the owner, the LLM scout or the literature.
- **Pre-registration** (its own PR, locked by SHA-256 and merge commit, D63): mechanism,
  universe, data and their point-in-time status, the primary metric net of costs and tax,
  the horizon, θ (the smallest net effect worth the added risk; the owner can amend it
  before the merge), the
  power to detect θ and the effect detectable at power 0.8, the adjustment, robustness
  checks and the trial budget.
- **Test.** Exploration on a development sample, every trial recorded in the trial
  registry; one confirmation run on a locked sample (a later period and, where data allow,
  another region); Bonferroni across the confirmation run and the deflated Sharpe across
  every trial. Dependence is explicit: cross-sectional effects as a monthly Fama–MacBeth
  series with Newey–West lags at least the horizon, events clustered by date, block
  bootstrap with blocks at least twice the longest lookback a strategy reads and a
  sensitivity over block lengths (D92); minimum samples in effective observations,
  n_eff = n / (1 + (n − 1)ρ) per non-overlapping period. Robustness: the sign in every pre-registered subperiod, a placebo
  that must fail, and a break-even cost multiplier above the declared cost uncertainty.
- **Combinations.** A few combinations with a stated mechanism are pre-registered and tested
  beside their components on the same sample, every one counted as a trial; none is found by
  search. The Hunter's k-of-n agreement is one of them.
- **Outcome** (D62). Data that fail the adequacy checks are **not evaluable**. Otherwise,
  with the interval [L, U] of the primary effect, exactly one of: **sufficient** (L ≥ θ),
  **insufficient** (U < θ; labelled negative when U < 0), **inconclusive** (L < θ ≤ U;
  labelled positive when L > 0). Power is reported beside the outcome; an inconclusive
  result under low power is never read as evidence against.
- **Shadow.** A sufficient or inconclusive-positive hypothesis runs live at zero weight;
  its outputs are logged and graded, and paper trading is therefore also a laboratory.
  Evidence from an LLM-derived signal is forward only (§5.10).
- **Active.** Weight is earned by a sufficient outcome and an activation condition (§2),
  and kept under the monitoring rule (§5.19); a layer whose twin breaches its boundary goes
  back to shadow, and only a new pre-registered test retires it.

Hypotheses by batch (DECISIONS D71):

| Batch | Hypotheses |
|---|---|
| 1 (Sessions 4c, 4d) | the core verdict (§5.4); one selection hypothesis chosen by a data audit among H1 cross-asset and country momentum through UCITS (Asness, Moskowitz & Pedersen 2013), H2 insider-purchase clusters in SEC Form 4 (Cohen, Malloy & Pomorski 2012) and H4 a stock-level composite — nearness to the 52-week high (George & Hwang 2004), 12-1 momentum with a crash guard (Jegadeesh & Titman 1993; Daniel & Moskowitz 2016), profitability (Novy-Marx 2013); H5, whether a volatility-scaled stop adds value to that signal (Kaminski & Lo 2014) |
| 2 (Session 21) | H3 forced selling: spin-offs and index deletions (Cusatis, Miles & Woolridge 1993); H6 conditioning the core's risk on the variance risk premium (Bollerslev, Tauchen & Zhou 2009), the excess bond premium (Gilchrist & Zakrajšek 2012) and trend; H9 hedging pressure in COT for commodity ETCs (de Roon, Nijman & Veld 2000); the Hunter's components and their agreement rule; the batch-1 candidates not chosen; the §6.4 re-test group |
| Forward only | H7 LLM-extracted event features beyond the price signal (a minimal extractor from Session 10); H8 an option against a share with a stop for a sufficient signal, on option surfaces collected from Session 8 or bought |
| Ideas | carry in bonds and equities (Koijen et al. 2018); defensive/low beta (Frazzini & Pedersen 2014); low volatility (Ang et al. 2006); VIX term-structure inversion; dollar strength and EM stress; oil shocks (Hamilton 2009); pre-FOMC drift (Lucca & Moench 2015) |

### 5.10 LLM layer

**Modular by configuration.** Providers and models are data in `config/llm.yaml`: base
URL, key name, model id, context window, capability flags (structured output, JSON mode,
tool use), price per million input and output tokens. One OpenAI-compatible transport
covers most providers (DeepSeek, Qwen, Kimi, GLM, MiniMax, OpenRouter, Groq); Gemini and
Anthropic have native adapters. Adding a new model is a YAML entry; adding a provider with
a new protocol is one adapter. Each role has an ordered provider list. A fixed evaluation
set per role (`obliskiq llm eval <provider/model>`) scores validity, accuracy, cost and
latency, so a new model is compared before it replaces an old one.

| Role | Output | Cadence |
|---|---|---|
| EventExtractor | `ExtractedEvent` (entities, region, sector, type, tone, novelty, sources) | each collector run |
| HypothesisScout | `HypothesisDraft`, also feeding the pattern registry as ideas | weekly and on event spikes |
| ExposureMapper | `ExposureAssessment` (purity, channel, confidence) | on candidate |
| DossierWriter | `Dossier` (readable, cites data) | on approval |
| DevilsAdvocate | `ChallengeReport` | weekly |
| CommandParser | owner message in any language → `OwnerCommand` (deposit, withdrawal, setting change, answer to a question), always echoed for confirmation; a deterministic grammar works without the LLM | on message |

Temperature 0; Pydantic-validated structured output; one retry, then fail closed; prompts
are t-string templates with escaped untrusted data; every call logged (provider, model,
prompt hash, tokens, cost, latency); a monthly budget; when the paid budget is spent →
free providers → no LLM. Trading never depends on the LLM. Prompts carry no account
identifiers or balances. `llm` cannot import `decision` or `execution`.

**Evidence is forward only** (DECISIONS D70). A model trained after a news item can carry
what followed it, so a backtest of an LLM-derived signal on history before the model's
training cutoff proves nothing. Only outputs produced live and stamped when produced count, so
a minimal EventExtractor starts in Session 10 with the first news collectors, under H7's
pre-registration (do its features add to the price signal), and Session 16 builds the rest of
the layer on its stored series. A role runs in shadow within its cost share of NAV and influences a
decision only when its activation condition holds (§2).

### 5.11 Decision layer

- **Expression selector.** A long option when the options sleeve is active (§5.6), IV
  percentile is at or below threshold, the chain is liquid, the tenor window is satisfiable, the payoff at the named target meets the
  minimum, real-time quotes exist for the option and its underlying, and the sleeve can
  afford it — choosing the candidate with the most convexity per unit of premium and the
  least correlation to the core. Otherwise shares or an eligible fund through the vehicle
  selector, with a volatility-scaled stop and trailing stop. Bearish: long put or core
  underweight.
- **Sizing, two regimes per layer.**
  - *Uncalibrated* (the start, and any layer without enough graded outcomes): a fixed risk
    budget — shares sized so the **gap-adjusted loss** (the stop distance plus a
    pre-registered quantile of the instrument's overnight gaps, wider across earnings
    dates) is a configured share of NAV, because a stop-limit does not fill through a gap
    (DECISIONS D67); within the position caps and above the absolute position floor
    derived from commissions (D66); options sized at a configured share of the sleeve, at
    least one contract, within affordability.
  - *Calibrated* (a layer with the pre-registered minimum of graded outcomes, counted in
    effective observations (§5.9), and Brier skill above zero): `f ≤ k × Kelly(p_eff, payoff)` (k = 0.25 by default) with
    `p_eff = p_base + shrink × (p_stated − p_base)`; `p_base` is the market-implied
    probability of reaching the target (options: N(d₂) at the target from the strike's
    smile IV) or the random-walk barrier probability (shares); `shrink` comes from
    calibration. Capped by sleeve budgets and risk limits.
- **Whole-book risk.** Historical and parametric expected shortfall (97.5% by default),
  historical stress scenarios (the crisis database), concentration (sector, country,
  single foreign currency), correlation clusters, liquidity (days to exit), FX in
  base-currency real terms — always including unmanaged and external holdings (positions
  the owner holds elsewhere, entered by hand and flagged stale after a configured age).
- **Breakers.** Rolling-month drawdown → freeze new buys and audit; hard peak-to-trough →
  force `shadow` and alert; the peak is D88's (the managed NAV at the day's last cycle). Their
  thresholds are keys of their own: the owner's drawdown
  tolerance (§11, DECISIONS D88) is a research and selection limit, neither a breaker threshold
  nor an order to sell.
- **Inertia.** Intents only for a band breach, an approved thesis, a scale-up confirmation,
  a fired trigger, stop, zombie or mandatory close, a cash-flow reservation or a risk breach;
  monthly turnover and realised-gain budgets.

### 5.12 Execution

- **Transport.** The IBKR Client Portal Web API, reached by one of two transports chosen in
  config (`ibkr.transport`), with the same endpoints and client above them:
  - `oauth`: OAuth 1.0a, live session token negotiated and refreshed before expiry
    (where IBKR grants OAuth to the account; the live account only, DECISIONS D42);
  - `gateway`: the Client Portal Gateway (IBKR's Java application) on the bot's machine,
    logged in automatically by a headless browser in the style of IBeam (Apache-2.0).
    Paper logins need no second factor, so this runs unattended in paper; it refuses
    profile live, whose login needs the owner's IBKR Mobile approval (DECISIONS D42).
  Both: `/iserver/auth/ssodh/init`, `/tickle` keepalive, re-arm on 401, no retry on 403,
  `/iserver/accounts` before order calls, automatic re-authentication after daily resets.
  The paper account is a different username from the owner's live login, so the owner's
  own sessions never take over the bot's; a competing session is detected and alerted.
  IBKR does not support automating the Gateway's login, and it did not complete from a cloud
  container, so the owner proves it on the deployment machine first (T1, DECISIONS D68):
  an unattended login on two different days, the read-only preflight and the paper order
  test. If it cannot complete there, the owner chooses between the TWS API with IBC and a
  daily manual login. Every failed re-login alerts within one heartbeat.
- **Calendars and phases.** `exchange_calendars` sessions, holidays and half-days, with
  market-pulse-bot's phase model (pre-market, auctions, lunch breaks, post-market,
  seasonal closes, synthetic calendars) and incident store (manual entries for
  market-wide halts and exceptional closures; structured feeds for single-stock halts,
  which block that instrument only), decide when a venue is tradable. Orders go only in
  the regular phase, inside `[open + offset, close − offset]` local, outside auctions,
  lunch breaks and incidents. IBKR's trading schedule for the contract is a cross-check:
  a disagreement alerts and is logged, and blocks only date-critical orders (below).
  Never UTC constants.
- **Date-critical instruments.** For an option, the last tradable session is the last
  session on or before expiry on which both the option's venue and its underlying's
  venue are open (an expiry on a holiday, or a Monday expiry after a Friday holiday,
  moves it earlier). Every exit rule — mandatory close, time window, zombie, the last
  chance to sell before expiry — is scheduled against that session minus a configured
  buffer of sessions; inside that buffer an unresolved calendar disagreement takes the
  earlier date.
- **Intent → order.** `OrderIntent` (id = `uuid.uuid7()`) state machine → whatif → place
  with `cOID` → reply policy → status → reconcile, after a configured grace period for
  IBKR's reporting lag before a missing order or position counts as drift. Restart-safe: the
  `cOID` is looked up before any placement. The cost model is given the listing venue,
  never `SMART`.
- **Paper guard.** In `paper` mode every broker write is refused unless the account id has
  a paper prefix from config (IBKR paper accounts start with `DU`).
- **Currency manager.** The base currency is selectable; working balances in other
  currencies are allowed within lite's single-foreign-currency cap. A trade is funded in
  its own currency by a limit-capped spot conversion shortly before the trade, batched to
  amortise the minimum commission; no balance may go negative; foreign cash above a buffer
  is repatriated.
- **Pricing.** An IBKR algo or peg with a limit cap where the venue and asset class support
  it through the Web API (listed per contract by `/iserver/contract/{conid}/algos`;
  verified live per venue); otherwise a patient limit at the reference mid ± ticks,
  repriced toward the touch a few times, then cancelled and retried at the next window.
  The collar widens with `σ × √(quote delay)`.
- **Reply policy.** Only whitelisted message ids are confirmed. `o354` (no market data) is
  confirmed only when the asset class's quote policy accepts delayed quotes, a fresh
  delayed quote exists and the order is limit-capped inside the collar; otherwise cancel
  and alert. Unknown messages → cancel and alert.
- **Protective exits** rest at the broker as stop-limit orders and are re-placed after
  fills, trailing roll-ups and partial exits, so a dead machine never leaves a position
  unprotected. Where IBKR's Web API supports attached orders (a child stop linked by
  `parentId`, probed in Session 5), the entry and its stop are cleared and placed as one
  package, closing the window between a fill and its stop. A stop-limit becomes a limit
  when it triggers and does not fill through a gap: its limit offset follows the
  instrument's volatility and gap distribution, and a triggered stop that has not filled
  is repriced toward the bid in steps, each through the gate. A limit deep enough to act
  as a market order is the owner's decision, off by default (DECISIONS D67).
- **Kill switch.** Cancels working entry orders and blocks new risk; keeps protective exits.
  `flatten` is a separate, explicit command.
- **Modes.** `shadow` (simulated fills with the cost model), `paper` (IBKR paper account),
  `live-small` (capital cap), `live`. Downgrades are automatic; upgrades need the promotion
  gate and an owner command.

### 5.13 Market data

Quote policy per asset class, in config:

| Asset class | Default policy |
|---|---|
| Options | `realtime_required` on the option and its underlying: otherwise no entry |
| Stocks, ETFs, ETCs, bonds | `realtime_preferred`: delayed accepted up to a maximum age, with the widened collar and an IBKR-side algo where available |
| Mutual funds | NAV-priced; no quote policy |

Subscriptions stay on the owner's main username, for his own use as well; its paper
account receives them through IBKR's market-data sharing setting, so nothing is billed
twice. IBKR may serve the paper session delayed data while the owner is logged in live
(verify in Session 3); the quote policy then refuses option entries rather than trade on
stale prices. Non-professional prices listed by IBKR Ireland
(2026-10-07): OPRA US options L1 USD 1.50/month (waived at USD 20 of monthly commissions);
Eurex Core L1 waived; US stocks and ETFs streaming from Cboe One and IEX complimentary
(availability through the API verified in Session 3); LSE L1 GBP 1.00; Bolsa de Madrid
Plus L1 EUR 7.00; Xetra L1 EUR 16.25; Euronext Cash L1 EUR 89.10.

**Initial plan** (`config/owner.yaml: market_data.plan`): OPRA L1, Eurex Core L1 and the
complimentary US streaming feed; everything else delayed. With it, the options sleeve
starts on US-listed options (option and underlying both real-time); Eurex single-stock
options become available when the underlying's venue is subscribed. US stock
subscriptions beyond the free feed are reconsidered once the scoreboard shows the bot
earns them. The vehicle selector prefers venues the plan covers.

### 5.14 Scheduling

One asyncio service (`obliskiq serve`): a heartbeat every few minutes (health, kill switch,
broker session, trigger evaluation, intents whose venue is open); four decision cycles per
trading day anchored to venue sessions (Asia, Europe, US, US-late), computed from
calendars; collectors per venue after the close and per source cadence; monthly core
evaluation; weekly grading, DevilsAdvocate and digest. Crash-safe through run records;
idempotent intents.

### 5.15 Capital and cash flows

- **Managed capital.** `capital.mode: whole_account | cap`. In paper mode the cap makes a
  large paper balance behave like the real account size, so minimum trades, integer
  shares and commissions are simulated honestly.
- **Owner commands** (CLI, Telegram, MCP; natural language through CommandParser, always
  confirmed): *deposit* ("I added 1,237.57 € to your cash") raises managed capital and
  deploys it toward targets, optionally in tranches; *withdrawal with a date* ("in 15
  days I need 1,000 €") creates a reservation.
- **Reservation planner — acts as soon as the need is known.** Investable capital drops at
  once and targets are recomputed on the reduced capital. The plan is built immediately:
  cheap sources execute at the next windows (the cash line, overweights, exits the
  strategy would make anyway); the rest is scheduled before `due date − settlement lag −
  buffer`, choosing sells that minimise tax cost + trading cost + **strategy cost**.
  Strategy cost is the expected return forgone and the tracking error to the new targets;
  it is highest for positions in their entry window, probe positions awaiting
  confirmation, theses with a pending catalyst and positions with strong trend, and lowest
  for the cash line, overweights, invalidated or decaying theses and weak trends. Losses
  are preferred over gains; the two-month rule is respected.
- **Pacing the sales.** Each sale in the plan is paced by its own expected return: a
  position the plan sells anyway, or one with weak expected return, is sold at once or in
  tranches, and its proceeds are set aside for the withdrawal; a position with strong
  expected return is held to the last safe days (`due date − settlement lag − buffer`),
  and the owner may be offered a later date instead. The last safe day is never crossed
  without an owner answer. An expected return enters only from a layer whose evidence is
  sufficient (§5.9); otherwise the ranking of strategy cost above is the rule.
- **Asking before cutting.** If meeting the date would mean cutting protected positions,
  the bot asks the owner — postpone to a proposed date, reduce the amount, or proceed —
  with the tax and strategy cost of each option; it also proposes tax-motivated timing
  where the date allows (for example, selling after year-end defers the tax by a year).
  Without an answer by the decision deadline, the withdrawal wins and the plan executes.
- **Unexplained cash changes** at the broker freeze new buys and alert until classified.
- **Performance** is time-weighted, so flows never look like profit or loss.

### 5.16 Jurisdiction and tax profiles

`config/owner.yaml: jurisdiction` selects `config/jurisdictions/<id>.yaml` (data) and the
policies registered in the `jurisdiction` package (code); the `tax` package holds the lot
engine and tax ledger that call them.

- **EligibilityPolicy** — what may be bought and held, and on what conditions.
- **TaxPolicy** — lot method, wash-sale rule, loss offset and carry-forward, rate table for
  after-tax estimates, dividend withholding, fund-switch rules, swap-partner rule, annual
  report export.
- **`es` profile:** FIFO is mandatory for homogeneous securities (LIRPF art. 37.2);
  homogeneous means from the same issuer with substantially the same rights (RIRPF
  art. 8). Two-month rule (LIRPF art. 33.5.f): a loss on listed securities is deferred
  when homogeneous securities were bought within the two months before or after the sale,
  and released when those securities are sold. Losses offset gains and carry forward four
  years. ETF switches are taxable; mutual-fund switches without tax are assumed
  unavailable at IBKR until verified. Swap partners must differ in issuer and index by
  default. Savings-base rate table (currently 19%–30%). Gains and losses on
  foreign-currency balances are kept as currency lots, taxed under the rule an official
  source confirms (Session 7, DECISIONS D69). The lot engine and the `es` policy are built
  in Session 4b, so the research harness and the live path use the same code.
- **Keeping rules current.** A monthly scheduled Claude task checks the official sources
  for each active profile (for `es`: BOE, AEAT) and, if a rule or rate changed, opens a
  PR updating the profile data with citations and tests, under the normal review protocol.
  The rate table also records the tax year it applies to, and the bot alerts when it is
  used in a year it does not cover.
- A new country is a new YAML, a new policy module if rules differ, and tests. Example:
  in Switzerland private capital gains are generally untaxed unless the investor is deemed
  a professional trader, and the tax authority's safe-harbour criteria limit turnover and
  derivatives use — a Swiss profile would cap turnover and could disable the option sleeve
  (verify locally).

### 5.17 Owner settings

- `config/owner.yaml` is the one file the owner edits: plain-language comments, schema
  validation, applied at the next cycle boundary, every change audited. Expert defaults
  live in `config/defaults/*.yaml`.
- Change paths: edit the file; `obliskiq settings set <key> <value>`; Telegram `/set` and
  MCP `update_setting` for whitelisted keys. Remote changes may only tighten risk or
  manage budgets and cash flows within bounds; loosening a risk limit requires the CLI on
  the machine.
- Viewing: Telegram `/status`, the MCP cockpit, and a daily self-contained HTML dashboard.

### 5.18 Data sources and credentials

| Source | Use | Credential | Status |
|---|---|---|---|
| IBKR Client Portal Web API | bars, snapshots, chains, curves, contract rules, trading schedules, market scanner, account, positions, NAV, orders | the paper username and password for the Gateway's automated login (paper); OAuth keys for the live account where granted (DECISIONS D42) | read path verified live over OAuth (lite); Gateway route and order path new |
| FRED / ALFRED (+ API v2 bulk) | US and global rates, curves, breakevens, credit, CPI, Eurostat HICP redistribution | `FRED_API_KEY` | verified |
| ECB Data Portal | €STR, deposit rate, FX reference rates, yield curves | none | verified |
| Bank of Japan Time-Series API | JGB yields, FX, monetary base | none | verify |
| BIS Data Portal | policy rates, credit-to-GDP gaps, effective FX | none | verify |
| Eurostat, Bank of England, SNB, Bundesbank | euro-area HICP flash and activity, UK and Swiss rates, German yields | none | verify |
| Bank of Canada Valet, ABS, RBA, Banco Central do Brasil (SGS), Banxico, Norges Bank, MAS, CBRT EVDS | macro-board regions beyond the US, euro area and Japan (India through BIS and DBnomics) | none (Banxico, EVDS: free token, optional) | verify |
| DBnomics | fallback aggregator for series without a direct fetcher (live use only: no vintages) | none | verify |
| ONS, Statistics Canada | UK and Canada CPI with release times | none | verified |
| EIA v2 | energy inventories | `EIA_API_KEY` | verified |
| Federal Reserve Board | excess bond premium | none | verify |
| CFTC COT | positioning percentiles and velocity | none | verified |
| SEC EDGAR | Form 4 insider filings, company facts | `SEC_USER_AGENT` (a contact string, not a secret) | verify |
| Polymarket Gamma, Kalshi public | event probabilities and velocity | none | verify |
| GDELT DOC 2.0, curated RSS, exchange halt feeds | events, news, single-stock halts | none | verify |
| ETF issuer holdings files (e.g. iShares) | single-stock candidates per grid cell | none | verify |
| yfinance, Ken French and AQR data libraries, Cboe | research history only, never decisive live | none | research |
| LLM providers | the six roles | API keys | Session 16 |
| Telegram Bot API | cockpit and alerts | bot token, owner chat id | Session 6 |
| Heartbeat monitor, off-box backup storage | operations | ping URL, storage keys | Session 19 |

Not used: X/Twitter, Reddit, Google Trends, Alpha Vantage, OECD (until something consumes
it), e-Stat CPI (no per-observation release date), China's NBS and its scraping wrappers
(AkShare, TuShare: no stable API, no release dates), Russia, Saudi Arabia, the UAE and
Taiwan's official portals (not tradable from this account or not in the grid, spreadsheet
downloads without release dates; oil and geopolitics come through EIA, COT and GDELT), INE and Banco de España (Spain is
covered through Eurostat and the ECB), marketdata.app (one device per account
makes it unusable from cloud sessions; optional on the deployment machine only). Every row
stores its source, reference date, release date or explicit policy, and units. A source
without a defensible release date may feed live views but never a backtest. SEC Form 4
arrives up to two business days after the trade; it is timestamped on the filing date and
graded as such. Sources are
declared in `config/sources.yaml`, so adding or removing one is a config change plus a
fetcher. A declared source runs only when a registered hypothesis, a component or a safety
rule consumes it (§2).

### 5.19 Validation, capital allocation and promotion

**Operational promotion ladder:**

| Level | Entry criteria (operational, not alpha) |
|---|---|
| L0 shadow | Default after deployment. |
| L1 paper | 10 consecutive trading days of shadow with no unhandled error, clean reconciliation, no stuck intent, every daily reset re-logged in unattended. Fully autonomous from here. |
| L2 live-small | ≥ 8 weeks of paper: no rail violation, order success above threshold, slippage within the cost model's band, breaker, kill and cash-flow drills passed. IBKR simulates paper fills, so real transaction costs are first measured at this level, with capital at the live-small cap. |
| L3 live | ≥ 3 months of live-small, an owner command and a scoreboard report. |

**Capital follows evidence** (DECISIONS D65). A live record confirms a moderate edge only
slowly (t ≈ IR·√years: about 16 years at an information ratio of 0.5 for t = 2), so a layer's
capital follows its research outcome (§5.9) and its live results are monitored, not used as
proof. A layer earns an exploration budget only with a sufficient outcome and its activation
condition (§2), within the owner's ceiling; the budget is the owner's tolerance for a bet not
yet confirmed live, recorded as a risk decision. An increase needs the pre-registered pooled
re-test (history plus the live twin) to remain sufficient, and an owner command. The core
absorbs whatever the satellite does not hold; budgets keep their defaults (§11 row 2) until
evidence moves them.

**Monitoring.** Each active layer has a twin: the same signals on the same dates simulated
with the cost model. The live-minus-twin gap measures implementation and cost; the twin
against the backtest's predictive distribution for the elapsed time measures decay or chance.
Both are reviewed monthly from activation against one-sided boundaries whose false-alarm
probability over the planned looks is held at the pre-registered level by an alpha-spending
function. A gap beyond its boundary freezes the layer's new entries and opens an operational
investigation; a twin below its boundary returns the layer to shadow at zero weight and opens
a research review; only a new pre-registered test retires a layer. Monitoring cannot tell
decay from bad luck in a short sample, so it reduces risk and never declares a verdict. The
scoreboard still reports every layer, component and pattern — TWR excess against the
references, Newey–West intervals, Brier skill with minimum samples in effective
observations.

### 5.20 Deployment and releases

- **Machine.** Provider-agnostic: a Docker image with systemd on any small always-on Linux
  machine (ARM64 or x86_64, ≥ 1 GB RAM) — Oracle Always Free, Google e2-micro, a low-cost
  VPS or a home machine. Nightly SQLite online backup with an integrity check, pushed
  off-box; a heartbeat monitor; secrets in files readable only by the service user. The
  bot uses the paper account of the owner's main username until promotion, through the
  transport the account allows; once it runs continuously, cloud sessions stop calling
  IBKR with that paper login unless the bot is paused. The machine also runs the Gateway
  when the transport is `gateway`. The owner brings it up first, before Session 6, to prove
  the unattended login and the order shapes (T1, SESSION_PROMPTS §A.5).
- **Releases.** Semantic versions in `pyproject.toml`. Every merge to `main` that changes
  the version publishes a GitHub Release automatically (a workflow creates the tag and the
  notes from `CHANGELOG.md`); versions below 1.0.0 are pre-releases; 1.0.0 closes the build
  phase. A release also builds the Docker image. The machine updates to a release tag with
  a health check and automatic rollback.
- **After the build.** Improvements and new features run as feature sessions from the
  template in `docs/SESSION_PROMPTS.md`, each ending in a release.

---

## 6. Reuse map

Session 1 copies everything below into `legacy/` with `legacy/MANIFEST.md` (repository,
commit, path, licence, target, action). Later sessions port from `legacy/`, never from
memory. Session 20 deletes `legacy/`.

### 6.1 From ObliskIQ-lite-MCP @ b1ebd49 (the base)

| Legacy path | Action | Target |
|---|---|---|
| `src/obliskiq/ibkr/{oauth,session,cp_client,preflight}.py` | port with tests | `ibkr/` (orders added) |
| `src/obliskiq/quant/bsm.py`, `compute/{iv_solve,smile,stats,calendar}.py` | port | `quant/`, `calendars/` |
| `src/obliskiq/macro/{fetchers,series,rates}.py` | port | `data/macro/` |
| `src/obliskiq/collect/*` (universe, backfill, chain, curve, macro_pull, retry, runlog, status, sequence, strikes, vol, nav, source, state) | port | `data/market/`, `orchestration/` |
| `src/obliskiq/collect/{option_vendor,mcp_capture}.py`, `ibkr/connector.py` | reference | optional |
| `src/obliskiq/compute/{indicators,build_grid,rankings,universe_census,frames}.py` | port into the measurement store and views | `grid/` |
| `src/obliskiq/compute/{gates,screen,risk_engine,penalty,fx_engine,commodity_engine,catalysts}.py` | port | `decision/`, `engines/` |
| `src/obliskiq/store/*` and migrations | port into a fresh chain | `store/` |
| `src/obliskiq/server/*` | port and extend | `server/` |
| `config.yaml`, `universe.yaml`, `calendars.yaml` | restructure into defaults, owner, universe, jurisdictions (§2) | `config/` |
| `tests/*`, including the guard tests (`test_no_unread_config_keys`, `test_no_magic_numbers`, `test_no_committed_secrets`, `test_point_in_time`, `test_hostile_input`, `test_migration_chain`) | port with their modules | `tests/` |
| `references/*-playbook.md`, `commodities-universe.md`, `ROADMAP.md` (measurements) | knowledge | `docs/knowledge/` |
| `docs/{DECISIONS,COMPUTE,COLLECTORS,SERVER,DEPLOY,ORACLE-CLOUD,GOOGLE-CLOUD}.md` | rationale | `docs/DECISIONS.md` (condensed), `docs/knowledge/` |
| `.claude/skills/obliskiq-lite/SKILL.md` | basis of the cockpit skill | `.claude/skills/obliskiq-cockpit/` |
| `deploy/*`, `Dockerfile` | port | `deploy/` |

### 6.2 From Inversor-Inteligente @ 68280e1 (paths under `ObliskIQ/`)

| Legacy path | Action | Target |
|---|---|---|
| `modules/analytics/asymmetry_hunter.py` and tests | port the component maths and the agreement rule onto typed inputs; extend per §5.5 | `engines/satellite/` |
| `modules/analytics/stat_utils.py` and test | port as is | `quant/inference.py` |
| `modules/analytics/{ai_scoreboard,forecast_panel}.py` and tests | port the methodology | `scoreboard/` |
| `modules/analysis/{analyst_router,providers,llm_analyst}.py`, `modules/config/ai_providers.py` and tests | port; structured outputs; registry; budgets | `llm/` |
| `modules/market/{cftc_cot_analyzer,gdelt_client,polymarket_client}.py`, `scripts/backfill_cot_history.py`, `config/{gdelt_regions,polymarket_markets,event_calendar}.json` and tests | port | `data/positioning/`, `data/events/` |
| `modules/market/{rss_news_enhanced,vader_financial_lexicon}.py` | reference and fallback | `data/events/` |
| `modules/execution/{continuity_guard,paper_gate,monitoring_suite}.py` and tests | port the concepts | `engines/satellite/`, `execution/`, `scoreboard/` |
| `modules/execution/phase4_decision_engine.py` (`apply_quarter_kelly_clamp`) | port | `decision/sizing.py` |
| `modules/analytics/opportunistic_optimizer.py` and `tests/test_opportunistic_optimizer.py` | fix and port the risk-parity part; keep the valid tests; drop the date-based crisis switch (look-ahead in backtests, never fires live), regime-label leverage and Kelly-then-normalise | `quant/risk_budget.py` |
| `modules/analytics/pattern_library.py` | definitions only (logic and citation) into the pattern registry; the stated IC, n and hit-rate figures are discarded | `research/pattern_registry.yaml` |
| `modules/analytics/transaction_costs.py` and test | port | `quant/costs.py` |
| `modules/data/crisis_database.py`, `research/validation/{stress_tester,monte_carlo}.py` | port for stress scenarios only | `decision/portfolio_risk.py`, `research/` |
| `modules/testing/walk_forward_backtester.py` | reference (delta execution, costs, bias disclosure) | `src/obliskiq/research/` (Session 4b) |
| `modules/notifications/{telegram_notifier,run_summary}.py` | port | `reporting/` |
| `modules/notifications/email_notifier.py` | reference (Telegram is the channel) | — |
| `modules/config/{settings,strategy_universe}.py`, `modules/utils/risk_enforcer.py` | reference (constants and imports the ported modules and tests rely on) | — |
| `modules/config/global_vehicles.py` | reference (exchange suffixes, ADR map) | universe seeds |
| docs `ASYMMETRY_HUNTER, HUNTER_READINESS, AI_SCOREBOARD, PAPER_TRADING_GATE, STRATEGY_V1_1_VERDICT, STRATEGY_V2_DESIGN, GEO_INPUTS, COT_REAL, AI_PROVIDERS`; root `docs/{PROVENANCE_PURGE_REPORT,KNOWN_LIMITATIONS}.md` | knowledge | `docs/knowledge/legacy-findings.md` |
| `modules/macro/analyzer.py`, `regime_transition_detector.py` | not ported (biased classifier); indicator ideas feed the regime-fragility component | — |
| social sentiment, Google Trends, 13F, Alpha Vantage, the four-phase orchestrator, prompt composer, hybrid formatter | not ported | — |

### 6.3 From market-pulse-bot @ 900dbf4

| Path | Action | Target |
|---|---|---|
| `src/market_pulse_bot/{calendar_engine,market_engine,config}.py` and tests | port the schedule abstraction, phase model and exchange config | `calendars/` |
| `src/market_pulse_bot/halt_detector.py`, `data/manual_incidents.yaml.example` and tests | port the incident store | `calendars/incidents.py` |
| `config/exchanges.yaml` | port the 26 venues' phase windows, seasonal closes and synthetic calendars | `config/defaults/venues.yaml` |
| `text_formatter.py`, `notification_backend.py`, `i18n.py`, `app.py` | reference (badge language and message state for the digest) | `reporting/` |

### 6.4 Legacy pattern triage

| Group | Patterns | Treatment |
|---|---|---|
| Built into the design | `tech_003` (200-day trend → trend pillar); `credit_001`, `cross_001` (regime fragility); `sent_003`, `sent_007`, `alt_001` (put/call and skew from our own chains → vol components) | graded as part of their component |
| Re-test on our data | `alt_002` + `sent_004` (insider buying clusters, Form 4); `tech_004` respecified at stock level as nearness to the 52-week high (George–Hwang); `tech_001` (momentum-crash guard); `macro_009` (dollar strength → EM stress); `macro_010` (oil shock) | Session 21 batch |
| Grounded but too rare to validate (≈7–22 events each) | `macro_001` (curve inversion), `macro_003` (Sahm rule), `macro_004` (leading index), `macro_008` (ISM collapse), `macro_006` (inflation acceleration) | low-weight inputs to regime fragility only, never stand-alone triggers |
| Dropped | `sent_006`, `sent_010`, `alt_004`, `alt_003`, `sent_005`, `sent_001`, `sent_002`, `macro_002`, `macro_005`, `macro_007`, `sent_008`, `sent_009`, `tech_002`, `tech_005` | definitions recorded in the registry as `retired` |

---

## 7. Technology

- **Python 3.14**, standard build, `requires-python = ">=3.14,<3.15"`. Checked on
  2026-10-07: every library this plan uses or may use installs from 3.14 wheels (numpy,
  scipy, pandas, pydantic, httpx, cryptography, exchange-calendars, mcp, statsmodels,
  scikit-learn, polars, duckdb, cvxpy, riskfolio-lib, skfolio, arch, quantstats).
  Free-threading (3.14t) is not used; the code stays thread-safe so it can be switched on
  later by configuration if profiling ever shows CPU-bound parallel work.
- Concurrency: asyncio for I/O, numpy vectorisation for compute, no mutable module
  globals.
- 3.14 features used: t-strings for LLM prompts; `compression.zstd` for raw-payload
  archives; `uuid.uuid7()` for time-ordered ids; deferred annotations;
  `python -m asyncio ps` for debugging a stuck service.
- **Pydantic v2** only; domain models frozen with `extra="forbid"`; `pydantic-settings`
  for the environment.
- Core dependencies: `httpx`, `numpy`, `scipy`, `pandas`, `exchange_calendars`, `PyYAML`,
  `cryptography`, `mcp`. Test-only: `pytest`, `pytest-asyncio`, `respx`, `hypothesis`,
  `import-linter`, `skfolio` (independent cross-check of risk budgeting). Research extras:
  `yfinance`, `statsmodels`, `quantstats`. A further dependency is added only when it
  earns its place, with a `DECISIONS.md` entry. Dependabot proposes updates weekly.
- Considered and not used: NautilusTrader and ib_async (both need TWS or IB Gateway; the
  Client Portal Gateway was chosen for its REST surface and lite's verified port, and it
  needs an automated login too, so the TWS API with IBC remains the fallback if T1 fails,
  DECISIONS D68); `pandas_market_calendars` (wraps `exchange_calendars`, which
  we use directly). ibind (Apache-2.0) is read as a reference for order and reply
  handling, with attribution on anything copied.
- Tooling: `uv` (lockfile, uv-managed Python), `ruff`, `mypy --strict`, a coverage floor
  that only ratchets up, `make check`.
- Money: `Decimal` at the order boundary. Time: timezone-aware UTC; local time through
  `zoneinfo`.
- Licence: AGPL-3.0-or-later (already in the repository; all three source repositories
  are compatible).

---

## 8. Repository layout

```
AGENTS.md   CLAUDE.md (one line: @AGENTS.md)   LICENSE   CHANGELOG.md
pyproject.toml  uv.lock  Makefile  .python-version
config/   owner.yaml  defaults/*.yaml  universe.yaml  sources.yaml  llm.yaml
          jurisdictions/es.yaml
src/obliskiq/   config, domain, store, calendars, ibkr, data, quant, grid, engines,
                jurisdiction, tax, capital, llm, decision, execution, orchestration,
                research, scoreboard, reporting, server, cli
tests/    unit/ integration/ fixtures/ property/
research/ preregistrations/ reports/ explore/ pattern_registry.yaml
docs/     MASTER_PLAN.md DECISIONS.md ROADMAP.md SESSION_PROMPTS.md RUNBOOK.md DEPLOY.md
          knowledge/
deploy/   Dockerfile  obliskiq.service  backup.sh  update.sh  env.example
legacy/   temporary; MANIFEST.md; excluded from all tooling
.github/  workflows/{ci,release}.yml  dependabot.yml  pull_request_template.md
```

---

## 9. Engineering conventions (`AGENTS.md` is authoritative)

- **Nothing hardcoded.** Values in `config/`; jurisdiction and tax rules in profiles;
  venues, currencies, schedules, budgets, providers and thresholds all configurable; tests
  fail on orphan keys and on magic numbers in decision paths.
- **One owner per fact.** Config = values; code = mechanics; `DECISIONS.md` = why;
  knowledge files = observed external behaviour; this file = design.
- **Honest degradation.** Missing input → explicit status and zero weight; untested is
  never passed.
- **Point-in-time everywhere.** Reads bounded by `as_of`; macro aligned on release date.
- **Money paths fail closed.** Ambiguity cancels and alerts.
- **Live evidence over fixtures.** Fixtures are evidence about our parsing, not the
  vendor's format. With paper credentials, sessions verify live and tag findings with
  route and date; without them, checks are marked `VERIFY-LIVE(...)` and queued.

---

## 10. Session sequence

One session = one branch = one PR, merged by the agent (`AGENTS.md`), released
automatically; a pre-registration goes first in its own PR. Prompts, model and effort:
`docs/SESSION_PROMPTS.md`. A session that cannot finish with quality merges a coherent
subset and inserts an `Na` prompt for the remainder.

| # | Session | Outcome |
|---|---|---|
| 1 | Foundation | scaffold, docs, owner settings and defaults, jurisdiction registry, CI, releases, legacy snapshot |
| 2 | Store, calendars and numerics | store and migrations, calendar and phase layer, BSM, IV, smile, stats, inference |
| 3 | IBKR read path | OAuth and Gateway transports, session lifecycle, client, rate limits, quote status, contract identity, trading schedules, live preflight |
| 4 | Order path, kill switch and modes | intents, order client, reply and quote policy, clearance lock, state machine, paper guard, modes, kill switch |
| 4a | Pre-trade rails | pre-trade checks with reason codes, eligibility skeleton, approval queue, flatten, paper order test |
| T1 | Deployment machine (owner) | unattended login on two days, read-only preflight, paper order test; runs beside 4b–4d, before 6 |
| 4b | Research harness | point-in-time research data, simulator with costs and the `es` tax engine, statistics, decision rule, trial registry, European commissions |
| 4c | Core verdict | pre-registration, then one after-tax run of the core's variants against the references; a frontier from which the owner picks the variant Session 13 builds |
| 4d | First selection hypothesis | data audit, choice among H1, H2, H4, pre-registration, one confirmation run, H5 beside it |
| 4e | Total-risk scaling | the core's risky share scaled by a re-estimated volatility, against static A on the objective at the owner's point: estimators, a bootstrap that respects lookbacks, a long-history check, pre-registration, one confirmation run (DECISIONS D89, D90, D92) |
| 5 | Execution mechanics | currency manager, pricing, resting stops with gap handling and attached orders, executor, reconciliation |
| 6 | Walking skeleton | service, cycles, skeleton targets → intents → paper → reconcile → digest, on the T1 machine |
| 7 | Capital, cash flows and tax | managed capital, cash-flow ledger, reservation planner, owner questions, lots wired to fills, currency lots, TWR |
| 8 | Universe and market-data collectors | global universe with hedged classes and look-through, stock candidates, venue profiles, eligibility policy, collectors (option surfaces stored forward) |
| 9 | Broker capabilities and vehicle selector | capabilities measured, core candidates, swap partners, vehicle selector |
| 10 | Macro, positioning, insiders and events | the sources a registered hypothesis, component or safety rule consumes; incident feeds; catalyst calendar; a minimal EventExtractor (H7's clock) |
| 11 | Measurement store and views | measurement store, equity grid, macro board, rankings, correlations, IV solve, gap distributions |
| 12 | Engines and gates | option surface, options sleeve (behind its activation condition), fx, commodity, risk engine, gates |
| 13 | Core engine | the variants 4c registered and any 4e found sufficient, risk budgeting, the tax-aware trend tilt, rebalancing |
| 15 | Satellite engine | Hunter components in shadow, measured independence, entry rules, lifecycle, triggers, selection, pattern registry |
| 16 | LLM layer | provider and model registry, evaluation set, budget, structured outputs, six roles on Session 10's stamped series |
| 17 | Decision layer | expression selection, sizing by gap-adjusted loss, activation conditions, whole-book risk, breakers, inertia, intents |
| 18 | Scoreboard, patterns, capital and promotion | TWR attribution, TCA, grading, calibration, twins and monitoring, capital by evidence, promotion |
| 19 | Cockpit, settings and deployment | MCP cockpit, Telegram, settings flow, dashboard, Docker, machine, backups, updates |
| 19a | Capacity and scale | bonds, baskets, mutual funds, capacity limits, venues and currencies, tax at scale, every activation condition |
| 20 | Red-team and cleanup | failure-mode audit, mutation tests, `legacy/` deleted, docs truth pass, 1.0.0 |
| 21 | Research: second batch | H3, H6, H9, the Hunter's components and agreement rule, batch-1 candidates not chosen |

Sessions 4b–4d put evidence before machinery (DECISIONS D61); the core's backtest lives in
4c, so there is no Session 14. After Session 20 (owner steps): `obliskiq preflight` on the
machine, L0 → L1 (autonomous paper), observe ≥ 8 weeks, then decide on L2. Session 21 can
run any time after Session 4d (its Hunter components after Session 15), and later batches
reuse its prompt.

---

## 11. Owner decisions

| # | Question | Decision |
|---|---|---|
| 1 | Account size, minimum ticket, core lines | Any size. `trading.min_trade` supports an absolute amount, a % of managed capital, or both combined; never below the commission floor. Core granularity follows capital. |
| 2 | Capital budgets | In `config/owner.yaml` (defaults: core 70, equity satellite 20, option premium 5, cash ≥ 5), moved by the capital allocation rule (§5.19) up to an owner-set ceiling; option premium at most 5, with lite's penalty balance (DECISIONS D88). |
| 3 | Autonomy | Fully autonomous in paper, all sleeves. Live autonomy set at promotion. |
| 4 | LLM providers | Modular registry; expected a low-cost provider with a monthly budget and free Gemini as backup. |
| 5 | Machine | Free tiers being sought; deployment is provider-agnostic. |
| 6 | IBKR username | Paper: the paper account of the owner's main username, sharing his market data. Live: decided at promotion (a dedicated username avoids competing with the owner's own logins but needs its own subscriptions). |
| 7 | Tax lots and two-month rule | As Spanish law requires: FIFO and the two-month rule, minimising tax within the law. |
| 8 | Market data | OPRA L1 and Eurex Core L1 on the owner's main username, shared with its paper account; the complimentary US feed used; everything else delayed. US stock subscriptions reconsidered once the bot proves useful. |
| 9 | Existing holdings | All `unmanaged` by default. |
| 10 | Universe and limits | Global, all asset classes in §5.3; lite's asset rules and limits as defaults. |
| 11 | Base currency | Selectable; EUR today; other currencies held as working balances. |
| 12 | Withdrawals | Planned as soon as announced; the bot asks before cutting protected positions. |
| 13 | Releases | Automatic on every version change; 1.0.0 at the end of the build. |
| 14 | Objective (DECISIONS D60) | After-tax log growth of liquidation value within a drawdown limit, tax simulated as it arises; references scored under the same rule. Aspiration: 30–50 % a year or more, not a promise or a gate. The horizon is very long; the owner does not need the money now. |
| 15 | Scale (D66) | Capabilities that pay only at larger capital are built in Session 19a, before 1.0.0, and run by computed activation conditions. |
| 16 | Drawdown tolerance (D88) | 30 % from the previous peak of the value the bot manages (unmanaged holdings do not count), the peak taken once a day at the last cycle's NAV: the limit the research holds candidates to and the core's variant is chosen by, not a guaranteed maximum loss and not an order to sell. Reached on at most 5 % of 20-year paths (D91), which resolves to A on Session 4c's frontier. |
| 17 | Exposure caps by asset class (D88) | None set; optional configuration keys, read on look-through over the whole book. The mandate (§2) is unchanged. |
| 18 | Adaptation (D89) | A requirement: the allocation adapts within the owner's limits through rules a pre-registered test found sufficient on the objective at the owner's point (D92); the static variants are the fallback and the benchmark. Total-risk scaling is tested first. |

Open: the LLM
provider and budget (Session 10's minimal extractor waits for a key); the machine (T1); which
holdings to release; the paper managed-capital cap; the satellite budget ceiling; whether
IBKR supports Spanish tax-free fund switches; whether a hedge inside a UCITS share class fits
the mandate; a deep emergency limit for protective exits (off by default); buying
option-surface history for H8; the transport fallback if T1 fails (TWS API with IBC, or a
daily manual login).

---

## 12. Glossary

**ERC** equal risk contribution. **TSMOM** time-series momentum. **ES** expected
shortfall. **TWR** time-weighted return. **cOID** client order id. **LST** live session
token. **PRIIPs** EU regulation requiring a key information document; blocks US ETFs for
EU retail investors. **UCITS** EU fund regime. **Zombie** an option whose probability of
reaching its target has collapsed. **Penalty balance** option-sleeve debt from losses,
repaid by realised gains. **Swap partner** an equivalent vehicle from a different issuer
on a different, highly correlated index. **Base rate** the market-implied or random-walk
probability of reaching a target. **Unmanaged** holdings the bot counts but never trades.
**θ** the smallest net effect a pre-registered test must show to be worth having.
**Sufficient, insufficient, inconclusive, not evaluable** the four outcomes of a test
(§5.9). **n_eff** the effective number of independent observations. **Twin** a layer's
signals simulated on the same dates with the cost model, against which live results are
monitored. **Activation condition** the computed rule under which a capability runs.
**T1** the owner's first run on the deployment machine.
