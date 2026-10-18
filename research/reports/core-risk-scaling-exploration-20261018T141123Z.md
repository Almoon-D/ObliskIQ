# Scaling the core's total risk to a target volatility, against static A, after Spanish tax

**EXPLORATORY — never evidence (DECISIONS D64).** Study `core-risk-scaling` (portfolio_comparison).

## Inputs

- Pre-registration: `core-risk-scaling.md`, SHA-256 `67d17bec84d5059b36f54b9b7bbc632a1419ab10786f42f2fa055dfba3a688c6`
- Lock: none (exploratory)
- Run at: 2026-10-18T14:11:23Z
- Inputs SHA-256: `e3bac4947a4a3545e66dc4f9438567b71e6362c62d861fbe604d6f7df91e3b0a`
- Bootstrap: 5000 paths of 24-month blocks, seed 20261018
- alpha 0.05 (config research.decision), 6 test(s) in the confirmation run (Bonferroni), each interval at level 0.9833; target power 0.8 (config research.decision)
- Development window: 2001-01-01 to 2004-11-30; the comparisons' portfolios only, each comparison one exploratory trial
- Marks: daily closes
- Horizon: 2001-01-01 to 2004-11-30, 47 rebalance dates (2001-01-31 to 2004-11-30); initial capital 100,000.00

## Outcomes

Each candidate's primary effect is log_growth_difference: its annualised log growth of liquidation value after costs and tax minus A's. theta 1.00%: one point a year of after-tax log growth over static A, the owner's neutral allocation (D91): below it the difference is within the proxies' own uncertainty (index against fund, fees, tracking and spreads of a few tenths of a percent a year each) and does not pay for an estimator, more equity and more turnover than A; over twenty years it is about 22 % more terminal wealth

None in this run: an exploration on the development window tests the comparisons only.

## Comparisons

Secondary tests, in the same Bonferroni count: each portfolio's annualised log growth of liquidation value after costs and tax minus the other's, paired on the same paths.
- E-fast against E-declared: theta 0.50%, half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth
- E-blend against E-declared: theta 0.50%, half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth
- B-fast against B-declared: theta 0.50%, half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth

| Comparison | Candidate minus | theta | Outcome | Estimate / yr | Interval | Power to detect theta | Detectable at target power | Before data: power / detectable |
|---|---|---|---|---|---|---|---|---|
| E-fast against E-declared | E-fast minus E-declared | 0.50% | **inconclusive** (sign uncertain, low power) | -0.66% | [-1.18%, 1.10%] | 0.095 | 1.50% | 0.085 / 1.59% |
| E-blend against E-declared | E-blend minus E-declared | 0.50% | **inconclusive** (sign uncertain, low power) | -0.08% | [-1.38%, 2.68%] | 0.034 | 2.83% | 0.085 / 1.59% |
| B-fast against B-declared | B-fast minus B-declared | 0.50% | **inconclusive** (sign uncertain, low power) | -0.67% | [-1.08%, 0.55%] | 0.166 | 1.13% | 0.239 / 0.96% |

## The frontier

Not computed in an exploration on the development window.

## What this test cannot conclude

- Whether the effect reaches theta: the interval holds values on both sides of it.
- Anything against the effect: the test had low power to detect theta, so an inconclusive result is not evidence that the effect is absent.
- That a portfolio will keep its place after the sample: past growth after tax is no promise of future superiority.
- How a portfolio behaves in a crisis unlike the episodes in the sample; the bootstrap reshuffles the sample's own months and cannot create a new kind of crisis.
- Anything about a period, market or cost regime outside this sample.

## Adequacy checks

| Check | Passed | Detail |
|---|---|---|
| data loaded | yes | every series is in the research store |
| point in time | yes | every series is as_observed |
| coverage | yes | 100.0% of 47 rebalance dates fully priced (pre-registered minimum 1) |

## Portfolios after tax

