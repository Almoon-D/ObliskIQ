# marketdata.app — observed behaviour

Companion to `ibkr-playbook.md`, same discipline: every line carries whether it
was **executed** against the live API or derived from documentation, and the date.

Status tags: **[MD ✓ 2026-08-28]** executed live on that date;
**[MD ?]** documentation-derived, never executed.

---

## Why this route exists

The build was sequenced on the premise that option-chain history cannot be
retrieved retrospectively, which made daily self-collection the single highest
priority. That is true of IBKR and **false of the market**. This vendor serves
historical end-of-day chains with implied vol and full greeks.

**Mind the two credit tiers.** The live/cached tier bills **per option symbol**
(observed: 3 contracts, 3 credits). The much cheaper 1-per-1000 price applies to
historical pulls only and is still `[MD ?]`. So the DAILY run is the expensive
one and the backfill is the cheap one — the opposite of the intuition, and the
assumption that mis-sized every budget in the first cut of this route.

It covers the twelve USD-listed `collect_chain` names. It does **not** cover the
three European listings (ASML/AEB, SAN/BM, TTE/SBF) — for those the original
argument holds exactly as written and `chain_collector` remains the only route.

---

## Verified live

### The chain endpoint returns implied vol and full greeks  [MD ✓ 2026-08-28]

`GET /v1/options/chain/{symbol}/` with `dte=120&side=call&strikeLimit=3` on SPY
returned, per contract: `bid bidSize ask askSize mid last volume openInterest
inTheMoney intrinsicValue extrinsicValue underlyingPrice iv delta gamma theta
vega`, all non-null. Recorded verbatim at
`tests/fixtures/option_vendor/raw/chain_SPY_live.json`.

### HISTORICAL chains carry NO implied vol and NO greeks  [MD ✓ 2026-09-09]

**The question the backfill was waiting on, and the answer is no.** One dated
call for SPY on the 2026-09-02 session returned **608 contracts, every one of
them with `iv` and `delta` null**. Prices and size were complete on the same
rows: `bid`, `ask`, `last`, `openInterest` and `volume` were non-null 608/608.
So the historical endpoint serves a full chain minus exactly the two columns
this route was bought for.

The pricing page's "IV & Greeks — included" is a TIER statement, not an
endpoint one: it is true of the live chain, which does return them (above), and
not of the dated one. The third-party comparison that said greeks and IV are not
served historically was right, and the marketing page was the misleading source.

Taken through the collector rather than by hand, so what is recorded is what the
store would hold:

```
obliskiq-collect vendor-chain --only SPY --as-of 2026-09-02
```

It reported **degraded**, exit 2, with the reason already written for this case:
*"608 of 608 contracts carry no implied vol, above the 20% ceiling"*. Nothing had
to change to handle it — `option_vendor.max_null_iv_fraction` and the null-iv
count were built for this answer and met it correctly on the first live run.

**What it costs downstream.** `widest_iv_hv` ranks on `iv_minus_hv`, so a chain
with no implied vol cannot populate it. Measured on the store this probe wrote:
608 historical rows populated **0** implied-vol leaves, and grid coverage did not
move (238/2092 before and after). A vendor backfill alone will fill
`chain_snapshots` with prices and open interest and will **not** turn on the
implied-vol half of the grid. `ROADMAP.md` carries what that leaves open.

Solving IV from the mid price is the remaining route and is still not taken here:
it needs a risk-free rate at each contract's own maturity and a dividend yield.
`quant/bsm.py::implied_vol` would do the solving (Newton with a bisection
fallback and a per-contract `converged` mask), the rates are in the store, and
the dividend yields are the gap — every `universe.dividend_yield` is `null` and
marked UNVERIFIED. That is a compute-layer phase, not a collector change.

### The historical tier really is 1 credit per 1000 symbols  [MD ✓ 2026-09-09]

The same call: **608 contracts, `x-api-ratelimit-consumed: 1`**. The documented
1-per-1000 historical price is now observed rather than assumed, and the gap
against the live tier is as large as `config.yaml` warns — the live tier billed
3 credits for 3 contracts on 2026-08-28, so the same 608 contracts would have
cost roughly 608. `option_vendor.max_credits_per_run` is sized for the live
tier and is not what bounds a backfill.

