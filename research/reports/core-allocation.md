# The core's variants against a global equity fund, after Spanish tax

**Confirmation run.** Study `core-allocation` (portfolio_comparison).

## Inputs

- Pre-registration: `core-allocation.md`, SHA-256 `3be725378ed7223a9e31dc230f74d27768469ad66d18f8c6670bc8b2d01dcb8f`
- Lock: merge commit `7537c6c398f7984464cd1d16966e97fe1a1ec6f1`
- Run at: 2026-10-17T13:26:38Z
- Inputs SHA-256: `c5207063238d81fe1f0ddc7ba62fb746f72acfcacdee8b8faf436e7c7f71ed22`
- Bootstrap: 5000 paths of 12-month blocks, seed 20261017
- alpha 0.05 (config research.decision), 9 test(s) in the confirmation run (Bonferroni), each interval at level 0.9889; target power 0.8 (config research.decision)
- Horizon: 2005-12-01 to 2026-09-30, 250 rebalance dates (2005-12-30 to 2026-09-30); initial capital 100,000.00

## Outcomes

Each candidate's primary effect is log_growth_difference: its annualised log growth of liquidation value after costs and tax minus equity reference's. theta 1.00%: one point a year of after-tax log growth over buying and holding the equity reference: below it the difference is within the proxies' own uncertainty (index against fund, fees, tracking and spreads of a few tenths of a percent a year each) and does not pay for a managed core's complexity, turnover and model risk; over twenty years it is about 22 % more terminal wealth

| Candidate | Outcome | Estimate / yr | Interval | Power to detect theta | Detectable at target power | Before data: power / detectable |
|---|---|---|---|---|---|---|
| 60/40 | **inconclusive** (sign uncertain, low power) | -2.18% | [-5.16%, 1.42%] | 0.038 | 4.45% | 0.036 / 4.60% |
| A | **inconclusive** (sign uncertain, low power) | -4.06% | [-9.79%, 2.90%] | 0.016 | 8.56% | 0.015 / 9.02% |
| B | **inconclusive** (sign uncertain, low power) | -4.88% | [-10.76%, 2.42%] | 0.016 | 8.81% | 0.015 / 9.23% |
| C | **inconclusive** (sign uncertain, low power) | -4.39% | [-9.44%, 1.75%] | 0.019 | 7.45% | 0.017 / 7.89% |
| D10 | **inconclusive** (sign uncertain, low power) | -4.93% | [-10.58%, 1.40%] | 0.017 | 8.17% | 0.015 / 8.95% |
| D20 | **inconclusive** (sign uncertain, low power) | -3.47% | [-7.99%, 1.44%] | 0.022 | 6.39% | 0.020 / 6.92% |
| D30 | **inconclusive** (sign uncertain, low power) | -2.00% | [-5.74%, 1.52%] | 0.035 | 4.69% | 0.029 / 5.22% |
| D40 | **inconclusive** (sign uncertain, low power) | -0.55% | [-4.23%, 1.73%] | 0.060 | 3.44% | 0.040 / 4.29% |
| D50 | **inconclusive** (sign uncertain, low power) | 0.74% | [-3.72%, 2.06%] | 0.068 | 3.23% | 0.036 / 4.58% |

## The frontier

For each drawdown limit and largest acceptable probability of breaching it: the portfolio with the highest median after-tax log growth among those that respect it (best), and the one it resolves to (chosen) — the simplest, then the lowest-turnover, of the best and the portfolios whose growth the bootstrap cannot tell apart from it (their paired interval at the confirmation level holds zero). Choosing a point is the owner's authorisation (DECISIONS D60), not this report's.