| Portfolio | Liquidation value | Log growth / yr | Max drawdown (daily closes) | Costs | Tax paid | Trades | Turnover / yr | Median bootstrap growth | P5 terminal wealth multiple | Median minus ln(1 + 30%) | Median minus ln(1 + 50%) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| E-fast | 85,159.14 | -4.19% | -28.81% | 1,346.63 | 0.00 | 49 | 132.05% | -4.99% | 0.611 | -31.23% | -45.54% |
| E-blend | 87,081.76 | -3.61% | -25.51% | 972.19 | 0.00 | 32 | 93.15% | -4.68% | 0.658 | -30.92% | -45.23% |
| B-fast | 85,010.74 | -4.24% | -28.22% | 3,591.33 | 0.00 | 90 | 185.88% | -4.78% | 0.617 | -31.02% | -45.33% |
| E-declared | 87,334.19 | -3.54% | -29.15% | 726.78 | 81.00 | 22 | 70.48% | -5.18% | 0.611 | -31.42% | -45.73% |
| B-declared | 87,207.00 | -3.57% | -28.77% | 1,360.56 | 144.44 | 12 | 65.20% | -4.71% | 0.622 | -30.94% | -45.25% |

The owner's stated aspiration (DECISIONS D60) is reported as a distance from each portfolio's median after-tax log growth, never as a gate.

Sales not made because their costs would exceed what they raise (the units stay held, and count as nothing at the horizon): A n/a on the historical path, on n/a bootstrap paths; E-fast 0 on the historical path, on 0 bootstrap paths; E-blend 0 on the historical path, on 0 bootstrap paths; B-fast 0 on the historical path, on 0 bootstrap paths; E-declared 0 on the historical path, on 0 bootstrap paths; B-declared 0 on the historical path, on 0 bootstrap paths.

Probability that a bootstrap path's mark-to-market drawdown reaches each limit within 20 years:

| Portfolio | 10% | 20% | 30% | 40% | 50% |
|---|---|---|---|---|---|
| A | n/a | n/a | n/a | n/a | n/a |
| E-fast | 100.0% | 91.9% | 46.8% | 14.3% | 0.0% |
| E-blend | 100.0% | 90.4% | 37.1% | 2.5% | 0.0% |
| B-fast | 100.0% | 88.7% | 37.5% | 11.7% | 0.0% |
| E-declared | 100.0% | 93.1% | 54.7% | 18.1% | 0.0% |
| B-declared | 100.0% | 91.9% | 45.4% | 14.6% | 0.0% |

## Portfolios before tax

The same strategies, trading the same way, with no tax withheld, settled or paid.

| Portfolio | Liquidation value | Log growth / yr | Max drawdown (month ends) | Costs | Tax paid | Trades |
|---|---|---|---|---|---|---|
| A | n/a | n/a | n/a | n/a | n/a | n/a |
| E-fast | 85,159.14 | -4.19% | -26.43% | 1,346.63 | 0.00 | 49 |
| E-blend | 87,081.76 | -3.61% | -23.41% | 972.19 | 0.00 | 32 |
| B-fast | 85,010.74 | -4.24% | -26.26% | 3,591.33 | 0.00 | 90 |
| E-declared | 87,431.84 | -3.51% | -26.21% | 723.02 | 0.00 | 21 |
| B-declared | 87,393.32 | -3.52% | -25.77% | 1,353.45 | 0.00 | 10 |

## Static weights

Each core strategy's weights by role before the tilt (a total-risk strategy's at its declared share).

| Role | A | E-fast | E-blend | B-fast | E-declared | B-declared |
|---|---|---|---|---|---|---|
| WORLD | 9.03% | 52.94% | 52.94% | 60.00% | 52.94% | 60.00% |
| EM | 6.52% | 5.88% | 5.88% | n/a | 5.88% | n/a |
| COMMOD | 10.82% | n/a | n/a | n/a | n/a | n/a |
| EGB_INT | 33.80% | n/a | n/a | 40.00% | n/a | 40.00% |
| EGB_LONG | 11.27% | n/a | n/a | n/a | n/a | n/a |
| UST | 10.90% | n/a | n/a | n/a | n/a | n/a |
| IL | 12.30% | n/a | n/a | n/a | n/a | n/a |
| GOLD | 5.36% | n/a | n/a | n/a | n/a | n/a |
| CASH | n/a | 41.18% | 41.18% | 0.00% | 41.18% | 0.00% |

## Year by year

Mark-to-market value, after tax paid as it arose (the last year ends at the horizon before the terminal sale and its tax):

