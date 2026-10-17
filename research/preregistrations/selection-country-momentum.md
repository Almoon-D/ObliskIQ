---
id: selection-country-momentum
title: Country momentum through UCITS, with and without a volatility-scaled stop, after Spanish tax
kind: cross_section
mechanism: >-
  Momentum across national equity markets: a market that rose over the past year tends to keep
  rising over the next month. Investors under-react to news that moves a whole economy, and
  capital moves between countries slowly (benchmark weights, home bias, mandates rebalanced by
  calendar), so country trends persist; the other side is the investor who holds fixed country
  weights or rebalances toward the laggards. A long-only investor captures the winners' side by
  holding the markets with the strongest past year. A protective stop adds value only when
  returns are serially correlated after a loss, as they are in momentum regimes; under a random
  walk it costs its trades and the tax it realises early (H5).
citation: >-
  Asness, Moskowitz and Pedersen (2013), Value and Momentum Everywhere, Journal of Finance 68(3);
  Chan, Hameed and Tong (2000), Profitability of Momentum Strategies in the International Equity
  Markets, Journal of Financial and Quantitative Analysis 35(2); Bhojraj and Swaminathan (2006),
  Macromomentum, Journal of Business 79(1); Kaminski and Lo (2014), When Do Stop-Loss Rules Stop
  Losses?, Journal of Financial Markets 18.
universe: >-
  The countries in MSCI ACWI (developed and emerging) on each month end, from MSCI's market
  classification history: a change takes effect at the first month end after the review month
  that made it, for joiners and leavers alike. Each country is its MSCI standard index held
  through a representative UCITS ETF on Xetra in EUR. Venezuela (to May 2006) and Russia (to
  March 2022) are members MSCI's endpoint does not serve: they are named without a series and
  count as lost to survivorship, measured on every date. Not every country had a UCITS ETF at
  every date; the test holds it as if it had (a stated bias).
data_status: >-
  MSCI end-of-day index levels, net total return in EUR, as observed at the end of each
  calculation day and restated without vintages; from 29 December 2000 for 47 countries, 1 June
  2005 for Qatar, the United Arab Emirates and Kuwait, 1 September 2014 for Saudi Arabia (each
  series starting before its country joined MSCI ACWI, so every member is priced from its first
  month as a member, with the twelve months its signal reads).
  The vehicle's yearly cost of 0.5 % is deducted from each index day by day.
primary_metric: top_group_excess
alpha: null
target_power: null
tests_in_confirmation: 2
adjustment: >-
  Bonferroni across the two tests (H1 and H5), each interval at level 1 - 2 x 0.05 / 2 = 0.95;
  the deflated Sharpe across every trial the registry holds for this id, the exploratory ones
  included.