| Drawdown limit | Breach probability at most | Chosen | Its outcome against the reference | Best by median growth | Cannot be told apart from the best | Respecting it |
|---|---|---|---|---|---|---|
| 10% | 5% | D10 | **inconclusive** (sign uncertain, low power) | D10 | none | 1 |
| 10% | 10% | D10 | **inconclusive** (sign uncertain, low power) | D10 | none | 1 |
| 10% | 25% | D10 | **inconclusive** (sign uncertain, low power) | D10 | none | 1 |
| 10% | 50% | D10 | **inconclusive** (sign uncertain, low power) | B | D10 | 2 |
| 20% | 5% | A | **inconclusive** (sign uncertain, low power) | A | B, C, D10, D20 | 5 |
| 20% | 10% | A | **inconclusive** (sign uncertain, low power) | A | B, C, D10, D20 | 5 |
| 20% | 25% | A | **inconclusive** (sign uncertain, low power) | A | B, C, D10, D20 | 5 |
| 20% | 50% | A | **inconclusive** (sign uncertain, low power) | D30 | A, B, C, D10, D20 | 6 |
| 30% | 5% | A | **inconclusive** (sign uncertain, low power) | A | B, C, D10, D20 | 5 |
| 30% | 10% | A | **inconclusive** (sign uncertain, low power) | D30 | A, B, C, D10, D20 | 6 |
| 30% | 25% | 60/40 | **inconclusive** (sign uncertain, low power) | 60/40 | A, B, C, D10, D20, D30 | 7 |
| 30% | 50% | 60/40 | **inconclusive** (sign uncertain, low power) | D40 | 60/40, A, B, C, D10, D20, D30 | 8 |
| 40% | 5% | 60/40 | **inconclusive** (sign uncertain, low power) | 60/40 | A, B, C, D10, D20, D30 | 7 |
| 40% | 10% | 60/40 | **inconclusive** (sign uncertain, low power) | D40 | 60/40, A, B, C, D10, D20, D30 | 8 |
| 40% | 25% | 60/40 | **inconclusive** (sign uncertain, low power) | D50 | 60/40, A, B, C, D10, D20, D30, D40 | 9 |
| 40% | 50% | equity reference | the reference itself | equity reference | 60/40, A, B, C, D10, D20, D30, D40, D50 | 10 |
| 50% | 5% | 60/40 | **inconclusive** (sign uncertain, low power) | D40 | 60/40, A, B, C, D10, D20, D30 | 8 |
| 50% | 10% | 60/40 | **inconclusive** (sign uncertain, low power) | D50 | 60/40, A, B, C, D10, D20, D30, D40 | 9 |
| 50% | 25% | equity reference | the reference itself | equity reference | 60/40, A, B, C, D10, D20, D30, D40, D50 | 10 |
| 50% | 50% | equity reference | the reference itself | equity reference | 60/40, A, B, C, D10, D20, D30, D40, D50 | 10 |

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
| coverage | yes | 100.0% of 250 rebalance dates fully priced (pre-registered minimum 1) |
| effective observations | yes | 249 non-overlapping months (pre-registered minimum 240) |

## Portfolios after tax

| Portfolio | Liquidation value | Log growth / yr | Max drawdown | Costs | Tax paid | Trades | Turnover / yr | Median bootstrap growth | P5 terminal wealth multiple | Median minus ln(1 + 30%) | Median minus ln(1 + 50%) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| equity reference | 445,179.64 | 7.20% | -49.18% | 2,005.83 | 122,048.42 | 2 | 15.19% | 7.13% | 1.591 | -19.10% | -33.41% |
| 60/40 | 283,222.80 | 5.02% | -27.48% | 4,088.45 | 51,201.32 | 38 | 17.53% | 5.07% | 1.613 | -21.16% | -35.47% |
| A | 191,897.82 | 3.14% | -10.95% | 6,172.33 | 23,519.65 | 253 | 18.20% | 3.22% | 1.358 | -23.02% | -37.33% |
| B | 161,760.86 | 2.32% | -6.32% | 23,568.34 | 15,022.30 | 1021 | 102.49% | 2.30% | 1.163 | -23.93% | -38.24% |
| C | 179,170.13 | 2.81% | -6.88% | 23,165.79 | 19,869.08 | 1041 | 102.59% | 2.66% | 1.186 | -23.58% | -37.89% |
| D10 | 160,024.25 | 2.27% | -4.07% | 1,563.50 | 15,222.92 | 61 | 18.19% | 1.95% | 1.240 | -24.28% | -38.59% |
| D20 | 216,530.60 | 3.72% | -8.05% | 2,349.32 | 30,985.00 | 62 | 24.50% | 3.15% | 1.328 | -23.09% | -37.40% |
| D30 | 294,057.81 | 5.20% | -11.99% | 3,129.14 | 53,778.69 | 61 | 28.05% | 4.36% | 1.376 | -21.88% | -36.19% |
| D40 | 397,134.19 | 6.65% | -16.54% | 3,780.59 | 92,715.00 | 47 | 28.58% | 5.51% | 1.404 | -20.72% | -35.03% |
| D50 | 519,036.15 | 7.94% | -19.79% | 4,321.88 | 151,695.63 | 22 | 27.66% | 6.49% | 1.409 | -19.75% | -34.06% |

