---
id: core-risk-scaling
title: Scaling the core's total risk to a target volatility, against static A, after Spanish tax
kind: portfolio_comparison
mechanism: >-
  Volatility clusters: a market's recent volatility forecasts its volatility over the next weeks (Engle 1982; Bollerslev 1986), and falls come in turbulent states (Black 1976). In those states expected returns do not rise in proportion to risk (Moreira and Muir 2017), so a book that scales its risky share by a target over its forecast volatility holds less when markets are turbulent and more when they are calm. Without leverage, capped at one, its benefit is mainly a smaller drawdown for a given average exposure (Harvey et al. 2018), which lets a base with more equity than static A respect the owner's point (DECISIONS D91, D92) and so compound more after tax. It costs turnover, the tax on gains realised when it cuts, and whipsaws when a volatility spike marks a bottom; real-time versions have not systematically beaten their unmanaged base (Cederburg et al. 2020). The test measures what remains after costs and Spanish tax.
citation: >-
  Moreira and Muir (2017), Journal of Finance 72(4); Harvey, Hoyle, Korgaonkar, Rattray, Sargaison and Van Hemert (2018), Journal of Portfolio Management 45(1); Cederburg, O'Doherty, Wang and Yan (2020), Journal of Financial Economics 138(1); Engle (1982), Econometrica 50(4); Bollerslev (1986), Journal of Econometrics 31(3); Black (1976), Proceedings of the American Statistical Association; Ledoit and Wolf (2004), Journal of Multivariate Analysis 88(2); Patton (2011), Journal of Econometrics 160(1).
universe: >-
  Session 4c's nine roles and their proxies, unchanged (DECISIONS D93): static A holds all of them; each total-risk base is a fixed mix of 4c's roles with more equity than A: global equity (WORLD 90 %, EM 10 %) and the global 60/40 (WORLD 60 %, EGB_INT 40 %), the compositions of D60's two references with emerging markets at a declared, roughly market-weight share. Index and fund proxies that exist today.
data_status: >-
  Every series as observed, read daily on MSCI's calculation days (D93): MSCI World and Emerging Markets net in EUR from 2000-12-29; the 5- and 17-year constant-maturity Bund zeros from the Bundesbank's curve; the euro cash index (EONIA, then the euro short-term rate); TIP, IEF and CRSOX adjusted closes and GLD's close at ECB reference rates. The long-history check adds Ken French's developed markets and US bill, daily from 1990-07 (USD), and AQR's Century of Factor Premia equity-index and fixed-income market returns, monthly from 1926-07 (local excess returns plus the US bill), each revised without vintages and read as public at its period's close.
primary_metric: log_growth_difference
theta: "0.01"
theta_justification: >-
  one point a year of after-tax log growth over static A, the owner's neutral allocation (D91): below it the difference is within the proxies' own uncertainty (index against fund, fees, tracking and spreads of a few tenths of a percent a year each) and does not pay for an estimator, more equity and more turnover than A; over twenty years it is about 22 % more terminal wealth
alpha: null
target_power: null
tests_in_confirmation: 6
adjustment: >-
  Bonferroni across the six tests of the confirmation run (three candidates against A, three against their twins), each interval at level 1 - 2 x 0.05 / 6; the frontier's comparisons at the same level; the deflated Sharpe across every trial the registry holds for this id, this run's tests included.