| Year | A | E-fast | E-blend | B-fast | E-declared | B-declared |
|---|---|---|---|---|---|---|
| 2001 | n/a | -8.84% | -7.01% | -10.07% | -6.33% | -7.59% |
| 2002 | n/a | -16.29% | -15.07% | -15.43% | -17.55% | -16.60% |
| 2003 | n/a | 7.13% | 6.36% | 6.51% | 9.00% | 7.84% |
| 2004 | n/a | 4.49% | 3.99% | 5.60% | 4.05% | 5.57% |

Before tax:

| Year | A | E-fast | E-blend | B-fast | E-declared | B-declared |
|---|---|---|---|---|---|---|
| 2001 | n/a | -8.84% | -7.01% | -10.07% | -6.33% | -7.59% |
| 2002 | n/a | -16.29% | -15.07% | -15.43% | -17.52% | -16.58% |
| 2003 | n/a | 7.13% | 6.36% | 6.51% | 9.00% | 8.04% |
| 2004 | n/a | 4.49% | 3.99% | 5.60% | 4.13% | 5.58% |

Tax paid, by the year it was paid (the horizon's includes the terminal sale's):

| Paid in | E-fast | E-blend | B-fast | E-declared | B-declared |
|---|---|---|---|---|---|
| 2002 | n/a | n/a | n/a | 29.67 | 17.38 |
| 2003 | n/a | n/a | n/a | n/a | 127.06 |
| 2004 | n/a | n/a | n/a | 51.33 | n/a |

## Robustness

- E-fast against E-declared: not run (the outcome is not evaluable).
- E-blend against E-declared: not run (the outcome is not evaluable).
- B-fast against B-declared: not run (the outcome is not evaluable).

- Placebo: not run

## Forecast check

A check that each estimator works, **never an outcome** (DECISIONS D92): its forecast of the base's next-month variance from each rebalance date, against the variance of the base's daily returns over that month, scored by QLIKE (r/f - ln(r/f) - 1, zero for a perfect forecast), beside the declared matrix's constant forecast. A forecast that follows recent volatility beats a constant one almost by construction, so this says nothing about growth at the owner's point.

| Strategy | Estimator | Months | Mean loss | Declared's mean loss | Difference | Interval | Months it did better |
|---|---|---|---|---|---|---|---|
| E-fast | fast | 46 | 0.2090 | 0.3675 | -0.1585 | [-0.2853, -0.1601] | 63.0% |
| E-blend | blend | 46 | 0.2046 | 0.3675 | -0.1629 | [-0.3126, -0.1611] | 54.3% |
| B-fast | fast | 46 | 0.1868 | 0.3709 | -0.1841 | [-0.3194, -0.1786] | 60.9% |

## Long-history check

Descriptive, before tax and never an outcome (DECISIONS D92 rule 4): each total-risk strategy on the longest history its roles have, at a flat cost a leg, its estimator's lengths converted to the history's observations; the share of moving-block paths of whole months reaching each limit within each horizon.

## Selection across trials

- Trials of this study, this run's tests included: 3
- E-fast against E-declared: deflated Sharpe n/a
- E-blend against E-declared: deflated Sharpe n/a
- B-fast against B-declared: deflated Sharpe n/a

## Structure check

No structure check was pre-registered.

## Data

| Series | Vendor | Point in time | Downloaded | SHA-256 | Source |
|---|---|---|---|---|---|
| msci_world_eur.level | msci | as_observed | 2026-10-18T13:58:17Z | `d015bd8a00df2aacd9e594dece6a2a192d1189038f92b33843909d2824541feb` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=990100&index_variant=NETR&currency_symbol=EUR |
| msci_em_eur.level | msci | as_observed | 2026-10-18T13:58:19Z | `9da2a456b3585e2869ad6d515ddf95f9ecc17f38ae4be1955492d811bccac99c` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=891800&index_variant=NETR&currency_symbol=EUR |
| bund_zero_05y.level | derived | as_observed | 2026-10-18T13:58:26Z | `7cf9ad387d51d448901b9dc0db4185e609739703372f7d8697ff5b175fcdb65b` | derived: 5-year constant-maturity zero from bbk_bund_05y.value and bbk_bund_04y.value |
| euro_cash.level | derived | as_observed | 2026-10-18T13:58:26Z | `bb1c5c18dee220fd8551c03a45eefe5c016879daec1840d4686510dcfbe115c5` | derived: overnight accrual of ecb_eonia.value through 2021-12-31, then ecb_estr.value |