The owner's stated aspiration (DECISIONS D60) is reported as a distance from each portfolio's median after-tax log growth, never as a gate.

Sales not made because their costs would exceed what they raise (the units stay held, and count as nothing at the horizon): equity reference 0 on the historical path, on 0 bootstrap paths; 60/40 0 on the historical path, on 0 bootstrap paths; A 0 on the historical path, on 0 bootstrap paths; B 0 on the historical path, on 167 bootstrap paths; C 0 on the historical path, on 139 bootstrap paths; D10 0 on the historical path, on 0 bootstrap paths; D20 0 on the historical path, on 0 bootstrap paths; D30 0 on the historical path, on 0 bootstrap paths; D40 0 on the historical path, on 0 bootstrap paths; D50 0 on the historical path, on 0 bootstrap paths.

Probability that a bootstrap path's mark-to-market drawdown reaches each limit:

| Portfolio | 10% | 20% | 30% | 40% | 50% |
|---|---|---|---|---|---|
| equity reference | 100.0% | 91.3% | 72.3% | 37.3% | 13.4% |
| 60/40 | 99.0% | 57.1% | 10.7% | 1.4% | 0.1% |
| A | 70.6% | 4.9% | 0.1% | 0.0% | 0.0% |
| B | 48.5% | 0.7% | 0.0% | 0.0% | 0.0% |
| C | 71.9% | 3.2% | 0.0% | 0.0% | 0.0% |
| D10 | 0.9% | 0.0% | 0.0% | 0.0% | 0.0% |
| D20 | 69.2% | 4.0% | 0.0% | 0.0% | 0.0% |
| D30 | 97.9% | 47.7% | 5.7% | 0.2% | 0.0% |
| D40 | 100.0% | 75.1% | 34.4% | 6.5% | 0.7% |
| D50 | 100.0% | 91.0% | 58.5% | 22.7% | 5.8% |

## Portfolios before tax

The same strategies, trading the same way, with no tax withheld, settled or paid.

| Portfolio | Liquidation value | Log growth / yr | Max drawdown | Costs | Tax paid | Trades |
|---|---|---|---|---|---|---|
| equity reference | 567,228.05 | 8.36% | -49.18% | 2,005.83 | 0.00 | 2 |
| 60/40 | 339,055.36 | 5.88% | -27.48% | 4,075.84 | 0.00 | 24 |
| A | 220,213.44 | 3.80% | -10.47% | 5,442.56 | 0.00 | 144 |
| B | 181,487.34 | 2.87% | -5.92% | 23,701.39 | 0.00 | 962 |
| C | 203,407.02 | 3.42% | -6.85% | 23,744.25 | 0.00 | 978 |
| D10 | 177,717.39 | 2.77% | -4.07% | 1,475.02 | 0.00 | 32 |
| D20 | 253,984.63 | 4.49% | -8.04% | 2,304.23 | 0.00 | 36 |
| D30 | 358,727.06 | 6.16% | -11.99% | 3,102.90 | 0.00 | 36 |
| D40 | 504,030.77 | 7.79% | -16.53% | 3,782.66 | 0.00 | 28 |
| D50 | 683,126.62 | 9.26% | -19.80% | 4,363.26 | 0.00 | 18 |