trial_budget: 6
study:
  start: 2004-12-01
  end: 2026-09-30
  rebalance: month_end
  calendar: WORLD
  initial_capital: "100000"
  fx_dataset: ecb_fx
  instruments:
    - key: WORLD
      price_series: msci_world_eur.level
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "151802054881"
      expense_ratio: "0.002"
      security:
        kind: etf
        isin: IE00B4L5Y983
        issuer: iShares
        tracked_index: MSCI World
        listed: true
    - key: WORLD_SWAP
      price_series: msci_world_eur.level
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "11608993792"
      expense_ratio: "0.002"
      security:
        kind: etf
        isin: null
        issuer: Vanguard
        tracked_index: FTSE Developed
        listed: true
    - key: EM
      price_series: msci_em_eur.level
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "10487581696"
      expense_ratio: "0.0018"
      security:
        kind: etf
        isin: IE00B4L5YC18
        issuer: iShares
        tracked_index: MSCI Emerging Markets
        listed: true
    - key: EM_SWAP
      price_series: msci_em_eur.level
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "5988283392"
      expense_ratio: "0.0018"
      security:
        kind: etf
        isin: null
        issuer: Vanguard
        tracked_index: FTSE Emerging
        listed: true
    - key: EGB_INT
      price_series: bund_zero_05y.level
      income_series: null
      currency: EUR
      venue: XAMS
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "1350178048"
      expense_ratio: "0.002"
      security:
        kind: etf
        isin: null
        issuer: iShares
        tracked_index: euro government bonds 3-7 years
        listed: true
    - key: EGB_INT_SWAP
      price_series: bund_zero_05y.level
      income_series: null
      currency: EUR
      venue: XAMS
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: "0.002"
      security:
        kind: etf
        isin: null
        issuer: the EGB_INT swap partner's issuer
        tracked_index: the EGB_INT swap partner's index
        listed: true
    - key: EGB_LONG
      price_series: bund_zero_17y.level
      income_series: null
      currency: EUR
      venue: XAMS
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "1008975744"
      expense_ratio: "0.002"
      security:
        kind: etf
        isin: null
        issuer: iShares
        tracked_index: euro government bonds 15-30 years
        listed: true
    - key: EGB_LONG_SWAP
      price_series: bund_zero_17y.level
      income_series: null
      currency: EUR
      venue: XAMS
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: "0.002"
      security:
        kind: etf
        isin: null
        issuer: the EGB_LONG swap partner's issuer
        tracked_index: the EGB_LONG swap partner's index
        listed: true
    - key: IL
      price_series: tip.adj_close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: iShares
        tracked_index: US Treasury inflation-protected securities
        listed: true
    - key: IL_SWAP
      price_series: tip.adj_close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: the IL swap partner's issuer
        tracked_index: the IL swap partner's index
        listed: true
    - key: UST
      price_series: ief.adj_close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "4940705280"
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: iShares
        tracked_index: US Treasuries 7-10 years
        listed: true
    - key: UST_SWAP
      price_series: ief.adj_close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: the UST swap partner's issuer
        tracked_index: the UST swap partner's index
        listed: true
    - key: GOLD
      price_series: gld.close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etc
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: null
      security:
        kind: bond
        isin: null
        issuer: iShares
        tracked_index: gold (physical)
        listed: true
    - key: GOLD_SWAP
      price_series: gld.close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etc
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: null
      security:
        kind: bond
        isin: null
        issuer: the GOLD swap partner's issuer
        tracked_index: the GOLD swap partner's index
        listed: true
    - key: COMMOD
      price_series: crsox.adj_close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: Invesco
        tracked_index: Bloomberg Commodity
        listed: true
    - key: COMMOD_SWAP
      price_series: crsox.adj_close
      income_series: null
      currency: USD
      venue: XLON
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: null
      security:
        kind: etf
        isin: null
        issuer: the COMMOD swap partner's issuer
        tracked_index: the COMMOD swap partner's index
        listed: true
    - key: CASH
      price_series: euro_cash.level
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: "24196042752"
      expense_ratio: "0.001"
      security:
        kind: etf
        isin: null
        issuer: Xtrackers
        tracked_index: euro overnight rate
        listed: true
    - key: CASH_SWAP
      price_series: euro_cash.level
      income_series: null
      currency: EUR
      venue: XETR
      asset_class: etf
      issuer_country: null
      income_country: null
      market_cap_usd: null
      expense_ratio: "0.001"
      security:
        kind: etf
        isin: null
        issuer: the CASH swap partner's issuer
        tracked_index: the CASH swap partner's index
        listed: true
  roles:
    WORLD:
      primary: WORLD
      partner: WORLD_SWAP
    EM:
      primary: EM
      partner: EM_SWAP
    EGB_INT:
      primary: EGB_INT
      partner: EGB_INT_SWAP
    EGB_LONG:
      primary: EGB_LONG
      partner: EGB_LONG_SWAP
    IL:
      primary: IL
      partner: IL_SWAP
    UST:
      primary: UST
      partner: UST_SWAP
    GOLD:
      primary: GOLD
      partner: GOLD_SWAP
    COMMOD:
      primary: COMMOD
      partner: COMMOD_SWAP
    CASH:
      primary: CASH
      partner: CASH_SWAP

  core:
    cash_role: CASH
    environments:
      growth_up: [WORLD, EM, COMMOD]
      growth_down: [EGB_INT, EGB_LONG, UST, IL]
      inflation_up: [IL, COMMOD, GOLD]
      inflation_down: [WORLD, EM, EGB_INT, EGB_LONG, UST]
    risk:
      volatilities:
        WORLD: "0.15"
        EM: "0.20"
        COMMOD: "0.17"
        EGB_INT: "0.04"
        EGB_LONG: "0.12"
        UST: "0.10"
        IL: "0.10"
        GOLD: "0.16"
      correlations:
        - [WORLD, EM, "0.8"]
        - [WORLD, COMMOD, "0.4"]
        - [WORLD, EGB_INT, "0"]
        - [WORLD, EGB_LONG, "0"]
        - [WORLD, UST, "0.2"]
        - [WORLD, IL, "0.3"]
        - [WORLD, GOLD, "0.2"]
        - [EM, COMMOD, "0.5"]
        - [EM, EGB_INT, "0"]
        - [EM, EGB_LONG, "0"]
        - [EM, UST, "0.1"]
        - [EM, IL, "0.3"]
        - [EM, GOLD, "0.3"]
        - [COMMOD, EGB_INT, "-0.1"]
        - [COMMOD, EGB_LONG, "-0.1"]
        - [COMMOD, UST, "0.1"]
        - [COMMOD, IL, "0.3"]
        - [COMMOD, GOLD, "0.4"]
        - [EGB_INT, EGB_LONG, "0.85"]
        - [EGB_INT, UST, "0.4"]
        - [EGB_INT, IL, "0.3"]
        - [EGB_INT, GOLD, "0.2"]
        - [EGB_LONG, UST, "0.4"]
        - [EGB_LONG, IL, "0.3"]
        - [EGB_LONG, GOLD, "0.2"]
        - [UST, IL, "0.8"]
        - [UST, GOLD, "0.4"]
        - [IL, GOLD, "0.5"]
    bands:
      absolute: "0.05"
      relative: "0.25"
    scaling:
      estimation_start: 2000-12-29
      periods_per_year: 261
      estimators:
        fast:
          half_life_days: "21"
          window_days: null
          ewma_share: "1"
          shrinkage: ledoit_wolf
          prior_observations: "21"
        blend:
          half_life_days: "21"
          window_days: 261
          ewma_share: "0.5"
          shrinkage: ledoit_wolf
          prior_observations: "21"
  reference:
    name: A
    strategy: risk_balance
    budgets: {growth_up: "0.25", growth_down: "0.25", inflation_up: "0.25", inflation_down: "0.25"}
    tilt: false
    simplicity: 3
  candidates:
    - name: E-fast
      strategy: total_risk
      base: {WORLD: "0.9", EM: "0.1"}
      target_volatility: "0.0891"
      floor: "0.25"
      max_change: "0.25"
      estimator: fast
      tax_aware: true
      hold_costly_reductions: false
      simplicity: 6
    - name: E-blend
      strategy: total_risk
      base: {WORLD: "0.9", EM: "0.1"}
      target_volatility: "0.0891"
      floor: "0.25"
      max_change: "0.25"
      estimator: blend
      tax_aware: true
      hold_costly_reductions: false
      simplicity: 6
    - name: B-fast
      strategy: total_risk
      base: {WORLD: "0.6", EGB_INT: "0.4"}
      target_volatility: "0.0915"
      floor: "0.25"
      max_change: "0.25"
      estimator: fast
      tax_aware: true
      hold_costly_reductions: false
      simplicity: 6
  twins:
    - name: E-declared
      strategy: total_risk
      base: {WORLD: "0.9", EM: "0.1"}
      target_volatility: "0.0891"
      floor: "0.25"
      max_change: "0.25"
      estimator: declared
      tax_aware: true
      hold_costly_reductions: false
      simplicity: 5
    - name: B-declared
      strategy: total_risk
      base: {WORLD: "0.6", EGB_INT: "0.4"}
      target_volatility: "0.0915"
      floor: "0.25"
      max_change: "0.25"
      estimator: declared
      tax_aware: true
      hold_costly_reductions: false
      simplicity: 5
