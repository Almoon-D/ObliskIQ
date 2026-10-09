# Geo & Prediction-Market Inputs — Asymmetry Hunter components g & h

**Version:** 6.5.0 · **Status:** deterministic evidence streams, circuit-broken, graded
**Prerequisite:** Asymmetry Hunter v1 (`docs/ASYMMETRY_HUNTER.md`)

> Per the February WorldMonitor analysis, the Asymmetry Hunter gains two external
> evidence streams. Both are **evidence, not standalone triggers** — they become
> weighted votes in the existing **≥ 3-component** candidate rule. Nothing fires on
> g or h alone (`min_components = 3`).

---

## 1. What was added

| Component | Module | Free API | Phase 1 key | Weight |
|-----------|--------|----------|-------------|-------:|
| **g — Polymarket divergence** | `modules/market/polymarket_client.py` | Gamma (`gamma-api.polymarket.com`) | `polymarket_divergence` | 0.07 |
| **h — GDELT pulse** | `modules/market/gdelt_client.py` | GDELT 2.0 DOC (`api.gdeltproject.org/api/v2/doc/doc`) | `gdelt_pulse` | 0.06 |

Both clients: HTTP via `modules/utils/http.fetch_with_retries` (retries + size
guard), wrapped by a Phase 1 **circuit breaker** (`polymarket_api`, `gdelt_api`),
and **graceful skip** on any failure — the component then degrades to 0 weight with
an honest `status` flag, exactly like the existing six.

The five original weights renormalized down (crowding/vol 0.22→0.19,
regime/cross 0.20→0.17, sentiment 0.16→0.15) to make room; the set still sums to
1.0, so `weights_hash` rolled over by design (AI-Scoreboard attribution moves to
the new config; old grades stay tied to the old hash).

---

## 2. Component g — Polymarket divergence

### APIs & config
- **Gamma API**, public, no key. Each curated market is fetched by slug:
  `GET https://gamma-api.polymarket.com/markets?slug=<slug>`. The "Yes" implied
  probability is parsed from the JSON-encoded `outcomes` / `outcomePrices` lists.
- **`config/polymarket_markets.json`** — hand-editable curated list (Fed decisions,
  recession-by-date, elections, geopolitical flashpoints). Each entry:
  `{id, slug, question, yes_means, data_stance}`. Add/remove freely; unreachable or
  unparseable markets are dropped.

### Daily snapshots + velocity
Every run, each market's probability is snapshotted to the warehouse table
`polymarket_snapshots` (idempotent per `(date, market_id)`). **Velocity** =
today's probability minus the prior stored snapshot's probability
(`DataWarehouse.get_polymarket_velocity`), so probability *acceleration* is
available to the report and to future logic.

### Component math
`gather_polymarket()` (in `phase1_intelligence.py`) compares each market's
crowd-implied probability `market_prob` to **our own data-implied stance**
`our_prob`, keyed by `data_stance`:

| `data_stance` | our_prob proxy (documented, deliberately simple) |
|---------------|--------------------------------------------------|
| `recession_regime` | `regime_transition.composite_score / 100` (a high leading-indicator transition score ⇒ elevated recession odds) |
| `fed_dovish` | same transition score as an easing-pressure proxy (a weakening economy raises the odds the Fed cuts) |
| `null` | no anchor → snapshots/velocity only, **no divergence vote** |

The headline signal is the most-divergent data-anchored market:

```
divergence = our_prob − market_prob          # signed
direction  = "off" if divergence > 0 else "on"   # we're more bearish than the crowd ⇒ risk-off
is_extreme = |divergence| ≥ polymarket_divergence_extreme   # default 0.20
score      = min(100, 50 + |divergence| / threshold × 25)   # 0.20→62.5, 0.40→100
```

`_polymarket_divergence_signal()` reads the precomputed dict (just like
`cross_asset_divergence`) and maps `direction` to a per-asset LONG/SHORT vote via
the asset's **risk polarity**.

---

## 3. Component h — GDELT pulse

### APIs & config
- **GDELT 2.0 DOC API**, public, no key. Per region we pull two timelines over a
  7-day window and keep the latest point:
  `mode=timelinevolraw` (article volume) and `mode=timelinetone` (average tone),
  `format=json`.
- **`config/gdelt_regions.json`** — per-region `{query, theme}` plus the static
  **`asset_map`**: `REGION → {ETF: "long"|"short"}` describing each asset's stance
  *under geopolitical stress* (long = benefits, short = hurt).