### Implied vol is a DECIMAL  [MD ✓ 2026-08-28]

`iv: 0.1495`, not `14.95`. Matches `chain_snapshots.iv` ("decimal, not percent")
with no conversion. Pinned by a test, because a vendor switching to percent would
put a number a hundred times too large in the column and nothing downstream
would question it.

### HTTP **203**, not 200  [MD ✓ 2026-08-28]

The caching tier answers `203 Non-Authoritative Information` with a complete
body. `macro/fetchers.py::_HttpFetcher._get_json` raises on anything `!= 200`, so
a base class copied from there rejects good data. `option_vendor` accepts
`{200, 203}`.

### The response is COLUMNAR  [MD ✓ 2026-08-28]

Parallel arrays — `optionSymbol[]`, `strike[]`, `iv[]` — the same shape as the
IBKR **MCP** route and unlike Client Portal's `data[]` of row objects. Three
decoders now exist for three routes, and that is correct: a decoder written for
one silently returns nothing on another.

### The LIVE tier takes no `date=`, and that once decided the row's date  [MD ✓ 2026-08-28]

Reaching the live/cached tier means omitting `date=` entirely — the parameter is
what selects the historical tier. `VendorClient.chain` used to derive the session
it stamped rows with from the same argument, so a run pinned to a past date
stamped its NEWEST session with `eastern_session()`: the clock, not the date the
run was given. In production the two agree, which is why it survived; a
backfill re-run for a past day did not. The session and whether the request
carries `date=` are separate arguments now (`trade_date`, `dated_request`).

Worth stating as a rule rather than a fix: **a collector must read the clock
exactly once, at the top of the run.** Every date below that point comes from the
run, or the run cannot be replayed.

### Timestamps are epoch SECONDS, US Eastern  [MD ✓ 2026-08-28]

`expiration: 1798750800` is 2026-12-31 **16:00 America/New_York**.

Two traps. `timeutil.epoch_ms_to_stamp` takes **milliseconds** — feeding it these
lands in January 1970. And it returns UTC: a 16:00 ET expiry is 20:00 or 21:00
UTC, so the *date* survives a UTC reading, but only because the equity close is
early enough. That is a coincidence of the US cash close, not a rule, so the
conversion is done in ET explicitly.

The `date=` parameter names a **US session**. A collector running at 02:00 UTC
asking for `utc_today()` asks for a date New York has not reached.

### `underlyingPrice` ships inside the chain response  [MD ✓ 2026-08-28]

Spot and option quotes come from one response and cannot disagree about the
moment they describe. The delayed-spot-paired-with-live-quotes failure is
structurally impossible on this route — worth noting because it is a real risk on
the IBKR routes, where the two are separate calls.

### Credit accounting is in the response headers  [MD ✓ 2026-08-28]

`x-api-ratelimit-consumed`, `x-api-ratelimit-remaining`, `x-api-ratelimit-limit`,
`x-api-ratelimit-reset`. Spend is recorded from the header rather than estimated
from the contract count.

**Live/cached data costs one credit per option symbol returned** — a 3-contract
request consumed 3. Documentation says historical costs 1 per 1000 symbols
**[MD ?]**; that has not been executed (see below).

### The observed limit was 10,000/day, not the documented 100  [MD ✓ 2026-08-28]

`x-api-ratelimit-limit: 10000` on a free-tier token, against a pricing page that
documents 100 daily credits for "Free Forever". Most likely the 30-day new-account
demo. **Plan for 100 after the trial lapses**, and re-read the header rather than
assuming either figure.

### ONE IP AT A TIME, enforced hard  [MD ✓ 2026-08-28]

> **A cloud sandbox is not reliably blocked, and that is not a reprieve.** The
> 2026-09-09 probe above went through from an agent sandbox on the first try and
> was never refused. One call is not evidence that the egress held still; it is
> evidence that it did not move during one call. The single-IP rule is unchanged
> and a 252-session backfill still wants a host with a stable address — a block
> partway through leaves the walk resumable but idle for
> `option_vendor.ip_block_cooldown_seconds` each time it is hit.


> `Access denied. Only one device is permitted. Your IP address has changed, and
> your account is temporarily blocked for security reasons. Please wait 5 minutes
> before trying again.`

