---
# The scoreboard's rules (DECISIONS D62, D63, D65, D149, D155, D156; MASTER_PLAN §5.9, §5.11,
# §5.19). Merged in its own pull request, with no version change, before the scoreboard grades,
# calibrates, weighs, promotes, monitors or funds anything; its lock follows the merge. Nothing
# here may change after the merge: a change is a new pre-registration with a new id.
id: scoreboard
title: How the scoreboard grades forward, calibrates, learns, promotes, monitors and funds
kind: scoreboard
rationale: >-
  A forward record is read once, by rules fixed before it exists. Without them every look at a
  growing record is a new chance to find an edge that is not there, and a minimum counted in
  predictions (many names in one week share the market's move) promises evidence the record does
  not hold. These rules decide when a layer's forward record says anything, when a stated
  probability may size a position, when a component's weight moves, when a pattern trades, when
  live results take capital back, and how capital steps up.
citation: >-
  Newey and West (1987), Econometrica 55(3), on the standard error of a dependent mean; Kish
  (1965), Survey Sampling, on the design effect n / (1 + (n - 1) rho); Brier (1950), Monthly
  Weather Review 78(1), and Murphy (1973), Journal of Applied Meteorology 12(4), on the score of a
  stated probability and its skill against a base rate; Efron and Morris (1975), Journal of the
  American Statistical Association 70(350), on shrinking many estimates toward a common prior;
  Lan and DeMets (1983), Biometrika 70(3), on spending a false-alarm probability over repeated
  looks; McLean and Pontiff (2016), Journal of Finance 71(1), on effects that shrink after
  publication.
dependence:
  rho: "0.25"
  rho_estimated: "0.248"
  rho_inputs_sha256: 5ee96b0d4ebfd5df3a46066237761609f9fe1b27abfed528c9b1e2f01f9d7029
  overlap: divide_by_horizon_weeks
grading:
  observation: first_per_layer_instrument_direction_week
  cohort: iso_week
  outcome: signed_excess_log_return
  currency: base
  reference_role: world_equity
  horizon_days: 28
  newey_west_lags: horizon_weeks
  alpha: "0.05"
  theta_per_horizon: "0.005"
  min_cohorts: 26
  min_effective: "30"
  max_mark_age_days: 7
panel:
  universe: tradable_candidates
  horizon_days: 28
  metric: rank_ic
  theta: "0.03"
  min_names: 20
  min_coverage: "0.6"
  min_cohorts: 26
  min_effective: "30"
  probability: p_outperform
  base_rate: climatology
calibration:
  min_cohorts: 26
  min_effective: "50"
  brier_skill_above: "0"
  shrink: slope_lower_bound
  shrink_alpha: "0.05"
weights:
  method: empirical_bayes_toward_zero
  min_components: 3
  min_cohorts: 26
  min_effective: "30"
  every_months: 1
patterns:
  promote: [sufficient]
  pool: [inconclusive_positive]
  pooling: inverse_variance
  min_cohorts: 26
  min_effective: "30"
  shadow_upper_above: "0"
  retire: new_preregistration
monitoring:
  period: month
  first_look_month: 3
  planned_months: 120
  spending: pocock_type
  combine_looks: union_bound
  gap:
    alpha: "0.10"
    tolerance_per_month: "0.001"
    distribution: student_t
    action: freeze_new_entries
  twin:
    alpha: "0.05"
    distribution: normal
    action: shadow_at_zero_weight
budgets:
  stock_book:
    first_share: "0.50"
    share_step: "0.10"
    ceiling: investable_less_cash_floor
    every_months: 12
    record: live
    min_months_first: 12
    min_months_between: 12
    min_closed_first: 30
    min_closed_between: 15
    retest: pooled_with_live
  other_layer:
    first_share_of_sleeve: "0.25"
    share_step_of_sleeve: "0.25"
    ceiling: sleeve_budget
    every_months: 12
    record: live
    min_months_first: 12
    min_months_between: 12
    min_closed_first: 30
    min_closed_between: 15
    retest: pooled_with_live
  thesis_book: thesis-book
trial_budget: 0
---

# How the scoreboard grades forward, calibrates, learns, promotes, monitors and funds

A rule set, not a test of an effect: it fixes, before any forward record exists, how every record
the system produces is read and what each reading may change. No layer is active at this merge, no
pattern is in shadow, and the thesis book's record is empty, so no rule was chosen with its result
known. A layer's own pre-registration may state any of these quantities for that layer (its
horizon, θ, steps, looks, minimums); it then overrides the matching rule here, and only for that
layer. The thesis book's capital is H13's (`research/preregistrations/thesis-book.md`), not this
file's. Not investment advice.

