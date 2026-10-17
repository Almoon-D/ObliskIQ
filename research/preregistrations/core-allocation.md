---
id: core-allocation
title: The core's variants against a global equity fund, after Spanish tax
kind: portfolio_comparison
mechanism: >-
  Two documented sources of return an unlevered core can hold, and the tax that holding them
  costs a Spanish resident. Risk balance across growth and inflation environments spreads risk
  over regimes without forecasting them (Bridgewater 2012; Qian 2005; Asness, Frazzini and
  Pedersen 2012). Time-series momentum and a moving average cut exposure in sustained falls
  (Moskowitz, Ooi and Pedersen 2012; Hurst, Ooi and Pedersen 2017; Faber 2007): it pays when
  losses persist, because investors under-react to news and then de-risk late, and it costs in
  whipsaws and in tax realised early. Without leverage a risk balance holds little equity (two
  assets of 16 % and 5 % volatility at equal risk hold about 24 %, DECISIONS D60), so it may
  compound less than equities after tax; the test measures by how much, and whether the tilt
  and an equity-dominant book change that.
citation: >-
  Moskowitz, Ooi and Pedersen (2012), Journal of Financial Economics 104(2); Hurst, Ooi and
  Pedersen (2017), Journal of Portfolio Management 44(1); Faber (2007), Journal of Wealth
  Management 9(4); Asness, Frazzini and Pedersen (2012), Financial Analysts Journal 68(1); Qian
  (2005), PanAgora Asset Management; Bridgewater Associates (2012), The All Weather Story.
universe: >-
  The nine core roles of MASTER_PLAN section 5.4, fixed before any result: global developed
  equity, emerging-market equity, euro government bonds of intermediate and of long duration,
  inflation-linked government bonds, US Treasuries, gold, broad commodities and cash. Each role
  is a long-history proxy in EUR priced for costs and tax as a representative UCITS vehicle and a
  modelled swap partner. Index and fund proxies, not stock lists: no member of a role can drop
  out, but the indices and funds chosen are ones that exist today.
data_status: >-
  Every series as observed. MSCI World and Emerging Markets net total return in EUR (MSCI, daily
  from 29 December 2000); 5- and 17-year constant-maturity zero-coupon German Federal bonds
  derived from the Bundesbank's Svensson yields (daily from August 1997); a euro overnight
  accrual of EONIA, then the euro short-term rate (ECB Data Portal, from January 1999); the
  adjusted closes of TIP, IEF and CRSOX and the close of GLD (Yahoo, USD, from 2002 to 2004),
  converted at ECB reference rates. The structure check reads the Ken French US market and bill
  (1926 to 2026) as public at the month's close, revised without vintages. Each proxy's bias is
  stated in the prose.
primary_metric: log_growth_difference
theta: "0.01"
theta_justification: >-
  one point a year of after-tax log growth over buying and holding the equity reference: below
  it the difference is within the proxies' own uncertainty (index against fund, fees, tracking
  and spreads of a few tenths of a percent a year each) and does not pay for a managed core's
  complexity, turnover and model risk; over twenty years it is about 22 % more terminal wealth
alpha: null
target_power: null
tests_in_confirmation: 9
adjustment: >-
  Bonferroni across the nine candidates, each interval at level 1 - 2 x 0.05 / 9; the deflated
  Sharpe across every trial the registry holds for this id, the nine candidates of this run
  included; the frontier's comparisons at the same level.
trial_budget: 0
study:
  start: 2005-12-01
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
    tilt:
      momentum_months: 12
      average_months: 10
      hysteresis: "0.02"
      floor: "0.25"
    tax_aware:
      benefit_fraction: "0.05"
  reference:
    name: equity reference
    strategy: buy_and_hold
    weights: {WORLD: "1"}
    simplicity: 1
  candidates:
    - name: 60/40
      strategy: banded_weights
      weights: {WORLD: "0.6", EGB_INT: "0.4"}
      simplicity: 2
    - name: A
      strategy: risk_balance
      budgets: {growth_up: "0.25", growth_down: "0.25", inflation_up: "0.25", inflation_down: "0.25"}
      tilt: false
      simplicity: 3
    - name: B
      strategy: risk_balance
      budgets: {growth_up: "0.25", growth_down: "0.25", inflation_up: "0.25", inflation_down: "0.25"}
      tilt: true
      simplicity: 5
    - name: C
      strategy: risk_balance
      budgets: {growth_up: "0.7", growth_down: "0.1", inflation_up: "0.1", inflation_down: "0.1"}
      tilt: true
      simplicity: 5
    - name: D10
      strategy: equity_overlay
      equity: WORLD
      fraction: "0.2"
      tilt: true
      simplicity: 4
    - name: D20
      strategy: equity_overlay
      equity: WORLD
      fraction: "0.4"
      tilt: true
      simplicity: 4
    - name: D30
      strategy: equity_overlay
      equity: WORLD
      fraction: "0.6"
      tilt: true
      simplicity: 4
    - name: D40
      strategy: equity_overlay
      equity: WORLD
      fraction: "0.8"
      tilt: true
      simplicity: 4
    - name: D50
      strategy: equity_overlay
      equity: WORLD
      fraction: "1"
      tilt: true
      simplicity: 4
