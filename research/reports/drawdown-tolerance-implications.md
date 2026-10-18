# The owner's drawdown tolerance: what its probability and horizon imply

On 2026-10-17 the owner set the drawdown tolerance at **30 % from the previous peak** of the
value the bot manages; unmanaged holdings do not count. The horizon is very long, and the owner
does not need the money now (DECISIONS D88). Two things were open when this report was written:

- the probability of breaching it that is acceptable;
- how that probability is stated: over a number of years, or as a rate.

The owner then chose 30 % reached on at most 5 % of 20-year paths, which resolves to A on
Session 4c's frontier (D91).

This report shows what each choice implies, using two sources: Session 4c's published results,
and an illustration built only from stated assumptions. It recommends no value. It is not
evidence for or against any portfolio (D64).

## What the probability measures

- **It is a filter on simulated histories, not a ceiling on loss.** D60 uses the limit to filter
  candidates. A portfolio respects it when the share of simulated paths whose mark-to-market
  drawdown reaches 30 % is at most the chosen probability. That is a statement about a set of
  simulated histories over a stated horizon. It does not cap any real loss, and it does not order
  a sale when the limit is reached (D88).
- **It needs a horizon.** A longer path contains the shorter one, so its deepest drawdown can only
  be equal or deeper. For a given portfolio, the probability therefore rises with the horizon.
  When monthly returns are drawn independently with any volatility, the probability tends to one
  as the horizon grows without bound. "A very long horizon" cannot be the evaluation horizon by
  itself: the probability needs a number of years, or has to be stated as a rate. A rate (form 2
  below) needs no horizon, so it does not grow with the horizon the way form 1 does.