comparisons:
  - name: E-fast against E-declared
    candidate: E-fast
    against: E-declared
    theta: "0.005"
    theta_justification: >-
      half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth
    sd_per_period: "0.0066"
  - name: E-blend against E-declared
    candidate: E-blend
    against: E-declared
    theta: "0.005"
    theta_justification: >-
      half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth
    sd_per_period: "0.0066"
  - name: B-fast against B-declared
    candidate: B-fast
    against: B-declared
    theta: "0.005"
    theta_justification: >-
      half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth
    sd_per_period: "0.0040"
bootstrap:
  n_boot: 5000
  block_months: 24
  seed: 20261018
  sensitivity:
    block_months: [12, 36, 60]
    n_boot: 1000
drawdown_limits: ["0.1", "0.2", "0.3", "0.4", "0.5"]
frontier:
  breach_probabilities: ["0.05", "0.1", "0.25", "0.5"]
aspiration: ["0.3", "0.5"]
subperiods:
  - [2004-12-01, 2015-12-31]
  - [2016-01-01, 2026-09-30]
cost_uncertainty_multiplier: "3.67"
robustness: >-
  The sign of each test's effect in two halves (December 2004 to December 2015, January 2016 to September 2026); the break-even cost multiplier against a declared cost uncertainty of 3.67 (the illiquid friction tier over the liquid one, as in Session 4c); the bootstrap repeated with blocks of 12, 36 and 60 months; the breach at the owner's point and the growth on three longer histories (MSCI in EUR from 2001, Ken French's developed markets from 1990, AQR's Century from 1926), before tax; the forecast check, never an outcome. A portfolio comparison has no placebo.