trial_budget: 4
study:
  development:
    start: 2001-12-01
    end: 2007-12-31
  confirmation:
    start: 2008-01-01
    end: 2026-09-30
  calendar: USA
  initial_capital: "100000"
  fx_dataset: null
  vehicle:
    currency: EUR
    venue: XETR
    asset_class: etf
    market_cap_usd: null
    expense_ratio: "0.005"
    kind: etf
    issuer: representative UCITS ETF issuer
  members:
    - key: AUSTRALIA
      name: MSCI Australia
      price_series: msci_australia_eur.level
      spans: [[2001-01-01, null]]
    - key: AUSTRIA
      name: MSCI Austria
      price_series: msci_austria_eur.level
      spans: [[2001-01-01, null]]
    - key: BELGIUM
      name: MSCI Belgium
      price_series: msci_belgium_eur.level
      spans: [[2001-01-01, null]]
    - key: CANADA
      name: MSCI Canada
      price_series: msci_canada_eur.level
      spans: [[2001-01-01, null]]
    - key: DENMARK
      name: MSCI Denmark
      price_series: msci_denmark_eur.level
      spans: [[2001-01-01, null]]
    - key: FINLAND
      name: MSCI Finland
      price_series: msci_finland_eur.level
      spans: [[2001-01-01, null]]
    - key: FRANCE
      name: MSCI France
      price_series: msci_france_eur.level
      spans: [[2001-01-01, null]]
    - key: GERMANY
      name: MSCI Germany
      price_series: msci_germany_eur.level
      spans: [[2001-01-01, null]]
    - key: HONG_KONG
      name: MSCI Hong Kong
      price_series: msci_hong_kong_eur.level
      spans: [[2001-01-01, null]]
    - key: IRELAND
      name: MSCI Ireland
      price_series: msci_ireland_eur.level
      spans: [[2001-01-01, null]]
    - key: ISRAEL
      name: MSCI Israel
      price_series: msci_israel_eur.level
      spans: [[2001-01-01, null]]
    - key: ITALY
      name: MSCI Italy
      price_series: msci_italy_eur.level
      spans: [[2001-01-01, null]]
    - key: JAPAN
      name: MSCI Japan
      price_series: msci_japan_eur.level
      spans: [[2001-01-01, null]]
    - key: NETHERLANDS
      name: MSCI Netherlands
      price_series: msci_netherlands_eur.level
      spans: [[2001-01-01, null]]
    - key: NEW_ZEALAND
      name: MSCI New Zealand
      price_series: msci_new_zealand_eur.level
      spans: [[2001-01-01, null]]
    - key: NORWAY
      name: MSCI Norway
      price_series: msci_norway_eur.level
      spans: [[2001-01-01, null]]
    - key: PORTUGAL
      name: MSCI Portugal
      price_series: msci_portugal_eur.level
      spans: [[2001-01-01, null]]
    - key: SINGAPORE
      name: MSCI Singapore
      price_series: msci_singapore_eur.level
      spans: [[2001-01-01, null]]
    - key: SPAIN
      name: MSCI Spain
      price_series: msci_spain_eur.level
      spans: [[2001-01-01, null]]
    - key: SWEDEN
      name: MSCI Sweden
      price_series: msci_sweden_eur.level
      spans: [[2001-01-01, null]]
    - key: SWITZERLAND
      name: MSCI Switzerland
      price_series: msci_switzerland_eur.level
      spans: [[2001-01-01, null]]
    - key: UNITED_KINGDOM
      name: MSCI United Kingdom
      price_series: msci_united_kingdom_eur.level
      spans: [[2001-01-01, null]]
    - key: USA
      name: MSCI USA
      price_series: msci_usa_eur.level
      spans: [[2001-01-01, null]]
    - key: BRAZIL
      name: MSCI Brazil
      price_series: msci_brazil_eur.level
      spans: [[2001-01-01, null]]
    - key: CHILE
      name: MSCI Chile
      price_series: msci_chile_eur.level
      spans: [[2001-01-01, null]]
    - key: CHINA
      name: MSCI China
      price_series: msci_china_eur.level
      spans: [[2001-01-01, null]]
    - key: COLOMBIA
      name: MSCI Colombia
      price_series: msci_colombia_eur.level
      spans: [[2001-01-01, null]]
    - key: CZECH_REPUBLIC
      name: MSCI Czech Republic
      price_series: msci_czech_republic_eur.level
      spans: [[2001-01-01, null]]
    - key: EGYPT
      name: MSCI Egypt
      price_series: msci_egypt_eur.level
      spans: [[2001-01-01, null]]
    - key: GREECE
      name: MSCI Greece
      price_series: msci_greece_eur.level
      spans: [[2001-01-01, null]]
    - key: HUNGARY
      name: MSCI Hungary
      price_series: msci_hungary_eur.level
      spans: [[2001-01-01, null]]
    - key: INDIA
      name: MSCI India
      price_series: msci_india_eur.level
      spans: [[2001-01-01, null]]
    - key: INDONESIA
      name: MSCI Indonesia
      price_series: msci_indonesia_eur.level
      spans: [[2001-01-01, null]]
    - key: KOREA
      name: MSCI Korea
      price_series: msci_korea_eur.level
      spans: [[2001-01-01, null]]
    - key: MALAYSIA
      name: MSCI Malaysia
      price_series: msci_malaysia_eur.level
      spans: [[2001-01-01, null]]
    - key: MEXICO
      name: MSCI Mexico
      price_series: msci_mexico_eur.level
      spans: [[2001-01-01, null]]
    - key: PERU
      name: MSCI Peru
      price_series: msci_peru_eur.level
      spans: [[2001-01-01, null]]
    - key: PHILIPPINES
      name: MSCI Philippines
      price_series: msci_philippines_eur.level
      spans: [[2001-01-01, null]]
    - key: POLAND
      name: MSCI Poland
      price_series: msci_poland_eur.level
      spans: [[2001-01-01, null]]
    - key: SOUTH_AFRICA
      name: MSCI South Africa
      price_series: msci_south_africa_eur.level
      spans: [[2001-01-01, null]]
    - key: TAIWAN
      name: MSCI Taiwan
      price_series: msci_taiwan_eur.level
      spans: [[2001-01-01, null]]
    - key: THAILAND
      name: MSCI Thailand
      price_series: msci_thailand_eur.level
      spans: [[2001-01-01, null]]
    - key: TURKEY
      name: MSCI Turkey
      price_series: msci_turkey_eur.level
      spans: [[2001-01-01, null]]
    - key: QATAR
      name: MSCI Qatar
      price_series: msci_qatar_eur.level
      spans: [[2014-06-01, null]]
    - key: UNITED_ARAB_EMIRATES
      name: MSCI United Arab Emirates
      price_series: msci_united_arab_emirates_eur.level
      spans: [[2014-06-01, null]]
    - key: SAUDI_ARABIA
      name: MSCI Saudi Arabia
      price_series: msci_saudi_arabia_eur.level
      spans: [[2019-06-01, null]]
    - key: KUWAIT
      name: MSCI Kuwait
      price_series: msci_kuwait_eur.level
      spans: [[2020-12-01, null]]
    - key: ARGENTINA
      name: MSCI Argentina
      price_series: msci_argentina_eur.level
      spans: [[2001-01-01, 2009-05-31], [2019-06-01, 2021-11-30]]
    - key: PAKISTAN
      name: MSCI Pakistan
      price_series: msci_pakistan_eur.level
      spans: [[2001-01-01, 2008-12-31], [2017-06-01, 2021-11-30]]
    - key: JORDAN
      name: MSCI Jordan
      price_series: msci_jordan_eur.level
      spans: [[2001-01-01, 2008-11-30]]
    - key: MOROCCO
      name: MSCI Morocco
      price_series: msci_morocco_eur.level
      spans: [[2001-01-01, 2013-11-30]]
    - key: VENEZUELA
      name: MSCI Venezuela
      price_series: null
      spans: [[2001-01-01, 2006-05-31]]
    - key: RUSSIA
      name: MSCI Russia
      price_series: null
      spans: [[2001-01-01, 2022-03-31]]
  signal:
    lookback_months: 12
    skip_months: 1
  selection:
    enter_fraction: "0.3333"
    keep_fraction: "0.5"
    reset_month: 12
  stop:
    volatility_sessions: 63
    horizon_sessions: 21
    multiple: "2"
  portfolios:
    universe:
      rule: universe
      lag_months: 0
      stop: false
    momentum:
      rule: top_group
      lag_months: 0
      stop: false
    momentum_stop:
      rule: top_group
      lag_months: 0
      stop: true
    placebo:
      rule: top_group
      lag_months: 12
      stop: false