## Dependence and effective observations

Names graded in the same week share the market's move and much else, so a week's predictions are
not independent observations (DECISIONS D63). A cohort of `n` observations whose outcomes
correlate on average by ρ carries

    n_eff(n) = n / (1 + (n − 1) ρ)

and consecutive weekly cohorts graded over `h` weeks share `h − 1` of them, so a record's
effective observations are

    N_eff = Σ over cohorts c of n_eff(n_c) / h

**ρ = 0.25**, estimated from history before this merge with the harness's `panel_correlation`
(`research/explore/scoreboard_rho/estimate.py` on the research store; inputs SHA-256
`5ee96b0d…f9d7029`): the mean pairwise correlation of four-week excess log returns in euro against
the equity reference (IWDA), over non-overlapping four-week periods from January 2010 to November
2026, among the universe's tradable candidates with a Yahoo history.

| Names | Count | ρ by start week of the grid | ρ by half | Largest | n_eff of a full cohort |
|---|---|---|---|---|---|
| every tradable candidate | 51 | 0.222, 0.220, 0.218, 0.217 | 0.206, 0.216 | 0.222 | 4.2 |
| equity funds and stocks | 28 | 0.165, 0.205, 0.205, 0.165 | 0.248, 0.141 | 0.248 | 3.6 |
| stocks | 9 | 0.167, 0.201, 0.218, 0.172 | 0.149, 0.180 | 0.218 | 3.3 |

The registered ρ is the largest estimate, rounded up: a larger ρ counts fewer effective
observations, so the minimums below are reached later, never sooner. A cohort of 51 names at
ρ = 0.25 carries 3.8 effective observations; a year of weekly cohorts graded at four weeks carries
about 49. Every report states a record's count and its `N_eff` side by side, and the ρ its graded
outcomes show, which decides nothing.

## Forward grading (the decision rule of D62)

**Observation.** Each call a layer makes on an instrument: a signal, a component, a pattern (in
shadow or active), a thesis (on the date it is approved), an LLM nomination, a forecast of the
panel. One observation per layer, instrument, direction and ISO week: the first wins, and a call
repeated within the week is the same bet seen again.

**Outcome.** Over the horizon, 28 calendar days from the observation's date, the instrument's log
return in the base currency less the equity reference's (the vehicle the core holds for its role
`world_equity`), signed by direction: `+` for a call that it will do better, `−` for one that it
will do worse. Prices are the last close on or before each date, at most 7 days old, converted at
that day's rate; an observation without both marks is not graded (it is counted beside the record,
never filled in).

**Statistic.** Each weekly cohort's mean outcome; the record's estimate is the mean of the cohort
means with a Newey–West standard error of `h` lags (4 for 28 days: at least the horizon, D63),
floored at the independent variance. Its interval `[L, U]` is two-sided at level `1 − 2α`,
α = 0.05.

**Minimum.** No verdict before **26 graded weekly cohorts** and **N_eff ≥ 30**. Below it the
record is reported as pending and nothing is concluded.

**Verdict**, against θ = **0.005 per 28 days** (0.5 % of excess return a call; a layer's own θ
replaces it): sufficient when `L ≥ θ`; insufficient when `U < θ` (negative when `U < 0`);
inconclusive otherwise (positive when `L > 0`). A grade describes a record; only the rules below
let it move weight or capital.

θ: the liquid tier's assumed round trip is 0.4 % (`costs.yaml`: two legs of 0.1 % spread and
0.1 % slippage); a call acted on once per horizon must clear it. Below 0.5 % a call does not pay
the trade it asks for.

## The weekly forecast panel

