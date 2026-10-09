# The computation layer

Five engines over the store the collectors fill. **They touch no network**: every
one reads SQLite and writes derived tables back, which is what makes the whole
layer runnable and testable in a sandbox with no IBKR access, and why a grid
build can be scheduled beside the collectors without a second set of
credentials.

```
obliskiq-compute solve-iv    ATM implied vol where the route served none
obliskiq-compute grid        market x sector x the fifteen indicator families
obliskiq-compute rankings    the four ranked views and the correlation matrix
obliskiq-compute fx          the four-way decomposition per market
obliskiq-compute commodity   curves, wrappers, positioning, inventory, proxies
obliskiq-compute census      the weekly all-gates count
obliskiq-compute all         solve-iv, grid, rankings, fx and commodity, in order
```

Flags: `--as-of YYYY-MM-DD` (defaults to today, UTC), `--dry-run` (compute and
report, write nothing), `--nav` (census override), `--horizon-days` (fx),
`--config`, `--log-level`, `--log-format`.

Exit codes match the collectors: **0** everything ok, **1** something failed,
**2** a run was degraded. A sparse grid is *degraded*, not failed — most cells
are legitimately absent on a young store, and a scheduler should be able to tell
"still filling up" from "broken".

---

## What each module owns

| Module | Owns |
|---|---|
| `compute/stats.py` | Pure numerics. Returns `None` rather than a number nobody could defend. A correlation history can be sampled **non-overlapping**, which is what makes its standard deviation a dispersion. |
| `compute/frames.py` | Typed store reads. Macro aligns on **release**, never reference date. |
| `compute/calendar.py` | Trading-day arithmetic with holidays, from `calendars.yaml`. |
| `compute/gates.py` | Every gate, once. The census, the screener and the risk engine share it. |
| `compute/smile.py` | The strike's own implied vol — what N(d₂) must be computed on. |
| `compute/indicators.py` | The fifteen families from SKILL.md. |
| `compute/build_grid.py` | Cells → `grid`, instrument disclosure → `grid_coverage`, per-family reach. |
| `compute/iv_solve.py` | ATM implied vol inverted from mid prices where the route served none. Its own column, its own provenance, bounded and labelled. |
| `compute/rankings.py` | The four ranked views, and the cross-market matrix. |
| `compute/risk_engine.py` | Greeks, the zombie test, stops, caps, the breaker. |
| `compute/penalty.py` | The sleeve budget as a shared pool — gross less the penalty balance less what open positions commit — and the penalty balance itself. |
| `compute/fx_engine.py` | Four-way decomposition, flags, the carry-illusion test. |
| `compute/commodity_engine.py` | Curves and wrappers, COT, inventory, proxy quality. |
| `compute/universe_census.py` | The weekly all-gates count, and how far each family reaches. |
| `store/ledger.py` | Ledger writes, with the gates enforced in the store. |

`obliskiq-lite-mcp` wraps these as MCP tools — see `docs/SERVER.md`. Nothing
there reimplements a rule that lives here: the tools compose these engines, and
`server/guards.py` composes the same `CandidateGates` before a ledger write
rather than restating any threshold of its own.

---

## Family 7 reads the LEVEL of implied vol and the DIRECTION of it

`implied_vol_atm` is the level and it is symmetric: it says how much movement is
priced and nothing about which way. `implied_vol_skew` is the risk reversal — the
out-of-the-money call's implied vol minus the out-of-the-money put's, both at
`smile.skew_delta` — and it says which tail the market is paying for. Two
underlyings at the same ATM vol can be pricing opposite risks, and until this leaf
existed the board could not tell them apart.

Nothing new is collected for it. The chain collector already buys up to
`collectors.chain.max_expiries_per_underlying` expiries at
`max_strikes_per_expiry` strikes on both rights and stores every row with its own
`iv` and `delta`; the grid published one number off all of it.

Four properties, each of which a later change could get wrong:

- **Delta, not moneyness.** A fixed ±10% band is a different thing on a 15-vol
  name and a 90-vol one, so a skew defined that way is not comparable across the
  board — which is the only reason a grid cell exists. The delta is the number the
  exchange priced, and `chain_snapshots` has carried it since migration 001.
- **The band has to BRACKET the target on both sides, or the cell refuses.** Each
  leg is interpolated between the two strikes either side of the target delta, the
  same treatment `Smile.iv_at` gives a target strike and the same refusal it makes
  rather than clamping to a wing. Reading the nearest listed strike instead would
  publish a 45-delta reading under a 25-delta name, and it would read as an
  ordinary number all the way to the output. The `n/a` carries the |delta| span
  each side actually reached, so the refusal is checkable.
- **Off the hygiene-filtered smile, like everything else that reads one.** A skew
  computed off a strike the spread, depth, delta-band or convexity filters dropped
  is not a skew, and two standards would make the grid and the risk engine
  disagree about the same instrument on the same day.
- **One family, two leaves, and both are always emitted.** It sits under
  `FAMILY_IMPLIED_VOL` in `FAMILY_INPUTS`, so it inherits the chain-or-snapshot
  freshness group and `MARKET_DATA_FAMILIES` still derives it. An inapplicable or
  absent cell writes both leaf names anyway: a grid whose leaf set changes with
  its status is a grid whose rows cannot be joined across days.

The IV TERM STRUCTURE is a different question, and the section below is it.

---

## Family 7 also reads the SHAPE ACROSS TIME, and which two expiries define it

`implied_vol_atm` is symmetric in time as well as in direction. It says how much
movement is priced at one tenor and nothing about *when*. "Vol is high" and "vol
is high AND inverted" are opposite trades — the second says the market expects
the move soon — and until this leaf existed the grid could not tell them apart.
There has been a REALISED term structure since family 5 was built
(`rv_10d`/`rv_30d`/`rv_90d` and `rv_term_slope`) and no implied twin.

Three leaves, all inside `FAMILY_IMPLIED_VOL`: `implied_vol_atm_front`,
`implied_vol_atm_back`, and `implied_vol_term_slope`. The slope is **front over
back, minus one** — bit for bit the statistic `rv_term_slope` computes, so the
implied and realised term structures can be subtracted rather than merely read
side by side. Positive is inverted. It is a leaf and not a sixteenth family: the
indicator set is fifteen and a test asserts it, and sitting under family 7 means
it inherits the chain-or-snapshot freshness group and `MARKET_DATA_FAMILIES`
from `FAMILY_INPUTS` rather than from a list somebody keeps by hand.

### Which expiry pair, and the constraint that decided it

**A fixed DTE pair is not available, and the reason is stronger than "listed
expiries are what they are".** The chain collector buys only expiries inside
`tenor.entry_dte_min`–`entry_dte_max`, capped at
`collectors.chain.max_expiries_per_underlying`. A front month was therefore never
collected on ANY name — a 30-versus-90 slope is not merely unlisted, it is
absent from the store by construction. Both targets have to sit inside the band
the collectors actually buy, and `tests/test_compute_indicators.py` asserts they
do: outside it the leaf would report `n/a` on every cell for ever with nothing
saying why.

Each leg is the **nearest listed expiry** to a target in `smile.term_structure`,
and a leg further from its target than `max_target_deviation_days` is refused
with the DTE it reached. Two alternatives were rejected — front-listed versus
last-listed, and front versus Nth listed — and `docs/DECISIONS.md` carries the
argument. Both are always available, and that is the problem: the measured tenor
pair drifts with the listing cycle, so the same leaf answers a different question
every month.

### The window label is the join key, and carries nothing else