tests:
  - name: H1 country momentum
    candidate: momentum
    against: universe
    theta: "0.02"
    theta_justification: >-
      two points a year of after-tax log growth over holding every member equally: the top group
      adds tracking error of several points a year, a turnover of several times a year and model
      risk, and below two points the excess is inside the uncertainty of the cost model itself
      (its two friction tiers differ by 0.8 % of value a trade, 1.6 % a round trip)
    sd_per_period: "0.015"
  - name: H5 volatility-scaled stop
    candidate: momentum_stop
    against: momentum
    theta: "0.01"
    theta_justification: >-
      one point a year of after-tax log growth over the same top group without the stop: a stop
      adds resting orders, trades and tax realised early, and below one point it does not pay for
      them
    sd_per_period: "0.008"
placebo:
  candidate: placebo
  against: universe
  test: H1 country momentum
bootstrap:
  n_boot: 10000
  block_months: 12
  seed: 20261017
newey_west_lags: 12
periods_per_year: 12
subperiods:
  - [2008-01-01, 2016-12-31]
  - [2017-01-01, 2026-09-30]
cost_uncertainty_multiplier: "2"
robustness: >-
  The sign of each test's effect in both subperiods; the break-even cost multiplier above 2 (the
  friction tiers are labelled assumptions, and the illiquid tier these vehicles take is the
  highest, so the declared uncertainty is a doubling of costs); the placebo, the same top group on
  the signal of twelve months earlier (momentum from 24 to 13 months back), which must not reach
  H1's theta against the universe.
adequacy:
  min_coverage: "0.99"
  max_lost_share: "0.05"
  point_in_time: [as_observed]
  min_effective_observations: 200
  min_development_observations: 60
---

# Country momentum through UCITS, with and without a volatility-scaled stop, after Spanish tax

Session 4d's selection hypothesis, chosen by the data audit
(`research/reports/selection-data-audit.md`, DECISIONS D84): H1, momentum across national equity
markets held through UCITS ETFs, with H5, whether a volatility-scaled stop adds value to it, as
the registered secondary test. Each section explains the matching front-matter field. Not tax
advice.

## Mechanism and citation

