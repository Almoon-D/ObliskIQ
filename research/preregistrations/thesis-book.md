---
# H13, a forward test of a book (DECISIONS D62, D63, D65, D156). Merged in its own pull request,
# with no version change, before any thesis exists; its lock follows the merge. Nothing here may
# change after the merge: a change is a new pre-registration with a new id.
id: thesis-book
title: Written ideas from the LLM analyst and the owner, held in a concentrated book
kind: forward_book
hypothesis: H13
mechanism: >-
  A judgement about the economy, a sector or a company, written down with its target, horizon and
  invalidation before the first order and held in a few concentrated positions, earns more after
  costs and tax than the same capital held in global equities. The other side of each trade is
  the investor who holds the index weight of that instrument, or who sold it to the book.
citation: >-
  No literature supports the effect for this book; the rationale is the owner's ruling (DECISIONS
  D156). Against it: Barber and Odean (2000), Journal of Finance 55(2), on individual investors'
  trading; Fama and French (2010), Journal of Finance 65(5), on luck and skill among active
  funds; Bessembinder (2018), Journal of Financial Economics 129(3), on how few stocks carry the
  market's return. On repeated looks: Lan and DeMets (1983), Biometrika 70(3).
universe: >-
  Every thesis the book trades from this merge on: an instrument the active jurisdiction profile
  lets the account buy, on a venue with a calendar, that passed the book's deterministic gate
  (MASTER_PLAN §5.5). No thesis exists at this merge, so none was chosen with its result known;
  a thesis the gate refused, or one withdrawn before its first fill, is outside the record and
  is counted beside it.
data_status: >-
  The record is produced live and exists only from the first thesis. Fills are the account's own
  (the broker's executions on the live account; the broker's simulated ones on the paper
  account; the cost model's in shadow), stamped when they happen. Month-end marks are the last
  close of each instrument on its venue, converted at that day's rate, as the collectors store
  them. A thesis's text and triggers cannot change once it is approved (a store trigger), so
  nothing is graded against a target written after the fact. Known biases: paper and shadow
  fills are simulated and show no market impact, queue or partial fill; a thesis's source and
  conviction are fixed at intake, but the owner chooses which of his ideas to hand in.
sources: [owner, analyst]
convictions: [normal, high, max]
records: [shadow, paper, live]
reference:
  role: world_equity
  strategy: matched_twin
primary_metric: log_growth_difference
period: month
theta: "0.02"
theta_justification: >-
  Two points a year of after-tax log growth on the book's capital: the core's own bar of one
  point over the equity reference (core-allocation), doubled. A book of at most eight positions
  adds single-name and judgement risk that the log growth of a record of at most five years
  does not show in full, and it costs attention; below two points a year its capital is better
  left in the reserve.
looks:
  every_months: 6
  count: 10
step:
  record: live
  alpha: "0.05"
  spending: equal
  distribution: student_t
  against: theta
  min_closed_first: 8
  min_closed_between: 4
  without_best_thesis: above_zero
  share_step: "0.10"
  share_ceiling: "0.50"
  steps_per_look: 1
back_to_shadow:
  records: [paper, live]
  alpha: "0.10"
  spending: equal
  distribution: student_t
  against: zero
  min_closed: 4
groups:
  by: [source, conviction]
  months_from: first_entry_of_group
  back_to_shadow: same_boundary
  step: none
adjustment: >-
  A union bound over the ten looks: each look spends a tenth of its boundary's alpha, so the
  probability that a book with an edge of exactly theta ever steps is at most 0.05 whatever the
  dependence between looks. Steps are decided on the book's record alone, so the groups add no
  step. Each group's return to shadow holds its own 0.10; the groups are not adjusted for one
  another, and the report says how many were looked at.
trial_budget: 0
adequacy:
  max_mark_age_days: 7
robustness: >-
  The statistic without its best thesis stays above zero (a condition of every step). Reported,
  never decisive: the same statistic on the paper and shadow records, on the theses the gate
  refused had they been held to their own exits in shadow, per half of the record, and with the
  twin replaced by the reserve the capital would otherwise have been in.
power:
  tracking_error: "0.15"
  paths: 400000
  seed: 20261113
---

# Written ideas from the LLM analyst and the owner, held in a concentrated book (H13)

A forward test: a judgement has no history to replay, so its evidence is the record the book
produces from its first thesis. This file fixes how that record is read, before any thesis exists.
Not investment advice.

## Mechanism and citation

The owner's belief is that returns come from judgement about the economy, a sector and a company
(DECISIONS D156). The book tests it: each idea is written with its instrument, what is expected,
a target, a horizon and what would prove it wrong, passes one deterministic gate and is held until
one of its own exits.

The evidence is against such a book. Individual investors who trade lose to the market after
costs (Barber and Odean 2000); few active funds show skill beyond luck after fees (Fama and
French 2010); most single stocks return less than Treasury bills over their lives, and a few carry
the market (Bessembinder 2018), so a book of eight names is more likely than not to miss them.
For theses a language model writes there is no out-of-sample record. The rationale for running the
test is the owner's ruling, not a published effect.

## Universe

Every thesis the book trades after this merge, from any source (`owner` and `analyst` at this
merge; a source added later is a group from its first thesis). The book's configuration (the
number of positions, the caps, the conviction multipliers) is the owner's and may change: the
record is of the book as it traded. A thesis the gate refuses and an idea withdrawn before its
first fill are not in the record; their count is reported beside it.

