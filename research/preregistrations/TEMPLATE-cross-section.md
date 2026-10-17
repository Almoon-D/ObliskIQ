---
# A cross-section's pre-registration (DECISIONS D62, D63; MASTER_PLAN section 5.9): a selection
# signal tested as the Fama-MacBeth series of a top group's monthly excess return after costs
# and tax. Copy this file to research/preregistrations/<id>.md, fill every field, and merge it in
# its own pull request with no version change. After the merge, write its lock with
# `obliskiq research lock <file> --commit <merge commit>`; exploration
# (`obliskiq research run <file> --explore`) runs on the development sample and counts against
# the trial budget; `obliskiq research run <file>` produces the one confirmation run on the
# confirmation sample. Nothing here may change after the merge: a change is a new id.
id: study-id                     # lower-case letters, digits, underscores and hyphens
title: What is tested, in one line
kind: cross_section
mechanism: >-
  Why the effect should exist: the economic mechanism, and who is on the other side of the trade.
citation: Author (year), Journal; or a stated rationale when no literature exists.
universe: >-
  The members and how they were chosen, before any result was seen; the members a vendor does
  not serve are named too (with no series), so the share lost to survivorship is measured.
data_status: >-
  Each series, its vendor, and its point-in-time status; the known biases.
primary_metric: top_group_excess  # the candidate's monthly log return minus the other's, both
                                  # after costs and tax, the last month to liquidation value;
                                  # the mean times periods_per_year
alpha: null                      # null takes research.decision.alpha (0.05)
target_power: null               # null takes research.decision.target_power (0.8)
tests_in_confirmation: 1         # must equal the number of tests below (Bonferroni across them)
adjustment: >-
  Bonferroni across the tests of the confirmation run; the deflated Sharpe across every trial
  the registry holds for this id.
trial_budget: 2                  # exploratory tests allowed before the confirmation run
study:
  development:                   # exploration runs here
    start: 2002-01-01
    end: 2007-12-31
  confirmation:                  # the one confirmation run; later than the development sample
    start: 2008-01-01
    end: 2025-12-31
  calendar: USA                  # a member with a series: its observed days date the month ends
  initial_capital: "100000"      # in the base currency
  fx_dataset: null               # ECB reference rates when the vehicle's currency is not the base
  vehicle:                       # how every member is held
    currency: EUR
    venue: XETR                  # the listing venue picks the commission schedule
    asset_class: etf
    market_cap_usd: null         # the friction tier's size; null is the illiquid tier, labelled
    expense_ratio: "0.005"       # deducted from an index proxy day by day; null for fund prices
    kind: etf                    # the tax rules' kind of security
    issuer: representative UCITS issuer
  members:
    - key: USA
      name: MSCI USA
      price_series: msci_usa_eur.level   # null: the vendor does not serve it (lost)
      spans: [[2001-01-01, null]]        # first and last day of membership; null: still
    - key: JAPAN
      name: MSCI Japan
      price_series: msci_japan_eur.level
      spans: [[2001-01-01, null]]
  signal:
    lookback_months: 12          # momentum from 12 month ends back ...
    skip_months: 1               # ... to 1 month end back
  selection:
    enter_fraction: "0.3333"     # a name enters within the top third of the ranked members
    keep_fraction: "0.5"         # and is kept while within the top half
    reset_month: 12              # every holding back to equal weights at December's month end
  stop: null                     # {volatility_sessions, horizon_sessions, multiple}: a trailing
                                 # stop that many daily standard deviations, scaled to the
                                 # horizon, below the highest close since entry
  portfolios:                    # rule universe or top_group; lag_months > 0 is a placebo
    universe: {rule: universe, lag_months: 0, stop: false}
    momentum: {rule: top_group, lag_months: 0, stop: false}
tests:
  - name: the signal
    candidate: momentum
    against: universe
    theta: "0.02"                # the smallest net effect worth the added risk, a year
    theta_justification: Why an effect below theta is not worth having.
    sd_per_period: "0.015"       # sd of one month's excess, for the power before data
placebo: null                    # {candidate, against, test}: must not reach the test's theta
bootstrap:
  n_boot: 10000                  # moving-block resamples of each monthly series
  block_months: 12
  seed: 20261017
newey_west_lags: 12              # at least the horizon (one month)
periods_per_year: 12
subperiods:                      # inside the confirmation sample; the sign must hold in each
  - [2008-01-01, 2016-12-31]
  - [2017-01-01, 2025-12-31]
cost_uncertainty_multiplier: "2" # the break-even cost multiplier must exceed it (D63)
robustness: >-
  The sign in every subperiod; the break-even cost multiplier above the declared cost
  uncertainty; the placebo.
adequacy:                        # failing any is "not evaluable" (D62)
  min_coverage: "0.99"           # served member-dates with a fresh price
  max_lost_share: "0.05"         # members not served, on every rebalance date
  point_in_time: [as_observed]
  min_effective_observations: 120  # months in the confirmation sample
  min_development_observations: 60 # months in the development sample
---

# Title

Each section explains the matching front-matter field in prose. Not tax advice.

## Mechanism and citation

## Universe and survivorship

## Data and their point-in-time status

## The signal, the portfolios and the horizon

## Primary metric

## Theta and why

## Power

State the output of `obliskiq research power` for this file.

## Development and confirmation samples, and the trial budget

## Adjustment

## Robustness checks and the placebo

## What a sufficient, insufficient or inconclusive outcome would lead to (D62)