A market that rose over the past year tends to keep rising over the next month (Asness,
Moskowitz and Pedersen 2013; Chan, Hameed and Tong 2000; Bhojraj and Swaminathan 2006). News
that moves a whole economy is absorbed slowly, and capital moves between countries slowly:
benchmark weights, home bias and mandates rebalanced by the calendar keep it where it was. The
other side of the trade is the investor who holds fixed country weights or rebalances toward the
laggards. A long-only investor captures the winners' side by holding the markets with the
strongest past year and cannot short the losers (the mandate), so the effect tested is the top
group's excess over the whole universe, not a long-short spread.

A protective stop sells a position after a loss. Kaminski and Lo (2014) show it adds expected
return only when returns are serially correlated after a loss, as in momentum regimes; under a
random walk it costs the trades and, for a Spanish resident, the tax it realises early. A stop
scaled to each market's volatility adapts the exit to the market's own noise (H5).

## Universe and survivorship

The universe on each month end is the countries in MSCI ACWI then, developed and emerging, from
MSCI's market classification history (its "Market Classification" page, read 2026-10-17, and its
annual reviews): Qatar and the United Arab Emirates from May 2014, Saudi Arabia from May 2019,
Kuwait from November 2020; Argentina until May 2009 and from May 2019 to November 2021; Pakistan
until December 2008 and from May 2017 to November 2021; Jordan until November 2008; Morocco until
November 2013; Venezuela until May 2006; Russia until March 2022; every other current member
throughout. A change takes effect at the first month end after the month of the review that made
it, for joiners and leavers alike, so the universe on a date uses only what was announced well
before it. 49 countries in 2001, 44 to 50 since, 53 in all.

Each country is its MSCI standard (large and mid cap) index. Venezuela and Russia are named with
no series: MSCI's endpoint does not serve them. They count as lost on every date they are
members: at most 4.1 % of the members (2001–2005), about 2 % to March 2022, none after. Russia's
exit in March 2022, at a price close to zero, is the one large loss the test cannot see; the
report states the share. A member is held through a representative UCITS ETF on Xetra; not every
country had, or has, such a fund at every date, and the test holds it as if it had.

## Data and their point-in-time status

MSCI end-of-day index levels, net total return in EUR, as observed at the end of each
calculation day (weekdays, holidays included) and restated without vintages: from 29 December
2000 for 47 countries, 1 June 2005 for Qatar, the United Arab Emirates and Kuwait, 1 September
2014 for Saudi Arabia, each before the country joined. Datasets `msci_<country>_eur` in
`config/defaults/research.yaml`. Each index loses the vehicle's yearly cost of 0.5 % day by day,
a round figure between the 0.1–0.3 % of the large developed-market funds and the 0.65–0.74 % of
single emerging-market funds. Dividends are net of the withholding MSCI applies; a fund's
withholding differs by domicile and treaty.

## The signal, the portfolios and the horizon

- **Signal.** On each month end, each member's return from the close twelve month ends back to
  the close one month end back (12-1 momentum), on month-end closes in EUR. A member with less
  history is not ranked.
- **Top group** (`momentum`). Of the n ranked members, the group holds round(n / 3) (halves up).
  A name enters within the top third and is kept while within the top half. Held names inside the
  keep rank stay, best first; the best-ranked others fill the group. Equal weights at entry; a
  leaver is sold in full and the entrants share equally what the continuing holdings leave;
  nothing else trades until the December month end, when every holding returns to equal weight.
- **Universe** (`universe`). Every member with a price, equally weighted, trading only on
  membership changes and at the December reset.
- **Stop** (`momentum_stop`, H5). The same top group; each position also carries a trailing stop
  two standard deviations of its last 63 daily log returns, scaled to 21 sessions (about two
  monthly standard deviations), below its highest close since entry, fixed at each month end and
  watched on every daily close. The whole position is sold at the first close at or below it, so a
  gap through the stop is borne in full; the cash waits for the next month end, where the name
  re-enters if the signal still selects it.
- **Horizon.** One month: the signal is read again at every month end.
- **Costs** (`quant/costs.py`): IBKR's fixed commission on Xetra (0.10 %, at least EUR 4) and the
  friction model's illiquid tier (no fund size is declared, and most country UCITS ETFs hold far
  less than its USD 10 billion threshold): labelled assumptions (D63).
- **Tax** (`es_irpf`): FIFO lots, the two-month rule, offsets and carry-forward, each year's tax
  paid at its payment date, everything sold and taxed at the horizon. Each country's fund is its
  own security (another index), so selling one and buying another defers no loss.