bootstrap:
  n_boot: 5000
  block_months: 12
  seed: 20261017
drawdown_limits: ["0.1", "0.2", "0.3", "0.4", "0.5"]
frontier:
  breach_probabilities: ["0.05", "0.1", "0.25", "0.5"]
aspiration: ["0.3", "0.5"]
subperiods:
  - [2005-12-01, 2015-12-31]
  - [2016-01-01, 2026-09-30]
cost_uncertainty_multiplier: "3.67"
robustness: >-
  The sign of each candidate's effect in two pre-registered halves (December 2005 to December
  2015, January 2016 to September 2026); the break-even cost multiplier against a declared cost
  uncertainty of 3.67, the ratio of the illiquid friction tier to the liquid one on a leg; a
  portfolio comparison has no placebo; the structure check (the tilt before tax on a century of
  the US market) is descriptive.
adequacy:
  min_coverage: "1"
  point_in_time: [as_observed]
  min_effective_observations: 240
power:
  sd_per_period: "0.0351"
  by_candidate:
    60/40: "0.0179"
    B: "0.0359"
    C: "0.0307"
    D10: "0.0348"
    D20: "0.0269"
    D30: "0.0203"
    D40: "0.0167"
    D50: "0.0178"
  periods: 249
  periods_per_year: 12
  names_per_period: 1
  rho: "0"
structure:
  market_series: [ff3_market.Mkt-RF, ff3_market.RF]
  cash_series: ff3_market.RF
  start: 1926-07-01
  end: 2026-08-31
  cost_per_leg: "0.003"
  point_in_time: [release_lag]
  subperiods:
    - [1926-07-01, 1949-12-31]
    - [1950-01-01, 1979-12-31]
    - [1980-01-01, 2004-12-31]
    - [2005-01-01, 2026-08-31]
---

# The core's variants against a global equity fund, after Spanish tax

The first investment experiment of ObliskIQ (SESSION_PROMPTS Session 4c; MASTER_PLAN §1, §5.4,
§5.9; DECISIONS D60–D63). Every constant is in the front matter above; this prose explains each
one. Nothing here may change after the merge: a change is a new pre-registration with a new id.
No return or statistic of any series below was computed before this text was written; only that
each series exists and its span were checked (2026-10-17). Not tax advice.

## What is tested

Ten portfolios are simulated monthly from 30 December 2005 to 30 September 2026 with €100,000,
costs on every leg and Spanish tax (the `es_irpf` policy) as it arises; at the horizon every
position is sold and that tax paid (D60). The **equity reference** buys one global accumulating
UCITS equity fund and holds it. Each of the nine **candidates** is one test against it: its
annualised log growth of liquidation value after costs and tax, minus the reference's.

| Portfolio | Strategy | Simplicity |
|---|---|---|
| equity reference | buy and hold WORLD | 1 |
| 60/40 | 60 % WORLD, 40 % EGB_INT, wide bands | 2 |
| A | static risk balance, 25 % of risk per environment, wide bands | 3 |
| B | A + the tax-aware trend tilt | 5 |
| C | a growth-tilted risk budget (70 % of risk to growth up, 10 % to each other environment) + the tilt | 5 |
| D10, D20, D30, D40, D50 | WORLD at 20, 40, 60, 80 and 100 % with the tilt as an overlay to cash, the rest in cash: D sized to each drawdown limit of the grid | 4 |

The 60/40 is the second reference of D60 ("a global 60/40"); it is scored under the same rule
and tested like a candidate, so the report says whether it beat equities after tax. Simplicity
ranks the portfolios for the frontier's resolution: one fund held; two funds and bands; eight
roles and bands; two funds, bands and the tilt; eight roles, bands and the tilt.

