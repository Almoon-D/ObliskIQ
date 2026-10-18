---
# A pre-registration (DECISIONS D62, D63) of a portfolio comparison; a selection signal tested
# as a cross-section starts from TEMPLATE-cross-section.md. Copy this file to
# research/preregistrations/<id>.md, fill every field, and merge it in its own pull request with
# no version change. After the merge, write its lock with
# `obliskiq research lock <file> --commit <merge commit>` and merge the lock; only then may
# `obliskiq research run <file>` produce the confirmation run. Nothing here may change after the
# merge: a change is a new pre-registration with a new id.
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
tests_in_confirmation: 1         # one test per candidate (Bonferroni across them); must equal
                                 # the number of candidates
adjustment: >-
  Bonferroni across the tests of the confirmation run; the deflated Sharpe across every trial
  the registry holds for this id.
trial_budget: 20                 # exploratory trials allowed before the confirmation run (each
                                 # candidate of an exploratory run is one trial); 0 for none
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
      expense_ratio: null        # a vehicle's yearly cost deducted from an index proxy; null when
                                 # the price already carries its fund's costs
      security:
        kind: etf
        isin: IE00B4L5Y983
        issuer: iShares
        tracked_index: MSCI World
        listed: true
  # The core's variants name roles: each role's primary vehicle and swap partner (instrument
  # keys), and what the variants share (environments, declared long-run risk, bands, and where
  # used the tilt, the tax-aware rule and `scaling`: the estimators of a total-risk strategy,
  # their daily data's start and the trading days in a year). Both null when only fixed_weights
  # and buy_and_hold are named.
  roles: null
  core: null
  reference:
    name: the reference
    strategy: buy_and_hold       # fixed_weights, buy_and_hold, banded_weights, risk_balance,
                                 # equity_overlay or total_risk (a base, a target volatility, a
                                 # floor, a largest change, an estimator or `declared`, tax_aware,
                                 # hold_costly_reductions)
    weights: {WORLD: "1"}
    simplicity: 1                # the frontier resolves to the lowest rank, then turnover
  candidates:                    # each one is tested against the reference
    - name: the candidate
      strategy: fixed_weights
      weights: {WORLD: "1"}
      simplicity: 2
  twins: []                      # simulated and reported, tested only through `comparisons`
comparisons: []                  # secondary tests: {name, candidate, against, theta,
                                 # theta_justification, sd_per_period}; each counts in Bonferroni
bootstrap:
  n_boot: 1000                   # paired moving-block bootstrap paths
  block_months: 12               # at least twice the longest lookback a portfolio reads (D92)
  seed: 20261016
  sensitivity: null              # {block_months: [three or more], n_boot}
drawdown_limits: ["0.2", "0.35"] # the probability of breaching each is reported (D60)
frontier: null                   # {breach_probabilities: [...]}: with drawdown_limits, the grid
aspiration: null                 # yearly growth rates reported as a distance, never a gate
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
  by_candidate: null             # {candidate: sd} where a candidate's differs
  periods: 192
  periods_per_year: 12
  names_per_period: 1
  rho: "0"                       # average correlation between names in a period
structure: null                  # a pre-tax check of the tilt on the longest history
                                 # (descriptive, never an outcome)
marks: month_end                 # or daily: drawdowns on daily closes (D88)
owner_point: null                # {limit, probability, horizon_years} (D91): outside it a
                                 # candidate cannot be sufficient (D92)
breach_horizons_years: []        # the owner's limit reached within each horizon (needs the point)
long_history: []                 # longer histories for the breach (D92 rule 4), before tax
forecast_check: null             # {loss: qlike}: each estimator's forecast, never an outcome
development: null                # {start, end}: exploration runs the comparisons there
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
