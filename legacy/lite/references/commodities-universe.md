# Commodity and materials universe

Curated proxy list. Every proxy carries a purity caveat — a producer equity is
never a clean commodity exposure, and pretending otherwise is how you end up
trading specialty-chemicals multiples while believing you are trading lithium.

`commodity_beta` and `r2` are recomputed monthly by `build_grid.py` against the
reference price series where one exists. Below `min_r2` the proxy is flagged
unreliable and must not be presented as commodity exposure.

---

## Directly priced (reference series available)

| Commodity | Bucket | Reference | Notes |
|---|---|---|---|
| WTI / Brent | energy | FRED `DCOILWTICO`, `DCOILBRENTEU`; IBKR futures | EIA inventories, OPEC+ decisions, Hormuz risk premium |
| Natural gas (Henry Hub) | energy | FRED `DHHNGSP` | Extreme seasonality; RV percentiles need seasonal adjustment |
| **TTF vs Henry Hub spread** | energy | ICE TTF via IBKR | European industrial competitiveness. Directly relevant to an EUR book and usually overlooked |
| Gold | precious_monetary | IBKR futures / LBMA | Driven by real yields, DXY, central-bank buying — **not** the industrial cycle |
| Silver | precious_monetary | IBKR futures | Hybrid: ~50% industrial. Sits between buckets; report both drivers |
| Copper | industrial_metals | IBKR futures | The cleanest global-cycle read available |
| **Baltic Dry Index** | industrial_metals | free daily | Leading indicator for bulk demand. Free, and a genuine early signal |

## Strategic / niche — no free spot feed, equity proxies only

| Theme | Proxies | Purity caveat |
|---|---|---|
| Uranium | SPUT (Sprott Physical Uranium, Canadian-listed), CCJ, URA | **SPUT is the closest thing to a spot proxy.** Spot uranium is thin; the term-contract market is what actually matters and is not free. CCJ carries contracted pricing and equity beta |
| Lithium | ALB, SQM, LIT | **ALB is a diversified specialty chemicals company**, not a lithium pure-play. Expect weak beta to lithium carbonate. SQM adds Chilean political risk |
| Copper producers | FCX, COPX | **FCX is materially gold-exposed too**, and carries high equity beta. Not a clean copper read |
| Rare earths | MP, REMX | Extremely policy-driven — Chinese export controls dominate fundamentals |
| Industrial gases | LIN, APD | **Poor proxy. Do not use for helium or gas pricing.** Oligopoly with long-term contracted pricing; trades as a quality industrial compounder with near-zero response to spot. Expect R² well under the threshold — flag and exclude |
| Agriculture | MOS, NTR, DBA | Fertiliser names track natural gas input costs as much as crop prices |

---

## Regime signals

**Term structure.** Steep backwardation signals physical tightness and is
**directionally supportive of long exposure**, not a dislocation warning. Steep
contango signals oversupply and, critically, destroys any futures-tracking
wrapper held over a 90–200 day tenor through negative roll. Check the curve
before choosing the instrument, every time.

**COT positioning.** Speculative positioning at a 5-year extreme raises the
probability that a move, once it starts, is violent — the unwind is the fuel.
It does **not** time the move; extremes persist for months. Use as a
conditioning variable requiring separate price or volatility confirmation.

**Coiled commodities.** 30-day realised vol at a multi-year low percentile,
inside a compressed range, with positioning at an extreme. This is the highest
quality setup in the commodity space and maps directly onto the mandate.

**Inventory flows.** EIA weekly for crude and products. Deviation from the
5-year seasonal band matters far more than the absolute level.