### Roles, environments and the static weights

Environments (Bridgewater 2012): growth up — WORLD, EM, COMMOD; growth down — EGB_INT,
EGB_LONG, UST, IL; inflation up — IL, COMMOD, GOLD; inflation down — WORLD, EM, EGB_INT,
EGB_LONG, UST. Cash belongs to none. Each environment's risk budget is split equally among its
roles, a role in two environments taking a share of each; the weights whose shares of variance
equal those budgets are solved once (`quant/risk_budget.py`) on the **declared** long-run
covariance below and held fixed: a static risk balance. No covariance is estimated from the data:
gold's and commodities' proxies start in late 2004, too late for a multi-year estimate before a
2005 start (DECISIONS D80).

Declared volatilities in EUR, a year: WORLD 15 %, EM 20 %, COMMOD 17 %, EGB_INT 4 %, EGB_LONG 12 %,
UST 10 %, IL 10 %, GOLD 16 %. Correlations as listed in the front matter: equities 0.8 with each
other and 0.4–0.5 with commodities; no stable sign between equities and euro bonds (0); the
dollar the US-listed proxies share once converted to EUR (0.1–0.3 with equities, 0.8 between
Treasuries and inflation-linked bonds, 0.4 with gold); the two euro bond maturities 0.85. These
are the author's round long-run assumptions, of the order published in long-term capital market
assumptions; they are not estimated from the test's data, and the matrix is positive definite.
Under them A holds about 9 % WORLD, 7 % EM, 11 % COMMOD, 34 % EGB_INT, 11 % EGB_LONG, 11 % UST,
12 % IL and 5 % GOLD (about 6 % volatility), and C about 16 %, 12 %, 17 %, 28 %, 9 %, 8 %, 7 % and
3 % (about 8 %); the report prints the weights the harness computed. Without leverage even 70 %
of risk to growth holds under a third in equities: that is what C measures.

D's sizing: a book of equity fraction *e* and cash falls by about *e* times the equity fall.
Global equities lost about half of their value in the 2000–03 bear market, which ended before
this test's span; with that design fall of 50 %, D sized to a drawdown limit *L* holds
*e* = min(1, *L* / 0.5) in WORLD: 20 % for 10 %, … 100 % for 50 %. The tilt then cuts the equity
further in a negative trend. The design fall is general market history, not a statistic of this
test's data.

### Bands, the tilt and tax-aware execution

- **Bands** (A, B, C, D, the 60/40): nothing trades while every role sits inside its outer band,
  the smaller of 5 percentage points and 25 % of its target (the "5/25" rule of thumb); when one
  leaves it, every role goes back to its target (the simulator's 0.5 % no-trade band skips what
  barely moves). The inner band is the target itself.
- **The trend tilt** (B, C, D) on each risky role, evaluated on month-end closes in EUR:
  12-month momentum in excess of the cash role's 12-month return (Moskowitz, Ooi and Pedersen
  2012) and the close against its 10-month moving average (Faber 2007). Each signal is a state
  that turns negative only below −2 % and positive only above +2 % (hysteresis), replayed from
  the first month the history allows. A role's scale is 0.25 + 0.75 × (positive signals) / 2:
  whole with both positive, 0.625 with one, the floor of 0.25 with none — never zero
  (MASTER_PLAN §5.4). The freed weight goes to the cash role.
- **Tax-aware execution** (with the tilt): a trend reduction is made only while the tax its
  sales would add to the year together (the lot engine's own answer, FIFO, offsets and
  carry-forwards included, so a loss on one vehicle offsets a gain on the other) is at most 5 %
  of the value sold; otherwise the role keeps its weight, the cash role gives up what it would
  have received, and a held reduction alone trades nothing else. The 5 % is the
  benefit a reduction is assumed to buy: the order of the loss a sustained fall would still
  inflict on what is moved (equity falls of 20–50 % of which an exit avoids a part), discounted
  for the whipsaws that make most exits worth little — an assumption, not an estimate. Losses
  are taken freely. A purchase that would defer a loss under the two-month rule goes to the
  role's **swap partner** (another issuer and index, D7), and a sale comes first from the vehicle
  with the smaller gain for its value. Cash flows other than income do not arise (no deposits).

## Universe