Every ISO week, at its first run, the universe's tradable candidates with a close at most 7 days
old (the panel's universe, hashed and reported; a change of universe starts a new record) are
forecast by the LLM provider the analyst uses: for each, the probability that its 28-day return in
the base currency beats the equity reference's, and its expected excess return. The model sees
trailing price statistics only. A cohort is kept when at least 60 % of the universe was forecast
and at least 20 names were; otherwise nothing is kept and the week is retried at its next run.

Graded at 28 days: per cohort, the Spearman rank correlation (IC) of the expected excess with the
realised excess, and the same for 12-1 momentum (the deterministic baseline logged beside every
forecast). The verdict is D62's on the mean IC (Newey–West, 4 lags, level `1 − 2α`) against
**θ = 0.03**, after 26 cohorts and N_eff ≥ 30; the incremental IC (the model's IC less
momentum's) and the top-minus-bottom decile spread are reported beside it. The stated
probabilities are scored by Brier against the base rate: the share of all graded forecasts that
beat the reference (climatology, known only after the fact, which favours the base rate). A
reliability table bins them in fifths.

## Calibration (when a stated probability may size)

A layer that states probabilities (the panel; the analyst's second opinions; a source that states
the probability of reaching a target) is **calibrated** while, on its graded record:

- at least 26 weekly cohorts and **N_eff ≥ 50**; and
- **Brier skill above zero** against its base rate: each forecast's own base rate where the layer
  records one (the random-walk barrier probability for shares, `N(d₂)` at the target for options,
  MASTER_PLAN §5.11), else climatology.

It is uncalibrated when either fails, and is re-read each time the record is graded, so it can
switch back. Uncalibrated, the stated probability sizes nothing (the fixed risk budget, §5.11).
Calibrated, `p_eff = p_base + shrink × (p_stated − p_base)`, where **shrink** is the one-sided 95 %
lower bound of the slope `β` in

    o_i − p_base,i = β (p_stated,i − p_base,i) + ε_i

fitted through the origin by least squares over the graded forecasts, its standard error scaled by
`√(n / N_eff)`, and clipped to [0, 1]. The slope is the share of the stated edge that the outcomes
bore out; its lower bound keeps only what the record shows with confidence. A bound at or below
zero gives `p_eff = p_base`, which sizes nothing beyond the base rate.

## Component weights (empirical Bayes)

For the components of a layer that combines them (the event sleeve, when it exists), each
component `j` with at least 26 graded cohorts and N_eff ≥ 30 has a graded mean `m_j` and a
Newey–West standard error `s_j`. With at least 3 such components, the prior variance is
estimated by the method of moments, `τ² = max(0, mean(m_j²) − mean(s_j²))` (prior mean zero), and
each mean is shrunk:

    m*_j = m_j × τ² / (τ² + s_j²)

The weight of a component is `max(0, m*_j)` divided by the sum of those values; a component below
its minimum keeps the weight its registration gives it. When every `m*_j` is at or below zero no
component earns a weight from the record and the registration's weights stand. Weights are
recomputed at most once a month, and each set is stored with its SHA-256.

## Patterns: shadow to active, and retirement

A registry entry in `shadow` is graded every day at zero weight, by the rules above, on its own
horizon and θ. It becomes `active` only when all of these hold, logged and reported:

- its confirmation outcome is **sufficient**; or it was **inconclusive positive** and the pooled
  estimate is now sufficient: the inverse-variance-weighted mean of its confirmation estimate and
  its forward estimate, in the primary metric its registration names, with the pooled standard
  error `(1/s_h² + 1/s_f²)^(-1/2)`, its interval at the registration's level and θ. When the
  forward record does not measure that metric, nothing is pooled and the entry stays in shadow;
- its activation condition holds (computed from configuration, MASTER_PLAN §2);
- its forward record has at least 26 graded cohorts and N_eff ≥ 30, and that record's `U` is above
  zero (it does not show the pattern losing).

A pattern leaves `active` for `shadow` by the monitoring rule below. Only a new pre-registered
test retires a pattern, and only on an insufficient outcome.

## Monitoring (D65)

Each active layer has a **twin**: the layer's own signals on the same dates, simulated with the
cost model. From its activation, at the end of each calendar month `m` (the first month counted
whole), with `n = m` months of record:

- the **gap** `g_m`: the layer's live log return less its twin's, on the layer's capital;
- the **twin's excess** `e_m`: the twin's log return less the layer's reference's (as its test
  measured it).

**Looks** are every month end from month 3 to month 120. Each boundary spends its false-alarm
probability α over the planned 120 months by a Pocock-type function,
`α(t) = α · ln(1 + (e − 1) t)`, `t = m / 120`; look `m` spends `α(m/120) − α(m_prev/120)`
(month 3 spends `α(3/120)`), and the looks are combined by a union bound, so the probability of
any false alarm over the 118 looks is at most α under any dependence between them. After month
120 this registration is spent for that layer: it keeps its status until a new pre-registration
reads its record.

**Gap breach** (α = 0.10): with `ḡ` and `s` the mean and sample standard deviation of the gaps,

    t_gap = (ḡ + 0.001) / (s / √n)  ≤  −q_t(1 − α_m; n − 1)

that is, the live layer lags its twin by more than a tolerance of 0.1 % a month with confidence
(about 1.2 % a year: each leg's cost misestimated by 0.25 % at a monthly turnover of a fifth).
Action: the layer's new entries are frozen, its open positions keep their exits, an alert is sent
and an operational investigation opens. The freeze lifts only by an owner command after the
investigation; the looks go on.

**Twin breach** (α = 0.05): with `S = Σ e_m` and the backtest's predictive distribution for `n`
months, from the confirmation run's monthly series of the primary metric (mean `μ_b`, standard
deviation `σ_b`, standard error of the mean `se_b`),

    z_twin = (S − n μ_b) / √(n σ_b² + n² se_b²)  ≤  −Φ⁻¹(1 − α_m)

that is, the twin has done worse than the backtest predicts for the elapsed time, its own sampling
and the estimate's uncertainty allowed for. Action: the layer returns to shadow at zero weight, an
alert is sent and a research review opens; it trades again only under a new pre-registration.

Monitoring cannot tell decay from bad luck in a short record: it reduces risk and never declares
a verdict. A look with a month that cannot be measured (a missing mark, a twin that could not be
simulated) is not evaluable and changes nothing; it is not repeated.

## Capital: the exploration budget and its steps (D65, D149, D155, D156)

Shares are of investable capital (managed capital less what is reserved and unmanaged, Session 7).
The reserve holds whatever the layers do not, less the cash floor. No step needs an owner command;
the owner's commands remain the move to the live account, the kill switch and `flatten`.

**The stock book** (the Hunter's systematic book). On a sufficient outcome and its activation
condition it takes **0.50**. At every 12 months of its **live-account** record after that it
steps up by **0.10**, once per look, to investable capital less the cash floor at most, when all
of these hold:

- the **pooled re-test** is still sufficient: the registered test re-run on its confirmation
  sample with the book's realised live-account months appended, on the same primary metric, θ,
  level and dependence correction;
- no gap breach is outstanding, and the last look's gap is inside its boundary;
- at least 12 live-account months and 30 closed positions at the first step, and 12 months and 15
  closed positions more since the previous step.

Paper months and the twin's returns count for nothing here: both are simulations (D155).

**Any other layer** with a sufficient outcome (an options expression, an event-sleeve
combination, a pattern) takes **0.25 of its sleeve's budget** and steps by 0.25 of it, to the
sleeve's budget at most, under the same looks, re-test and minimums.

**The thesis book** follows H13 (`thesis-book`): its exploration share on the live account while
its paper record is not below its boundary, its steps and its return to shadow at H13's looks, per
source and conviction level.

**Taking capital back.** A twin breach returns a layer to shadow at zero weight (above); the
book's capital goes back to the reserve. A gap breach freezes new entries and keeps the share.

## Power and what the rules can find

Stated before any record, from the history above: the sample standard deviation of a name's
28-day excess log return is 0.055 across the tradable candidates and 0.060 across equity funds
and stocks. At 0.06 per observation and θ = 0.005:

| N_eff | Standard error per 28 days | Effect found with probability 0.8 (`L > 0`) | P(sufficient) at θ + 0.02 |
|---|---|---|---|
| 30 | 0.011 | 0.027 | 0.57 |
| 50 | 0.0085 | 0.021 | 0.76 |
| 100 | 0.006 | 0.015 | 0.95 |
| 200 | 0.0042 | 0.011 | 1.00 |

A layer's forward grade separates only large edges: 2.7 % a call over four weeks at the minimum.
The panel's mean IC has a standard error near `1/√50 / √(13 × years)`, so it finds an IC of 0.098
after a year and 0.069 after two with probability 0.8, and a true IC of 0.06 is sufficient against
θ = 0.03 with probability 0.19 after a year and 0.29 after two. The panel exists to show quickly
whether a model is useful at all, not to measure a small edge.

## Adjustment

Every rule tests one quantity at one level; the layers are not adjusted for one another, and each
report says how many layers and groups were read. The monitoring looks are combined by a union
bound within each boundary (above). Weights and calibration are estimates, not tests.

## Trial budget

Zero. There is no development sample and no exploratory trial.

## What each outcome leads to

- **Forward grade**: reported; it moves nothing by itself.
- **Calibrated**: the layer's stated probabilities size through the shrink above; uncalibrated,
  they size nothing.
- **Weights**: a new versioned set when the record allows one; otherwise the registration's.
- **Pattern**: `shadow` → `active` under the rule above, logged and reported.
- **Gap breach**: new entries frozen, alert, investigation; lifted by the owner.
- **Twin breach**: shadow at zero weight, alert, research review.
- **Budget step**: the share rises at a look when every condition holds, audited and reported.