The window is the two ACTUAL DTEs and nothing else: `"<front>d vs <back>d"`, the
same shape `rv_term_slope`'s `"10d vs 90d"` takes. This is the point of the leaf
rather than a nicety. The grid ACCUMULATES history rather than re-measuring it,
and `get_grid_cell(history=N)` warns the caller to compare only where `window`
matches — so a slope between 28 and 91 days and one between 28 and 35 days have
to be distinguishable from the row alone. Provenance (both expiries, both ATM
sources, the hygiene counts) travels in the cell's `reason` instead, which an
`ok` cell is allowed to carry: in the window it would differ every session and
nothing would ever compare.

Two floors make the label mean something. Below
`smile.term_structure.min_separation_days` apart the two legs are not a term
structure and the cell refuses with the span it measured. And a name with one
listed expiry ahead is `n/a` naming that expiry — tested BEFORE the per-leg
tolerance, because a single-expiry name also fails the back leg's tolerance and
reporting *that* would send a reader looking for a tolerance to widen when what
is missing is a second expiry to widen it onto.

### Both legs are exchange-quoted, or there is no cell

Unlike the ATM level, which falls back to a vendor figure or a solved one under
`data.atm_iv_prefer`, the term structure has **no fallback at all**.
`implied_vol_solved` sits in its own column precisely because an inversion of a
mid price through a European model is not an exchange quote; a slope with a
solved front and a quoted back would carry that methodology break inside a single
number, where no consumer could see it. Reading both ends off one
hygiene-filtered chain makes the break impossible rather than discouraged. A
clamped ATM read is not refused — clamping is a fact about strike position, not
about method — but it is named in the provenance.

---

## Reading two numbers off one smile

`assess_option` reads the collected smile **twice, at two different points**,
because it prices two different things. N(d₂) asks how likely the underlying is
to reach the TARGET, so it takes the volatility at the target. The greeks price
the contract that is actually held, so they take the volatility at its own
STRIKE. Both quotes travel on the result with their provenance — measured at a
listed strike, interpolated between two, or clamped to the outermost one that
survived hygiene — and `check_position_risk` reports each.

They used to share the target's volatility. On a normal equity skew a call's
3:1 target sits further out than its strike, where implied vol is lower, so the
reported delta came out low and drifted towards firing zombie condition 1. Same
class of error as pricing a dividend payer at q = 0, and on the same gate.

---

## Three realised-vol estimators, and which question each answers

Indicator family 5 emits close-to-close, Parkinson and Garman-Klass, each as its
own named leaf with its own percentile. The estimator IS the leaf name, so no
provenance column exists to drift out of step with the value beside it.

They are not one number computed three ways. Close-to-close is the move between
consecutive closes and so contains the overnight gap; the two range estimators
read the intraday high and low and cannot see it at all. That makes them far
less noisy on the same sample — which matters most where the sample is short —
and systematically lower on any market that closes at night. `docs/DECISIONS.md`
carries the measurement on real bars.

So `most_compressed` orders on the **Parkinson percentile**, where a bias common
to the whole series cancels in the ranking, and every row carries the
close-to-close verdict beside it as a condition. `iv_minus_hv`, `OptionScreen`
and the chain collector's sigma ladder stay close-to-close, because an implied
vol prices the total variance to expiry and subtracting a figure that omits the
gap reads as expensive by the size of it. The asymmetry is deliberate.

Garman-Klass also reads the open. Where a listing restates the previous close
instead of publishing an opening print — above
`data.garman_klass_max_restated_open_share` of the window — it reports `n/a` with
the measured share, while Parkinson computes regardless. Both refusals are `n/a`
and never `inapplicable`: `bars` has the columns, so a route that filled them
would fill the cell.

### And all three rank against readings that share no bars

The same defect the correlation norm below was fixed for, in the family beside
it. A realised-vol percentile ranks today's reading against every rolling
reading in the daily window; sampled every session, two consecutive readings
share all but one of their bars, so the count reported beside the number was
many times the evidence behind it. Measured on real daily price history in
two-year windows: **474 readings resting on sixteen independent ones**.

The history therefore steps by the tenor, and `grid.realised_vol_percentile_min_independent`
says how few is too few — its own key rather than the correlation norm's,
because that one is documented as what a standard deviation needs and this one
floors a rank.

