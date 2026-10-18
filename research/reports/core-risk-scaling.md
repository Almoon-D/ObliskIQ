# Scaling the core's total risk to a target volatility, against static A, after Spanish tax

**Confirmation run.** Study `core-risk-scaling` (portfolio_comparison).

## Inputs

- Pre-registration: `core-risk-scaling.md`, SHA-256 `67d17bec84d5059b36f54b9b7bbc632a1419ab10786f42f2fa055dfba3a688c6`
- Lock: merge commit `69c8cc85a4a6cf60db3b78aa0a15c0e6b53df83f`
- Run at: 2026-10-18T14:15:38Z
- Inputs SHA-256: `581a488c588273c20e7d300c1bed57dac5cc7974bf13f00c342358d7d6da210b`
- Bootstrap: 5000 paths of 24-month blocks, seed 20261018
- alpha 0.05 (config research.decision), 6 test(s) in the confirmation run (Bonferroni), each interval at level 0.9833; target power 0.8 (config research.decision)
- Marks: daily closes
- Horizon: 2004-12-01 to 2026-09-30, 262 rebalance dates (2004-12-31 to 2026-09-30); initial capital 100,000.00

## Outcomes

Each candidate's primary effect is log_growth_difference: its annualised log growth of liquidation value after costs and tax minus A's. theta 1.00%: one point a year of after-tax log growth over static A, the owner's neutral allocation (D91): below it the difference is within the proxies' own uncertainty (index against fund, fees, tracking and spreads of a few tenths of a percent a year each) and does not pay for an estimator, more equity and more turnover than A; over twenty years it is about 22 % more terminal wealth