## Data and their point-in-time status

Three records are kept apart and never pooled, one per mode the book traded in:

| Record | Fills | What it decides |
|---|---|---|
| `shadow` | the cost model's, simulated | nothing; reported |
| `paper` | the broker's, simulated | whether the book may take its exploration share on the live account |
| `live` | the broker's executions | the steps of capital, and the return to shadow |

**Stated limit: paper and shadow fills are simulated.** They show no market impact, queue or
partial fill, so neither record can raise the book's capital, whatever it shows (DECISIONS D155).

For each record two accounts are kept in the base currency:

- **The book**: every thesis position from its entry fills to its exit fills, at the fill prices
  less commissions and transaction taxes.
- **The twin**: for every amount a thesis's entry spends, the equity reference (the vehicle the
  core holds for its role `world_equity`, an accumulating global equity fund) is bought for the
  same amount at that session's close; at every exit the same share of that thesis's twin units
  is sold at that session's close. Each trade pays the cost model's spread, slippage and
  commission for that vehicle.

Money that is in no thesis is in neither account: it stays in the reserve, and adds nothing to
either side.

Both accounts are valued **after tax, at liquidation**: at a month end an account's profit to
date is its realised gains and losses, plus its open positions at their marks, less the tax the
active jurisdiction's tax policy puts on that account taken alone (its own lots, first in first
out, its losses against its gains, a closed tax year as assessed and the running year as if what
is still held were sold that day). A mark is the last close on or before the month end, at most
`adequacy.max_mark_age_days` = 7 days old, converted at that day's rate.

## Primary metric and horizon

For month `m` (calendar months; the first is the month of the record's first entry fill, counted
whole), let `K` be the book's capital on the last session before it: its share of investable
capital. With `p` the change over the month of the book's after-tax profit divided by `K`, and `q`
the twin's:

    x_m = ln(1 + p) − ln(1 + q)

the book's after-tax log growth minus the equity reference's on the same capital. A month with no
thesis has `x_m = 0` and counts: capital the book was given and did not use earned nothing over
the reference. The reported effect is `12 × mean(x)`, a year.

**Looks.** Ten, at the end of months 6, 12, …, 60 of a record. At look `k`, with `n = 6k` months,
mean `x̄` and sample standard deviation `s` (divisor `n − 1`):

    t_up   = (x̄ − θ / 12) / (s / √n)        t_down = x̄ / (s / √n)

and `c(a, n)` the `1 − a` quantile of Student's t with `n − 1` degrees of freedom. After the tenth
look this registration is spent: nothing further is decided on it, and the book keeps its share
until a new pre-registration reads its record.

**A step up** (the `live` record only): `t_up ≥ c(0.05 / 10, n)`, and

- at least 8 theses closed in the record at the first step, and at least 4 more closed since the
  previous step at each later one;
- `x̄` recomputed without the thesis that added most to the book's profit, and its twin, is above
  zero.

A step raises the book's share of investable capital by 0.10, once per look at most, to 0.50 at
most: the first share a stock book takes on a sufficient historical test (DECISIONS D149), which a
forward record of five years does not outweigh. It starts from the exploration share the owner
sets (a tenth unless he sets another). No step needs an owner command.

**The return to shadow** (the `paper` and `live` records): `t_down ≤ −c(0.10 / 10, n)` with at
least 4 theses closed. On the live record the book then takes no new position on the account, its
open positions leave by their own exits, and it goes on in shadow, graded; it comes back to the
account only under a new pre-registration. On the paper record the book is given no exploration
share on the live account.

| Look | Months | Step up: `t_up` at least | Back to shadow: `t_down` at most |
|---|---|---|---|
| 1 | 6 | 4.032 | −3.365 |
| 2 | 12 | 3.106 | −2.718 |
| 3 | 18 | 2.898 | −2.567 |
| 4 | 24 | 2.807 | −2.500 |
| 5 | 30 | 2.756 | −2.462 |
| 6 | 36 | 2.724 | −2.438 |
| 7 | 42 | 2.701 | −2.421 |
| 8 | 48 | 2.685 | −2.408 |
| 9 | 54 | 2.672 | −2.399 |
| 10 | 60 | 2.662 | −2.391 |

A look is **not evaluable**, and changes nothing, when a held position or the reference has no
mark within seven days of a month end in the record. The look is not repeated later.