**What this is, and what it is not.** It is a window-honesty fix, not a ranking
correction. Every cell carries the same overlap, so the ordering barely moves:
across two-year windows of real daily prices the percentile shifts by 0.03 on
median and 0.08 at worst, and the two orderings correlate at 0.98. What was
wrong is `n_obs`, which is a claim about evidence.

All three estimators step together. Two percentiles built on different sampling
would be the estimator confusion this section exists to prevent, reintroduced
through the window — so the close-to-close leaf and the two range leaves change
under one commit or neither. A cell below the floor keeps its LEVEL and loses
only the rank: what a percentile needs and a level does not is a history to
stand in.

---

## What each ranked view actually tested

The four views each ORDER on **one named indicator**, so the ordering can be
checked against the cell it came from; a composite score nobody can reproduce is
what that design refuses. SKILL.md's *coiled* and *extended*, though, are four
conditions each — so the rest of the definition travels beside the ordering, in
`conditions`, rather than inside it. A row in `most_compressed` is not coiled
unless every condition there is true, and a `null` condition is one the store
could not measure, which is never the same as a failed one.

Ranked rows also carry the `status` and `market_data_status` of the cell behind
them. Ranked views admit `stale` cells deliberately — a stale reading is still
the best available — and carried nothing to say which were, in the one table a
session actually reads.

### And the trajectory of the cell, not only its level

A ranked value is a LEVEL. "94th percentile" does not say whether the reading is
still building or already unwinding, and those are opposite trades behind an
identical number.

The answer was in the grid the whole time. `rankings.TRAJECTORY_LEAVES` names a
CHOSEN, bounded set of leaves — returns at a short and a medium horizon in local
and base currency, the realised-vol term slope, both 52-week distances, the
long-run drawdown, and the positioning level with its change over each window in
`commodities.cot.change_windows_weeks` — and every ranked row carries them from
the same cell at the same `as_of`. Migration 016 gives them a column so they
survive the write the server reads back.

Four properties, and each of them is the point:

- **Served, never recomputed.** `_cells_for` already read every measured leaf of
  the ranked cells in ONE query to build the conditions map. This is that read,
  used twice.
- **Chosen, not exhaustive.** A cell holds forty-odd leaves — the count is
  derived from config, so it moves whenever a tenor list or a window list does,
  and it was 47 when this was last measured. A row carrying all of them is the
  raw table again, which is what the ranked views
  exist to replace. The set is declared in one place with a comment saying what
  question each leaf answers, and the positioning change windows are appended
  from config through `indicators.positioning_change_indicator`, so the family
  that emits the leaf and the view that carries it cannot spell it differently.
- **Every leaf carries its own `status`, `window` and `units`.** A market x sector
  cell is fed by four stores on four cadences, so a fresh percentile beside a
  stale return is an ordinary row — and one that cannot say which is which reads
  as healthier than the store is. That is what migration 012 added `status` for,
  applied to every value that travels beside the ranked one.
- **An unmeasured leaf is absent from the map.** Never zero and never a flat
  trajectory: zero is a reading.

The leaf a row already ranks on is not repeated in its own trajectory — it is the
row's `value`, and one number under two names invites a reader to look for a
difference that cannot exist.

**No synthesised regime label, per row or anywhere else.** It was proposed and
refused: a one-word verdict is a composite with prose in front of it, and it
cannot be reproduced from the cell it summarises. `conditions` stays the
sanctioned shape for a named verdict — true, false, or null where the store could
not measure it — and it holds SKILL.md's definitions, not a trajectory reading
bolted into them. `docs/DECISIONS.md` has the argument.

---

## The positioning level, and the velocity beside it

Family 15 is the only flow measure in the grid; everything else is
price-derived. It emitted a percentile and nothing else, so a crowded book still
building and one being dumped were the same number — which is the distinction the
whole measure exists for, because crowding does not time a move and an unwind
already underway is a different trade from a crowd still forming.