| Candidate | Outcome | Estimate / yr | Interval | Power to detect theta | Detectable at target power | Before data: power / detectable |
|---|---|---|---|---|---|---|
| E-fast | **inconclusive** (sign uncertain, outside the owner's point, low power) | 1.56% | [-2.07%, 4.58%] | 0.044 | 4.68% | 0.040 / 5.05% |
| E-blend | **inconclusive** (sign uncertain, outside the owner's point, low power) | 1.34% | [-2.54%, 4.82%] | 0.039 | 5.11% | 0.040 / 5.05% |
| B-fast | **inconclusive** (sign uncertain, outside the owner's point, low power) | 1.33% | [-1.79%, 4.13%] | 0.055 | 4.07% | 0.045 / 4.61% |

## Comparisons

Secondary tests, in the same Bonferroni count: each portfolio's annualised log growth of liquidation value after costs and tax minus the other's, paired on the same paths.
- E-fast against E-declared: theta 0.50%, half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth
- E-blend against E-declared: theta 0.50%, half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth
- B-fast against B-declared: theta 0.50%, half a point a year of after-tax log growth over the same base held at its declared share: below it, what the estimator adds is within its own turnover, tax and model risk and does not pay for an estimator the live core would have to run and monitor; over twenty years it is about 10 % more terminal wealth

| Comparison | Candidate minus | theta | Outcome | Estimate / yr | Interval | Power to detect theta | Detectable at target power | Before data: power / detectable |
|---|---|---|---|---|---|---|---|---|
| E-fast against E-declared | E-fast minus E-declared | 0.50% | **inconclusive** (sign uncertain, outside the owner's point, low power) | 0.06% | [-1.06%, 0.90%] | 0.124 | 1.31% | 0.085 / 1.59% |
| E-blend against E-declared | E-blend minus E-declared | 0.50% | **inconclusive** (sign uncertain, outside the owner's point, low power) | -0.16% | [-1.42%, 0.94%] | 0.087 | 1.56% | 0.085 / 1.59% |
| B-fast against B-declared | B-fast minus B-declared | 0.50% | **insufficient** (outside the owner's point, low power) | -0.51% | [-1.23%, 0.21%] | 0.247 | 0.95% | 0.239 / 0.96% |

## The frontier

For each drawdown limit and largest acceptable probability of breaching it: the portfolio with the highest median after-tax log growth among those that respect it (best), and the one it resolves to (chosen) — the simplest, then the lowest-turnover, of the best and the portfolios whose growth the bootstrap cannot tell apart from it (their paired interval at the confirmation level holds zero). Choosing a point is the owner's authorisation (DECISIONS D60), not this report's.

| Drawdown limit | Breach probability at most | Chosen | Its outcome against the reference | Best by median growth | Cannot be told apart from the best | Respecting it |
|---|---|---|---|---|---|---|
| 10% | 5% | none | none respects this point | none | none | 0 |
| 10% | 10% | none | none respects this point | none | none | 0 |
| 10% | 25% | none | none respects this point | none | none | 0 |
| 10% | 50% | none | none respects this point | none | none | 0 |
| 20% | 5% | A | the reference itself | A | none | 1 |
| 20% | 10% | A | the reference itself | A | none | 1 |
| 20% | 25% | A | the reference itself | A | none | 1 |
| 20% | 50% | A | the reference itself | A | none | 1 |
| 30% | 5% | A | the reference itself | A | none | 1 |
| 30% | 10% | A | the reference itself | A | none | 1 |
| 30% | 25% | A | the reference itself | E-fast | A, B-fast, E-blend | 4 |
| 30% | 50% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |
| 40% | 5% | A | the reference itself | E-fast | A, B-fast, E-blend | 4 |
| 40% | 10% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |
| 40% | 25% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |
| 40% | 50% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |
| 50% | 5% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |
| 50% | 10% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |
| 50% | 25% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |
| 50% | 50% | A | the reference itself | B-declared | A, B-fast, E-blend, E-declared, E-fast | 6 |

## The owner's point

The owner's point (DECISIONS D91): a drawdown of 30% from the running peak reached on at most 5% of 20-year paths, measured on daily closes. A candidate that does not respect it cannot be sufficient (D92). Beside it, the share of paths reaching the limit within each horizon, and its rate: drawdown episodes reaching it per decade and the share of time beyond it, over the paths. Each path is 21.75 years long.

| Portfolio | Reaches 30% within 20 years | Respects the point | within 5 years | within 10 years | within 15 years | within 20 years | Episodes per decade | Share of time beyond | Deepest fall on the historical path |
|---|---|---|---|---|---|---|---|---|---|
| A | 0.0% | yes | 0.0% | 0.0% | 0.0% | 0.0% | 0.00 | 0.00% | -14.73% |
| E-fast | 20.9% | **no** | 2.8% | 9.5% | 15.4% | 20.9% | 0.11 | 0.75% | -28.50% |
| E-blend | 22.0% | **no** | 2.5% | 9.7% | 16.1% | 22.0% | 0.11 | 0.95% | -29.25% |
| B-fast | 16.2% | **no** | 1.8% | 6.6% | 11.7% | 16.2% | 0.08 | 0.68% | -24.55% |
| E-declared | 46.8% | **no** | 11.4% | 26.1% | 36.4% | 46.8% | 0.27 | 1.41% | -33.12% |
| B-declared | 40.1% | **no** | 8.5% | 21.3% | 30.4% | 40.1% | 0.22 | 0.98% | -31.05% |

## What this test cannot conclude

- Whether the effect reaches theta: the interval holds values on both sides of it.
- That the candidate may receive capital: it does not respect the owner's point, so it cannot be sufficient whatever its growth (DECISIONS D92).
- Anything against the effect: the test had low power to detect theta, so an inconclusive result is not evidence that the effect is absent.
- That the effect is zero: only that an effect of theta or more is ruled out at this level.
- That a portfolio will keep its place after the sample: past growth after tax is no promise of future superiority.
- How a portfolio behaves in a crisis unlike the episodes in the sample; the bootstrap reshuffles the sample's own months and cannot create a new kind of crisis.
- Anything about a period, market or cost regime outside this sample.

## Adequacy checks

| Check | Passed | Detail |
|---|---|---|
| data loaded | yes | every series is in the research store |
| point in time | yes | every series is as_observed |
| coverage | yes | 100.0% of 262 rebalance dates fully priced (pre-registered minimum 1) |
| effective observations | yes | 261 non-overlapping months (pre-registered minimum 240) |

## Portfolios after tax

| Portfolio | Liquidation value | Log growth / yr | Max drawdown (daily closes) | Costs | Tax paid | Trades | Turnover / yr | Median bootstrap growth | P5 terminal wealth multiple | Median minus ln(1 + 30%) | Median minus ln(1 + 50%) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 221,019.20 | 3.65% | -14.73% | 6,655.38 | 31,716.07 | 261 | 16.43% | 3.28% | 1.463 | -22.96% | -37.27% |
| E-fast | 310,393.36 | 5.21% | -28.50% | 15,787.45 | 54,109.83 | 374 | 116.11% | 4.68% | 1.636 | -21.56% | -35.87% |
| E-blend | 295,899.13 | 4.99% | -29.25% | 13,122.10 | 51,574.26 | 356 | 96.71% | 4.56% | 1.532 | -21.67% | -35.98% |
| B-fast | 295,132.95 | 4.98% | -24.55% | 14,404.83 | 51,456.58 | 221 | 64.38% | 4.67% | 1.639 | -21.57% | -35.88% |
| E-declared | 306,308.12 | 5.15% | -33.12% | 2,348.27 | 57,174.03 | 80 | 18.48% | 4.73% | 1.575 | -21.51% | -35.82% |
| B-declared | 329,984.11 | 5.49% | -31.05% | 5,178.21 | 65,534.66 | 62 | 18.11% | 5.18% | 1.706 | -21.05% | -35.36% |

The owner's stated aspiration (DECISIONS D60) is reported as a distance from each portfolio's median after-tax log growth, never as a gate.

Sales not made because their costs would exceed what they raise (the units stay held, and count as nothing at the horizon): A 0 on the historical path, on 0 bootstrap paths; E-fast 0 on the historical path, on 0 bootstrap paths; E-blend 0 on the historical path, on 0 bootstrap paths; B-fast 0 on the historical path, on 0 bootstrap paths; E-declared 0 on the historical path, on 0 bootstrap paths; B-declared 0 on the historical path, on 0 bootstrap paths.

Probability that a bootstrap path's mark-to-market drawdown reaches each limit within 20 years:

| Portfolio | 10% | 20% | 30% | 40% | 50% |
|---|---|---|---|---|---|
| A | 91.6% | 4.6% | 0.0% | 0.0% | 0.0% |
| E-fast | 100.0% | 90.5% | 20.9% | 2.3% | 0.2% |
| E-blend | 100.0% | 90.6% | 22.0% | 2.4% | 0.1% |
| B-fast | 100.0% | 87.2% | 16.2% | 1.9% | 0.1% |
| E-declared | 100.0% | 90.1% | 46.8% | 7.4% | 1.3% |
| B-declared | 100.0% | 90.5% | 40.1% | 5.0% | 0.7% |

## Portfolios before tax

The same strategies, trading the same way, with no tax withheld, settled or paid.

| Portfolio | Liquidation value | Log growth / yr | Max drawdown (month ends) | Costs | Tax paid | Trades |
|---|---|---|---|---|---|---|
| A | 259,572.83 | 4.39% | -10.48% | 6,211.53 | 0.00 | 151 |
| E-fast | 398,069.93 | 6.35% | -23.19% | 17,345.69 | 0.00 | 341 |
| E-blend | 379,612.36 | 6.13% | -23.60% | 14,466.97 | 0.00 | 326 |
| B-fast | 365,429.98 | 5.96% | -22.14% | 15,405.13 | 0.00 | 184 |
| E-declared | 374,160.75 | 6.07% | -29.72% | 2,281.85 | 0.00 | 49 |
| B-declared | 405,251.38 | 6.43% | -27.38% | 5,243.54 | 0.00 | 34 |

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
| 2004 | -0.97% | -0.30% | -0.30% | -0.62% | -0.30% | -0.62% |
| 2005 | 18.64% | 19.97% | 22.54% | 17.10% | 17.58% | 17.10% |
| 2006 | -1.69% | 4.71% | 5.22% | 4.13% | 5.66% | 4.13% |
| 2007 | 2.80% | 0.27% | -0.21% | -0.33% | 2.25% | -0.32% |
| 2008 | -1.30% | -20.03% | -20.41% | -16.12% | -23.08% | -19.82% |
| 2009 | 9.78% | 15.51% | 10.80% | 13.53% | 18.14% | 17.60% |
| 2010 | 15.67% | 14.86% | 12.42% | 14.12% | 11.79% | 14.19% |
| 2011 | 7.73% | -4.51% | -4.06% | -0.21% | -2.24% | 2.45% |
| 2012 | 5.15% | 8.88% | 8.36% | 9.33% | 8.30% | 9.63% |
| 2013 | -5.96% | 13.44% | 14.59% | 11.98% | 10.65% | 11.98% |
| 2014 | 12.67% | 11.78% | 13.34% | 13.76% | 10.46% | 13.60% |
| 2015 | 1.49% | -0.16% | 1.11% | 4.00% | 4.99% | 5.99% |
| 2016 | 6.79% | 5.37% | 4.21% | 2.82% | 5.44% | 7.42% |
| 2017 | -2.49% | 5.19% | 5.94% | 2.73% | 4.80% | 3.17% |
| 2018 | 0.40% | -4.74% | -4.78% | -2.85% | -2.75% | -2.31% |
| 2019 | 9.22% | 14.25% | 15.56% | 16.77% | 15.22% | 17.73% |
| 2020 | 2.09% | -3.88% | -6.02% | -1.10% | 3.57% | 3.26% |
| 2021 | 8.14% | 18.80% | 15.60% | 14.53% | 15.26% | 17.38% |
| 2022 | -10.12% | -9.43% | -8.70% | -12.69% | -8.80% | -13.50% |
| 2023 | 3.18% | 12.14% | 10.99% | 11.71% | 11.39% | 12.54% |
| 2024 | 7.44% | 19.19% | 20.53% | 15.79% | 16.52% | 15.81% |
| 2025 | 1.67% | 1.76% | 1.06% | 2.57% | 4.17% | 3.61% |
| 2026 | 5.29% | 9.65% | 10.46% | 7.48% | 10.30% | 8.12% |

Before tax:

| Year | A | E-fast | E-blend | B-fast | E-declared | B-declared |
|---|---|---|---|---|---|---|
| 2004 | -0.97% | -0.30% | -0.30% | -0.62% | -0.30% | -0.62% |
| 2005 | 18.64% | 19.97% | 22.54% | 17.10% | 17.58% | 17.10% |
| 2006 | -1.38% | 5.54% | 5.79% | 4.13% | 6.00% | 4.13% |
| 2007 | 2.85% | 1.09% | 0.70% | -0.11% | 2.14% | -0.11% |
| 2008 | -1.03% | -17.80% | -17.40% | -16.17% | -23.01% | -19.81% |
| 2009 | 10.09% | 15.51% | 10.78% | 14.38% | 18.34% | 17.80% |
| 2010 | 16.01% | 14.86% | 12.43% | 14.16% | 12.09% | 14.29% |
| 2011 | 8.51% | -4.50% | -4.07% | -0.11% | -2.13% | 2.44% |
| 2012 | 5.58% | 9.00% | 8.36% | 9.88% | 8.45% | 10.08% |
| 2013 | -5.86% | 13.83% | 14.59% | 12.01% | 10.69% | 12.02% |
| 2014 | 13.22% | 13.42% | 14.85% | 14.14% | 10.94% | 14.00% |
| 2015 | 1.46% | 2.79% | 3.93% | 4.03% | 5.39% | 6.39% |
| 2016 | 7.58% | 7.10% | 6.31% | 6.90% | 6.08% | 7.50% |
| 2017 | -2.26% | 6.00% | 6.22% | 4.02% | 5.00% | 3.77% |
| 2018 | 0.36% | -4.38% | -4.49% | -2.21% | -2.75% | -2.23% |
| 2019 | 9.74% | 15.89% | 18.23% | 17.22% | 15.88% | 17.74% |
| 2020 | 2.64% | -3.01% | -5.51% | -1.06% | 3.72% | 3.95% |
| 2021 | 8.31% | 18.77% | 15.94% | 17.32% | 16.05% | 17.49% |
| 2022 | -9.88% | -7.84% | -8.13% | -12.20% | -7.86% | -12.74% |
| 2023 | 3.65% | 12.55% | 11.89% | 13.16% | 11.54% | 13.43% |
| 2024 | 7.85% | 19.41% | 20.56% | 15.93% | 16.59% | 15.91% |
| 2025 | 2.41% | 4.05% | 3.44% | 2.97% | 5.36% | 4.44% |
| 2026 | 5.71% | 11.19% | 11.68% | 8.33% | 10.49% | 8.36% |

Tax paid, by the year it was paid (the horizon's includes the terminal sale's):

| Paid in | A | E-fast | E-blend | B-fast | E-declared | B-declared |
|---|---|---|---|---|---|---|
| 2006 | 276.05 | 895.91 | 644.29 | n/a | 291.72 | n/a |
| 2007 | 17.11 | 1,065.95 | 1,200.13 | 243.22 | 25.69 | 243.22 |
| 2008 | 359.73 | 3,041.42 | 3,763.68 | 4.25 | 298.82 | 5.79 |
| 2009 | 318.19 | n/a | n/a | 773.56 | 256.40 | 166.59 |
| 2010 | 483.12 | n/a | n/a | 55.39 | 172.35 | 115.86 |
| 2011 | 67.89 | n/a | n/a | 127.95 | 261.18 | n/a |
| 2012 | 540.53 | 149.17 | n/a | 719.79 | 29.00 | 575.56 |
| 2013 | 111.24 | 538.38 | n/a | 38.44 | 29.36 | 31.49 |
| 2014 | 623.05 | 2,386.23 | 2,086.95 | 503.30 | 468.21 | 504.13 |
| 2015 | 115.03 | 5,365.87 | 4,998.44 | 60.79 | 656.82 | 783.02 |
| 2016 | 1,210.29 | 2,843.54 | 3,326.04 | 7,184.73 | 1,012.89 | 67.23 |
| 2017 | 185.29 | 1,364.79 | 488.14 | 2,287.73 | 105.35 | 1,295.52 |
| 2018 | 130.21 | 750.07 | 601.52 | 1,345.63 | 44.04 | 153.51 |
| 2019 | 159.23 | 2,908.33 | 4,124.42 | 828.59 | 1,324.35 | n/a |
| 2020 | 716.39 | 1,645.05 | 1,520.30 | 95.84 | 167.57 | 1,615.52 |
| 2021 | 929.82 | n/a | 879.96 | 5,631.61 | 1,596.04 | 196.65 |
| 2022 | 960.85 | 3,820.48 | 1,144.62 | 1,291.11 | 2,427.07 | 2,103.17 |
| 2023 | 1,399.75 | 867.35 | 1,683.40 | 3,034.93 | 259.55 | 2,361.11 |
| 2024 | 251.68 | 529.62 | 52.89 | 272.90 | 35.21 | 294.37 |
| 2025 | 1,543.79 | 6,686.24 | 6,129.26 | 1,093.32 | 3,312.66 | 2,686.77 |
| 2026 | 21,316.82 | 19,251.43 | 18,930.22 | 25,863.50 | 44,399.74 | 52,335.14 |

## Robustness

- E-fast: subperiods 2004-12-01 to 2015-12-31 0.29% sign holds, 2016-01-01 to 2026-09-30 3.40% sign holds; break-even cost multiplier 3.265 (declared cost uncertainty 3.67: **not exceeded**).
- E-blend: subperiods 2004-12-01 to 2015-12-31 0.13% sign holds, 2016-01-01 to 2026-09-30 3.16% sign holds; break-even cost multiplier 3.049 (declared cost uncertainty 3.67: **not exceeded**).
- B-fast: subperiods 2004-12-01 to 2015-12-31 0.36% sign holds, 2016-01-01 to 2026-09-30 2.70% sign holds; break-even cost multiplier 3.194 (declared cost uncertainty 3.67: **not exceeded**).
- E-fast against E-declared: subperiods 2004-12-01 to 2015-12-31 0.54% sign holds, 2016-01-01 to 2026-09-30 -0.24% **sign fails**; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- E-blend against E-declared: subperiods 2004-12-01 to 2015-12-31 0.38% **sign fails**, 2016-01-01 to 2026-09-30 -0.48% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- B-fast against B-declared: subperiods 2004-12-01 to 2015-12-31 -0.28% sign holds, 2016-01-01 to 2026-09-30 -0.68% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).

- Placebo: not applicable: a portfolio comparison has no signal to lag; its robustness is the sign in each subperiod and the break-even cost multiplier

## Block lengths

The longest lookback a portfolio reads is 12.0 months (an EWMA counts three half-lives, a window its length); the bootstrap's blocks are 24 months in a sample of 261: the blocks are at least twice it, as D92 rule 3 asks.

The bootstrap repeated with each registered block length (same seed): each test's interval at the confirmation level, and each portfolio's share of paths reaching 30% within 20 years.

| Blocks (months) | Paths | E-fast | E-blend | B-fast | E-fast against E-declared | E-blend against E-declared | B-fast against B-declared | A reaches it | E-fast reaches it | E-blend reaches it | B-fast reaches it | E-declared reaches it | B-declared reaches it |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 1000 | [-2.19%, 5.04%] | [-2.28%, 5.40%] | [-1.84%, 4.50%] | [-1.06%, 1.01%] | [-1.20%, 1.12%] | [-1.24%, 0.31%] | 0.2% | 19.4% | 18.5% | 15.1% | 25.9% | 19.0% |
| 36 | 1000 | [-2.15%, 4.27%] | [-2.80%, 4.61%] | [-1.94%, 4.01%] | [-1.01%, 0.93%] | [-1.38%, 0.81%] | [-1.16%, 0.18%] | 0.0% | 18.0% | 20.0% | 14.1% | 56.1% | 51.0% |
| 60 | 1000 | [-2.38%, 4.05%] | [-3.16%, 4.17%] | [-1.88%, 3.72%] | [-0.93%, 0.71%] | [-1.35%, 0.65%] | [-1.05%, -0.03%] | 0.0% | 4.9% | 7.6% | 3.7% | 56.3% | 54.3% |

## Forecast check

A check that each estimator works, **never an outcome** (DECISIONS D92): its forecast of the base's next-month variance from each rebalance date, against the variance of the base's daily returns over that month, scored by QLIKE (r/f - ln(r/f) - 1, zero for a perfect forecast), beside the declared matrix's constant forecast. A forecast that follows recent volatility beats a constant one almost by construction, so this says nothing about growth at the owner's point. The interval is a moving-block bootstrap's: the first and last months fall in fewer blocks, so on a short sample it can sit beside the mean.

| Strategy | Estimator | Months | Mean loss | Declared's mean loss | Difference | Interval | Months it did better |
|---|---|---|---|---|---|---|---|
| E-fast | fast | 261 | 0.3358 | 0.5208 | -0.1850 | [-0.3399, -0.0818] | 72.0% |
| E-blend | blend | 261 | 0.3700 | 0.5208 | -0.1508 | [-0.2883, -0.0444] | 69.7% |
| B-fast | fast | 261 | 0.3188 | 0.5080 | -0.1892 | [-0.3430, -0.0880] | 69.0% |

## Long-history check

Descriptive, before tax and never an outcome (DECISIONS D92 rule 4): each total-risk strategy on the longest history its roles have, at a flat cost a leg, its estimator's lengths converted to the history's observations; the share of moving-block paths of whole months reaching each limit within each horizon.

### MSCI and the Bundesbank in EUR, daily

daily, EUR, data from 2000-12-29, evaluated from 2001-07-01 to 2026-09-30; 1000 paths of 240 months in 24-month blocks; 0.30% a leg.

- Bias: MSCI indices and the Bundesbank's curve, not funds; the flat cost a leg stands in for every trading cost.
- Bias: German Federal bonds only, a fitted curve, for the euro bond role.
- Bias: From December 2004 the same series as the main sample; what this history adds is January 2001 to November 2004, the end of the 2000-03 bear market.
- Bias: Before tax.

| Check | Passed | Detail |
|---|---|---|
| data loaded | yes | every series is in the research store |
| point in time | yes | every series is as_observed |
| alignment | yes | 6718 periods on every series |

From 2001-07-31 to 2026-09-30 (302 months).

| Strategy | Log growth / yr | Deepest fall | Median path growth / yr | 10% within 5 y | 10% within 10 y | 10% within 15 y | 10% within 20 y | 10% anywhere | 20% within 5 y | 20% within 10 y | 20% within 15 y | 20% within 20 y | 20% anywhere | 30% within 5 y | 30% within 10 y | 30% within 15 y | 30% within 20 y | 30% anywhere | 40% within 5 y | 40% within 10 y | 40% within 15 y | 40% within 20 y | 40% anywhere | 50% within 5 y | 50% within 10 y | 50% within 15 y | 50% within 20 y | 50% anywhere |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| E-fast | 5.33% | -25.81% | 6.04% | 92.3% | 99.4% | 100.0% | 100.0% | 100.0% | 41.3% | 65.4% | 79.2% | 88.2% | 88.2% | 2.4% | 7.4% | 12.4% | 15.6% | 15.6% | 0.1% | 0.7% | 0.8% | 1.0% | 1.0% | 0.0% | 0.1% | 0.1% | 0.1% | 0.1% |
| E-blend | 5.18% | -25.77% | 5.90% | 92.3% | 99.4% | 100.0% | 100.0% | 100.0% | 41.3% | 65.8% | 79.7% | 88.1% | 88.1% | 1.9% | 7.8% | 12.3% | 15.7% | 15.7% | 0.1% | 0.5% | 0.7% | 0.8% | 0.8% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| B-fast | 5.18% | -23.97% | 5.98% | 90.7% | 99.5% | 100.0% | 100.0% | 100.0% | 36.8% | 60.8% | 74.0% | 84.2% | 84.2% | 0.8% | 4.1% | 6.7% | 8.9% | 8.9% | 0.1% | 0.3% | 0.4% | 0.5% | 0.5% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| E-declared | 5.07% | -32.78% | 5.79% | 88.5% | 99.1% | 99.9% | 100.0% | 100.0% | 47.0% | 68.6% | 81.4% | 89.7% | 89.7% | 10.4% | 23.0% | 32.9% | 40.7% | 40.7% | 0.7% | 3.1% | 4.7% | 6.4% | 6.4% | 0.1% | 0.4% | 0.5% | 0.7% | 0.7% |
| B-declared | 5.58% | -29.83% | 6.35% | 91.7% | 99.5% | 100.0% | 100.0% | 100.0% | 46.0% | 67.6% | 80.9% | 89.4% | 89.4% | 6.3% | 16.9% | 24.8% | 32.2% | 32.2% | 0.3% | 1.7% | 2.6% | 3.2% | 3.2% | 0.0% | 0.2% | 0.2% | 0.3% | 0.3% |

### Ken French developed markets in USD, daily

daily, USD, data from 1990-07-02, evaluated from 1991-01-01 to 2026-08-31; 1000 paths of 240 months in 24-month blocks; 0.30% a leg.

- Bias: A factor model's value-weighted market of every developed-market stock with market equity (Bloomberg data), not MSCI World.
- Bias: In US dollars, unhedged; a dollar investor's history, not a euro investor's.
- Bias: No daily emerging-market series; the EM role is mapped to the developed market. No bond role, so B-fast and its twin are not run here.
- Bias: The US one-month bill as cash, a daily simple rate; revised without vintages; before tax.

| Check | Passed | Detail |
|---|---|---|
| data loaded | yes | every series is in the research store |
| point in time | yes | every series is release_lag |
| alignment | yes | 9435 periods on every series |

- Not run (its roles lack a history here): B-declared, B-fast

From 1991-01-31 to 2026-08-31 (427 months).

| Strategy | Log growth / yr | Deepest fall | Median path growth / yr | 10% within 5 y | 10% within 10 y | 10% within 15 y | 10% within 20 y | 10% anywhere | 20% within 5 y | 20% within 10 y | 20% within 15 y | 20% within 20 y | 20% anywhere | 30% within 5 y | 30% within 10 y | 30% within 15 y | 30% within 20 y | 30% anywhere | 40% within 5 y | 40% within 10 y | 40% within 15 y | 40% within 20 y | 40% anywhere | 50% within 5 y | 50% within 10 y | 50% within 15 y | 50% within 20 y | 50% anywhere |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| E-fast | 6.56% | -30.33% | 6.71% | 90.8% | 99.2% | 100.0% | 100.0% | 100.0% | 37.7% | 62.7% | 77.5% | 85.5% | 85.5% | 6.4% | 16.0% | 24.1% | 32.5% | 32.5% | 0.7% | 2.6% | 4.7% | 6.1% | 6.1% | 0.0% | 0.1% | 0.1% | 0.3% | 0.3% |
| E-blend | 6.51% | -30.08% | 6.63% | 94.4% | 99.7% | 100.0% | 100.0% | 100.0% | 36.9% | 62.2% | 77.4% | 85.5% | 85.5% | 6.5% | 16.6% | 27.0% | 35.2% | 35.2% | 0.4% | 1.9% | 4.0% | 5.2% | 5.2% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| E-declared | 6.47% | -37.45% | 6.49% | 83.5% | 97.3% | 99.9% | 100.0% | 100.0% | 35.1% | 59.5% | 73.9% | 82.4% | 82.4% | 13.1% | 26.1% | 37.8% | 45.8% | 45.8% | 1.4% | 3.8% | 7.3% | 9.8% | 9.8% | 0.1% | 0.6% | 1.0% | 1.3% | 1.3% |

### AQR's Century of Factor Premia, monthly

monthly, local excess returns plus the US one-month bill, data from 1926-07-01, evaluated from 1927-07-01 to 2026-02-28; 1000 paths of 240 months in 24-month blocks; 0.30% a leg.

- Bias: Local-currency excess returns of equity indices (up to 43 markets, nine in the 1920s) and 10-year government bonds (up to 26), plus the US one-month bill; a currency-hedged dollar return, not a euro investor's.
- Bias: The workbook does not state how its market columns weight their countries.
- Bias: Monthly; the estimators read monthly returns, their lengths converted at 12 / 261 months a trading day, so they react a month late at best; drawdowns on month ends understate falls inside a month.
- Bias: The EM role is mapped to the equity indices, EGB_INT to the 10-year bonds (not five years).
- Bias: Gross of costs as published (a flat cost a leg here); rebuilt by AQR at each update, without vintages; before tax.

| Check | Passed | Detail |
|---|---|---|
| data loaded | yes | every series is in the research store |
| point in time | yes | every series is release_lag |
| alignment | yes | 1195 periods on every series |

From 1927-07-29 to 2026-02-27 (1183 months).

| Strategy | Log growth / yr | Deepest fall | Median path growth / yr | 10% within 5 y | 10% within 10 y | 10% within 15 y | 10% within 20 y | 10% anywhere | 20% within 5 y | 20% within 10 y | 20% within 15 y | 20% within 20 y | 20% anywhere | 30% within 5 y | 30% within 10 y | 30% within 15 y | 30% within 20 y | 30% anywhere | 40% within 5 y | 40% within 10 y | 40% within 15 y | 40% within 20 y | 40% anywhere | 50% within 5 y | 50% within 10 y | 50% within 15 y | 50% within 20 y | 50% anywhere |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| E-fast | 8.38% | -39.09% | 8.23% | 66.1% | 89.3% | 95.7% | 98.4% | 98.4% | 22.1% | 42.3% | 57.6% | 69.4% | 69.4% | 6.5% | 16.0% | 24.0% | 32.2% | 32.2% | 0.9% | 2.8% | 5.1% | 7.2% | 7.2% | 0.3% | 0.8% | 1.2% | 2.0% | 2.0% |
| E-blend | 8.73% | -40.72% | 8.50% | 69.1% | 90.4% | 96.4% | 98.9% | 98.9% | 24.7% | 45.9% | 61.7% | 73.3% | 73.3% | 6.7% | 16.4% | 24.5% | 32.3% | 32.3% | 0.9% | 2.6% | 4.5% | 6.5% | 6.5% | 0.2% | 0.7% | 1.1% | 1.6% | 1.6% |
| B-fast | 8.04% | -32.57% | 7.88% | 45.0% | 71.0% | 85.6% | 92.7% | 92.7% | 13.5% | 28.4% | 41.3% | 52.3% | 52.3% | 3.3% | 7.4% | 11.7% | 15.5% | 15.5% | 0.3% | 0.9% | 1.7% | 2.3% | 2.3% | 0.1% | 0.1% | 0.1% | 0.1% | 0.1% |
| E-declared | 7.75% | -35.17% | 7.63% | 47.7% | 75.7% | 87.1% | 93.9% | 93.9% | 11.2% | 23.8% | 35.3% | 46.0% | 46.0% | 4.3% | 10.1% | 15.7% | 20.9% | 20.9% | 0.4% | 1.3% | 1.9% | 2.6% | 2.6% | 0.1% | 0.4% | 0.4% | 0.4% | 0.4% |
| B-declared | 8.33% | -34.09% | 8.18% | 47.1% | 73.3% | 87.8% | 93.5% | 93.5% | 13.8% | 28.3% | 40.7% | 51.2% | 51.2% | 3.4% | 7.7% | 12.0% | 16.2% | 16.2% | 0.3% | 0.9% | 1.6% | 2.2% | 2.2% | 0.1% | 0.1% | 0.1% | 0.1% | 0.1% |

## Selection across trials

- Trials of this study, this run's tests included: 9
- E-fast: monthly Sharpe 0.0527 against 0.1033 expected from the best of 9 trial(s); probability 0.213
- E-blend: monthly Sharpe 0.0445 against 0.1033 expected from the best of 9 trial(s); probability 0.177
- B-fast: monthly Sharpe 0.0595 against 0.1033 expected from the best of 9 trial(s); probability 0.246
- E-fast against E-declared: monthly Sharpe -0.0437 against 0.1033 expected from the best of 9 trial(s); probability 0.008
- E-blend against E-declared: monthly Sharpe -0.0632 against 0.1033 expected from the best of 9 trial(s); probability 0.003
- B-fast against B-declared: monthly Sharpe -0.1364 against 0.1033 expected from the best of 9 trial(s); probability 0.000

## Structure check

No structure check was pre-registered.

## Data

| Series | Vendor | Point in time | Downloaded | SHA-256 | Source |
|---|---|---|---|---|---|
| msci_world_eur.level | msci | as_observed | 2026-10-18T13:58:17Z | `d015bd8a00df2aacd9e594dece6a2a192d1189038f92b33843909d2824541feb` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=990100&index_variant=NETR&currency_symbol=EUR |
| msci_em_eur.level | msci | as_observed | 2026-10-18T13:58:19Z | `9da2a456b3585e2869ad6d515ddf95f9ecc17f38ae4be1955492d811bccac99c` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=891800&index_variant=NETR&currency_symbol=EUR |
| bund_zero_05y.level | derived | as_observed | 2026-10-18T13:58:26Z | `7cf9ad387d51d448901b9dc0db4185e609739703372f7d8697ff5b175fcdb65b` | derived: 5-year constant-maturity zero from bbk_bund_05y.value and bbk_bund_04y.value |
| bund_zero_17y.level | derived | as_observed | 2026-10-18T13:58:27Z | `912c86d9af0cde4fee1c73119e2684b6cd38e6fe43fcd8c773af2c8f1352c4cf` | derived: 17-year constant-maturity zero from bbk_bund_17y.value and bbk_bund_16y.value |
| tip.adj_close | yahoo | as_observed | 2026-10-18T13:58:27Z | `97c927925e8227cf5ce46332ba22923b7ff9a25803ba6112c73c93373fb60fb4` | yfinance Ticker('TIP').history(auto_adjust=False, actions=True) |
| ief.adj_close | yahoo | as_observed | 2026-10-18T13:58:28Z | `666717c05ecc09a64d8830e4c6ec06fbdbf6c611b487ba3f4655fbe735782cb2` | yfinance Ticker('IEF').history(auto_adjust=False, actions=True) |
| gld.close | yahoo | as_observed | 2026-10-18T13:58:29Z | `9f045f89dd8a66aa84ef9129831b9ca183f04a7444dd9e71779b0ab2b62cf3bf` | yfinance Ticker('GLD').history(auto_adjust=False, actions=True) |
| crsox.adj_close | yahoo | as_observed | 2026-10-18T13:58:29Z | `2fc8c02e6b6d82dc36fcd00fe09957bf3254137a0deee2950e3a5b4408b1e44f` | yfinance Ticker('CRSOX').history(auto_adjust=False, actions=True) |
| euro_cash.level | derived | as_observed | 2026-10-18T13:58:26Z | `bb1c5c18dee220fd8551c03a45eefe5c016879daec1840d4686510dcfbe115c5` | derived: overnight accrual of ecb_eonia.value through 2021-12-31, then ecb_estr.value |
| ecb_fx.USD | ecb | as_observed | 2026-10-18T13:58:13Z | `8e925cef1572f8df5c8743229a761b029180f3b5cd4aa2213592192964ebd793` | https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip |

- Coverage: 100.00% of rebalance dates fully priced
- Without data: none
- Entering after the start: crsox.adj_close
- Ending before the horizon: none
- A universe listed today cannot show members that disappeared before it was listed; the report measures what the list can show.

## Bias notes

- A 17-year zero-coupon German Federal bond rolled daily at constant maturity; German Federal securities only (no other euro government's spread), and a fitted curve rather than traded bonds.
- A 5-year zero-coupon German Federal bond rolled daily at constant maturity; German Federal securities only (no other euro government's spread), and a fitted curve rather than traded bonds.
- Adjusted backwards for dividends: its level is not what traded; use its returns.
- An index, not a fund - no fund costs or tracking difference (a study deducts the vehicle's expense ratio).
- An overnight deposit rolled every day at EONIA, then the euro STR, actual/360; no fund cost (a study deducts the vehicle's expense ratio).
- Bundesbank estimates as published now, without vintages.
- Cantor Fitzgerald (formerly Credit Suisse) Commodity Return Strategy Fund, institutional class, a US mutual fund managed against the Bloomberg Commodity Index Total Return; its fee is in its price.
- Computed by the harness from series in the research store; it carries every bias of its inputs.
- ECB Data Portal values as published now; the ECB revises some series without vintages.
- ECB euro foreign exchange reference rates, set around 14:15 CET and published around 16:00 CET on TARGET days; a close before publication would not have had the day's rate.
- EONIA, the euro overnight index average, until its end on 3 January 2022; from 2 October 2019 it was the euro short-term rate plus 8.5 basis points.
- MSCI Emerging Markets (large and mid caps), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI World (large and mid caps of developed markets), net total return in EUR, daily; dividends reinvested after the withholding MSCI applies to non-resident institutions.
- MSCI index levels are published at the end of each calculation day; MSCI may restate history without vintages.
- SPDR Gold Shares (physically backed), NYSE Arca; its fee is in its price.
- The euro short-term rate (euro STR), from 1 October 2019; published the next TARGET day at 08:00 CET.
- The listing's history only; a fund that closed before the download is absent (survivorship).
- The session close.
- Units of the currency per euro.
- Yahoo closes are split-adjusted backwards and not point in time; a split after the date changes the level.
- Zero-coupon yield of listed German Federal securities at a residual maturity of 16 years (Svensson method), daily.
- Zero-coupon yield of listed German Federal securities at a residual maturity of 17 years (Svensson method), daily.
- Zero-coupon yield of listed German Federal securities at a residual maturity of 4 years (Svensson method), daily.
- Zero-coupon yield of listed German Federal securities at a residual maturity of 5 years (Svensson method), daily.
- iShares 7-10 Year Treasury Bond ETF, Nasdaq; its fee is in its price.
- iShares TIPS Bond ETF (US Treasury inflation-protected securities), NYSE Arca; its fee is in its price.
- Daily marks: between two month ends the holdings are carried at each day's close on the daily calendar (a price or rate not published that day is the latest within its age limit), and a bootstrap month brings its days with it; trades happen at month ends only, so no exit inside a month is simulated.
- Trades fill at the rebalance date's close, moved by the cost model's spread and slippage.
- Income is credited at the next rebalance date, converted at its ex-date's reference rate.
- A year's tax is paid at the first rebalance date on or after its payment date.
- The profile's current rules and rate table are applied to every simulated year.
- Currency conversions are instant at the reference rate and cost nothing; no currency balance is held, so gains on balances are not taxed.
- Losses still carried forward at the horizon are worth nothing.
- A sale whose costs would exceed what it raises (a minimum commission on a few units) is not made; its units stay held and count as nothing at the horizon.
- Bootstrap paths resample whole months of every instrument and rate together; on a path, income is paid at month end at the resampled month's yield, and the history before the first date is the real one.

Unverified figures the costs rest on: `commission:europe_eur`, `commission:europe_usd`, `friction_assumption`, `market_cap_unknown`, `tax_not_modelled`.

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