HTTP 403, `s: "error"`, and **`x-api-ratelimit-consumed: 0`** — a blocked call is
free, so retrying costs nothing but time.

**This makes the API unusable from a cloud agent sandbox**, whose egress proxy
rotates its source address on essentially every request; five spaced retries of
one historical call were all refused, and there is no sticky-egress option.
**The collector now remembers the block.** It is account-wide rather than about
the symbol that met it, so the run stops at the first 403, writes a cooldown into
`collector_state` for `option_vendor.ip_block_cooldown_seconds`, and reports
degraded. A run inside that window makes no call at all. Before this, all twelve
US targets rediscovered the same block independently on every run.

Collection must run from a host with a stable egress address — a laptop, or the
cloud VM once it exists (on Oracle or Google, with its address reserved:
`docs/DEPLOY.md` §4b).

---

### The historical tier's SPDR coverage is PER SYMBOL  [MD ✓ 2026-09-09, corrected 2026-09-10]

> **Corrected 2026-09-10.** This section was headed *"the historical tier does
> NOT cover the sector SPDRs"*. That generalised one 404 and it is wrong: XLF and
> XLE both served on the same tier and the same session date. See *Which SPDRs
> the historical tier actually serves* below. What survives is the last paragraph
> — coverage is per symbol and cannot be assumed either way.

The finding that decides whether a vendor backfill can fill the option half of the
**grid**, as opposed to the option half of the store. Three dated calls, all for
the 2026-09-02 session:

| symbol | measures a grid cell | result |
|---|---|---|
| SPY  | no — `grid_measures: false` | **608 rows** |
| QQQ  | yes — `us/technology`       | **592 rows** |
| XLP  | yes — `us/consumer_staples` | **HTTP 404** `{"s":"no_data","nextTime":null,"prevTime":null}` |

XLP returned the same 404 on all three retry attempts, so it is coverage rather
than a transient. `no_data` with **both** `nextTime` and `prevTime` null is the
shape to read: the endpoint is not saying "not on that date", it is saying it has
no history for the symbol at all.

**Why this bites harder than it looks.** Of the twenty-four instruments under
`collect_chain`, eleven are the measuring instrument for their market x sector
cell — the nine sector SPDRs, plus QQQ and XLE. SPY is **not** one of them:
`us/broad` is measured on **SPX**, and `universe.yaml` declares no index chain is
reachable on any route, so that cell is `inapplicable` with
`reason_code: options_not_available` however much SPY history is bought. A
backfill that covers SPY and QQQ but not the SPDRs therefore reaches **one** of
the eleven option-bearing cells, not eleven.

Whether the other eight SPDRs behave like XLP was untested when this was written;
it is partly answered now, and the answer is that **XLP is the exception rather
than the rule** — see below. What is settled is that coverage on this tier is
**per symbol and cannot be assumed** in either direction.

### Solving the IV the historical tier does not serve  [MD ✓ 2026-09-09]

The route out of *HISTORICAL chains carry NO implied vol* above, measured on the
chains those calls bought. `obliskiq-compute solve-iv` inverts the mid price
through `quant/bsm.py::implied_vol`:

| | |
|---|---|
| underlyings solved | 2 of 2 attempted (SPY, QQQ) |
| contracts converged | **61 of 61** (0% non-converged, ceiling 20%) |
| SPY ATM implied vol, 2026-09-02 | **0.1561** |
| QQQ ATM implied vol, 2026-09-02 | **0.2074** |
| grid `implied_vol_atm` leaves populated | **1**, `us/technology` — was 0 |

The SPY figure is coherent against the connector's own readings for the same name
on 2026-09-09: `historical-vol.annual_pct` 0.0956 and
`implied-vol-underlying.annual_iv` 0.1325 (a 30-day figure), against 0.1561 solved
at ~138 DTE. Realised below short-dated implied below long-dated implied is an
ordinary term structure, which is the sanity check available without a quoted
long-dated IV to compare against — it is **not** a verification of the solved
number.

Only `us/technology` lights up because only QQQ both measures a cell and is served
by this tier; see the coverage finding above. `iv_minus_hv` stays `n/a` on that
cell for an unrelated reason it states plainly — *"fewer than 31 usable daily
closes"* — because no QQQ bars had been collected into that store.

### Which SPDRs the historical tier actually serves  [MD ✓ 2026-09-10]