**Per source and per conviction level.** The same statistic on the theses of one source, and of
one conviction level, with their twins, over the same `K`, from the month of that group's first
entry fill, at the book's look dates, against the same boundaries with the same minimum of 4
closed theses:

- a source whose `t_down` is at or below its boundary is logged and no longer traded on the
  account; its theses go on in shadow, graded;
- a conviction level above `normal` whose `t_down` is at or below its boundary is sized as
  `normal` from then on;
- no group steps capital. The book's statistic keeps every thesis the account traded, whatever
  became of its source: a record that dropped its losers after the fact would step on selection.

The analyst's record is kept per segment (one model and one prompt hash, DECISIONS D128); a new
segment is a new group, and the source `analyst` is the segments together.

## Theta and why

θ = 0.02 a year on the book's capital, for a step. The core's bar over the equity reference is one
point a year; the book's is twice that, because eight positions carry a risk of a rare large loss
that a record of at most five years shows only in part, and because the book costs the owner's and
a model's attention. The return to shadow is tested against zero, not θ: a book that neither adds
nor loses keeps its exploration share, which is an authorisation and not evidence (DECISIONS
D156), and only a record that shows a loss against the reference sends it back.

## Power

Stated before any thesis, by simulation (400,000 paths of 60 independent normal months, seed
20261113), in tracking errors a year: the standard deviation of a month's `x`, annualised,
assumed near 15 % on the book's capital (eight positions of 35 % single-name excess volatility and
a mean correlation of 0.2 give 19 % fully invested, 11 % with half the slots filled).

| Excess over θ, in tracking errors a year | Step within 1 year | 2 years | 3 years | 5 years |
|---|---|---|---|---|
| 0 (an edge of exactly θ) | 0.009 | 0.015 | 0.019 | 0.025 |
| 0.5 | 0.024 | 0.051 | 0.079 | 0.133 |
| 1.0 | 0.056 | 0.147 | 0.247 | 0.443 |
| 1.5 | 0.117 | 0.328 | 0.533 | 0.810 |
| 2.0 | 0.216 | 0.571 | 0.807 | 0.973 |
| 2.5 | 0.350 | 0.790 | 0.952 | 0.999 |
| 3.0 | 0.511 | 0.926 | 0.993 | 1.000 |

The union bound allows 0.05; independent normal months use 0.025 of it. The minimum of closed
theses and the condition without the best thesis lower every figure and are not in the table.

At the effect the literature supports (none, or a loss), a step is about as likely as a false one.
At 15 % tracking error an excess of 50 % a year is more than three tracking errors and steps
within two years with probability above 0.9; an excess of 10 % a year is half a tracking error
and steps within five years with probability near 0.14. The test is run because the owner ruled
that the book exists and is graded (D156), not because a step is likely.

| Excess, in tracking errors a year | Back to shadow within 1 year | 2 years | 3 years | 5 years |
|---|---|---|---|---|
| 0 | 0.018 | 0.029 | 0.036 | 0.046 |
| −0.5 | 0.043 | 0.088 | 0.128 | 0.203 |
| −1.0 | 0.094 | 0.219 | 0.342 | 0.552 |
| −1.5 | 0.185 | 0.439 | 0.645 | 0.877 |

A book that loses half a tracking error a year to the reference is most likely still trading
after five years. What bounds its cost is its size, not this boundary.

## Adjustment

The ten looks share each boundary's alpha equally, by a union bound, so the false-step
probability over all looks is at most 0.05 and the false return of a book that matches the
reference at most 0.10, under any dependence between looks. The return's alpha is twice the
step's: a false return costs the account little (the book keeps its record in shadow), a false
step puts more capital at risk. The groups are not adjusted for one another.

## Robustness checks

- A condition of every step: `x̄` without the best thesis and its twin is above zero.
- Reported at every look, never decisive: the paper and shadow records; the theses the gate
  refused, as if held in shadow to their own exits; each half of the record; the book against the
  reserve in place of the twin.
- The assumptions that are not checked: months are treated as independent and near normal, and a
  book of few positions has fat-tailed months. The minimum of closed theses and the condition
  without the best thesis are the guard; they are not a proof.

## Trial budget

Zero. There is no development sample and no exploratory trial.

## What each outcome leads to (D62, D65, MASTER_PLAN §5.19)

- **A step** at a look: the book's share rises by 0.10 of investable capital, to 0.50 at most.
- **Neither boundary**: the book keeps its share. A small or absent edge never earns more than
  the exploration share.
- **The return to shadow**: no new position on the account; a new pre-registration before the
  book trades on it again.
- **Not evaluable**: nothing changes; the missing marks are data work.
- **Each closed thesis** is also graded against what it wrote (whether the target was reached, the
  payoff against the one stated, the days against the horizon), per source and conviction level.
  That grading describes the calls; it moves no capital.