It now emits a CHANGE leaf per window in `commodities.cot.change_windows_weeks`
beside the level, inside family 15: the indicator set is fifteen families and a
test asserts it.

**The two are not in the same units, and the naming has to carry that.** The
level is a percentile against `commodities.cot.percentile_window_years` of its
own history. The change is the net non-commercial **share of open interest** —
the quantity migration 015 established, never the raw contract count —
differenced over a window in weeks, so it is in share-of-open-interest points.
Differencing the percentile instead would rank a past reading against today's
distribution and call the result a flow.

**Weeks, and both endpoints release-aligned.** CFTC reports Tuesday's positions
on the following Friday, so a window in days would span a different number of
reports depending on which weekday the grid was built on. The newest endpoint is
the newest report PUBLISHED on or before `as_of`, and the older one is the newest
report published on or before the window's start — so the span is never shorter
than the label claims. Where a publication gap makes it longer, the window label
carries both release dates and `n_obs` carries the count, on the same
window-honesty rule as every other family.

**Below `commodities.cot.change_min_observations` readings inside the window, the
cell is `n/a` with the count.** A delta over two readings is a difference, not a
trend. The floor is also what catches a stale store: the older endpoint is
anchored at or before the window's start, so if the newest report is itself a
month old the two ends collapse towards each other, and the count — not the dates
— is what notices.

**A units change refuses the change leaves as well as the percentile**, and for a
stronger reason: `MacroSeries.units_known_at` refuses a percentile that would rank
two quantities against each other, and a difference across the same boundary
subtracts a share from a contract count and reports the contract count.

---

## The matrix is cross-asset, and a rate is not a price

`CorrelationMatrix` computed every unordered MARKET pair against its own norm
from the day it was written, and it was equity-index-to-equity-index only. FX sat
in the store as eleven ECB reference rates and rates sat there as a policy rate
and a 10-year yield per market; neither entered it. **Equity against rates
flipping sign is one of the cleanest regime markers there is**, and both series
were already being collected and paid for.

A **leg** is now `(asset_class, series, market)` rather than a market. A market
is not an identifier any more: `germany` has an equity index, a policy rate and a
long rate, and those are three different series. For `fx` and `rate` the series
is the macro id the market declares in `universe.yaml`; for `equity` it is the
**market name**, because a market is measured on exactly one index contract and
so names that series uniquely — which is also what let migration 019 copy every
pre-existing row across exactly rather than back-filling a symbol it had no way
to look up.

**Which pairs are computed is a module constant, not a config key** —
`rankings.CORRELATED_CLASS_PAIRS`, for the reason `DAILY_SEQUENCE` is one. It is
equity × equity, equity × FX and equity × rates. FX × FX and rate × rate are
deliberately absent: eleven ECB crosses against each other is the dollar index
measured eleven ways, and six euro-area markets share `ECB_DFR`, so rate × rate
would largely correlate that series with itself.

### A rate series is differenced; a price series is log-returned

`stats.paired_changes` takes a transform per side because the two legs need not
be the same kind of thing. A **log return on a rate near zero is not a return**,
and on a rate AT zero it does not exist — `ECB_DFR` has sat at zero — so a rate's
change is the first difference of its level, in the series' own units. FX and
equity are prices and keep log returns.

The transform is a **pure function of the asset class** and has no column of its
own. A derived fact with a column beside the value is free to drift out of step
with it, which is the same reason the three realised-vol estimators put the
estimator in the leaf NAME rather than in a provenance column.

### Point-in-time: filtered on release, aligned on reference

Every macro read goes through `MacroSeries.observations_known_at(as_of)`, which
keeps an observation only once its **release date** has passed. That is what stops
a correlation being built on prints nobody had.

Alignment is then on the **reference date** — the date the value applied — because
that is what a bar on the same day can be paired with. Intersecting bar dates
with *release* dates instead would pair a bar with almost nothing: a monthly
series has twelve release dates a year. The release date decides what was
knowable; the reference date decides what it lines up with, and the two jobs
belong to different columns.