adequacy:
  min_coverage: "1"
  point_in_time: [as_observed]
  min_effective_observations: 240
power:
  sd_per_period: "0.0210"
  by_candidate:
    B-fast: "0.0192"
  periods: 261
  periods_per_year: 12
  names_per_period: 1
  rho: "0"
structure: null
marks: daily
owner_point:
  limit: "0.3"
  probability: "0.05"
  horizon_years: 20
breach_horizons_years: [5, 10, 15, 20]
forecast_check:
  loss: qlike
development:
  start: 2001-01-01
  end: 2004-11-30
long_history:
  - name: MSCI and the Bundesbank in EUR, daily
    frequency: daily
    periods_per_year: 261
    currency: EUR
    data_start: 2000-12-29
    start: 2001-07-01
    end: 2026-09-30
    roles:
      WORLD: {kind: level, series: [msci_world_eur.level]}
      EM: {kind: level, series: [msci_em_eur.level]}
      EGB_INT: {kind: level, series: [bund_zero_05y.level]}
    cash: {kind: level, series: [euro_cash.level]}
    point_in_time: [as_observed]
    cost_per_leg: "0.003"
    block_months: 24
    n_boot: 1000
    biases:
      - MSCI indices and the Bundesbank's curve, not funds; the flat cost a leg stands in for every trading cost.
      - German Federal bonds only, a fitted curve, for the euro bond role.
      - From December 2004 the same series as the main sample; what this history adds is January 2001 to November 2004, the end of the 2000-03 bear market.
      - Before tax.
  - name: Ken French developed markets in USD, daily
    frequency: daily
    periods_per_year: 261
    currency: USD
    data_start: 1990-07-02
    start: 1991-01-01
    end: 2026-08-31
    roles:
      WORLD: {kind: returns, series: [kf_developed_daily.Mkt-RF, kf_developed_daily.RF]}
      EM: {kind: returns, series: [kf_developed_daily.Mkt-RF, kf_developed_daily.RF]}
    cash: {kind: returns, series: [kf_developed_daily.RF]}
    point_in_time: [release_lag]
    cost_per_leg: "0.003"
    block_months: 24
    n_boot: 1000
    biases:
      - A factor model's value-weighted market of every developed-market stock with market equity (Bloomberg data), not MSCI World.
      - In US dollars, unhedged; a dollar investor's history, not a euro investor's.
      - No daily emerging-market series; the EM role is mapped to the developed market. No bond role, so B-fast and its twin are not run here.
      - The US one-month bill as cash, a daily simple rate; revised without vintages; before tax.
  - name: AQR's Century of Factor Premia, monthly
    frequency: monthly
    periods_per_year: 12
    currency: local excess returns plus the US one-month bill
    data_start: 1926-07-01
    start: 1927-07-01
    end: 2026-02-28
    roles:
      WORLD: {kind: returns, series: [aqr_century_market.Equity indices Market, ff3_market.RF]}
      EM: {kind: returns, series: [aqr_century_market.Equity indices Market, ff3_market.RF]}
      EGB_INT: {kind: returns, series: [aqr_century_market.Fixed income Market, ff3_market.RF]}
    cash: {kind: returns, series: [ff3_market.RF]}
    point_in_time: [release_lag]
    cost_per_leg: "0.003"
    block_months: 24
    n_boot: 1000
    biases:
      - Local-currency excess returns of equity indices (up to 43 markets, nine in the 1920s) and 10-year government bonds (up to 26), plus the US one-month bill; a currency-hedged dollar return, not a euro investor's.
      - The workbook does not state how its market columns weight their countries.
      - Monthly; the estimators read monthly returns, their lengths converted at 12 / 261 months a trading day, so they react a month late at best; drawdowns on month ends understate falls inside a month.
      - The EM role is mapped to the equity indices, EGB_INT to the 10-year bonds (not five years).
      - Gross of costs as published (a flat cost a leg here); rebuilt by AQR at each update, without vintages; before tax.