## Static weights

Each core strategy's weights by role before the tilt.

| Role | 60/40 | A | B | C | D10 | D20 | D30 | D40 | D50 |
|---|---|---|---|---|---|---|---|---|---|
| WORLD | 60.00% | 9.03% | 9.03% | 16.39% | 20.00% | 40.00% | 60.00% | 80.00% | 100.00% |
| EGB_INT | 40.00% | 33.80% | 33.80% | 27.53% | n/a | n/a | n/a | n/a | n/a |
| EM | n/a | 6.52% | 6.52% | 11.81% | n/a | n/a | n/a | n/a | n/a |
| COMMOD | n/a | 10.82% | 10.82% | 17.05% | n/a | n/a | n/a | n/a | n/a |
| EGB_LONG | n/a | 11.27% | 11.27% | 9.18% | n/a | n/a | n/a | n/a | n/a |
| UST | n/a | 10.90% | 10.90% | 7.58% | n/a | n/a | n/a | n/a | n/a |
| IL | n/a | 12.30% | 12.30% | 7.41% | n/a | n/a | n/a | n/a | n/a |
| GOLD | n/a | 5.36% | 5.36% | 3.05% | n/a | n/a | n/a | n/a | n/a |
| CASH | n/a | n/a | n/a | n/a | 80.00% | 60.00% | 40.00% | 20.00% | 0.00% |

## Year by year

Mark-to-market value, after tax paid as it arose (the last year ends at the horizon before the terminal sale and its tax):

| Year | equity reference | 60/40 | A | B | C | D10 | D20 | D30 | D40 | D50 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2005 | -0.30% | -0.62% | -0.96% | -0.96% | -0.86% | -0.30% | -0.30% | -0.30% | -0.30% | -0.30% |
| 2006 | 7.19% | 4.12% | -1.42% | -1.86% | -0.31% | 3.67% | 4.55% | 5.43% | 6.31% | 7.19% |
| 2007 | -1.86% | -0.14% | 2.84% | 3.19% | 3.66% | 2.82% | 1.74% | 0.68% | -0.36% | -1.38% |
| 2008 | -37.76% | -19.71% | -1.15% | 1.72% | -3.34% | 1.46% | -0.87% | -3.27% | -5.63% | -7.99% |
| 2009 | 25.68% | 16.25% | 9.90% | 4.32% | 7.11% | 3.25% | 5.25% | 7.63% | 10.06% | 12.61% |
| 2010 | 19.29% | 14.29% | 16.01% | 13.75% | 15.71% | 3.98% | 7.58% | 11.16% | 15.01% | 18.68% |
| 2011 | -2.58% | 2.00% | 7.77% | 2.95% | -0.91% | -1.71% | -3.35% | -5.44% | -7.95% | -9.67% |
| 2012 | 13.82% | 9.62% | 5.25% | 2.44% | 1.82% | 1.70% | 3.32% | 5.28% | 7.21% | 9.31% |
| 2013 | 20.96% | 11.95% | -5.93% | -3.42% | -1.30% | 4.13% | 8.18% | 12.42% | 16.89% | 20.92% |
| 2014 | 19.26% | 13.87% | 12.64% | 7.88% | 7.33% | 3.95% | 7.23% | 11.21% | 15.67% | 19.24% |
| 2015 | 10.20% | 6.41% | 1.52% | 2.65% | 2.19% | 1.60% | 4.06% | 6.16% | 7.80% | 10.19% |
| 2016 | 10.50% | 6.64% | 7.58% | 4.70% | 5.53% | 2.05% | 3.44% | 5.62% | 8.55% | 10.50% |
| 2017 | 7.30% | 3.84% | -2.64% | -2.72% | -1.05% | 0.53% | 2.83% | 4.38% | 6.02% | 7.30% |
| 2018 | -4.30% | -2.29% | -0.05% | -1.95% | -3.39% | -1.29% | -2.57% | -3.36% | -4.08% | -4.30% |
| 2019 | 29.76% | 17.81% | 9.34% | 7.16% | 7.87% | 5.53% | 11.16% | 17.23% | 23.43% | 29.74% |
| 2020 | 6.12% | 3.37% | 3.35% | 0.66% | -0.35% | 0.18% | 1.45% | 2.72% | 5.00% | 6.12% |
| 2021 | 30.80% | 17.45% | 7.61% | 4.80% | 8.95% | 5.71% | 11.66% | 17.96% | 24.64% | 30.79% |
| 2022 | -12.95% | -13.47% | -10.23% | -4.74% | -3.66% | -3.48% | -6.33% | -8.87% | -11.43% | -12.95% |
| 2023 | 19.36% | 12.59% | 3.06% | 1.31% | 1.89% | 6.05% | 9.45% | 12.74% | 16.14% | 19.35% |
| 2024 | 26.34% | 15.83% | 7.39% | 5.42% | 7.82% | 8.17% | 12.68% | 17.31% | 22.46% | 26.33% |
| 2025 | 6.55% | 3.71% | 1.68% | 1.56% | 3.07% | 3.06% | 3.19% | 4.10% | 4.60% | 6.55% |
| 2026 | 15.38% | 8.16% | 4.95% | 5.23% | 10.75% | 4.17% | 7.15% | 9.96% | 12.53% | 15.38% |

