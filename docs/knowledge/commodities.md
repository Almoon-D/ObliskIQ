# Commodities

What the commodity engine (`engines/commodity`) and the `es` contango check rest on: how commodity
markets behave, which proxies stand in for an exposure no retail wrapper carries, and how pure each
proxy is. Condensed from lite's `references/commodities-universe.md` (@b1ebd49), a curated
reference, not a measurement. The values are in `config/defaults/commodities.yaml`; the roots,
their buckets and their series are `futures_roots` in `config/universe.yaml`.

Tag: `[lite ref]` = carried over from lite's reference, a practitioner's judgement never
measured here. `VERIFY-LIVE(<what>)` = queued in `docs/ROADMAP.md`.

## The roots and what can express them

A futures contract is never a position (the mandate); a root is a data series. Its `expression`
in the universe says what may carry the view:

| Bucket | Roots | Expressible through |
|---|---|---|
| energy | CL, BZ | an EU-listed physical ETC or a producer equity |
| energy | NG | a producer equity |
| energy | HO, RB, TTF | nothing a retail account may buy: a macro signal only |
| precious_monetary | GC, SI | an EU-listed physical ETC |
| industrial_metals | HG | a producer equity |
| grains_oilseeds | ZC, ZW, ZS | the fertiliser complex and Bunge, as a macro signal |
| softs | KC, CC | JDE Peet's; Barry Callebaut and Lindt |
| softs | SB, CT | nothing: a macro signal only |

- **Gold** is driven by real yields, the dollar and central-bank buying, not the industrial
  cycle. **Silver** is roughly half industrial: it sits between the buckets and both drivers
  are reported [lite ref].
- **Copper** is the cleanest read of the global cycle; the **Baltic Dry Index** is a free daily
  leading indicator of bulk demand [lite ref].
- **TTF against Henry Hub** is the spread behind European industrial competitiveness, and bears
  on an EUR book directly [lite ref].
- **Natural gas** is extremely seasonal: its realised-volatility percentiles need a seasonal
  adjustment (the universe marks it `seasonal: true`) [lite ref].
- IBKR returned no `/trsrv/futures` ladder for KC, NG, TTF and ZS (`ibkr.md`, Futures): those
  roots have no curve until the ladder answers, and their curve leaves are absent with a reason.

## Proxy purity

A producer equity is never a clean commodity exposure [lite ref]:

| Theme | Proxies | Caveat |
|---|---|---|
| Uranium | SPUT, CCJ, URA | SPUT (Sprott Physical Uranium) is the closest to a spot proxy; spot is thin and the term-contract market, which matters, is not free. CCJ carries contracted pricing and equity beta |
| Lithium | ALB, SQM, LIT | ALB is a diversified specialty-chemicals company with weak beta to lithium carbonate; SQM adds Chilean political risk |
| Copper producers | FCX, COPX | FCX is materially gold-exposed and carries high equity beta |
| Rare earths | MP, REMX | policy-driven: Chinese export controls dominate the fundamentals |
| Industrial gases | LIN, APD | not a proxy for helium or gas prices: an oligopoly on long contracts, near-zero response to spot |
| Agriculture | MOS, NTR, DBA | fertiliser names track natural-gas input costs as much as crop prices |

Lite recomputed each proxy's commodity beta and R² monthly against its reference series and
flagged a proxy below a minimum R² as unreliable. That regression is not ported (Follow-ups in
`docs/ROADMAP.md`): until it is, no proxy here is presented as measured commodity exposure.

## Regime signals

- **Term structure.** Steep backwardation is physical tightness and supports a long exposure;
  it is not a dislocation warning. Steep contango is oversupply, and over a 90–200 day hold it
  destroys a futures-tracking wrapper through negative roll. The curve is read before the
  instrument is chosen, every time [lite ref]: beyond
  `term_structure.contango_max_annualised_for_futures_wrapper` the wrapper is dropped, and under
  `es` a futures-tracking ETC is ineligible (DECISIONS D113, D137).
- **Positioning.** Speculative positioning at a five-year extreme makes a move, once started,
  likelier to be violent (the unwind is the fuel). It does not time the move: extremes persist
  for months. It conditions, and needs a separate price or volatility confirmation [lite ref]
  (`cot.require_price_confirmation`).
- **Coiled commodities.** Thirty-day realised volatility at a multi-year low percentile, inside
  a compressed range, with positioning at an extreme: lite's highest-quality commodity setup for
  the satellite's mandate [lite ref]. The screen bucket that finds it is not ported (Follow-ups).
- **Inventory.** EIA's weekly crude and product stocks: the deviation from the five-year seasonal
  band matters far more than the level [lite ref]. EIA's release dates are in `macro-sources.md`.

## What the engine reads

- **The curve** is Session 7's collection (`data/market/curves.py`): the deferred point's roll
  yield against the front, annualised on calendar days; the front carries none and an unpriced
  point carries none. The engine reads the front against the next priced rung: a positive roll
  is backwardation, a negative one contango, and the contango it checks is the roll's negative.
  `VERIFY-LIVE(futures curve prices)`: the ladder was seen live, its prices only in fixtures.
- **Positioning** is Session 10's CFTC reading (DECISIONS D134), **cycle position** the long
  price history over `sources.macro.commodity_history.window_years`, **inventory** the root's
  `inventory_series` against the same ISO week in prior years.