- **It can be stated in three ways, which select different portfolios for the same number:**
  1. at least once within H years (Session 4c's form, with H about 20.75 years);
  2. as a rate, such as expected breaches per decade or the share of years spent beyond the
     limit;
  3. within any rolling window of H years.

## What Session 4c shows at 30 %

The source is `research/reports/core-allocation.md`: 5,000 paths of 2005–2026 months resampled
in 12-month blocks, each path 249 months (about 20.75 years) long, after Spanish tax.

| Portfolio | Median after-tax log growth / yr | Share of paths reaching 30 % | Largest drawdown on the real path |
|---|---|---|---|
| equity reference | 7.13 % | 72.3 % | −49.18 % |
| D50 (WORLD 100 % with the tilt) | 6.49 % | 58.5 % | −19.79 % |
| D40 (WORLD 80 % with the tilt) | 5.51 % | 34.4 % | −16.54 % |
| 60/40 | 5.07 % | 10.7 % | −27.48 % |
| D30 (WORLD 60 % with the tilt) | 4.36 % | 5.7 % | −11.99 % |
| A (static risk balance) | 3.22 % | 0.1 % | −10.95 % |
| D20 | 3.15 % | 0.0 % | −8.05 % |
| C | 2.66 % | 0.0 % | −6.88 % |
| B | 2.30 % | 0.0 % | −6.32 % |
| D10 | 1.95 % | 0.0 % | −4.07 % |

The frontier at 30 %, for the probabilities 4c pre-registered:

| Probability at most | Best median among those respecting it | Resolves to (simplest of those the bootstrap cannot tell apart) |
|---|---|---|
| 5 % | A | A |
| 10 % | D30 | A |
| 25 % | 60/40 | 60/40 |
| 50 % | D40 | 60/40 |

How to read it:

- **Each portfolio needs a minimum probability.** The equity reference respects the limit only if
  the probability is at least 72.3 %, which is beyond 4c's grid. D50 needs 58.5 % or more; D40
  needs 34.4 % or more; the 60/40 needs 10.7 % or more; D30 needs 5.7 % or more.
- **"Best" and "resolves to" differ at 10 % and 50 %.** The best median there belongs to D30 and D40,
  but the bootstrap could not tell it from simpler portfolios, so the point resolves to A or to the
  60/40 under the pre-registered simplicity rank.
- **The comparison with buying equities is open.** No portfolio's growth was shown to differ from
  the equity reference's: every comparison was inconclusive, with low power.
- **The grid is coarse.** Values between the grid's (15 %, 35 %) were not pre-registered: the
  breach table shows which portfolios would respect them, but their resolution was never computed.
- **The adaptive variants are not here.** The ones D89 asks for are not on this frontier; a later
  pre-registration adds them.
- **Limits of the sample.** 2005–2026 is one sample with strong equity returns, and the bootstrap
  reshuffles its own months. Month-end marks understate falls inside a month, while D88 measures
  the drawdown on daily closes, so these shares err low against it. All of these
  probabilities are for about 20.75 years.

## How the horizon moves the probability (illustration)

This part reads no market data. Each profile has an annual volatility and an assumed arithmetic
return of 1.5 % cash plus 0.3 times its volatility. Monthly log returns are drawn independently,
normal or Student-t with four degrees of freedom (same variance), over 20,000 paths. The
assumptions drive the numbers. Independent draws have no volatility clustering and no crashes
that persist across months, so these probabilities err low.

Script: `research/explore/drawdown_horizon/horizon.py`, seed 20261017.

| Volatility | Assumed log growth | Tails | 10 years | 20 years | 30 years | 40 years | 50 years |
|---|---|---|---|---|---|---|---|
| 6 % | 3.12 % | normal | 0.2 % | 1.1 % | 2.0 % | 2.7 % | 3.4 % |
| 6 % | 3.12 % | Student-t | 0.5 % | 1.6 % | 2.5 % | 3.5 % | 4.5 % |
| 10 % | 4.00 % | normal | 11.8 % | 26.8 % | 38.9 % | 48.9 % | 57.5 % |
| 10 % | 4.00 % | Student-t | 11.5 % | 25.2 % | 37.2 % | 46.8 % | 54.9 % |
| 15 % | 4.88 % | normal | 47.6 % | 76.1 % | 88.8 % | 94.6 % | 97.5 % |
| 15 % | 4.88 % | Student-t | 43.7 % | 71.5 % | 85.4 % | 92.4 % | 96.3 % |
| 20 % | 5.50 % | normal | 78.1 % | 95.8 % | 99.2 % | 99.9 % | 100.0 % |
| 20 % | 5.50 % | Student-t | 72.6 % | 93.2 % | 98.3 % | 99.5 % | 99.9 % |

Under these assumptions:

- **At 10 %, only low volatility passes.** Among the profiles shown, only the 6 % one respects 30 %
  at every horizon; the 10 % profile already exceeds it at 10 years.
- **At 25 %, the 10 % profile passes up to about 20 years**, and at 20 years only narrowly.
- **At 50 %, the 10 % profile passes up to about 40 years**, and the 15 % profile only at 10 years.
- **The probability depends on growth as much as on volatility.** The 15 % profile at 20 years
  (76 %) is of the same order as 4c's equity reference over 20.75 years (72.3 %). But 4c's 60/40
  (10.7 %) sits well below the 10 % profile (26.8 %): the 2005–2026 sample grew more for its risk
  than these assumptions do.
- **Clustering would raise these probabilities.** Fat tails in independent monthly draws barely
  change them at long horizons. Volatility clustering and persistent crashes, which the
  illustration leaves out, would raise them.

## What the choice changes elsewhere

- **Session 13 waits.** Its precondition, the owner's point on the frontier, stays unmet until both
  values are set.
- **Research continues.** A study can report breach shares over a grid of probabilities, and
  over horizons its pre-registration names, without either value.
- **The breakers are separate.** Their thresholds (MASTER_PLAN §5.11) are keys of their own, and
  this tolerance does not set them (D88).
- **A real drawdown can exceed 30 %.** Whatever probability is chosen, it describes a simulated
  distribution, so a realised drawdown deeper than 30 % stays possible; the owner's own reading is
  that 30 % is not a guaranteed maximum.