Before tax:

| Year | equity reference | 60/40 | A | B | C | D10 | D20 | D30 | D40 | D50 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2005 | -0.30% | -0.62% | -0.96% | -0.96% | -0.86% | -0.30% | -0.30% | -0.30% | -0.30% | -0.30% |
| 2006 | 7.19% | 4.12% | -1.42% | -1.86% | -0.31% | 3.67% | 4.55% | 5.43% | 6.31% | 7.19% |
| 2007 | -1.86% | -0.14% | 2.84% | 3.19% | 3.66% | 2.82% | 1.74% | 0.68% | -0.36% | -1.38% |
| 2008 | -37.76% | -19.71% | -1.07% | 1.72% | -3.34% | 1.61% | -0.59% | -2.83% | -5.06% | -7.27% |
| 2009 | 25.68% | 16.46% | 10.09% | 4.32% | 7.11% | 3.28% | 5.31% | 7.72% | 10.17% | 12.65% |
| 2010 | 19.29% | 14.29% | 15.99% | 13.75% | 15.94% | 4.32% | 8.16% | 11.95% | 15.67% | 19.28% |
| 2011 | -2.58% | 2.00% | 8.51% | 3.22% | -0.76% | -1.75% | -3.35% | -5.44% | -7.97% | -9.67% |
| 2012 | 13.82% | 9.62% | 5.58% | 3.77% | 3.34% | 2.11% | 4.10% | 6.10% | 8.15% | 10.19% |
| 2013 | 20.96% | 11.96% | -5.84% | -3.02% | -1.02% | 4.32% | 8.43% | 12.68% | 16.92% | 20.96% |
| 2014 | 19.26% | 13.87% | 13.16% | 8.77% | 8.02% | 3.95% | 7.81% | 11.59% | 15.67% | 19.26% |
| 2015 | 10.20% | 6.66% | 1.46% | 2.65% | 2.17% | 2.05% | 4.11% | 6.18% | 8.22% | 10.20% |
| 2016 | 10.50% | 7.15% | 7.56% | 5.08% | 5.92% | 2.11% | 4.06% | 6.26% | 8.59% | 10.50% |
| 2017 | 7.30% | 3.88% | -2.25% | -2.31% | -0.74% | 1.08% | 2.86% | 4.42% | 6.02% | 7.30% |
| 2018 | -4.30% | -2.29% | 0.55% | -1.31% | -3.12% | -1.28% | -2.00% | -2.77% | -3.53% | -4.30% |
| 2019 | 29.76% | 17.80% | 9.45% | 7.31% | 8.12% | 5.59% | 11.23% | 17.28% | 23.49% | 29.76% |
| 2020 | 6.12% | 3.99% | 3.54% | 0.91% | 0.06% | 0.78% | 2.12% | 3.46% | 5.00% | 6.12% |
| 2021 | 30.80% | 17.51% | 8.41% | 5.40% | 9.40% | 5.80% | 11.72% | 18.00% | 24.64% | 30.80% |
| 2022 | -12.95% | -12.82% | -9.88% | -4.15% | -2.68% | -2.74% | -5.61% | -8.16% | -10.75% | -12.95% |
| 2023 | 19.36% | 13.50% | 3.61% | 2.64% | 2.60% | 6.14% | 9.56% | 12.79% | 16.25% | 19.36% |
| 2024 | 26.34% | 15.93% | 7.80% | 6.02% | 8.15% | 8.35% | 12.77% | 17.28% | 22.50% | 26.34% |
| 2025 | 6.55% | 4.47% | 2.41% | 2.42% | 3.25% | 3.22% | 4.01% | 4.88% | 5.66% | 6.55% |
| 2026 | 15.38% | 8.26% | 5.72% | 5.52% | 10.74% | 4.21% | 7.28% | 10.05% | 12.67% | 15.38% |