Alignment is by **calendar-date intersection, never by position**, which is the
rule `_aligned_returns` has always followed: Tokyo Monday must not pair with New
York Friday.

### Three refusals, and all of them produce counts rather than numbers

- **Mixed frequency is refused, never resampled, and the refusal fails CLOSED.**
  `macro.series[].frequency` is declared per series and travels on every row.
  Correlating a daily equity return against a monthly rate change compares twelve
  readings a year with two hundred and fifty, and any alignment that makes the
  lengths match has resampled one of them — the same thing `MacroPull._sane`
  refuses one layer up. Every declared `long_rate_series` is monthly, so equity ×
  long-rate is `n/a` across the board today and the row says why.

  Three states, not two. The column is **nullable**, so a leg declaring no
  cadence at all is refused rather than assumed to match — unioning an empty set
  into a daily one leaves one element and would let an undeclared series through
  as daily, which is the fail-open version of this guard. And a leg whose own
  rows disagree is refused too: one series has two writers (`macro_pull` and the
  risk-free-rate cache in `macro/rates.py`), and a series carrying both stamps
  cannot be correlated at either of them.
- **A units change inside the window refuses the pair**, on the same reasoning
  `units_known_at` already applies to a percentile: a difference across the
  boundary subtracts one quantity from another and reports the result.
- **The norm floor is unchanged.** `grid.correlation_norm_min_independent`
  applies exactly as before, and **most cross-asset pairs will sit below it for a
  long time**. That is the honest answer rather than a gap to work around.

Migration 019 adds `n_obs` and `norm_independent` so those counts are countable
in SQL rather than only readable inside a sentence. `grid.n_obs` has carried the
same fact for the grid since 007, on the same rule: a count is a claim about
evidence.

**`n_obs` is the aligned intersection, not the correlation's own window.**
`window_days` already states how wide the correlation is; what it does not say is
whether the pair had enough shared history for that window to exist, or for the
norm behind it. That is this column, and it is the same number the refusal path
reports when a pair falls short.

### One series, several claimants

Six euro-area markets declare `ECB_DFR` as their policy rate. It is **one series
and one leg** — emitting it per claimant would write the same measurement six
times under six different `market_b` values. The first market in sorted order is
recorded as the owner and the rest are dropped, so which market appears beside a
shared series is a stated rule rather than whatever the iteration reached first.
Group on `series_b` to find such a pair: `market_b` names one claimant, not all
of them.

---

## The correlation norm is built from readings that do not share data

A shift is measured against the pair's OWN history, and that history is sampled
**non-overlapping**: stepping one session at a time, two consecutive windows
differ by a single observation, so the series is smooth by construction and its
standard deviation measures that smoothness rather than the spread of the
correlation. Dividing by it inflates every z-score.

Independence costs readings, so `grid.correlation_norm_min_independent` says how
few is too few, and a pair below it reports no shift instead of an inflated one.
Against the two-year norm window that admits the 30- and 90-day shifts and
refuses the 250-day one — which is the honest reading of two years of history,
not a gap in collection. The norm window is itself capped by what the backfill
fetches; widening both together is a collector change, in `ROADMAP.md`.

---

## Rules the code enforces, not the caller

**A missing cell is `n/a` with a reason.** Never a silent proxy, never an
estimate presented as data. The reason names the missing input — which series,
how many observations, how many were needed — so a coverage report can be acted
on. Most cells are absent on a young store, and `obliskiq-compute grid` prints
the count and a reason histogram.

**Markets are measured on their native index contract.** KOSPI, not EWY. The
measurement instrument per cell is declared in `universe.yaml` with
`grid_measures: true`, and the loader refuses a second claimant for one cell.
The objection is the **currency trade a foreign listing embeds**, not the listing
itself: `_load_markets` refuses a market measured in a currency other than its
own, and a USD sector fund measuring the USD market embeds nothing.
Where an ETF measures a cell, `grid_coverage` carries its top-two
concentration, hedged flag and coverage note; where no dated holdings
disclosure supplies the concentration, the note says the two-stock test could
not run rather than implying it passed.

