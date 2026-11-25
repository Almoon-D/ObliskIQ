---
# H14, the idea ledger (DECISIONS D62, D63, D187; MASTER_PLAN §5.5, §5.19). Merged in its own pull
# request, with no version change, before any call is graded; its lock follows the merge. Nothing
# here may change after the merge: a change is a new pre-registration with a new id.
id: idea-ledger
title: Every written idea graded forward at its own levels, whether or not it trades
kind: idea_ledger
hypothesis: H14
mechanism: >-
  A written idea states an instrument, a target, an invalidation, a horizon and, from the
  analyst, the probability that the target comes first. If a source (a model and prompt, or the
  owner) knows something the price does not, its ideas reach their targets more often than their
  levels and the market imply, and earn more than the equity reference after costs. Eight slots
  give too few closed positions to tell a source from luck; grading every idea that passes the
  gate's own checks, traded or not, multiplies the record without putting capital at risk.
citation: >-
  No literature supports the effect for a language model's ideas, and none for this owner. On the
  statistic: Newey and West (1987), Econometrica 55(3); Kish (1965), Survey Sampling, on the
  design effect; Brier (1950), Monthly Weather Review 78(1), and Murphy (1973), Journal of
  Applied Meteorology 12(4), on the score of a stated probability against a base rate. Against
  the effect: Barber and Odean (2000), Journal of Finance 55(2); Fama and French (2010), Journal
  of Finance 65(5).
universe: >-
  Every idea handed to the thesis book's intake from this merge on, by the owner or the analyst,
  that passes the gate's checks other than the free slot and the size (below). No call exists at
  this merge, so none was chosen with its result known.
data_status: >-
  Produced live from the first call. A call is stored when it is written, with its source,
  segment, event tags and the time it was known; its levels and probability cannot change after.
  Marks are the stored daily closes of the instrument and the equity reference, converted at that
  day's rate, as the collectors store them. Known biases: the owner chooses which of his ideas to
  hand in; a close is not an intraday touch, so a level crossed and recrossed within a session is
  not seen; a call that waits for a slot is graded as if it had been entered at the first close.
population:
  sources: [owner, analyst]
  checks: [screen, view, market]
  not_checked: [slot, caps, liquidity, capital, size, cash, venue, entries_blocked]
  written_at: first_market_pass
  one_open_call_per: [instrument, segment]
outcome:
  start: first_close_after_written
  path: daily_closes
  resolution: [target_first, invalidation_first, horizon_close]
  currency: base
  reference_role: world_equity
  costs: cost_model_round_trip
  cost_trade_value: "10000"
  max_mark_age_days: 7
statistic:
  outcome: excess_log_return_after_costs
  cohort: iso_week_written
  estimate: mean_of_cohort_means
  newey_west_lags: mean_held_weeks
  rho: "0.25"
  alpha: "0.05"
  theta: "0.01"
  min_cohorts: 26
  min_effective: "30"
  groups: [segment, source, event_tag]
  adjustment: bonferroni_over_groups_past_minimum
calibration:
  probability: target_first
  base_rate: segment_climatology
  score: brier
  reliability_bins: 5
  min_cohorts: 26
  min_effective: "50"
  brier_skill_above: "0"
  shrink: slope_lower_bound
  shrink_alpha: "0.05"
rank:
  reads: calibration_passed
  score: expected_gain_at_reference
  ranked_first: positive_score
  otherwise: arrival
  touches_h13: false
never: [size, capital, gate]
trial_budget: 0
power:
  sd_per_call: "0.11"
---

# Every written idea graded forward at its own levels, whether or not it trades (H14)

A forward record: an idea has no history to replay, so its evidence is what follows it. This file
fixes, before any call exists, which ideas are graded, how each is resolved, how a record is read,
when a stated probability is trusted, and the one thing the ledger may change: the order in which
the book's free slots are offered. Not investment advice.

## Mechanism and citation

The thesis book holds at most eight positions (MASTER_PLAN §5.5), so in two years it closes a few
dozen: too few to tell a model, a prompt, the owner or a kind of event from luck (DECISIONS D187).
Most ideas never trade, for want of a slot. The ledger grades them all at their own levels, as if
each had been entered at its first close, so the record grows with the ideas and not with the
slots.

The evidence is against a source that beats the market: individual investors who trade lose to
it after costs (Barber and Odean 2000), and few active funds show skill beyond luck (Fama and
French 2010). For a language model's ideas there is no out-of-sample record. The ledger is run
because the owner ruled that every idea is graded (D187), not because an edge is likely; it moves
no capital, so a ledger that finds nothing costs only the computation.

## Universe: which ideas are calls

A **call** is an idea handed to the thesis book's intake (an analyst draft or an owner's idea)
that passes, on its first evaluation in the cycle, every check of the gate except the ones that
depend on the book:

- **checked** (`screen`, `view`, `market`): the draft's own words (a complete idea, levels that
  bracket the price in the direction of the view, dates that hold, a conviction its source may
  give), the view against the instrument, the mandate and the jurisdiction's eligibility, an
  instrument another book holds, a fresh quote strictly inside the levels, the minimum
  gain-to-loss ratio and the entry limit at that quote;
- **not checked**: a free slot, the sector, country and currency caps, the liquidity floor (it is
  measured on the size), the book's capital, a size worth trading, cash, the venue's session and
  a block on new entries.

A call is **written** at the first cycle in which its checks pass (`first_market_pass`), and
stored then with its levels, its horizon, its stated probability (none for an owner's idea that
states none), its source, its segment (the analyst's model and prompt hash, D128; `owner` for the
owner), its event tags (empty until Session 34) and the time it was known. An idea refused for
good by a checked finding is never a call; one withheld (no fresh quote, a gain-to-loss ratio
below the minimum, a price above its entry limit) becomes a call at the first cycle that passes,
or never.

**One open call per instrument and segment.** While a call of a segment on an instrument is
unresolved, a later idea of the same segment on the same instrument is not a call; it is counted
beside the record. Two segments may hold calls on one instrument.

Whether the call trades, waits or expires changes nothing in its grade.

## Data and their point-in-time status

Every read is bounded by the time it is made: a call is graded only on closes known by then, and
perturbing a close after `as_of` changes no grade made as of `as_of`. A mark is the last stored
close on or before its date, at most 7 days old, converted into the base currency at that day's
rate. Closes are daily: a level crossed within a session and recrossed by its close is not seen.

## Outcome of a call

From the **first close after the call was written** (`P₀`, its date `d₀`), the instrument's daily
closes are read in order up to the horizon:

- **target first**: the first close at or beyond the target, in the direction of the view;
- **invalidation first**: the first close at or beyond the invalidation;
- **horizon**: neither by the horizon; the call resolves at the last close on or before it.

The first close itself is read: one already at or beyond a level resolves the call there. The
resolving close is `P₁` on date `d₁`. A call whose horizon has not passed and whose levels
have not been crossed is **open**. With `R₀`, `R₁` the equity reference's marks (the vehicle the
core holds for its role `world_equity`) on `d₀` and `d₁`, and `s = +1` for a view that the
instrument rises, `−1` for one that it falls:

    e = s × (ln(P₁ / P₀) − ln(R₁ / R₀)) − c

where `c` is the **round trip's cost** as a share of the trade: two legs of the cost model's spread
and slippage for the instrument's tier and two legs of its commission on a trade of 10,000 in the
base currency (`cost_trade_value`), as `config/defaults/costs.yaml` states them on `d₀`.

A call is **not evaluable**, never marked at a stale price, when its instrument has no close
within 7 days after it was written, when two consecutive closes of the instrument between `d₀`
and `d₁` are more than 7 days apart (a level may have been crossed unseen), or when the reference
has no mark on `d₀` or `d₁`. It is counted beside the record with its reason.

The **stated probability** is the source's probability that the target comes first (before the
invalidation and the horizon); its outcome is `o = 1` on target first and `o = 0` otherwise.

## Primary statistic and the verdict (D62)

The cohort of a call is the ISO week it was written. A record's estimate is the mean of its
cohorts' mean `e`, with a Newey–West standard error of `h` lags, where `h` is the mean number of
weeks its resolved calls were held (`d₁ − d₀`, rounded up, at least one), floored at the
independent variance. Its effective observations are

    N_eff = Σ over cohorts c of n_c / (1 + (n_c − 1) ρ) / h,     ρ = 0.25

the scoreboard's registered ρ (`scoreboard.md`). The interval `[L, U]` is two-sided at level
`1 − 2α/k`, α = 0.05, where `k` is the number of groups past the minimum in that report.

**Minimum.** No verdict before **26 cohorts** with a resolved call and **N_eff ≥ 30**; below it the
group is reported as "too few" and nothing is concluded.

**Verdict** against θ = **0.01** a call: sufficient when `L ≥ θ`; insufficient when `U < θ`
(negative when `U < 0`); inconclusive otherwise (positive when `L > 0`).

**Groups.** The same statistic per segment, per source and per event tag (a call with several
tags counts in each). Every group past the minimum in a report is one test in the adjustment.

## Theta and why

θ = 0.01 a call: one percent of excess return over the reference, after the round trip's cost. A
call holds a slot for weeks to months; an edge below one percent a call is not worth the slot
and the attention, and the ledger exists to separate sources that clear it.

## Calibration

A segment's stated probabilities are read only on its **resolved calls that state one**:

- **Brier score** of the stated probabilities against the outcomes `o`, beside the Brier score of
  the segment's own **base rate**: the share of those calls that reached their target first
  (the climatology, known only after the fact, which favours the base rate). **Skill** is one
  less their ratio.
- **A reliability table** in fifths of the probability scale: the calls, the mean stated
  probability and the share that reached the target, per bin.
- **Minimum**: 26 cohorts and **N_eff ≥ 50**, counted as above on the calls that state a
  probability. Below it neither the score nor the table is read: they are reported as "too few".

A segment's calibration is **passed** while it is past the minimum and its skill is above zero;
it is re-read each time the ledger is graded and may switch back. Passed, a call's **calibrated
probability** is

    p_eff = p_base + shrink × (p − p_base)

with `p_base` the segment's base rate and **shrink** the one-sided 95 % lower bound of the slope
`β` in `o_i − p_base = β (p_i − p_base) + ε_i`, fitted through the origin by least squares, its
standard error scaled by `√(n / N_eff)` and clipped to [0, 1] (the scoreboard's rule).

## The rank rule

When a cycle offers a free slot to the candidates waiting for one:

- a candidate whose call belongs to a segment whose calibration is **passed** has the score
  `p_eff × g − (1 − p_eff) × ℓ`, where `g` is the target's gain and `ℓ` the invalidation's loss as
  shares of the gate's reference price in that cycle;
- candidates with a **positive** score are offered the slot first, highest score first;
- every other candidate (a segment not passed, an owner's idea with no probability, a score at or
  below zero) follows in **order of arrival** (D161).

Until a segment passes, every slot is offered in order of arrival. The rule orders the offer; the
gate still judges each candidate whole, and a candidate it withholds keeps its place.

**H13.** H13 registers which theses count in the book's record (every thesis it trades) and how
that record is read; it does not register the order in which slots are offered, and states that
the record is of the book as it traded (`thesis-book.md`, Universe). This rule does not touch
H13's registered mechanism, its lock or its gate.

## What the ledger never does

It never sizes a position, never moves capital, and never overrides the gate: it adds no check,
removes none and changes no size. Its one effect is the order of the offer above.

## Power

Stated before any call. A call's excess has a standard deviation near 0.11: the scoreboard's 0.06
for 28 days (`scoreboard.md`, Power), scaled to a mean holding near 13 weeks.

| N_eff | Standard error a call | Effect found with probability 0.8 (`L > θ`), one group | P(sufficient) at θ + 0.03, one group | P(sufficient) at θ + 0.03, four groups |
|---|---|---|---|---|
| 30 | 0.020 | 0.060 | 0.44 | 0.23 |
| 50 | 0.016 | 0.049 | 0.61 | 0.38 |
| 100 | 0.011 | 0.037 | 0.86 | 0.69 |
| 200 | 0.008 | 0.029 | 0.99 | 0.95 |

At the minimum a segment's record separates only an edge near six percent a call.

**How long it takes.** A week's calls share the market's move, so a cohort of any size carries at
most `1/ρ` = 4 effective observations, and calls held `h` weeks overlap, so a year of weekly
cohorts carries at most `52 × 4 / h`. Held about 13 weeks, a segment gains at most 16 a year
however many ideas it writes: N_eff = 30 takes about two years, and the calibration's 50 about
three. Three ideas a week (n_eff = 2 a cohort) give 8 a year: near four years to 30. Shorter
horizons shorten it in proportion; more segments side by side (D188) reach it no sooner each.

The probability of a sufficient outcome at the effect the literature supports (none) is the
false-positive rate; the ledger is run by the owner's ruling (D187), and it moves no capital on
any outcome.

## Adjustment

Bonferroni over the groups past the minimum in each report: the interval's level is `1 − 2α/k`.
Each report says `k` and how many groups were below the minimum. The calibration status is an
estimate re-read at every grading, not a test, and is not adjusted. Repeated reports of a growing
record are not adjusted: a verdict describes a record and moves nothing (the scoreboard's rule).

## Robustness checks

Reported beside every verdict, never decisive: the share of calls resolved at each of the three
outcomes; the record without its best call; the count of calls not evaluable and of ideas counted
beside the record, with their reasons.

## Trial budget

Zero. There is no development sample and no exploratory trial.

## What each outcome leads to (D62)

- **Sufficient, inconclusive or insufficient**, per segment, source or event tag: reported; it
  chooses which segment is worth running (D188) and moves no capital.
- **Calibration passed**: that segment's calls are offered free slots by the rank rule.
- **Calibration not passed** (too few, or skill at or below zero): its calls wait in order of
  arrival.
- **Not evaluable**: the call is counted beside the record with why; the missing marks are data
  work.