Tax paid, by the year it was paid (the horizon's includes the terminal sale's):

| Paid in | equity reference | 60/40 | A | B | C | D10 | D20 | D30 | D40 | D50 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2008 | n/a | n/a | 114.74 | n/a | n/a | 157.00 | 315.26 | 473.51 | 631.77 | 788.77 |
| 2009 | n/a | 159.19 | 69.46 | n/a | n/a | 20.13 | 51.01 | 79.17 | 96.85 | 32.27 |
| 2010 | n/a | 3.93 | n/a | n/a | 113.57 | 293.07 | 581.28 | 783.79 | 641.90 | 575.38 |
| 2011 | n/a | n/a | n/a | 300.19 | 169.41 | n/a | n/a | n/a | n/a | n/a |
| 2012 | n/a | n/a | 284.75 | 1,695.61 | 1,845.99 | 442.72 | 886.31 | 946.06 | 1,069.69 | 1,003.50 |
| 2013 | n/a | n/a | 63.84 | 482.94 | 359.55 | 174.80 | 353.28 | 216.95 | 27.15 | 25.01 |
| 2014 | n/a | n/a | 393.36 | 1,036.84 | 771.50 | 10.28 | 590.84 | 545.85 | n/a | n/a |
| 2015 | n/a | 374.70 | 86.84 | n/a | n/a | 559.15 | 32.67 | 43.31 | 716.89 | n/a |
| 2016 | n/a | 791.02 | 805.78 | 414.59 | 536.68 | 32.81 | 840.64 | 943.16 | 64.25 | n/a |
| 2017 | n/a | 78.59 | 654.69 | 556.29 | 416.45 | 706.37 | 46.98 | 62.97 | n/a | n/a |
| 2018 | n/a | n/a | 725.71 | 808.48 | 340.31 | 34.45 | 907.81 | 1,113.55 | 1,246.41 | n/a |
| 2019 | n/a | n/a | 154.21 | 115.62 | 158.83 | 26.72 | 57.68 | 94.14 | 124.28 | n/a |
| 2020 | n/a | 1,201.56 | 181.86 | 204.68 | 386.31 | 768.49 | 1,018.44 | 1,363.56 | n/a | n/a |
| 2021 | n/a | 126.29 | 1,142.90 | 802.80 | 540.55 | 42.52 | 67.37 | 92.57 | n/a | n/a |
| 2022 | n/a | 1,675.02 | 913.36 | 778.97 | 1,504.20 | 1,042.18 | 1,353.19 | 1,680.56 | 2,211.39 | n/a |
| 2023 | n/a | 1,861.06 | 863.66 | 1,745.87 | 954.50 | 59.84 | 96.52 | 134.63 | 251.87 | n/a |
| 2024 | n/a | 204.49 | 522.19 | 376.71 | 485.71 | 55.73 | 55.73 | n/a | 58.55 | n/a |
| 2025 | n/a | 2,033.16 | 990.55 | 503.34 | 469.07 | 73.75 | 1,599.42 | 2,141.87 | 3,960.84 | n/a |
| 2026 | 122,048.42 | 42,692.32 | 15,551.76 | 5,199.37 | 10,816.44 | 10,722.91 | 22,130.57 | 43,063.06 | 81,613.15 | 149,270.71 |