**A fixed horizon needs its full window.** `return_63d` computed from three bars
is correctly labelled and completely wrong. Year to date is the one exception —
in January it is legitimately short.

**N(d₂) uses the target strike's own smile implied vol.** Constant-vol
Black-Scholes understates the terminal probability on an out-of-the-money
strike, and that understatement is what produces a false zombie positive.

**The zombie test needs two consecutive review sessions**, and books the penalty
itself at *detection*. A third confirming session does not book it again.
Percentage drawdown is not one of the four conditions and there is no code path
that makes it one.

**Ranking consumes base-real only.** A config naming any other basis is refused
at construction of `FxEngine`.

**Every commodity view carries a wrapper recommendation.** Beyond the contango
ceiling the futures-tracking wrapper is rejected: the roll bleeds over a
90–200 day hold whether or not the thesis is right.

**A five-year percentile is never cycle position.** `PositioningRead` and
`CyclePosition` are separate types over separate windows, and `CyclePosition`
refuses construction on a window short enough to be a positioning percentile.

**A thin option universe is reported, never fixed by relaxing a gate.** So is a
thin ranked view: it carries its own denominator rather than being trimmed to
look full.

---

## The grid axes

Declared in `universe.yaml`, not derived:

- `sectors:` — the twelve-row axis (`broad` plus eleven sectors). A
  market × sector cell with no mapped instrument must be an explicit `n/a`; a
  derived axis would make that cell never appear and the gap invisible.
- `markets:` — per market: currency, country, the native index symbol, and the
  macro series ids indicators 11–13 read. A `null` series is a declared coverage
  gap: the cell reports `n/a` naming the missing series and never borrows a
  neighbour's number.
- `unmapped_cells:` — every pair no instrument measures, **with its reason**.

`grid.benchmark_market` names the market whose native index correlations are
measured against.

### A cell with no instrument is declared, never inferred

The loader refuses a universe where a market × sector pair is neither measured by
a `grid_measures` row nor declared under `unmapped_cells`, refuses a pair claimed
by both, and refuses a `remaining` block left behind after its market was mapped.

That completeness check is what makes the status honest. An unmapped cell used to
become `inapplicable` by inference from the absence of a row, so *nobody looked*
and *somebody looked and there is nothing* reached the grid wearing the same
status and the same synthesised sentence. The declared reason now travels
verbatim into `grid.reason` and into `grid_coverage.coverage_note`, and it is
what a reader acts on: it names the exchange that was searched, the fund family
that does not exist there, or the near-miss that was rejected and why.

**A mapped instrument the store does not hold is a different thing again.** It
reports `n/a` with `instrument_not_seeded` and stays in the collection-health
denominator, because `obliskiq-collect universe` owes it. Calling that structural
would blame the declaration for a seeding failure — and, worse, would let a
universe collector that never ran shrink its own denominator until the coverage
floor passed on a store with one instrument in it.

### The macro blanket, and its one exception

A market with no measuring instrument has every leaf of each sector blanketed
`inapplicable`. Four families — `currency_return`, `policy_rate`, `yield_curve`
and `positioning` — read the market spec and the macro store and touch no
instrument data at all, so on the **`broad` cell only** they compute normally.

The blanket stays over the other eleven sectors deliberately: per-market macro is
one fact, and repeating it across twelve sector cells would put twelve copies of
a single reading into the numerator and make a market with no instrument look
better collected than one with eleven. `indicators.MACRO_ONLY_FAMILIES` is
derived from `FAMILY_INPUTS` rather than listed, so a family that gains a bar
input leaves the set instead of going on asserting a property it no longer has.

---

## How far each family reaches