- Coverage: 100.00% of rebalance dates fully priced
- Without data: none
- Entering after the start: none
- Ending before the horizon: none
- A universe listed today cannot show members that disappeared before it was listed; the report measures what the list can show.

## Bias notes

- A 5-year zero-coupon German Federal bond rolled daily at constant maturity; German Federal securities only (no other euro government's spread), and a fitted curve rather than traded bonds.
- An index, not a fund - no fund costs or tracking difference (a study deducts the vehicle's expense ratio).
- An overnight deposit rolled every day at EONIA, then the euro STR, actual/360; no fund cost (a study deducts the vehicle's expense ratio).
- Bundesbank estimates as published now, without vintages.
- Computed by the harness from series in the research store; it carries every bias of its inputs.
- ECB Data Portal values as published now; the ECB revises some series without vintages.
- EONIA, the euro overnight index average, until its end on 3 January 2022; from 2 October 2019 it was the euro short-term rate plus 8.5 basis points.
- MSCI Emerging Markets (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI World (large and mid caps of developed markets), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI index levels are published at the end of each calculation day; MSCI may restate history without vintages.
- The euro short-term rate (euro STR), from 1 October 2019; published the next TARGET day at 08:00 CET.
- Zero-coupon yield of listed German Federal securities at a residual maturity of 4 years (Svensson method), daily.
- Zero-coupon yield of listed German Federal securities at a residual maturity of 5 years (Svensson method), daily.
- Daily marks: between two month ends the holdings are carried at each day's close on the daily calendar (a price or rate not published that day is the latest within its age limit), and a bootstrap month brings its days with it; trades happen at month ends only, so no exit inside a month is simulated.
- Trades fill at the rebalance date's close, moved by the cost model's spread and slippage.
- Income is credited at the next rebalance date, converted at its ex-date's reference rate.
- A year's tax is paid at the first rebalance date on or after its payment date.
- The profile's current rules and rate table are applied to every simulated year.
- Currency conversions are instant at the reference rate and cost nothing; no currency balance is held, so gains on balances are not taxed.
- Losses still carried forward at the horizon are worth nothing.
- A sale whose costs would exceed what it raises (a minimum commission on a few units) is not made; its units stay held and count as nothing at the horizon.
- Bootstrap paths resample whole months of every instrument and rate together; on a path, income is paid at month end at the resampled month's yield, and the history before the first date is the real one.

Unverified figures the costs rest on: `commission:europe_eur`, `friction_assumption`, `market_cap_unknown`, `tax_not_modelled`.

## Tax rules applied

- policy es_irpf; lots fifo; homogeneity same_isin_or_issuer_and_index
- negative balances carried forward 4 years, offsetting up to 0.25 of the other component (LIRPF art. 49 (BOE-A-2006-20764, consolidated text read 2026-10-16))
- savings rate table for tax years 2025 to 2026, verified 2026-10-16 (LIRPF arts. 66.1 and 76 as amended by Ley 7/2024 disp. final 7 (BOE-A-2024-26694); BOE consolidated text read 2026-10-16)
- tax paid on 06-30 of the year 1 after the tax year (Orden HAC/277/2026 art. 7.1 (BOE-A-2026-7041), filing and payment 8 April to 30 June 2026 for tax year 2025)
- foreign-tax credit at the average effective rate (LIRPF art. 80 (BOE-A-2006-20764, consolidated text read 2026-10-16))
- withholding at source US 0.15 (secondary: Spain–United States tax convention of 22 February 1990, art. 10.2.b, as amended by the 2013 protocol in force 27 November 2019; secondary sources read 2026-10-16)
- two-month rule: a loss is deferred by a homogeneous purchase within 2 months (listed) or 12 months (unlisted) of the sale (LIRPF art. 33.5.f and g)
- ETF switches are sales; fund switches are deferred only if the broker supports them (supported: False; LIRPF art. 94.1.a)
- swap partners differ in issuer: True, in tracked index: True

Not tax advice.