---

# Scaling the core's total risk to a target volatility, against static A, after Spanish tax

The first test of adaptation (SESSION_PROMPTS Session 4e; MASTER_PLAN §1, §5.4, §5.9; DECISIONS
D60, D62–D64, D79, D88–D94): does scaling the core's total risk by a re-estimated volatility, on
bases with more equity than A, grow the after-tax book more than static A while respecting the
owner's point? Every constant is in the front matter above; this prose explains each one. Nothing
here may change after the merge: a change is a new pre-registration with a new id. Not tax advice.

## What the author had seen before writing

This pre-registration is written after Session 4c, whose confirmation sample (December 2005 to
September 2026) this test's sample contains. Its author had read, in full and before writing,
`research/reports/core-allocation.md` and DECISIONS D88–D92:

- each 4c portfolio's after-tax and pre-tax results on the historical path and over the paths,
  year by year (for example the equity reference's −37.76 % in 2008 and −12.95 % in 2022, A's
  −1.15 % and −10.23 %, the 60/40's −19.71 % and −13.47 %), costs, taxes and turnover;
- the equity reference's deepest month-end fall on the historical path, −49.18 %;
- the share of paths reaching each drawdown limit on month-end marks: at 30 %, the equity
  reference 72.3 %, the 60/40 10.7 %, D30 5.7 %, A 0.1 %; and the frontier.

The author has computed no return, volatility, correlation or drawdown of any series at a daily
frequency, and none of any series since that report; Step 1 of this session checked the data's
spans, continuity and alignment only (D93). The bases are the compositions of D60's two
references, chosen before this test on that ground; the author knew that 4c's 60/40 breached
the owner's point on month-end marks, which is where scaling would act, and states it so a reader
can weigh it. The target volatilities come from design falls and the declared matrix below, not
from 4c's breach table, and the declared matrix is 4c's, unchanged.

## What is tested

Six portfolios are simulated monthly from 31 December 2004 to 30 September 2026 with €100,000,
costs on every leg and Spanish tax (`es_irpf`) as it arises; at the horizon every position is
sold and that tax paid (D60). The reference is **A**, Session 4c's static risk balance, exactly
as 4c ran it: 25 % of risk to each of four environments on the declared matrix, wide bands, no
tilt, no swap partners. The owner's point (D91) selects it, and every adaptation must beat it
after tax (D89, D92).

| Portfolio | Base (relative weights) | Target volatility | Estimator | Role |
|---|---|---|---|---|
| A | the static risk balance | — | — | reference |
| E-fast | WORLD 90 %, EM 10 % | 8.91 % | fast | candidate |
| E-blend | WORLD 90 %, EM 10 % | 8.91 % | blend | candidate |
| B-fast | WORLD 60 %, EGB_INT 40 % | 9.15 % | fast | candidate |
| E-declared | WORLD 90 %, EM 10 % | 8.91 % | the declared matrix | twin of E-fast and E-blend |
| B-declared | WORLD 60 %, EGB_INT 40 % | 9.15 % | the declared matrix | twin of B-fast |

**The rule** (`total_risk`, D94). At each month end the risky share is the target volatility
over the base's forecast yearly volatility, capped at one (no leverage), at least the floor of
25 % and moved by at most 25 percentage points from the share held; the risky roles hold the base
at that share and the cash role the rest. The forecast is the base's volatility under the
estimator's covariance of its roles' daily returns in EUR, through that month end and nothing
later. A **declared twin** applies the same rule to the declared matrix: its forecast is constant,
so its share is constant and it is a static portfolio, the base held at its declared share. The
pair attributes what estimation adds. There is no trend tilt: return timing is another mechanism
(D89).

**Execution** is the core's (D80, D94): A's bands (nothing trades while every role is inside its
outer band, the smaller of 5 points and 25 % of its target; when one leaves, every role returns to
target), the simulator's 0.5 % no-trade band, whole units, and tax-aware execution: a purchase
that would defer a loss under the two-month rule goes to the role's swap partner, sales come
first from the vehicle with the smaller gain, losses are taken freely. A reduction is **not** held
for its tax (`hold_costly_reductions: false`): the rule cuts risk when forecast volatility rises,
which after a long rise is when embedded gains are largest, and holding the cut for its tax would
switch the mechanism off when the owner's point depends on it.

**The floor and the largest change.** The floor (25 %, as the trend tilt's) keeps a quarter of the
base whatever the forecast: a volatility spike often marks a bottom, and a share of zero would
miss the recovery entirely. The largest change (25 points a month) keeps one noisy month from
swinging the book; a cut from a full share to the floor takes three months.

### Sizing the targets to the owner's point

The rule fixed before data: the target volatility is the declared volatility of the base held at
the share whose **design fall** equals the owner's limit of 30 %, capped at the whole base.

- Design falls are those of an equity crisis, from history outside this test's sample: global
  equities about 50 % (2000–03, Session 4c's design fall for D), emerging markets about 60 %
  (1997–98). In such a crisis euro government bonds did not fall (2000–03), so their design fall
  is zero. A base's design fall is the weighted sum: 51 % for E (0.9 × 50 % + 0.1 × 60 %), 30 %
  for B (0.6 × 50 %).
- The share is 30 % over the design fall, at most one: 0.588 for E, 1 for B.
- Under the declared matrix E's base has a volatility of 15.15 % a year and B's 9.14 %. The
  target is the share times that, to four decimals: 8.91 % for E (0.588 × 15.15 %), and for B the
  whole base, 9.15 % (rounded up so that the twin holds all of it).

So each twin sits at the owner's limit in a design crisis: E-declared is 58.8 % equity and the rest
cash, B-declared the 60/40 itself. A twin that suffers a fall worse than its design breaches; the
scaled candidates hold less as volatility rises, so they may respect the point while holding as
much on average, or more. That is the question.

### The estimators

Both read daily log returns in EUR on MSCI's calculation days from 29 December 2000
(`scaling.estimation_start`; the four years before the evaluation are its warm-up) and shrink
toward the declared matrix over 261 trading days a year (`quant/covariance.py`, D94):

- **fast**: an EWMA with a half-life of 21 trading days (a month), Ledoit–Wolf shrinkage toward the
  declared matrix, and a prior worth 21 observations of it. Its lookback is three half-lives, 63
  days, about 2.9 months.
- **blend**: the same EWMA blended half and half with an equal-weight window of the last 261 days
  (a year): the window anchors the forecast as volatility reverts toward its mean, at the price of
  a 12-month lookback. Same shrinkage and prior.

Both estimate second moments about zero (a daily mean is small beside its spread). The half-life
of a month follows the volatility-targeting literature (Harvey et al. 2018 find short half-lives
best for equities). The window is a year, not several: a two- or three-year window would need
blocks of 48 or 72 months (rule 3 below), of which the sample holds about five or four, too few
for an interval. The prior and the shrinkage keep the forecast at the declared matrix until data
accumulate.

## Universe

Session 4c's universe, unchanged (D93): nine roles, each a long-history proxy in EUR priced and
taxed as a representative UCITS vehicle with a modelled swap partner (another issuer and index).
The candidates and twins hold only WORLD, EM, EGB_INT and CASH; A holds all nine. Expense ratios,
fund sizes, friction tiers and the proxies' biases are 4c's (`core-allocation.md`, Universe). The
indices include companies that disappeared; choosing these roles and vehicles in 2026 is
hindsight about which asset classes became investable.

## Data and their point-in-time status

Every series is as observed (D93). A study that reads daily risk builds a daily history on MSCI's
calculation days (every weekday from 2000-12-29): each instrument's price in EUR is the latest
price and reference rate public that day within seven days; TARGET, German and US holidays leave
at most three weekdays in a row on a carried price. A month's daily returns sum to its month-end
change, so the daily and the monthly views agree. CRSOX repeats one value on five sessions in May
2014, read as days without a move. Coverage must be complete at every rebalance date, and a day
inside a month without a fresh price makes the run not evaluable. The marks are **daily closes**
(D88): between two month ends the account is valued each day at the holdings after the last
rebalance.

**What the simulation does not show** (every report states it): trades happen at month ends only,
so no exit inside a month is simulated; trades fill at the close moved by the cost model;
conversions are free; the 2026 savings scale applies to every year from 2005 (tax errs high);
a year's tax is paid at the first rebalance on or after 30 June of the next; losses still carried
at the horizon are worth nothing.

## Primary metric and horizon

For each candidate, the annualised log growth of liquidation value after costs and tax minus A's,
from 31 December 2004 to 30 September 2026 (21.75 years, 261 months), paired. Its interval is the
percentile interval of a moving-block bootstrap: 5,000 paths of **24-month blocks** of whole
months, every instrument, rate and day of a month resampled together, the real history before the
first date kept, seed 20261018; every portfolio is simulated on each path with its own tax.

**Secondary** (in the same Bonferroni count): each candidate against its declared twin, the same
paired effect, theta 0.5 % a year — what estimation adds.

**The owner's point** (D91): a candidate whose share of paths reaching a 30 % drawdown from the
running peak, on daily closes, within the first **20 years** exceeds 5 % does not respect it, is
labelled so, and **cannot be sufficient** (D92): an interval that would make it sufficient makes
it inconclusive (positive). Reported beside it for every portfolio: the share of paths reaching
30 % within 5, 10, 15 and 20 years; the rate (drawdown episodes reaching 30 % per decade, the
share of time beyond it); the breach of each limit of the grid (10–50 %) within 20 years; the
historical path's deepest fall on daily closes; the median after-tax growth, the 5th percentile
of terminal wealth and the distance from the owner's aspiration (30 % and 50 % a year, never a
gate).

**The frontier at the owner's point** (D79): for each limit and probability of 4c's grid, on
breaches within 20 years, the portfolio with the highest median growth among those that respect
it, resolved to the simplest of those the bootstrap cannot tell apart (simplicity: A 3; a twin 5;
a candidate 6), then the lowest turnover. A new point chosen from it is the owner's authorisation
(D60), recorded in DECISIONS; this test does not choose.

## Theta and why

θ = 1 % a year against A: one point a year of after-tax log growth over the owner's neutral
allocation; below it the difference is within the proxies' own uncertainty and does not pay for
an estimator, more equity and more turnover than A; over twenty years it is about 22 % more
terminal wealth. θ = 0.5 % a year against a twin: half a point a year over the same base at its
declared share; below it what the estimator adds is within its own turnover, tax and model risk
and does not pay for an estimator the live core would run and monitor; over twenty years about
10 % more wealth.

## Power

Before any data, from the declared matrix alone (`research/explore/risk_scaling_power/power.py`):
a candidate's yearly variance against A is the tracking variance of its average weights (its
declared share of its base) against A's, plus the variance its share adds around that average,
taken as 0.15 times the base's declared volatility — an assumption (the share moves between its
floor and one as volatility moves between calm and crisis); against its twin, only the second
term. A month's standard deviation is the yearly one over √12: E 2.10 %, B 1.92 % against A;
E 0.66 %, B 0.40 % against the twin. `obliskiq research power` on this file prints (α 0.05, six
tests, 261 independent months):