Three dated calls on the historical tier, all for the **2026-09-02** session so
they are directly comparable with the 2026-09-09 readings above. Spend was capped
at three probes before any were made, and the walk stopped at the first block
rather than waiting out a cooldown.

| symbol | measures a grid cell | result | credits |
|---|---|---|---|
| XLF | yes — `us/financials` | **21 rows**, `degraded` (null iv) | **1** |
| XLE | yes — `us/energy` | **30 rows**, `degraded` (null iv) | **1** |
| XLV | yes — `us/healthcare` | **HTTP 403, IP block** — no answer about coverage | **0** |
| | | | **2 total** |

**XLP's 404 does not generalise.** Two of the two SPDRs that got an answer served
history, so the tier reaches at least part of the sector complex. XLV is still
unknown: the block intervened before the endpoint replied, which is a different
outcome from `no_data` and must not be recorded as one.

**The row counts are an order of magnitude below SPY's 608 and QQQ's 592.** 21 and
30 contracts are what the collector's DTE and strike-band filters leave, not what
the tier holds; a sector SPDR chain is genuinely thinner than SPY's. Both runs
exited **2** with `iv` null throughout, which is the documented shape of this tier
and the reason `obliskiq-compute solve-iv` exists.

Still untested after this: **XLB, XLC, XLI, XLRE, XLU, XLV, XLY** — seven of the
eleven cell-measuring instruments.

### The IP block, caught in the act  [MD ✓ 2026-09-10]

The third call was refused, and the body names both addresses:

```json
{"s":"error",
 "errmsg":"Access denied. Only one device is permitted. Your IP address has
           changed, and your account is temporarily blocked for security
           reasons. Please wait 5 minutes before trying again.",
 "authorizedIP":"160.79.106.132",
 "blockedIP":"160.79.106.131"}
```

**The two addresses differ by one.** The sandbox's egress pool rotated a single
step between the second call and the third — which is exactly why *ONE IP AT A
TIME* above says a cloud sandbox is unusable for a walk, and why two calls
succeeding is not evidence that a third will.

Three things behaved as documented and are now confirmed rather than assumed:

* **A blocked call is free.** `vendor_fetch_log` recorded `credits` NULL for XLV
  against `1` for each of XLF and XLE. Total spend for the probe was **2 credits**.
* **The block is account-wide, not per symbol.** It was recorded once, against a
  cooldown key rather than against XLV.
* **The collector remembers it.** `collector_state` holds
  `cooldown.marketdata_app` with `until` 15 minutes out
  (`option_vendor.ip_block_cooldown_seconds`), the run reported `degraded` rather
  than failed, and a run inside that window makes no call at all.

**The 5 minutes the message asks for is not the wait this build takes**, and that
is deliberate: `ip_block_cooldown_seconds` is 15 minutes because the cost of
waiting too long is one skipped run and the cost of waiting too little is another
block on top of the first. Retrying into a block also locks out the *real*
collection host, since the ban is account-wide — so a sandbox probe that meets one
should stop, not sleep and retry.

## Not verified, and why it matters

### Historical depth on the free tier  [MD ?]

Documented as one year free, five on Starter ($12/mo annual). One year is exactly
`data.iv_self_source_min_sessions` (252), the point at which the IV percentile
stops depending on a vendor figure. Not executed.

### `no_data` for a market holiday  [MD ?]

Documented as one of three `s` values. Not observed live — the block intervened.
The collector treats it as an outcome, not a failure: zero rows, target not
failed, recorded in `vendor_fetch_log` so the weekday walk never buys the same
holiday twice. That is what lets the backfill use weekdays instead of importing
the exchange calendar into the collect layer.

---

## The split with IBKR

| Data | Route | Why |
|---|---|---|
| US option chains, today and historical | **marketdata.app** | One call per name; history retrievable |
| European option chains (ASML, SAN, TTE) | IBKR | The only route that reaches them |
| Bars, portfolio, trades, account, NAV | IBKR CP (OAuth) | Unattended |
| Live quotes at decision time, order staging, alerts | IBKR MCP connector | Session-time only |

The constraint that decides this split is **attendance**, not capability:
anything on a schedule must be servable without a human, and connector-only data
must never be load-bearing for an unattended artefact.