### Welford anomaly detection (incremental, cheap)
Per `(region, metric ∈ {volume, tone})` we keep a **Welford** running
mean/variance accumulator persisted in `gdelt_running_stats` (count, mean, M²).
Each run updates it incrementally and derives a z-score **against the
distribution before today's point** (so a genuine spike registers rather than
being absorbed):

```
z = (x − mean) / std        # std from M²/(count−1); None until count ≥ 2
# then: count += 1; delta = x − mean; mean += delta/count; M2 += delta·(x − mean)
```

Until the accumulator has seen `gdelt_min_history` (default 20) observations the
region reports `warming_up` and casts **no vote** — honest degradation, never a
fabricated z-score. Daily readings are also snapshotted to `gdelt_pulse`.

### Anomaly → asset mapping
A region is **anomalous** when `volume_z ≥ gdelt_anomaly_z` (default 2.5) **and**
`tone ≤ gdelt_tone_max` (default 0.0, i.e. negative/stress). `_gdelt_component()`
(in `asymmetry_hunter.py`) collects, per asset, the stance from every anomalous
region's `asset_map` entry:

- No mapped anomaly → neutral (no view, **not** degraded).
- Conflicting stances across regions → neutral.
- Consistent stance → a directional vote with
  `score = min(100, 50 + volume_z / gdelt_anomaly_z × 25)`.

Example map (shipped default):

| Region | Stances under stress |
|--------|----------------------|
| MIDDLE_EAST | USO long, GLD long, ITA long, SPY short |
| EUROPE | EWG short, FXE short, GLD long, ITA long, USO long |
| ASIA | EWJ short, GLD long, ITA long, SPY short |
| LATAM | EWZ short, GLD long |

Defense (ITA) and regional ETFs (EWG/EWJ/EWZ) were **added to the Hunter's
`ASSET_SPECS`** so the map is fully realized; they have no COT/commodity coverage,
so crowding (a) and commodity (d) self-degrade for them.

---

## 4. Wiring into the Hunter

`hunt()` computes the market-wide `polymarket_divergence` signal and loads the
GDELT map once, then in the per-asset loop the component list becomes:

```python
components = [crowding, vol, regime, cross, sentiment, polymarket, gdelt]
```

They participate identically to the others: only `available & is_extreme &
directional` components vote; a candidate needs ≥ `min_components` (3) independent
names agreeing on the same direction; weights are renormalized over contributors
only, so degraded streams never dilute. (A rounding fix floors the per-dossier
convexity-sleeve share to 4 dp so summed premiums never trip the 5% rail.)

---

## 5. Phase 1 report

`HybridFormatter` renders a one-screen **"🌐 Geo & Prediction Markets"** section
(before the Asymmetry Watchlist), reused verbatim in the Phase 2 prompt:

```
**🎲 Polymarket (prediction-market divergence)**
- Divergence: +30% (risk-off) — US recession 2026: crowd 60% vs our 90%
  - US recession 2026: crowd 60% (Δ7d +15%)

**🌍 GDELT (geopolitical pulse)**
- Most anomalous region: MIDDLE_EAST
  🔴 MIDDLE_EAST: vol z +5.0σ, tone -4
```

---

## 6. Grading & honesty

Dossiers that cite g or h are graded by the AI Scoreboard like every other
Asymmetry dossier (`source='asymmetry_hunter'`, `prompt_hash = weights_hash`).

**Honest limitations.**
- Our `our_prob` proxies (regime-transition score) are intentionally crude; a
  market without a supported `data_stance` contributes snapshots/velocity only.
- GDELT z-scores warm up for 20 observations per region before voting.
- Both streams self-zero when their API is unreachable (circuit breaker open).
- They are small-weight evidence by design — they sharpen conviction when they
  align with the core signals; they never manufacture a candidate alone.

---

## 7. Storage schema (SQLite + PostgreSQL)

| Table | Key | Columns |
|-------|-----|---------|
| `polymarket_snapshots` | `(date, market_id)` | question, prob_yes, velocity |
| `gdelt_pulse` | `(date, region)` | volume, tone, volume_z, tone_z |
| `gdelt_running_stats` | `(region, metric)` | count, mean, m2 (Welford) |

## 8. Tests

- `tests/test_polymarket_client.py` — recorded Gamma payloads: outcome/price
  parsing, snapshot persistence, velocity vs a fake warehouse, unreachable skip.
- `tests/test_gdelt_client.py` — recorded DOC timelines: Welford z-score,
  anomaly (spike + negative tone), warm-up degradation, unreachable region.
- `tests/test_asymmetry_hunter.py` — g and h count toward the ≥3 rule, degrade
  when absent, and surface a regional-ETF (EWG) candidate from regime+cross+gdelt.

All use mock/recorded payloads — **no live API calls in CI**.