## Robustness

- 60/40: subperiods 2005-12-01 to 2015-12-31 -0.25% sign holds, 2016-01-01 to 2026-09-30 -4.18% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- A: subperiods 2005-12-01 to 2015-12-31 -0.92% sign holds, 2016-01-01 to 2026-09-30 -7.57% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- B: subperiods 2005-12-01 to 2015-12-31 -1.77% sign holds, 2016-01-01 to 2026-09-30 -8.70% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- C: subperiods 2005-12-01 to 2015-12-31 -1.93% sign holds, 2016-01-01 to 2026-09-30 -7.75% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- D10: subperiods 2005-12-01 to 2015-12-31 -2.43% sign holds, 2016-01-01 to 2026-09-30 -8.23% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- D20: subperiods 2005-12-01 to 2015-12-31 -1.36% sign holds, 2016-01-01 to 2026-09-30 -7.00% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- D30: subperiods 2005-12-01 to 2015-12-31 -0.34% sign holds, 2016-01-01 to 2026-09-30 -5.99% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- D40: subperiods 2005-12-01 to 2015-12-31 0.62% **sign fails**, 2016-01-01 to 2026-09-30 -5.04% sign holds; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).
- D50: subperiods 2005-12-01 to 2015-12-31 1.54% sign holds, 2016-01-01 to 2026-09-30 -3.98% **sign fails**; break-even cost multiplier none (below theta even without costs) (declared cost uncertainty 3.67: **not exceeded**).

- Placebo: not applicable: a portfolio comparison has no signal to lag; its robustness is the sign in each subperiod and the break-even cost multiplier

## Selection across trials

- Trials of this study, this run's candidates included: 18
- 60/40: monthly Sharpe -0.1371 against 0.0969 expected from the best of 18 trial(s); probability 0.000
- A: monthly Sharpe -0.1191 against 0.0969 expected from the best of 18 trial(s); probability 0.001
- B: monthly Sharpe -0.1372 against 0.0969 expected from the best of 18 trial(s); probability 0.000
- C: monthly Sharpe -0.1386 against 0.0969 expected from the best of 18 trial(s); probability 0.000
- D10: monthly Sharpe -0.1501 against 0.0969 expected from the best of 18 trial(s); probability 0.000
- D20: monthly Sharpe -0.1311 against 0.0969 expected from the best of 18 trial(s); probability 0.000
- D30: monthly Sharpe -0.0982 against 0.0969 expected from the best of 18 trial(s); probability 0.002
- D40: monthly Sharpe -0.0403 against 0.0969 expected from the best of 18 trial(s); probability 0.018
- D50: monthly Sharpe 0.0443 against 0.0969 expected from the best of 18 trial(s); probability 0.195

## Structure check

Descriptive, before tax and never an outcome: the tilt with this study's constants on ff3_market.Mkt-RF + ff3_market.RF against ff3_market.RF, monthly, 0.30% a leg, against holding the market.

| Check | Passed | Detail |
|---|---|---|
| data loaded | yes | every series is in the research store |
| point in time | yes | every series is release_lag |
| history | yes | 1202 common months (the tilt needs 13 first) |

- From 1927-07-31 to 2026-08-31; the tilt held less than the whole market in 411 months.