| Test | theta | sd a month | se a year | Power to detect theta | Effect detectable at 0.8 |
|---|---|---|---|---|---|
| E-fast, E-blend against A | 1 % | 2.10 % | 1.56 % | 0.040 | 5.05 % |
| B-fast against A | 1 % | 1.92 % | 1.43 % | 0.045 | 4.61 % |
| E-fast, E-blend against E-declared | 0.5 % | 0.66 % | 0.49 % | 0.085 | 1.59 % |
| B-fast against B-declared | 0.5 % | 0.40 % | 0.30 % | 0.239 | 0.96 % |

The test has almost no power to detect θ against A: two decades of one book against another can
show only a difference of about five points a year. A sufficient outcome is therefore unlikely
even if an effect of θ exists, and an inconclusive one is no evidence against adaptation (D92).
The owner's point, which this test measures on every path, and the comparisons against the twins,
which have more power, are its main products.

## Adjustment

Bonferroni across the six tests (each interval at level 1 − 2 × 0.05 / 6 = 0.9833); the frontier's
comparisons at the same level; the deflated Sharpe across every trial the registry holds for this
id, this run's six tests and the exploration's included.

## The bootstrap and its block length (D92 rule 3)

The longest lookback a portfolio reads is blend's window, 12 months; blocks are twice it, 24
months, so about a quarter of a 12-month window comes from another block. A third of the sample
is 87 months, so the rule can be respected. Longer blocks leave fewer independent blocks and wider
intervals; the **sensitivity** repeats the bootstrap with 1,000 paths each of 12-, 36- and
60-month blocks (12 months breaks rule 3 for blend and shows what 4c's blocks would have done)
and reports each test's interval and each portfolio's share of paths reaching the owner's point.

## Robustness checks

- The sign of each test's effect in each half: 1 December 2004 to 31 December 2015 and
  1 January 2016 to 30 September 2026.
- The break-even cost multiplier against a declared cost uncertainty of 3.67 (4c's: the illiquid
  friction tier's cost on a leg over the liquid one's).
- The block-length sensitivity above.
- **The long-history check** (D92 rule 4; descriptive, before tax, never an outcome): every
  candidate and twin whose base a history covers, run at 0.3 % a leg on the history's own
  frequency with its estimator's lengths converted to the history's observations, with the core's
  bands; reported: its log growth and deepest fall as the history happened, and over 1,000
  moving-block paths of 24-month blocks, each 20 years long, the share reaching each limit within
  5, 10, 15 and 20 years. A cannot be run before 2004 on its proxies; its breach rests on
  2004–2026 alone, a span in which bonds gained from falling yields.

| History | Roles | From | Currency and frequency | Cash |
|---|---|---|---|---|
| MSCI and the Bundesbank | WORLD, EM, EGB_INT | 2001-07 (data from 2000-12-29) | EUR, daily | the euro cash index |
| Ken French developed markets | WORLD; EM mapped to it | 1991-01 (data from 1990-07-02) | USD, daily | the US one-month bill |
| AQR *Century of Factor Premia* | WORLD and EM to equity indices, EGB_INT to fixed income | 1927-07 (data from 1926-07) | local excess plus the US bill, monthly | the US one-month bill |

  Their biases are in the front matter: a factor-model market in dollars (Ken French); hedged
  local returns of up to 43 equity markets and 26 bond markets with an unstated weighting, read
  monthly so that the estimators react a month late at best and month-end marks understate falls
  (AQR); the main sample's own series after 2004 (MSCI). The MSCI history adds what this sample
  lacks: January 2001 to November 2004, the end of the 2000–03 bear market. B-fast and its twin
  have no bond role on Ken French's history and are not run there.
- **The forecast check** (D92; never an outcome): each estimator's forecast of its base's
  next-month variance against the realised variance of the base's daily returns that month,
  scored by QLIKE beside the declared matrix's constant forecast, with a 24-month block-bootstrap
  interval of the mean difference. It checks that the estimator works; it decides nothing.
- No placebo: a portfolio comparison has no signal to lag.

## Trial budget and the development window

Six exploratory trials: at most two exploratory runs of the three comparisons. Exploration runs on
the **development window**, January 2001 to November 2004, before the sample: the candidates and
twins on their own roles (A's proxies do not all exist yet), each comparison one trial, never
evidence. It checks that the harness runs on real data; it may not change a constant (a change is
a new id). The confirmation run is single-shot once it has an outcome (D76); a run the data or
the harness made not evaluable may be repeated after the fix under D83.

## What each outcome would lead to (D62, D89, D92)

- **Sufficient** (L ≥ θ against A, respecting the owner's point): the specification becomes a
  variant Session 13 builds beside the static ones, run in shadow first, earning weight by its
  evidence (D65, D89); a new point on the frontier is the owner's choice (D60).
- **Inconclusive, positive** (L > 0), respecting the point: it runs in shadow at zero weight
  (§5.9), graded forward; none is built on its forecast alone (D92).
- **Inconclusive otherwise, insufficient, or outside the owner's point**: recorded with its result
  in `research/pattern_registry.yaml`; an insufficient one is retired; only a new pre-registration
  revives it.
- **Not evaluable**: data or harness work, then the run again (D83).

The comparisons against the twins say whether estimation, rather than the base and target alone,
did the work: a candidate that beats A but not its twin is a static portfolio's success, which
the frontier shows and the owner may choose (D60) without an estimator.

Whatever the outcomes, this test cannot conclude that a portfolio will keep its place after the
sample, how any behaves in a crisis unlike the sample's (2008, 2011, 2020 and 2022 inside it; the
long histories add 1929–32, 1973–74, 1987 and 2000–02 before tax), or anything about costs, taxes
or markets outside it.

## Reproduce

```
obliskiq research data load ecb_fx ff3_market kf_developed_daily aqr_century_market \
  msci_world_eur msci_em_eur ecb_eonia ecb_estr bbk_bund_04y bbk_bund_05y bbk_bund_16y \
  bbk_bund_17y euro_cash bund_zero_05y bund_zero_17y tip ief gld crsox
obliskiq research power research/preregistrations/core-risk-scaling.md
obliskiq research run research/preregistrations/core-risk-scaling.md --explore
obliskiq research run research/preregistrations/core-risk-scaling.md
```

The confirmation run writes `research/reports/core-risk-scaling.md` with the inputs' hashes and
the seed.

## Constants hash

The SHA-256 of the front matter's values as canonical JSON (sorted keys, no spaces, dates and
decimals as written):
`b703b0d4d643c6a1bd2c1cb793ee605fe0c838786b7d5b6f66800fd02a253506`. The file's own SHA-256 and
the commit that merged it are in `core-risk-scaling.lock`, written after the merge with
`obliskiq research lock`.