Nine roles (MASTER_PLAN §5.4), each priced on one long-history proxy in EUR and costed and taxed
as a representative vehicle. Every role has a modelled swap partner priced on the same proxy:
its identity differs in issuer and tracked index, so the tax engine treats it as not homogeneous
(D7); its own tracking difference is ignored. Expense ratios are Yahoo's `netExpenseRatio` read
on 2026-10-17 (IWDA's confirmed on its iShares page); fund sizes set the friction tier (liquid at
USD 10 bn or more, else illiquid; null is the illiquid tier, labelled).

| Role | Proxy (series) | Since | Vehicle (venue, currency) | Expense ratio deducted | Fund size, tier | Bias of the proxy |
|---|---|---|---|---|---|---|
| WORLD | MSCI World net total return in EUR (`msci_world_eur.level`) | 2000-12-29 | iShares Core MSCI World UCITS ETF (Acc), IE00B4L5Y983 (XETR, EUR); partner Vanguard FTSE Developed World | 0.20 % | USD 151.8 bn, liquid; partner 11.6 bn, liquid | an index with MSCI's net withholding; no tracking difference beyond the fee; the partner's FTSE index is priced as MSCI's |
| EM | MSCI Emerging Markets net total return in EUR (`msci_em_eur.level`) | 2000-12-29 | iShares MSCI EM UCITS ETF (Acc), IE00B4L5YC18 (XETR, EUR); partner Vanguard FTSE Emerging Markets | 0.18 % | USD 10.5 bn, liquid; partner 6.0 bn, illiquid | as WORLD |
| EGB_INT | 5-year constant-maturity zero-coupon German Federal bond (`bund_zero_05y.level`) | 1997-08-07 | iShares € Govt Bond 3-7yr UCITS ETF (Acc) (XAMS, EUR) | 0.20 % | USD 1.35 bn, illiquid | German Bunds only, no other euro government's spread (2010–12 is milder than for a euro aggregate); a zero rather than a coupon index of similar duration; a fitted curve |
| EGB_LONG | 17-year constant-maturity zero-coupon German Federal bond (`bund_zero_17y.level`) | 1997-08-07 | iShares € Govt Bond 15-30yr UCITS ETF (XAMS, EUR), modelled as accumulating | 0.20 % | USD 1.01 bn, illiquid | as EGB_INT; a zero's convexity is above a 15–30 year index's |
| IL | TIP adjusted close in USD (`tip.adj_close`), at ECB rates | 2003-12-05 | iShares $ TIPS UCITS ETF (Acc) (XLON, USD) | none (TIP's 0.18–0.19 % is in its price) | not served, illiquid | US inflation and the dollar rather than euro inflation; a US fund's fee above the vehicle's 0.10 % |
| UST | IEF adjusted close in USD (`ief.adj_close`), at ECB rates | 2002-07-30 | iShares $ Treasury Bond 7-10yr UCITS ETF (Acc) (XLON, USD) | none (IEF's 0.15 % in its price) | USD 4.94 bn, illiquid | unhedged dollar exposure (the owner has not ruled on hedged classes, D69); a fee above the vehicle's 0.07 % |
| GOLD | GLD close in USD (`gld.close`), at ECB rates | 2004-11-18 | iShares Physical Gold ETC (XLON, USD), a debt security for tax | none (GLD's 0.40 % in its price) | not served, illiquid | GLD's fee above a UCITS ETC's |
| COMMOD | CRSOX adjusted close in USD (`crsox.adj_close`), at ECB rates | 2004-12-31 | Invesco Bloomberg Commodity UCITS ETF (XLON, USD) | none (the fund's fee in its price) | not served, illiquid | an actively run US mutual fund against the Bloomberg Commodity Index Total Return, its collateral and fee; Yahoo's back-adjustment for distributions |
| CASH | EONIA, then the euro short-term rate, accrued daily ACT/360 (`euro_cash.level`) | 1999-01-04 | Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C (XETR, EUR) | 0.10 % | USD 24.2 bn, liquid | an overnight deposit, not the fund's swap; EONIA was the euro STR plus 8.5 basis points from October 2019 |

Under the `es` profile each vehicle is eligible as a UCITS fund or an ETC (MASTER_PLAN §2); this
test buys none of them and makes no claim about their availability at IBKR (Session 9). No
dividend or coupon is paid by any proxy (each is accumulating or total return), so withholding
at source does not arise.

## Data and their point-in-time status

Every series is **as observed**: an index level or a fund's close is public at the end of its
day, a reference rate on its day, and a derived series when every input it used was. The
Bundesbank's curve is estimated from the day's closing prices and published afterwards; as a
proxy for a bond fund's closing price it is treated as public at that close (D78). Revisions:
MSCI, Yahoo's adjustments and the Bundesbank's estimates are read as published now, without
vintages. Coverage must be complete: every instrument has a price within seven days of every
rebalance date. History before the first rebalance date, where it exists (MSCI from 2000, the
Bundesbank from 1997, the ECB from 1999, the US funds from 2002–2004), feeds only the tilt's
lookbacks; a bootstrap path keeps it as it was. `obliskiq research data coverage` reports the
share of each year without a price.

**Survivorship.** The proxies are indices and funds that exist today. The equity indices
include every company that disappeared (MSCI's own history), but choosing these roles and these
vehicles in 2026 is hindsight about which asset classes became investable as UCITS funds.

**What the simulation does not show** (also in every report): monthly marks understate falls
inside a month; trades fill at the close moved by the cost model; currency conversions are free
and no currency balance is held; the 2026 savings scale is applied to every year from 2005
(Ley 7/2024; the personal and family minimum is not applied, so tax errs high); a year's tax is
paid at the first rebalance on or after 30 June of the next year; losses still carried at the
horizon are worth nothing.

## Primary metric and horizon

For each candidate, the annualised log growth of liquidation value after costs and tax minus the
equity reference's, from 30 December 2005 (the first month end on or after 1 December 2005) to
30 September 2026, about 20.75 years and 249 months. Its interval is the percentile interval of
a paired moving-block bootstrap: 5,000 paths of 12-month blocks of whole months (every proxy and
rate resampled together), seed 20261017, each path simulated for all ten portfolios with its own
tax. Each interval has level 1 − 2 × 0.05 / 9 = 0.9889.

Beside the tests: every portfolio's metrics before and after tax and year by year; the
probability that a path's mark-to-market drawdown reaches 10, 20, 30, 40 and 50 %; the median
after-tax log growth and the 5th percentile of terminal wealth; the distance of each median from
the owner's stated aspiration of 30 % and 50 % a year (as ln 1.3 and ln 1.5), reported and never
a gate (D60).

**The frontier.** For each drawdown limit (10, 20, 30, 40, 50 %) and each largest acceptable
probability of breaching it (5, 10, 25, 50 %) — a grid that runs from an owner who will rarely
accept a 10 % fall to one who accepts an even chance of a 50 % fall — the portfolio with the
highest median after-tax log growth among those whose share of paths reaching the limit is at
most the probability, references included. Portfolios whose paired growth difference from that
one has an interval holding zero, at the same level, cannot be told apart from it; the point
resolves to the simplest of them (the ranks above), then the lowest turnover. Each point is
reported with its portfolio's outcome against the equity reference. Choosing a point is the
owner's authorisation, recorded in DECISIONS afterwards; this test does not choose (D60).

## Theta and why

θ = 1 % a year of after-tax log growth over the equity reference: below it the difference is
within the proxies' own uncertainty (index against fund, fees, tracking and spreads of a few
tenths of a percent a year each) and does not pay for a managed core's complexity, turnover and
model risk; over twenty years it is about 22 % more terminal wealth. A variant that grows less
than equities can still be the owner's choice on the frontier, for its lower drawdown; θ asks
only whether a variant also out-grows them.

## Power

Before any data, each candidate's monthly standard deviation of the growth difference is taken
from the declared covariance: the tracking error of its untilted weights against WORLD, and for a
tilted one the tilt's own added in quadrature, (1 − floor) × the untilted volatility × √0.3,
with 0.3 an assumed share of months in a negative trend; monthly is the yearly figure over √12.
`obliskiq research power` on this file prints (α 0.05, nine tests, 249 independent months):

| Candidate | sd a month | se a year | Power to detect θ | Effect detectable at 0.8 |
|---|---|---|---|---|
| 60/40 | 1.79 % | 1.36 % | 0.036 | 4.60 % a year |
| A | 3.51 % | 2.67 % | 0.015 | 9.02 % |
| B | 3.59 % | 2.73 % | 0.015 | 9.23 % |
| C | 3.07 % | 2.33 % | 0.017 | 7.89 % |
| D10 | 3.48 % | 2.65 % | 0.015 | 8.95 % |
| D20 | 2.69 % | 2.05 % | 0.020 | 6.92 % |
| D30 | 2.03 % | 1.54 % | 0.029 | 5.22 % |
| D40 | 1.67 % | 1.27 % | 0.040 | 4.29 % |
| D50 | 1.78 % | 1.35 % | 0.036 | 4.58 % |

The test has almost no power to detect θ: twenty years of one portfolio against another can
show only a difference of four to nine points a year. A sufficient outcome is therefore unlikely
even if an effect of θ exists, and an inconclusive one under this power is no evidence against
it. The frontier, which compares distributions of growth and drawdown rather than testing θ, is
the test's main product for the owner's choice; the outcomes say which candidates are shown to
grow less than the reference (insufficient, negative), and whether any is shown to grow more.

## Adjustment

Bonferroni across the nine candidates; the deflated Sharpe across every trial the registry holds
for this id, the nine candidates of this run included; the frontier's pairwise comparisons at
the confirmation level.

## Robustness checks

- The sign of each candidate's effect in each half: 1 December 2005 to 31 December 2015 and
  1 January 2016 to 30 September 2026.
- The break-even cost multiplier — the factor on every leg's cost at which the effect falls to
  θ — against a declared cost uncertainty of 3.67: the D63 default, the next friction tier up,
  as the ratio of the illiquid tier's cost on a leg to the liquid tier's
  ((0.1 + 0.5 + 0.5) / (0.1 + 0.1 + 0.1) percent). The friction tiers are labelled assumptions,
  and the illiquid tier applied to the bond, gold and commodity vehicles is far above their
  quoted spreads, so the check is severe.
- No placebo: a portfolio comparison has no signal to lag.
- **The structure check** (descriptive, before tax, never an outcome and outside the Bonferroni
  count): the tilt with these constants on the Ken French US market (the excess return plus the
  bill) against the bill, monthly from July 1926 to August 2026 (its first evaluation a year
  later, once the lookback is full), rebalanced to its scale each month at 0.3 % a leg on both
  legs, against holding the market. Reported: log growth, maximum
  drawdown and the probability of reaching each drawdown limit for both arms; the difference in
  log growth with a block-bootstrap interval of the realised returns (same paths, blocks and
  seed); the same in four subperiods (1926–49, 1950–79, 1980–2004, 2005–26). It is the longer
  pre-tax history the trend overlay's structure has; tax cannot be simulated there (no EUR rates
  before 1999 and other law).

## Trial budget

Zero exploratory trials: every constant above comes from the literature, the plan or a stated
assumption, and no specification was tried on the data. The confirmation run is single-shot
once it has an outcome (D76); only a run whose data are not evaluable may be repeated after the
data are fixed.

## What a sufficient, insufficient or inconclusive outcome would lead to (D62)

- **Sufficient** (L ≥ θ): the candidate out-grew the equity reference after tax by at least θ;
  it goes to shadow and can earn the core's budget when the owner chooses its point on the
  frontier (Session 13).
- **Insufficient** (U < θ), labelled negative when U < 0: an out-growth of θ or more is ruled
  out; the candidate can still be the owner's choice for its drawdown, with the growth it gives up
  stated. It is not retired as a core design, because the core's purpose includes drawdown.
- **Inconclusive** (L < θ ≤ U): neither is shown; under this power that is the expected outcome
  for most candidates and is never read as evidence against. The frontier decides the owner's
  choice.
- **Not evaluable**: data work, then the run again.

Whatever the outcomes, this test cannot conclude that a portfolio will keep its place after the
sample, how any behaves in a crisis unlike the sample's (2008, 2011, 2020, 2022 are the episodes
it holds; the structure check adds 1929–32, 1973–74, 1987 and 2000–02, before tax and in the US
only), or anything about costs, taxes or markets outside it.

## Reproduce

```
obliskiq research data load ecb_fx ff3_market msci_world_eur msci_em_eur ecb_eonia ecb_estr \
  bbk_bund_04y bbk_bund_05y bbk_bund_16y bbk_bund_17y euro_cash bund_zero_05y bund_zero_17y \
  tip ief gld crsox
obliskiq research data coverage --prereg research/preregistrations/core-allocation.md
obliskiq research run research/preregistrations/core-allocation.md
```

The run writes `research/reports/core-allocation.md` with the inputs' hashes and the seed.

## Constants hash

The SHA-256 of the front matter's values as canonical JSON (sorted keys, no spaces, dates and
decimals as written):
`ef39cb4f1db9bde1a806f4cce1160dcdb16e7e29af54de17f6d93986c36a7b9f`. The file's own SHA-256 and
the commit that merged it are in `core-allocation.lock`, written after the merge with
`obliskiq research lock`.