| Arm | Log growth / yr | Max drawdown | Breach probability by limit |
|---|---|---|---|
| tilt | 9.47% | -49.19% | 10%: 100.0%, 20%: 100.0%, 30%: 84.5%, 40%: 29.9%, 50%: 4.6% |
| held | 9.72% | -83.65% | 10%: 100.0%, 20%: 100.0%, 30%: 100.0%, 40%: 99.4%, 50%: 90.6% |

- Difference in log growth, tilt minus held: -0.25%, block-bootstrap interval of the realised returns [-1.88%, 1.54%]

| Subperiod | Growth difference / yr | Max drawdown difference |
|---|---|---|
| 1926-07-01 to 1949-12-31 | 1.47% | 34.46% |
| 1950-01-01 to 1979-12-31 | -0.49% | 31.31% |
| 1980-01-01 to 2004-12-31 | -1.01% | 19.11% |
| 2005-01-01 to 2026-08-31 | -0.83% | 28.32% |

## Data

| Series | Vendor | Point in time | Downloaded | SHA-256 | Source |
|---|---|---|---|---|---|
| msci_world_eur.level | msci | as_observed | 2026-10-17T12:30:36Z | `d015bd8a00df2aacd9e594dece6a2a192d1189038f92b33843909d2824541feb` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=990100&index_variant=NETR&currency_symbol=EUR |
| msci_em_eur.level | msci | as_observed | 2026-10-17T12:30:37Z | `9da2a456b3585e2869ad6d515ddf95f9ecc17f38ae4be1955492d811bccac99c` | https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?index_codes=891800&index_variant=NETR&currency_symbol=EUR |
| bund_zero_05y.level | derived | as_observed | 2026-10-17T12:30:45Z | `7cf9ad387d51d448901b9dc0db4185e609739703372f7d8697ff5b175fcdb65b` | derived: 5-year constant-maturity zero from bbk_bund_05y.value and bbk_bund_04y.value |
| bund_zero_17y.level | derived | as_observed | 2026-10-17T12:30:45Z | `912c86d9af0cde4fee1c73119e2684b6cd38e6fe43fcd8c773af2c8f1352c4cf` | derived: 17-year constant-maturity zero from bbk_bund_17y.value and bbk_bund_16y.value |
| tip.adj_close | yahoo | as_observed | 2026-10-17T12:30:45Z | `8d3b32b988a3f05171cd927726395b13686f397ef00b736a7d7af2573d521d4a` | yfinance Ticker('TIP').history(auto_adjust=False, actions=True) |
| ief.adj_close | yahoo | as_observed | 2026-10-17T12:30:48Z | `4f74dff3ec9f28f2bf6d294c2df7ae81f3f53c8894ef15f093f90c3d309b35ec` | yfinance Ticker('IEF').history(auto_adjust=False, actions=True) |
| gld.close | yahoo | as_observed | 2026-10-17T12:30:48Z | `9f045f89dd8a66aa84ef9129831b9ca183f04a7444dd9e71779b0ab2b62cf3bf` | yfinance Ticker('GLD').history(auto_adjust=False, actions=True) |
| crsox.adj_close | yahoo | as_observed | 2026-10-17T12:30:49Z | `e98755151d0deb60ca34e51ce237321829d32589d275bf5bf946c60000925554` | yfinance Ticker('CRSOX').history(auto_adjust=False, actions=True) |
| euro_cash.level | derived | as_observed | 2026-10-17T12:30:45Z | `bb1c5c18dee220fd8551c03a45eefe5c016879daec1840d4686510dcfbe115c5` | derived: overnight accrual of ecb_eonia.value through 2021-12-31, then ecb_estr.value |
| ecb_fx.USD | ecb | as_observed | 2026-10-17T12:30:34Z | `8e925cef1572f8df5c8743229a761b029180f3b5cd4aa2213592192964ebd793` | https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip |

- Coverage: 100.00% of rebalance dates fully priced
- Without data: none
- Entering after the start: none
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
- Monthly marks: drawdowns understate falls inside a month, and no exit inside a month is simulated.
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
