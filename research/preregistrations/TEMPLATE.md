---
# A pre-registration (DECISIONS D62, D63). Copy this file to research/preregistrations/<id>.md,
# fill every field, and merge it in its own pull request with no version change. After the
# merge, write its lock with `obliskiq research lock <file> --commit <merge commit>` and merge the
# lock; only then may `obliskiq research run <file>` produce the confirmation run. Nothing here
# may change after the merge: a change is a new pre-registration with a new id.
id: study-id                     # lower-case letters, digits, underscores and hyphens
title: What is tested, in one line
kind: portfolio_comparison       # the study kind the harness runs
mechanism: >-
  Why the effect should exist: the economic mechanism, and who is on the other side of the trade.
citation: Author (year), Journal; or a stated rationale when no literature exists.
universe: >-
  The instruments and how they were chosen, before any result was seen; what a universe listed
  today cannot show (members that disappeared before it was listed).
data_status: >-
  Each series, its vendor, and its point-in-time status (as observed, vintages, or a declared
  publication lag); the known biases.
primary_metric: log_growth_difference   # candidate minus reference, annualised log growth of
                                        # liquidation value after costs and tax (D60)
theta: "0.01"                    # the smallest net effect worth the added risk, a year
theta_justification: Why an effect below theta is not worth having.
alpha: null                      # null takes research.decision.alpha (0.05): the interval has
                                 # level 1 - 2 alpha, Bonferroni-adjusted over the tests below
target_power: null               # null takes research.decision.target_power (0.8)
tests_in_confirmation: 1         # the tests of the confirmation run (Bonferroni)
adjustment: >-
  Bonferroni across the tests of the confirmation run; the deflated Sharpe across every trial
  the registry holds for this id.
trial_budget: 20                 # exploratory trials allowed before the confirmation run
study:
  start: 2010-01-01              # the first rebalance date is the first month end on or after
  end: 2025-12-31                # the horizon: everything is sold and taxed on its month end
  rebalance: month_end
  calendar: WORLD                # the instrument whose sessions date the month ends
  initial_capital: "100000"      # in the base currency
  fx_dataset: ecb_fx             # ECB reference rates (null when every instrument is in the base)
  instruments:
    - key: WORLD
      price_series: eunl.close   # research_history series ids (`obliskiq research data load`)
      income_series: null        # dividends per unit, or null for an accumulating fund
      currency: EUR
      venue: XETR                # the listing venue: it picks the commission schedule
      asset_class: etf
      issuer_country: null       # transaction taxes follow the issuer; null for a fund
      income_country: null       # who withholds on its income (withholding_at_source in es.yaml)
      market_cap_usd: null       # for the friction tier; null is the illiquid tier, labelled
      security:
        kind: etf
        isin: IE00B4L5Y983
        issuer: iShares
        tracked_index: MSCI World
        listed: true
  candidate:
    name: the candidate
    strategy: fixed_weights      # fixed_weights or buy_and_hold
    weights: {WORLD: "1"}
  reference:
    name: the reference
    strategy: buy_and_hold
    weights: {WORLD: "1"}
bootstrap:
  n_boot: 1000                   # paired moving-block bootstrap paths
  block_months: 12
  seed: 20261016
drawdown_limits: ["0.2", "0.35"] # the probability of breaching each is reported (D60)
subperiods:                      # the sign must hold in each (D63)
  - [2010-01-01, 2017-12-31]
  - [2018-01-01, 2025-12-31]
cost_uncertainty_multiplier: "2" # the break-even multiplier must exceed it (D63): by default the
                                 # next friction tier up
robustness: >-
  The sign in every subperiod; the break-even cost multiplier above the declared cost
  uncertainty; the placebo, where the study kind has one.
adequacy:                        # failing any is "not evaluable" (D62)
  min_coverage: "1"              # share of rebalance dates with a fresh price for every instrument
  point_in_time: [as_observed, vintages]
  min_effective_observations: 120
power:                           # stated before any data: `obliskiq research power <file>`
  sd_per_period: "0.02"          # sd of one observation of the per-period metric
  periods: 192
  periods_per_year: 12
  names_per_period: 1
  rho: "0"                       # average correlation between names in a period
---

# Title

Each section explains the matching front-matter field in prose. Not tax advice.

## Mechanism and citation

## Universe

## Data and their point-in-time status

## Primary metric and horizon

## Theta and why

## Power

State the output of `obliskiq research power` for this file: the power to detect theta and the
effect detectable at the target power.

## Adjustment

## Robustness checks

## Trial budget

## What a sufficient, insufficient or inconclusive outcome would lead to (D62)