A ranked view orders the cells of **one** family, and the four views print
identically whatever they ordered. `most_extended` ranks every cell with bars;
`widest_iv_hv` ranks the cells with a reachable option chain, which is a small
fraction of the grid — and the only way to discover that was to read
`universe.yaml` and work out which rows set `collect_chain`.

`build_grid.family_reach` derives per-family reach from the `grid` table: cells
total, cells reachable (not `inapplicable`), cells populated, and the structural
code accounting for most of the shortfall. Counted in **cells, not leaves** — a
family emits between one and ten leaves per cell, so a leaf count would rank the
wordy families as the far-reaching ones.

It surfaces in three places, and none of them requires reading code:

- `obliskiq-compute census` prints the whole table, worst first, and names any
  family below `grid.min_cells_for_ranked_view`. It prints **even when the census
  itself cannot run**: counting contracts needs a NAV and a collected chain,
  while reach is a property of the declarations.
- `obliskiq-compute grid` prints the narrowest family on the line after the
  totals, because the totals average over families that reach everything.
- Every `grid_rankings` row carries `reachable` and `ranked_over`, and below the
  floor the view's `detail` says so in words. **Labelled, never suppressed:**
  suppressing `widest_iv_hv` would delete the only implied-vol signal the store
  holds in order to avoid admitting the signal is thin, which is the opposite of
  the rule that a cell says `n/a` with its sample count.

---

## Running it end to end, offline

```bash
export OBLISK_DB_PATH=/tmp/oblisk-verify.db
obliskiq-collect universe --source fixture --fixtures tests/fixtures/ibkr
obliskiq-collect daily    --source fixture --fixtures tests/fixtures/ibkr \
    --skip vendor-chain --skip macro   # vendor-chain ignores --source and bills
obliskiq-compute all
obliskiq-compute census
```

Then check the store:

```sql
SELECT COUNT(DISTINCT indicator_family) FROM grid;      -- 15
SELECT COUNT(DISTINCT indicator) FROM grid;            -- 47 leaves
SELECT status, COUNT(*) FROM grid GROUP BY status;      -- ok / n/a / stale / inapplicable
SELECT indicator_family, COUNT(DISTINCT market || sector)
  FROM grid WHERE status != 'inapplicable' GROUP BY 1;  -- per-family reach
SELECT dimension, COUNT(*) FROM grid_rankings GROUP BY dimension;
SELECT asset_class_a, asset_class_b, status, COUNT(*)
  FROM correlation_matrix GROUP BY 1, 2, 3;      -- equity | fx | rate
SELECT verdict, passed_all_gates, min_required FROM universe_census;
```

Every `grid` row that is not `ok` carries a `reason`; the schema enforces it and
a test asserts it.

---

## Scheduling

The grid is a **daily** job and the census a **weekly** one
(`data.universe_census_frequency`). Both run after the collectors, because
`obliskiq-compute` only reads what they wrote. On the deployment VM, add them to
the same timer set described in `docs/COLLECTORS.md`:

```
obliskiq-collect daily      # bars, chains, curves, macro
obliskiq-compute all        # grid, rankings, fx, commodity
obliskiq-compute census     # weekly
```

A `degraded` exit from either is worth an alert but not a page: it means an
upstream step is not keeping up, or the store is still filling.

Systemd units ship alongside the collectors':

```
systemctl enable --now obliskiq-compute@all.timer      # weekdays, 23:30 UTC
systemctl enable --now obliskiq-compute@census.timer   # Saturdays, 06:00 UTC
```

`deploy/obliskiq-compute@.service` sets **`PrivateNetwork=true`**. The layer
touches no network by design, so the kernel enforces that rather than the code
merely promising it — and a future engine that reaches for an API fails there
rather than quietly acquiring a dependency the design forbids. The unit is
ordered `After=obliskiq-collect@daily.service` but does not depend on it: a grid
built on yesterday's bars is worth more than no grid, so a failed collector run
must not suppress the computation. `deploy/crontab.example` carries the same two
schedules for a machine without systemd.