- **Capital.** EUR 100,000: about EUR 6,000 a position in the top group, EUR 2,000 in the
  universe.

## Primary metric

`top_group_excess`: each account's value month by month, from the initial capital to the
liquidation value at the horizon; the candidate's monthly log return minus the other's is the
Fama–MacBeth series of its excess return after costs and tax (D85). The effect is the series'
mean times 12, its sum the difference in log growth. H1 compares `momentum` with `universe`; H5
compares `momentum_stop` with `momentum`.

## Theta and why

- **H1, 2 % a year.** The top group adds tracking error of several points a year against the
  universe, turns over several times a year and carries model risk. Below two points the excess
  is inside the cost model's own uncertainty: its two friction tiers differ by 0.8 % of value a
  trade (1.6 % a round trip), on that turnover. Over the confirmation sample's 18.7 years, 2 % a year is about 45 %
  more terminal wealth.
- **H5, 1 % a year.** A stop needs resting orders and monitoring, adds trades and realises tax
  early. Below one point a year over the same group it does not pay for them.

## Power

`obliskiq research power` before any data:

```
power of selection-country-momentum before any data
  confirmation months     224 (one observation a month)
  alpha 0.05, 2 test(s) in the confirmation run
  test                               theta  se a year   power  detectable at 0.8
  H1 country momentum                2.00%    1.2027%   0.383            3.3694%
  H5 volatility-scaled stop          1.00%    0.6414%   0.344            1.7970%
```

The standard deviations per month are declared, not estimated from returns of any signal. A
random third of the members against all of them moves by 1.09 % a month (the audit, 2,000
draws): the floor for a top group without the signal's own tilt; H1 declares 1.5 %. H5's two
portfolios differ only when a stop fires; it declares 0.8 %. Both tests have low power at their
theta: an inconclusive outcome is the likely one and would not be evidence against either.

## Development and confirmation samples, and the trial budget

- **Development**: month ends from December 2001 to December 2007 (72 months), the first with a
  full year of history. Exploration (`--explore`) runs here; each exploratory run counts its two
  tests as trials; the budget is 4.
- **Confirmation**: month ends from January 2008 to September 2026 (224 months), read as of
  30 September 2026, run once.
- Exploration may not change anything above. A run that shows a defect of the harness is handled
  as D83 says.

## Adjustment

Bonferroni across the two tests: each interval at level 0.95. The deflated Sharpe of each
test's monthly series is taken across every trial the registry holds for this id, the
exploratory ones included.

## Robustness checks and the placebo

- The sign of each test's effect in 2008–2016 and 2017–2026.
- The break-even cost multiplier above 2. The illiquid tier is the model's highest, so the
  declared uncertainty is a doubling of costs (D63).
- **Placebo**: the same top group ranked on the signal of twelve months earlier (momentum from
  24 to 13 months back), against the universe. It must not reach H1's theta: its interval's
  lower end stays below 2 %. (In the development sample it holds the universe until its signal
  has history, in December 2002.)
- Each test's estimate before tax, reported and not tested.

## Adequacy checks (D62)

- Served member-dates with a fresh price: at least 99 %.
- Members not served: at most 5 % on every date.
- Every series as observed.
- At least 200 months in the confirmation sample and 60 in the development sample.

Failing any makes the outcome not evaluable.

## What each outcome would lead to (D62, D65)

| Outcome | H1 country momentum | H5 the stop |
|---|---|---|
| Sufficient | `shadow` in `research/pattern_registry.yaml`: the top group runs at zero weight from Session 15, graded each month by its after-cost excess over the universe on the same members (its twin, §5.19); an exploration budget needs its activation condition and the owner (D65, D66). | The stop is part of the expression of this signal (§5.11, Session 17). |
| Inconclusive, positive | `shadow` at zero weight; the forward months the twin computes on the same members and rules are appended to this confirmation series, and the same interval and rule are applied after every further 36 months (D65's pooled re-test). | The stop runs in shadow beside the top group, pooled the same way. |
| Inconclusive, sign uncertain | `tested`; the signal is collected forward (it costs one monthly read of the indices). | `tested`; no stop for this signal on growth grounds. |
| Insufficient | `retired` with this report. | `retired`: protective exits stay a risk control (D67), not an edge, for this signal. |
| Not evaluable | `idea` with the data it needs. | `idea` with the data it needs. |

## Constants hash

The SHA-256 of the front matter as canonical JSON (sorted keys, no spaces):
`d5f8d489470779b47b121fd1ef08b664f0a9da7a73557dc67a2a5101216d74b9`.
