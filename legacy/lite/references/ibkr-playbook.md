# IBKR MCP playbook

Empirically observed behaviour. Append as new quirks are found.

## Which route a line describes

There are **three** IBKR routes and they do **not** behave the same, so every
finding below is tagged with the one it was observed on:

- **MCP** — the IBKR MCP connector's tools. The skill uses them.
- **CONNECTOR** — the same tools reached over HTTPS at IBKR's **Custom
  Connector** endpoint, `https://api.ibkr.com/v1/api/mcp-public`. Same upstream,
  same response shapes; different transport and a completely different
  credential. See *The Custom Connector route* below.
- **CP** — the Client Portal Web API over OAuth 1.0a. This is the route the
  collectors are written against, and the only one this build can authenticate on
  its own today.

**A correction, and it matters for the other two lines.** This section used to say
MCP was "available only inside a chat session", so a VM with no human present
could never call it. That is no longer true: the Custom Connector answers a
headless container. What has not been established is that this build can hold the
OAuth 2.0 bearer token the endpoint takes, which is a different limit and the one
that actually binds. An MCP finding and a CONNECTOR finding describe the same
upstream and can be read across; neither transfers to CP.

A finding verified on MCP is **not** thereby verified on CP. The response shapes
diverge structurally — see *MCP and CP are not the same API* below — and reading
an MCP-verified line as a CP fact is the mistake this section exists to prevent.

Status tags: **[MCP ✓ 2026-08-23]** verified live on that date;
**[CONNECTOR ✓ 2026-09-03]** executed against the remote endpoint on that date;
**[DOC]** read in IBKR's published material and never executed;
**[CP ?]** documentation-derived, never executed.

## Confirmed by testing

- **Option price history is unavailable.** `get_price_history` on an option
  contract returns `{"error":"No historical market data available"}` even for
  highly liquid contracts (originally tested at 49,114 open interest, 445
  contracts traded that session). There is no as-of endpoint for historical
  chains. Per-strike IV history can only be self-collected.
  **[MCP ✓ 2026-08-23]** — re-tested on a fresh contract, SPY DEC 18 '26 770 Call
  (conid 689497493, 9,334 open interest), `security_type="OPT"`, `exchange=SMART`:
  same error, verbatim. This is the premise the entire collector design rests on
  and it holds.
- **Weekly bars:** `step=ONE_WEEK, step_count=260` returns 5+ years of OHLCV in a
  single call. **[MCP ✓ 2026-08-23]**
  - **Corrected: `step_count` is approximate, not a count.** 260 requested on SPY
    returned bars from 2019-05-28 to 2026-08-17 — about 378 weeks. A 10-bar
    request on SPX returned 15. IBKR appears to serve a *span* it derives from the
    request rather than the exact number asked for. Nothing may assume
    `len(bars) == step_count`, and a chunking loop that derives its stride from
    the returned count must measure what actually came back. The backfill already
    does this — it judges progress by whether the earliest bar moved.
- **Daily bars:** work, but probe the cap before assuming 5 years in one call.
- **Index contracts:** use `get_price_history` with `security_type="IND"` and the
  native exchange for the level. **[MCP ✓ 2026-08-23]** — SPX (conid 416904) on
  `CBOE` returned bars.
  - **Corrected: `get_price_snapshot` on an index is not empty.** The original
    line said it was. SPX returned `historical-vol` and
    `implied-volatility-percentile` but no `last`, no `bid-ask` and no
    `top-status`. The *quote* side is absent; the *derived vol* side is served,
    and it is the side the grid actually wants for indicator 8. Do not skip the
    snapshot call on an index on the strength of the old wording.
- **Futures and futures options:** require the native exchange; SMART returns
  empty. **[MCP ✓ 2026-08-23]** — the HG (copper) ladder resolved on `COMEX`.
- **Key casing:** responses hyphenate what requests underscore
  (`option_midpoint_iv` in, `option-midpoint-iv` out). **[MCP ✓ 2026-08-23]**
  - **The INNER keys are not consistently cased, and that is a separate trap.**
    In one response `option-midpoint-iv` carried `{dailyIv, annualIv, isValid}`
    while `implied-vol` carried `{daily_iv, annual_iv, is_valid}` — camelCase and
    snake_case side by side, for the same three concepts. Never reuse an
    accessor across fields on the assumption the inner shape matches.
- **Strikes** are returned as strings. Cast before arithmetic.
  **[MCP ✓ 2026-08-23]** — `get_option_data` returns `"strike": "760"`.
- **Chains** must be bounded with `min_strike`/`max_strike` or liquid names
  return fragmented or truncated sets. **[MCP ✓ 2026-08-23]** — IBKR's own tool
  documentation now says an unbounded chain on a liquid name returns hundreds of
  rows, which agrees.
- **`get_pa_allocation` ignores the `currency` parameter.** **[MCP ✓ 2026-08-23]**
  **This replaces the previous claim, which was wrong.** The old line said a
  non-base currency "silently returns the prior trading day". It does not. On a
  EUR-base account, `EUR`, `USD` and `GBP` each returned `realtime: true` and a
  **byte-identical** NAV of `11170.338885808784`, matching the EUR
  `net_liquidation` from `get_account_summary`. Only the echoed `currency` label
  changed. No FX conversion is applied and no prior-day switch happens.

  IBKR's own tool documentation still asserts the prior-day behaviour, so the
  vendor documentation is wrong here too — which is the reason to test rather
  than read.

  The practical rule is unchanged and now better founded: **read allocation in
  the account's base currency and never request another.** A caller that trusts
  the label gets base-currency figures wearing a different name, and every cap
  and weight computed from them is wrong by the FX rate rather than visibly
  broken.
- **Vendor IV percentiles** are not comparable to standard vol indices for broad
  ETFs — S&P 500 was once reported at the 100th percentile while VIX sat near a
  three-month low. Cross-check before relying on them.
  **[MCP ✓ 2026-08-23]** — the field itself works and is documented below. The
  anomaly did not reproduce on this date (SPY 52-week percentile 0.1195), which
  neither confirms nor clears the original observation: one clean reading is not
  evidence that a sporadic fault is gone. The standing advice to cross-check, and
  to switch to the self-computed percentile at
  `data.iv_self_source_min_sessions`, is unchanged.

## Found during the build

### From the IBKR-Portmanteau port (Session 1)

All **documentation-derived and UNVERIFIED** — written in a sandbox with no live
IBKR access. Session 5 must confirm or correct every line.

- **Client Portal Web API OAuth 1.0a, live session token.** The access-token
  secret is RSA-decrypted (PKCS#1 v1.5) with the consumer's *encryption* key to
  give the DH "prepend". The prepend is prefixed to the signature base string for
  `/oauth/live_session_token` and is NOT itself an OAuth parameter. The token is
  `HMAC-SHA1(key=B^a mod p, msg=prepend)`, verified against
  `live_session_token_signature = HMAC-SHA1(key=token, msg=consumer_key)`.
  SHA-1 here is IBKR's protocol, not a choice.
- **The DH shared secret is a signed big integer.** A value whose high bit is set
  carries a leading zero byte. Omitting it derives a different token and every
  later call fails authentication with no useful error.
- **A wrong encryption key may not be detected at decryption.** Where OpenSSL
  implements implicit rejection for PKCS#1 v1.5 as a Bleichenbacher
  countermeasure, decrypting with the wrong key returns indistinguishable random
  bytes rather than raising; on other builds it raises. Both behaviours were
  observed across one CI matrix run, on the same commit, differing only by Python
  version and therefore by the bundled OpenSSL. Do not depend on either: the
  live-session-token signature check is the backstop that catches the mismatch on
  the builds that return quietly, and must never be skipped.
- **`/iserver/auth/ssodh/init`** opens the brokerage session and is required before
  any `/iserver/*` call. `/tickle` keeps it alive.
- **`/iserver/secdef/search` ranks by its own relevance, not by ticker match.** A
  short ticker routinely returns an unsponsored ADR or a foreign listing ahead of
  the instrument asked for. Always scan for an exact ticker match before accepting
  the first row. Carried over from IBKR-Portmanteau, which had this right.
- **Snapshot fields do not populate on the first call** for a conid the session has
  not subscribed to. The first response is a skeleton; poll until the requested
  field ids appear. Treating the first response as data yields an empty quote
  indistinguishable from a halted one. *Needs live confirmation of how many polls
  and at what spacing.*
- **`/iserver/marketdata/history` and options chains have no equivalent in
  IBKR-Portmanteau.** Its bars and chains both came from Yahoo Finance. A
  Yahoo-sourced chain carries no IBKR conid, and conid is what the snapshot and
  history endpoints take — so that route cannot feed the collector at all. Chains
  must come from `/iserver/secdef/strikes` + `/iserver/secdef/info`.

### From the collectors (Session 2)

Documentation-derived unless marked otherwise. Session 5 confirms or corrects.

#### Field-definition audit — scale, units and lookbacks

The single most dangerous class of bug in this layer is a field that parses
cleanly and means something else. Each of these is now pinned by a unit test.

- **Implied vol has been observed both as a bare decimal and as a
  percent-suffixed string.** `safe_float("31.42%")` returned the *default*, so an
  IV became **absent** rather than wrong — no error, no row, no trace, which is
  the worse failure. `safe_float` now strips a trailing `%`; deciding whether the
  number means 31.42% or 0.3142 is a units question and belongs to the caller.
- **Units are declared, never guessed from magnitude.**
  `ibkr.cp_api.marketdata.implied_vol_units` is `auto | decimal | percent`; under
  `auto`, a `%` suffix means percent and everything else falls back to
  `implied_vol_units_fallback`. There is deliberately no magnitude heuristic: a
  threshold that reads 2.0 as "200%, therefore 0.02" turns a genuine 200%-IV name
  into a 2% one and nobody notices.
- **A decoded value beyond its plausible range is a unit error, not a quote.**
  `implied_vol_max_plausible` and `delta_max_plausible` drop it to NULL with a
  warning rather than storing it.
- **Strikes are strings** (already recorded above) and prices carry thousands
  separators; both cast before arithmetic. `C`/`H`/`L` prefixes denote a closing
  price and halted bounds.
- **Two different year bases are in play, and they are not interchangeable.**
  `numerics.bsm.day_count_basis` (365) measures *contractual* time to expiry for
  pricing. `data.trading_days_per_year` (252) annualises *realised diffusion* and
  is the 252 in the strike-bound formula `sqrt(T/252)`, where `T` is in **trading**
  days. Converting calendar DTE at 252/365 makes the two agree; feeding calendar
  days straight into `/252` inflates the variance by 365/252 and the band width by
  about 1.20x. `collect/strikes.py::diffusion_tau_years` does the conversion the
  long way so the cancellation is visible rather than assumed.
- **Realised vol is annualised**, as is vendor IV and `data.sigma_default`. A
  fallback ladder whose rungs were quoted in different units would silently
  rescale the strike band depending on which rung fired.

#### Endpoints written new for the collectors

- **`/trsrv/futures?symbols=CL,NG`** — the Client Portal equivalent of the MCP
  `search_futures` tool, used for the futures ladder. Rows carry `conid`,
  `symbol` and `expirationDate` as a bare `YYYYMMDD`. **Entirely unverified.**
  Futures need the native exchange; SMART returns empty.
- **`/iserver/marketdata/history` window semantics are ambiguous.** Whether
  `startTime` bounds the *start* or the *end* of the requested span is not clear
  from the documentation. The backfill is written to be correct either way: it
  judges progress by whether the earliest bar actually moved earlier, and stops
  when it does not. Settle the semantics in Session 5 and simplify.
- **The documented per-call daily bar cap
  (`ibkr.cp_api.history.max_bars_per_request`) is not trusted.** The backfill
  *probes* it — asks for `collectors.backfill.probe_request_days` of calendar span
  and derives the chunk size from the bar count that comes back — and remembers the
  result in `collector_state`. Record the real figure here once it is observed.

#### Chain retrieval, in practice

- **One `/iserver/secdef/info` call at the strike nearest spot returns every
  expiry in the month.** That is what makes per-expiry strike bounds affordable:
  the alternative is a call per strike before knowing which strikes are wanted.
  Unverified, and the single assumption the chain collector's call budget rests
  on — check it first in Session 5.
- **`/iserver/secdef/info` at one strike returns rows across all expiries in the
  month**, so strikes are resolved once per month rather than once per expiry.
- **PRIIPs restricts the cash instrument, not the option.** An EU retail account
  cannot buy SPY for want of a KID but may trade listed options on it. Chain
  collection therefore keys off `universe.collect_chain` and never off
  `priips_restricted`. This is a rule about the account, not about the API, but it
  is recorded here because it is the mistake most likely to silently halve the
  collected universe.

#### Macro sources

- **FRED's plain observations endpoint carries no publication date.** ALFRED's
  `output_type=4` ("initial release only") returns rows whose `realtime_start`
  *is* the first-publication date, which is the only route that makes
  `release_date` a fact. Aligning a CPI print to its reference month instead builds
  look-ahead bias into every percentile derived from it.
- **The ECB's SDMX carries no first-publication date at all**, beyond the daily
  reference rates that are public on the day they describe. Its
  `release_date_policy` is `unknown` and the column stays NULL — a visible coverage
  gap rather than an invented date.
- **CFTC (Tuesday positions, published Friday) and EIA (week ending Friday,
  published Wednesday) publish on a fixed schedule**, so `reference_plus_lag` is a
  fact rather than a guess. Confirm both lags live.

#### Snapshot fields for the UNDERLYING

- **`implied-volatility-percentile` is documented for the IBKR MCP connector, and
  no Client Portal Web API field id for it has been verified.** The computation
  layer wants it: it is the day-one source for grid indicator 8, and the
  self-computed alternative needs `data.iv_self_source_min_sessions` snapshots
  before it exists at all. `ibkr.cp_api.marketdata.underlying_snapshot_fields.`
  `implied_vol_percentile` is therefore declared **null**, the column stays NULL,
  and the grid reports which source is missing.

  **Session 5 must find the id.** Guessing one is worse than leaving it null:
  `7633` is *Implied Vol./Historical Vol %*, a ratio, and writing that into a
  percentile column produces a number in roughly the right range that means
  something else entirely. Setting the id in config is then a one-line change
  and the grid picks it up with no code edit.

- `7284` is read as *Historical Volatility (30d)* and `7283` as *Option Implied
  Volatility*. Both are UNVERIFIED and both are unit-normalised through
  `implied_vol_units`, so a percent-suffixed string and a bare decimal land in
  the same place. The percentile deliberately is **not** normalised that way: it
  is already a fraction, and dividing it by a hundred would turn a 90th
  percentile into a 0.9th.

- The underlying field set is requested **separately from the per-option one**.
  Asking for historical vol and a percentile on every strike of every expiry
  would spend market-data lines on fields that carry no meaning for an individual
  contract.

### From live verification (Session 5, 2026-08-23)

Observed against the **IBKR MCP connector** on a live EUR-base account. The CP
Web API was **not** reachable — no OAuth consumer key is registered — so nothing
in this section is a CP fact. Corrections to earlier lines are applied in place
above rather than only listed here.

Session date was a **Sunday**. The market was closed and `top-status` read
`FROZEN` on SPY, which bounds what the entitlement findings can mean.

#### The IV percentile field exists — on MCP

- **`implied_volatility_percentile` is a real, working `market_data_names` value.**
  It returns a three-window object, not a scalar:

  ```json
  "implied-volatility-percentile": {
      "high_13w": 0.0634920671582222,
      "high_26w": 0.031496062874794006,
      "high_52w": 0.11952191591262817
  }
  ```

  **Values are fractions** — `0.1195` is the 11.95th percentile, not 0.1195%.
  The existing warning against dividing this by a hundred stands and is now
  confirmed rather than inferred. `high_52w` is the one the grid's indicator 8
  wants.

  **The values are exact rational counts, and the denominators give the window
  lengths in sessions.** Recovering the fractions from the returned floats:

  | | SPY | SPX |
  |---|---|---|
  | `high_13w` | 4/63 | 5/63 |
  | `high_26w` | 4/127 | 5/127 |
  | `high_52w` | 30/251 | 32/251 |

  The denominators — **63, 127, 251** — are identical across both instruments, so
  they are the window lengths in trading sessions rather than anything
  instrument-specific. The numerator is the count of sessions whose IV was below
  the current reading. This is a plain rank, computed on a session count, not a
  smoothed or interpolated statistic.

  It also explains a shape that otherwise looks like a fault: **a 26-week
  percentile can be lower than the 13-week one.** For SPY the numerator is 4 in
  both, so the 13 extra weeks contributed no session below current IV at all,
  while the denominator doubled. The reading halved without anything being wrong.
  Do not "correct" for it, and do not treat the three windows as if the longer
  ones bound the shorter.

- **This does NOT settle the CP field id, and the config stays null.** The whole
  point of the earlier warning was that `7633` is *Implied Vol./Historical Vol %*
  — a ratio — and writing it into a percentile column produces a plausible number
  meaning something else. Nothing observed here maps the MCP field onto a CP
  numeric id. `ibkr.cp_api.marketdata.underlying_snapshot_fields.`
  `implied_vol_percentile` remains **null**, and the grid keeps reporting
  `iv_percentile_unavailable`. Finding the CP id is still open work.

#### Implied vol: prefer `implied_vol`, and check the validity flag

- **`option_midpoint_iv` returned a sentinel on a liquid contract.** On SPY
  DEC 18 '26 770 Call (9,334 open interest), in the same response:

  ```json
  "option-midpoint-iv": {"dailyIv": -1.0, "annualIv": -15.874507866387544, "isValid": false},
  "implied-vol":        {"daily_iv": 0.009207142516970634, "annual_iv": 0.14615885631260156, "is_valid": true}
  ```

  `-1.0` is the "no value here" sentinel and `annualIv` is simply that sentinel
  annualised. **Prefer `implied_vol`; never read either without its validity
  flag.** BUILD-BRIEF names `option_midpoint_iv` as the IV field, which on this
  evidence is the weaker of the two.

- **A ceiling does not catch this, and that was a live defect.** The plausibility
  guard tested `abs(value)`, so `-1.0` sat inside a max of 5.0 and was stored as
  an implied vol of −1.0. Fixed with a configured floor
  (`implied_vol_min_plausible`). Delta keeps a magnitude-only test on purpose: a
  put's delta is legitimately negative.

  The smile filters `iv > 0`, so N(d₂) and the zombie test were never exposed.
  The option **screen** was: a −1.0 gives an IV−HV spread near −1.1, the widest
  negative on the board, so the contract sorts straight to the top of the
  cheapest-volatility ranking. A quote IBKR declined to give becomes the strongest
  buy signal on the screen.

#### Units, confirmed

- **IBKR annualises volatility with √252**, exactly: `0.009207142516970634 ×
  √252 = 0.14615885631260156`, and the sentinel's `−1.0 × 15.8745 = −15.8745`.
  This independently corroborates `data.trading_days_per_year = 252` as the right
  basis for realised diffusion, and the strike-band formula that uses it.
- **`historical-vol` is a fraction despite the `_pct` suffix on its keys.**
  `{"daily_pct": 0.0072718, "annual_pct": 0.11543624630279695}` on SPY is 11.5%
  annualised, not 0.115%. Precisely the parses-cleanly-means-something-else
  hazard the field audit exists for.
- On this date SPY's vendor IV (0.1220 annualised) sat just above its 30-day
  historical vol (0.1154) — a sane IV−HV spread, which is a weak but real check
  that both fields are being read in the same units.

#### Market data entitlements

- **SPX index history carries `"delayed": 900`** — 15 minutes. SPY equity history
  carried no delay flag. So the account has no realtime CBOE index entitlement,
  and any index-level reading is a quarter-hour stale.
- **A closed market cannot be distinguished from a missing entitlement.**
  `top-status: FROZEN` on a Sunday means "no live session", not "not entitled".
  Anything that looks like an entitlement gap here has to be re-tested during
  market hours before a subscription is bought on the strength of it.

#### `search_futures` returns the ladder unsorted

- **[MCP ✓]** The HG copper ladder came back in neither expiry nor contract-month
  order: the front month (`202608`, last trading 2026-08-27) was the **17th** of
  48 rows, after `202609`, `202612`, `202703` and a `203003`. Anything taking the
  first row as the front month gets an arbitrary contract, and a roll yield
  computed off it is meaningless. Sort by expiry before use — `_futures_from_rows`
  already does.
- `search_futures` on a conid that is not a futures underlying fails with
  `"No data is available."` rather than an empty ladder. Resolve the root through
  `search_contracts` and take the row whose `sections` include `FUT`.

#### Contract search still needs the exact-ticker discipline

- **[MCP ✓]** `search_contracts("SPY")` did put the correct ARCA listing first,
  so the "routinely returns an ADR ahead of the instrument" wording is not what
  was seen on this sample. The discipline still holds for a different reason: the
  same result set contained `5SPY`/`SP5Y` (5× long), `SSPY`/`SPYS` (−5× short)
  and a spread of foreign listings. `search_contracts("HG")` returned the COMEX
  copper index first but Hamilton Insurance Group second — so a naive "first row
  with sections including STK" picks the wrong instrument outright.
  Match on exact `symbol` **and** the security type wanted, then disambiguate on
  `description`/`country_code`.

#### `get_account_trades` needs an explicit, wide period

**[MCP ✓ 2026-08-23]** It defaults to **`TODAY`**, and the window is a hard cut
rather than a soft one. Reconciling on 2026-08-23:

- `DAYS_90` returned **one** equity trade and looked like a clean, empty book.
- `YEAR_TO_DATE` returned **51** trades, including a **closed option round trip**
  from May that `DAYS_90` could not see.

The ledger reconciliation the sleeve depends on is only as good as this window.
A 90-day window on a book that trades a few times a year silently reports
"nothing to reconcile" while an unrecorded realised loss sits outside it —
and under the penalty rules an unrecorded option loss overstates the available
sleeve by twice its value. Use `YEAR_TO_DATE` at minimum, and the quarter-by-quarter
periods (`LAST_QUARTER` … `FOUR_QUARTERS_AGO`) to reach further back.

Also: **commission is included in `average_price`** on a position, but reported
separately on a trade. `(net_amount + commission) / size` reproduces the position's
average, and comparing a raw trade price against it will be off by the commission.

#### MCP and CP are not the same API

The shapes diverge enough that a decoder written for one silently returns nothing
on the other. This matters because the collectors are CP-only and everything
above is MCP.

| | MCP | CP (as coded, **[CP ?]**) |
|---|---|---|
| Bars | parallel columnar arrays — `time[]`, `open[]`, `close[]`… with ISO stamps | `data[]` of `{t,o,h,l,c,v}` with epoch ms |
| Expiries | composite id strings, e.g. `756733@SMART/OPT/SMART/20261218/SPY/1` | `MMMYY` months, e.g. `DEC26` |
| Futures rows | `contract_month` (`YYYYMM`), `last_trading_date` | `expirationDate` (`YYYYMMDD`) |

`_bars_from_body` reads `data[]` and `_futures_from_rows` reads `expirationDate`,
so both would return empty against MCP-shaped responses. That is correct for
their route and is recorded here so nobody "fixes" one into the other.

- **Duplicate expiry dates carry distinct `trading_class` values.** SPY listed
  both `SPY` and `2SPY` on 2026-09-04, -09-11 and -09-16. Keying an expiry on its
  date alone merges two different contracts.

#### The opening print, and one thing NOT observed

**Confirmed, on the Session 7 ASML capture:** the daily bars carry a genuine
open. All 40 rows have all four prices, every one is inside its own high and
low, and **not one of the 39 gaps opens at exactly the previous close**. The
overnight move is material — mean absolute log gap 0.01602 against 0.01937
intraday — so ASML on FTA publishes a real opening auction print and
Garman-Klass may read it.

**UNVERIFIED, and asserted from the design brief rather than from this route:**
that some non-US listings serve an open that is merely the previous close
restated. **No fixture or capture in this repository exhibits it**, on any
instrument, on either route — this was checked rather than assumed. The
Garman-Klass leaf refuses above `data.garman_klass_max_restated_open_share` and
is tested against a constructed series; if a live pull ever produces a listing
that restates, record the venue here, because that is the first observation of
it. Note also that a venue serving a real intraday range with a fake open
contradicts itself on any gap day — yesterday's close falls outside today's
high and low — so the first symptom may be bars dropped for a failed ordering
check rather than a restatement share.

### From the viability census (Session 6, 2026-08-24, MCP, market CLOSED)

Pulled at 01:45 UTC on a Monday: US regular hours had not opened and Europe was
shut. Every line below carries that context, because a closed market and a
missing entitlement look alike from a snapshot.

#### `option_volume` does not return an option's volume — `volume` does

The tool documents `option_volume` as "Option trading volume for today for
individual option contracts (OPT and FOP)" and `volume` as "Today's total trading
volume for the instrument". On an OPT contract it is the other way round:

```
market_data_names=["option_volume"]  ->  key absent from the response
market_data_names=["volume"]         ->  {"volume": {"volume": 33.0}}
```

Reproduced across every option contract sampled. **This one is load-bearing for
the census.** `liquidity.require_session_volume` is true, and an absent volume
leaves the gate UNTESTED — and an untested gate is not a passed gate, so reading
the documented field would have reported zero contracts clearing every gate on a
board where ten did.

#### An unknown parameter is accepted and ignored

`get_price_snapshot(fields=[...])` — the wrong name for `market_data_names` —
returns HTTP success with the default `last` + `bid-ask` and no error at all. A
mistyped parameter is therefore indistinguishable from "the venue does not serve
that field", which is the more plausible reading and the wrong one. Any
"unavailable" field is worth re-requesting with the parameter name checked
against the schema before it is written down as a coverage gap.

Contrast: an unknown value *inside* a documented parameter, and a missing
required one, both fail loudly — `search_contracts(symbol_or_name=...)` returns
`required property 'query' not found`. It is only extra top-level keys that
vanish silently.

#### The IV percentile is served on a frozen market, from an invalid IV

Three EUR-listed names read `top-status: FROZEN` with
`implied-vol-underlying: {daily_iv: 0.0, annual_iv: 0.0, is_valid: false}` — and
still returned a populated `implied-volatility-percentile`:

| | current IV | 13w | 26w | 52w |
|---|---|---|---|---|
| ASML (AEB) | invalid | **0.0** | **0.0** | 0.2863 |
| SAN (BM) | invalid | 0.0909 | 0.0469 | 0.1490 |
| TTE (SBF) | invalid | 0.1515 | 0.0859 | 0.5177 |

ASML's shorter windows reading exactly `0.0` is the tell: a percentile is the
rank of the CURRENT reading within its window, so an invalid current IV produces
a rank of zero rather than an omitted field. **Never read
`implied-volatility-percentile` without checking `implied-vol-underlying.is_valid`
on the same response.** Same discipline the Session 5 line about `implied_vol`
already demands, one level up.

The US names were all `REALTIME` on the underlying at the same moment, so this is
a venue-hours effect and not an entitlement one.

#### An underlying can read REALTIME while its own options read FROZEN_DELAYED

Every US option contract sampled returned `top-status: FROZEN_DELAYED` while its
underlying returned `REALTIME`. The two sides of the same name disagree outside
regular hours, so an entitlement conclusion drawn from the option side alone is
unsafe. Quotes taken in that state are the previous session's marks and are fine
for a census, which counts what clears a gate, and not for an order.

#### The `-1.0` implied-vol sentinel reproduces on EUREX

`SAN (BSDE) DEC 18 '26 13 Call` returned
`implied-vol: {daily_iv: -1.0, annual_iv: -1.0, is_valid: false}`. Session 5
observed this on a US contract and it drove the `implied_vol_min_plausible`
fix; seeing it on a second venue confirms it is an IBKR-wide sentinel rather
than a US-market quirk. The floor is doing real work.

#### EUREX single-stock options carry two trading classes per date

Santander lists `BSD2` and `BSDE` on every regular expiry. Same hazard the SPY /
`2SPY` line records, on a European venue: an expiry keyed on its date alone
merges two distinct contracts.

### The sector-ETF and native-index sweep (Session 9, 2026-08-29, MCP)

Run to answer one question before declaring anything: which markets can actually
be measured, and on what. Roughly fifty calls. The two findings below cost more
than the coverage they bought.

#### An explicit `exchange=TSEJ` DEFEATS history that the same call serves without it  [MCP ✓ 2026-08-29]

```
get_price_history(55275720, security_type="STK", exchange="TSEJ", step=ONE_DAY, 60)
    ->  {"error":"No market data permissions"}
get_price_history(55275720, security_type="STK",                  step=ONE_DAY, 60)
    ->  60 daily bars, "delayed": 1200
```

Reproduced on 1617 and on 1621. **This inverts the native-exchange rule**, which
holds for indices and futures and does not hold for TSEJ-listed shares. The old
rule is not wrong — it is narrower than it reads, and the two cases look
identical from the error message, which says "permissions" for what is really a
routing choice.

`universe.history_exchange` exists for this: `exchange` stays the listing venue
because that is what identifies a contract, and the history request sends
`history_exchange`, which is null on all eleven TOPIX-17 rows. Anything reading
`universe.exchange` to build a history call will silently collect nothing from
Japan.

Untested on the CP route. `/iserver/marketdata/history` takes the same parameter
and may or may not behave the same way; assume nothing.

#### `search_contracts` matches ONE token, and caps at ten  [MCP ✓ 2026-08-29]

`KODEX` returns ten rows. `SAMSUNG KODEX` returns nothing, as do `NF TPX-17` and
`KODEX SEMICON`, though all three are literal substrings of descriptions the
single-token search does return. Numeric tickers work (`1617`, `2828`), and
several markets reuse the same numeric symbol, so the result must be
disambiguated on `exchange` or `country_code` rather than on position.

Consequence for discovery: a series has to be enumerated one ticker at a time.
There is no way to ask for "every TOPIX-17 fund".

#### Six of thirteen declared native indices did not survive contact  [MCP ✓ 2026-08-29]

`universe.yaml` carried the warning that every exchange code and conid in it was
documentation-derived. It was right, and the failures are not all the same shape
— three symbols do not exist, two resolve and return nothing, one errors:

| Market | Was declared | What happened | Now measured on |
|---|---|---|---|
| eurozone | `SX5E` / EUREX | symbol does not resolve; the contract is `ESTX50` (4356500) | `ESTX50`, delayed 900 |
| uk | `UKX` / ICEEU | not a contract. The real FTSE 100 index is `Z` (1027221) and returned `Details currently unavailable` twice | `ISF` (13444223), LSEETF, GBP |
| china | `XIN9` / SGX | symbol is `XIN9I` (466719269), which returns an **empty series**, not an error | **nothing** — see below |
| israel | `TA35` / TASE | no index contract; the only `TA35` matches are leveraged ETPs | `KSM.F59` (366247788), TASE |
| brazil | `IBOV` / BVMF | no index contract; the `IBOV` matches are forecast and prediction-market instruments | `BOVA11` (766495610), B3 |
| australia | `AS51` / ASX | the contract is on **PSE**, not ASX, and returns an **empty series** | `STW` (14065804), ASX |
| south_africa | `TOP40` / JSE | nothing resolves. The SATRIX funds IBKR serves are offshore feeders and a bond portfolio | **nothing** — see below |

**An empty series is not an error and must not be read as one.** `XIN9I` and
`AS51` both return a well-formed response with every array empty. Code that
checks only for an `error` key treats that as a successful collection of zero
bars, which is how a market can look collected and be blank.

`china` and `south_africa` are left with **no measuring instrument on purpose.**
For south_africa nothing serves at all. For china the iShares FTSE China A50
(`2823`, SEHK, 42474926) does serve daily bars, but it is priced in **HKD** while
the market is declared **CNY**: measuring it there would report HKD returns under
China's name and hand indicator 11 an EURCNY series that does not describe them.
The loader now refuses that mismatch rather than leaving it to a reader to
notice.

#### What the sector ETFs served  [MCP ✓ 2026-08-29]

| Market | Series | Venue | Sectors | Quote state |
|---|---|---|---|---|
| japan | NEXT FUNDS TOPIX-17, 1617-1633 | TSEJ, **exchange omitted** | 11 of 11 | `delayed: 1200` |
| eurozone | iShares STOXX Europe 600, EXV/EXH/EXI | IBIS | 11 of 11 | no delay flag |
| korea | Samsung KODEX | KRX | 3 of 11 — banks, steel, construction | **no delay flag** |
| hong_kong | iShares Hang Seng TECH (3067) | SEHK | 1 of 11 | no delay flag |

KRX carrying no delay flag is worth noting against Session 7's "delay is per
exchange, not per account": it is consistent with that finding, and it means the
Korean rows are the only non-US, non-European cells whose bars are not marked
indicative.

Korea's missing eight are missing because the funds do not exist on this route,
not because the probe was cut short: KODEX covers banks, securities, insurance,
steel, construction and transport, and nothing for energy, healthcare,
technology, staples, utilities or real estate.

### From going live on the reachable route (Session 7, 2026-08-24, MCP, EUROPE OPEN)

Taken between 08:10 and 10:00 UTC on a Monday. **European markets were trading**
— ASML on AEB quoted two-sided with real sizes and `top-status: REALTIME` — so
these are the first readings in this file not bounded by a closed market. US
regular hours had not opened; anything US-specific below says so.

Session 7 could not test the CP route at all: IBKR have not approved the OAuth
application, so every `[CP ?]` in this file is still `[CP ?]`.

#### Futures curve history IS retrievable, contrary to the founding premise  [MCP ✓ 2026-08-24]

**This falsifies a claim BUILD-BRIEF and `curve_collector.py` both rest on.**
The stated premise is that "the API returns the curve as it is now, and
curve-shape history cannot be recovered later", which is why curve collection
was ranked as irreplaceable alongside option chains.

It is not. `get_price_history` on an individual futures contract works:

```
GC DEC-26 (462941472, COMEX, FUT), ONE_DAY, SIX_MONTHS
  -> 123 daily bars, 2026-02-26 .. 2026-08-21, "delayed": 600
```

Every rung of a ladder has its own retrievable daily history, and
`search_futures` takes `include_expired=true`. So the curve on any past date can
be reconstructed after the fact by pulling each then-listed contract's bars.

**Option chains remain genuinely irreplaceable** — option price history still
returns "No historical market data available", re-confirmed in Session 5 — so the
build order is unchanged in its essentials. But the two are no longer the same
kind of loss, and a day without a curve snapshot is now a recoverable
inconvenience rather than a permanent hole.

#### COMEX futures snapshots return nothing while their history serves  [MCP ✓ 2026-08-24]

```
get_price_snapshot(732156872, exchange="COMEX")                  ->  {}
get_price_snapshot(732156872, exchange="COMEX", fields=[...])    ->  {}
get_price_history (732156872, "FUT", COMEX, ONE_DAY)             ->  bars, delayed: 600
```

Empty on the snapshot, and not because the market was shut: COMEX gold trades
almost around the clock and this was 08:45 UTC. So the account has no realtime
futures entitlement, while delayed history is served.

`curve_collector` prices its rungs from `quotes()` — the snapshot — so it writes
a complete ladder with a NULL price on every rung and no roll yield. It now
reports **degraded** rather than `ok` for that, and the fix, if the curve is
wanted before a futures subscription is bought, is to price the rungs from each
contract's last daily bar instead.

#### Delay is per exchange, not per account  [MCP ✓ 2026-08-24]

| Instrument | Type | Exchange | Snapshot quote | `top-status` | Derived vol | History |
|---|---|---|---|---|---|---|
| SPY | STK | SMART | bid/ask/last | `REALTIME` | hist-vol, IV percentile | — |
| ASML | STK | AEB | bid/ask/last | `REALTIME` | hist-vol, IV percentile | `delayed: 900` |
| DAX | IND | EUREX | **absent** | absent | hist-vol, IV percentile, IV-underlying | `delayed: 900` |
| SPX | IND | CBOE | absent (S5) | — | IV percentile (S5) | `delayed: 900` (S5) |
| ASML options | OPT | FTA | **absent** | absent | implied vol, open interest | n/a |
| GC | FUT | COMEX | **empty `{}`** | — | — | `delayed: 600` |

Three things worth separating, because they look alike and are not:

1. **`top-status: REALTIME` is about entitlement, not about the session.** SPY
   read `REALTIME` at 09:48 UTC, hours before the US open. Session 5 saw `FROZEN`
   on a Sunday. So the field says "you are entitled and there is a book", not
   "regular hours".
2. **A snapshot can serve derived volatility with no quote at all.** DAX returned
   historical vol, an ATM implied vol and the IV percentile, and no price. This is
   the same shape Session 5 found on SPX, and it is the shape the grid wants.
3. **The delay differs by exchange**: 900 seconds on CBOE indices, AEB equities
   and EUREX indices; 600 on COMEX futures. It is not one account-wide figure.

#### The IV percentile is served for European indices too  [MCP ✓ 2026-08-24]

DAX returned `{high_13w: 0.2923…, high_26w: 0.1496…, high_52w: 0.3055…}`. This is
grid indicator 8's data, available today through `record_iv_percentile`, for a
market the CP route cannot serve it for.

Session 5 found SPY's values were exact rational counts (4/63, 4/127, 30/251) and
concluded the denominators are the window lengths in trading sessions. Confirmed
on a second instrument — DAX resolves to **19/65, 19/127 and 77/252** — with one
correction: **the denominator is not a constant.** SPY's own values on 2026-08-24
resolve to denominators of 62 and 126 where the day before they were 63 and 127.
It is a rolling count of that instrument's own sessions, so two instruments, and
the same instrument on two days, do not share a denominator.

**One anomaly, recorded and not explained.** ASML's 52-week value
(0.29921260476…) resolves exactly to 38/127 — the same denominator as its
26-week reading, not a year of sessions. Another reason to keep treating the
vendor percentile as a cross-check rather than as the measure.

#### IBKR's `historical-vol` is not reproducible from IBKR's own bars  [MCP ✓ 2026-08-24]

ASML, from the daily closes `get_price_history` serves for the same contract:

```
IBKR historical-vol.annual_pct        0.5399   (daily_pct 0.0340083 x sqrt(252))
close-to-close over 30 returns        0.4623
```

16.8% apart. Nothing reproduces IBKR's figure: not close-to-close (sample,
population or zero-mean), not Parkinson, not Garman-Klass, over any window from
5 to 39 days, with or without the partial in-progress bar. The closest fit is 6.3%
off.

**Consequence, and it is not academic.** Against an ATM implied of 0.4604 the
IV−HV spread is −0.0794 on the vendor figure and −0.0019 on the self-computed
one: a factor of 42, and the difference between "cheap volatility, rank it top"
and "fairly priced, ignore it". The grid already measured this self-computed;
the dislocation screen did not, and now does.

Treat `underlying_snapshots.historical_vol_30d` as the cross-check its own
comment calls it, and never as interchangeable with realised vol.

#### The `-1.0` implied-vol sentinel is not a liquidity artefact  [MCP ✓ 2026-08-24]

Three of eleven ASML strikes sampled on one expiry returned
`is_valid: false` with `annual_iv: -1.0`:

| Strike | Open interest | Trades today |
|---|---|---|
| 1000 | 984 | 3 |
| 1400 | 654 | 0 |
| 1900 | 717 | 1 |

A ~27% sentinel rate on a liquid single-stock name, on contracts that traded
that session. Session 6 saw it on EUREX and Session 5 on a US contract; this
confirms it is neither venue-specific nor a thin-contract signal. **Never read
`annual_iv` without `is_valid`** — and note that `implied_vol_max_plausible`
alone would not catch it, because −1.0 sits well inside any plausible magnitude.

#### Implied vol is per STRIKE, shared across the right  [MCP ✓ 2026-08-24]

The ASML DEC-26 1500 call and 1500 put returned byte-identical implied vol
(0.4604277959834679), while the 1600 call differed (0.4584553654063636). That is
put-call parity holding, not a stuck field. Recorded because an identical pair
reads like a bug, and a capture only needs ONE snapshot per strike to record the
smile — though open interest still differs by side.

The sampled smile, spot 1510.6, 116 DTE: 0.4831 at 1100, falling to 0.4578 at
1700, rising to 0.4624 at 2000. Well formed.

#### Open interest arrives as a PAIR on a single contract  [MCP ✓ 2026-08-24]

```
1500 Call  ->  option-open-interest: {callInterest: 213, putInterest: 0}
1500 Put   ->  option-open-interest: {callInterest: 0,   putInterest: 453}
```

The side that does not belong to the contract reads **0, not absent**. Reading
the wrong slot, or summing them, yields a plausible "no open interest" for a
contract that has plenty.

#### `get_price_history` is COLUMNAR on MCP and row-per-bar on CP  [MCP ✓ 2026-08-24]

MCP returns parallel arrays — `time[]`, `open[]`, `high[]`, `low[]`, `close[]`,
`volume[]` — with `time` as ISO-8601 strings. CP returns `data: [{t, o, h, l, c,
v}]` with `t` in epoch milliseconds. Handing an MCP body to a CP decoder yields
**zero bars and no error**, which is the worst available failure: a silent empty
series. `collect/mcp_capture.py::bars_from_history` translates.

Two smaller notes on the same response. An index carries no `volume` array at
all. And `chart_end` overshoots: a request made on 2026-08-24 reported
`chart_end: 2026-08-25T09:32:14Z`, a day in the future, while the last bar was
2026-08-24 and partial.

#### Chain retrieval inverts between the routes  [MCP ✓ 2026-08-24]

CP is per-**month**: one `/iserver/secdef/strikes` for the ladder, then one
`/iserver/secdef/info` per strike. MCP is per-**expiration**: `get_option_data`
takes an `expiration_id` and returns a row per strike carrying **both** the call
and the put conid, bounded by `min_strike`/`max_strike`.

One MCP call replaces a ladder call plus N strike calls. On the ASML DEC-26
expiry that is 1 call against 40. `expiration_id` must be passed verbatim from
`get_option_parameters`; it contains `@` and `/` and cannot be constructed.

#### A date can list FOUR trading classes, not two  [MCP ✓ 2026-08-24]

ASML lists `AS9`, `AS9C`, `ASL` and `ASLM` on 2026-12-18. Session 6 recorded two
on EUREX (Santander's `BSD2`/`BSDE`) and SPY's `SPY`/`2SPY` before that; four is
the most seen so far. A capture must name the class it took, and an expiry keyed
on its date alone merges distinct contracts.

#### `search_contracts`'s `security_type` filter is ignored  [MCP ✓ 2026-08-24]

`search_contracts(query="GC", security_type="FUT")` returned bonds, funds and
equities alongside the futures root; `security_type="IND"` on "DAX" likewise
returned stocks and bonds. The parameter is accepted and has no visible effect,
which is the same failure mode Session 6 recorded for an unknown parameter.

**This makes the exact-ticker discipline load-bearing rather than a nicety.**
Searching a short ticker returns dozens of unrelated instruments and the filter
will not narrow them.

#### `search_futures`'s `representative_only` had no effect, and `symbol` is per-expiry  [MCP ✓ 2026-08-24]

`representative_only=false` returned the identical 34-contract GC ladder as the
default. Consistent with the tool's own note that single-contract-per-expiry
futures return the same ladder either way.

Separately, **the tool documentation is wrong about `symbol`**: it states the
field "is identical for every expiry rather than a per-contract code" and "must
never be displayed". The ladder returned `GCQ6`, `GCU6`, `GCV6`, `GCX6`, `GCZ6` —
distinct per expiry. The advice to identify a contract by `contract_month` /
`last_trading_date` is still the right advice; the stated reason is not the real
one. `mcp_capture.futures_rows` drops `symbol` anyway, on the grounds that a
field the vendor documents as unusable should not be relied on.

Also re-confirmed: **the ladder is unsorted.** The GC front month (`GCQ6`,
202608) was the 9th of 34 rows.

#### An expiry window can hold ONE expiry, not the two to four assumed  [MCP ✓ 2026-08-24]

`collectors.chain` comments say the 90-200 DTE window "holds 2-4" expiries. On
ASML on 2026-08-24 it holds exactly one: 2026-11-20 is 88 DTE (below the floor)
and 2027-03-19 is 207 (above the ceiling), leaving only 2026-12-18 at 116.

European monthly-only listings are sparser than US weeklies, so this is a
property of the venue, not an error. Recorded so the count is not read as a
collection failure — and the window was **not** retuned to make the number look
better.

### The second sector sweep (Session 11, 2026-09-03, MCP)

Session 9 asked "does an index contract resolve" and stopped where none did.
This one asked "what sector-fund family does this market's exchange list", market
by market. Eighteen cells that had been recorded as structural gaps were not.
Roughly forty calls.

#### `search_contracts` matches SYMBOLS, not descriptions — mostly  [MCP ✓ 2026-09-03]

```
search_contracts("KODEX Semiconductor")   ->  []
search_contracts("091170")                ->  SAMSUNG KODEX BANKS ETF, KRX
search_contracts("SATRIX")                ->  6 rows, all matched on description
search_contracts("TACHLIT")               ->  18 of 119 rows, matched on description
```

So a descriptive query answers for some issuers and returns empty for others, and
an empty result from a name search is **not** evidence the fund is not listed.
Search by the local code where the market has one. Session 9's Korean coverage
finding survives that stricter test: `091160`, `091180`, `117460`, `266420` and
`139260` all return nothing by code, so IBKR's KRX ETF coverage really is banks,
steel, construction and transport.

A prefix search is truncated silently: `"KSM.F"` reported `"count": 210` and
returned fifteen rows. Never treat the returned rows as the whole family.

#### Stock Connect northbound reaches Shanghai listings  [MCP ✓ 2026-09-03]

```
search_contracts("510300")
    ->  {"underlying_contract_id": 571433003, "exchange": "SEHKNTL",
         "description": "HUATAI-PB CSI 300 ETF", "country_code": "HK"}
get_price_history(571433003, security_type="STK", exchange="SEHKNTL", ONE_DAY, 5)
    ->  5 daily bars around 4.62, volume ~5-8e8, "delayed": 900
```

`SEHKNTL` is Shanghai–Hong Kong Stock Connect northbound. `country_code` reads
**HK** for a Shanghai-listed fund — the field is the access route, not the
listing venue, and reading it as the domicile would put this row in Hong Kong.

**NOT VERIFIED, and it matters: the contract currency.** Northbound settles in
offshore yuan while the quote is the onshore Shanghai price. Neither
`search_contracts` nor `get_price_snapshot` returns a currency field on any
route tried, so whether IBKR stamps CNY or CNH on this contract could not be
established. `universe.yaml` declares `CNY` — the currency of the price, which is
what the ECB `EURCNY` onshore reference rate describes — and marks it
`# UNVERIFIED`. **Settle it with `/iserver/secdef/info` on the CP route**, which
returns `currency` directly; if it says CNH, change the one field and record the
basis.

#### A fund's name is not its index  [MCP ✓ 2026-09-03]

`KSM.F144 "KSM ETF TEL-COMP"` reads as a technology fund. TEL-COMP is Tel
Bond-Composite, a corporate BOND index. The bars are what gave it away:

```
open 43.125  high 43.172  low 43.125  close 43.175
```

A close ABOVE the day's high, and a range of a few hundredths on a five-session
window. Two rules: pull bars before declaring any row, and treat a degenerate
range as a signal about what the instrument is rather than as a data glitch. A
realised vol off that series would have been near zero and would have ranked
first on the compression screen.

#### Conids worth pinning, because the symbol is ambiguous  [MCP ✓ 2026-09-03]

| symbol | conid | exchange | collides with |
|---|---|---|---|
| `SLF` | 45208985 | ASX | Sun Life Financial on TSE and NYSE |
| `ATEC` | 407597432 | ASX | Alphatec Holdings on NASDAQ |
| `XLF` | 4215220 | ARCA | a Mexican cross-listing, and `XLFS`/`XLF3` in London |
| `3191` | 438186903 | SEHK | Joyful Honda on TSEJ, YJN on TPEX |

`/iserver/secdef/search` ranks by its own relevance across every venue at once,
so the escape hatch in `universe.conid` is the rule for these, not the exception.
Every row this session added carries one.

#### The nine US Select Sector SPDRs all resolve on ARCA with OPT  [MCP ✓ 2026-09-03]

`XLB` 4215200, `XLI` 4215227, `XLY` 4215215, `XLP` 4215210, `XLV` 4215205,
`XLF` 4215220, `XLC` 322317077, `XLU` 4215235, `XLRE` 209048377. All served
daily bars, `"delayed": 900`. Every one carries an `OPT` section, which is what
takes the option-derived grid families from two reachable cells to eleven.

Watch the `.IV` twins on `PSE` — `XLB.IV`, `XLF.IV` and the rest are *underlying
trading value* INDEX contracts, not the funds. They rank high in the search and
would resolve to something that looks right and is not.

---

### The Custom Connector route (Session 10, 2026-09-03)

Run from a **headless cloud container** — no browser, no human at a terminal, no
interactive session anywhere — to settle whether IBKR's Custom Connector is a
route a scheduled collector could use. Everything below is either executed here
or explicitly labelled as read rather than run.

#### The endpoint is an OAuth 2.0 protected resource, not OAuth 1.0a  [CONNECTOR ✓ 2026-09-03]

Unauthenticated, `https://api.ibkr.com/v1/api/mcp-public` answers `401` with:

```
www-authenticate: Bearer resource_metadata="https://api.ibkr.com/v1/api/mcp-public/.well-known/oauth-protected-resource"
```

That is RFC 9728. So the two routes this repository can reach share no credential
machinery at all: CP is OAuth 1.0a with an RSA-signed Diffie-Hellman handshake and
a live session token; the connector is a plain OAuth 2.0 bearer token.

**IBKR does not serve the RFC-canonical metadata path.** RFC 9728 §3.1 inserts the
well-known segment between host and path; that form returns `404` here. IBKR
serves the **suffix** form the header names. Follow the header — a client that
constructs the URL itself concludes "not an OAuth 2.0 resource" about an endpoint
that plainly is. (Its RFC 8414 sibling for the authorization server is served at
**both** forms.)

#### What the authorization server advertises  [CONNECTOR ✓ 2026-09-03]

`GET https://api.ibkr.com/oauth2/.well-known/oauth-authorization-server`:

| | |
|---|---|
| scopes | `openid profile email account-ids mcp.read mcp.write` |
| grants | `authorization_code`, **`client_credentials`**, `refresh_token` |
| client auth | `none`, `client_secret_basic`, `client_secret_post`, `private_key_jwt` |
| PKCE | `S256` |
| token endpoint | `https://api.ibkr.com/oauth2/api/v1/token` |
| registration | `https://api.ibkr.com/oauth2/register` |

`obliskiq-collect connector` re-runs this walk on demand and needs no credentials.

#### `client_credentials` is wired, not boilerplate  [CONNECTOR ✓ 2026-09-03]

A grant listed in metadata is not necessarily a grant the server implements. At
the token endpoint the two cases are distinguishable:

```
grant_type=client_credentials&client_id=<placeholder>
    -> {"error":"server_error", ...}                       routed, failed resolving the client
grant_type=not_a_real_grant&client_id=<placeholder>
    -> {"error":"invalid_request",
        "detail":"Invalid GrantType value: not_a_real_grant"}   rejected by name
```

The grant name is recognised. Only the client is missing.

#### The connector answers a headless container  [CONNECTOR ✓ 2026-09-03]

`whats_new`, `get_price_history` and `get_price_snapshot` all returned data from a
container with no browser and nobody present. **A bearer token minted earlier
works with no interactive session at call time.** This is the line that falsifies
"the MCP connector lives inside a chat session".

It does **not** show that this repository can obtain such a token. The token here
is held by the MCP client, not by this build.

#### NOT established, and deliberately not tested  [DOC / UNTESTED]

- **Whether this account can mint a token for a VM.** Two routes exist on paper:
  dynamic registration plus `authorization_code` + PKCE, which needs a human at a
  browser **once** and then a `refresh_token`; or `client_credentials` with a
  registered client. **[UNTESTED]**
- **Nothing was POSTed to `/oauth2/register`.** A `GET` returns `405`, so it is
  POST-only, and registering creates state on a live brokerage account — a
  decision for the account holder, not for a spike. **[UNTESTED]**
- IBKR's own material says authentication happens "on IBKR's own secure login
  screen" and that the Web API's OAuth 2.0 supports "only `private_key_jwt`
  client authentication". Both **[DOC]**, neither executed.
- **Token lifetime and refresh behaviour: unmeasured.** **[UNTESTED]**

#### Bars: the whole window arrives in ONE call  [CONNECTOR ✓ 2026-09-03]

```
get_price_history(756733 SPY, STK, step=ONE_DAY, period=TWO_YEARS)
    -> 2024-09-04 .. 2026-09-02, complete, one response
```

That is the entire `data.daily_window_years` window unchunked. Weekly at five
years in one call was already recorded on MCP. So on this route the probed
per-call cap and the backward-walking chunk loop **do not engage at all**, and
`ibkr.cp_api.history.max_bars_per_request` remains a CP question.

Note the `period` form is what was verified. `step_count` is still approximate on
this route as recorded above, so prefer `period`.

#### A 0.0 IV percentile is legitimate, and looks exactly like the broken one  [CONNECTOR ✓ 2026-09-03]

**This corrects the inference in the Session 6 entry below, though not its
advice.** Session 6 read ASML's `0.0` shorter windows as the *tell* of an invalid
current IV. It is not a tell, because a genuine zero looks identical:

| | ASML, 2026-09-03, AEB, trading | SPY, 2026-09-03 |
|---|---|---|
| `top-status` | REALTIME | REALTIME |
| `implied-vol-underlying` | `annual_iv 0.3779`, **`is_valid: true`** | `annual_iv 0.1186`, `is_valid: true` |
| `high_13w` | **0.0** | 0.078125 = 5/64 |
| `high_26w` | **0.0** | 0.03937… = 5/127 |
| `high_52w` | 0.20784… = 53/255 | 0.075396… = 19/252 |

ASML's zeros are real: its current IV is the lowest reading of both windows. That
is the most compressed a name can be, and it is exactly what the grid hunts.

So there are two ways to get `0.0` — a true zeroth percentile and the rank of an
invalid IV — and **`implied-vol-underlying.is_valid` is the only thing that
separates them.** Consequences, both load-bearing:

1. **Never read the percentile without the validity flag**, and always request
   `implied_vol_underlying` in the same call. Session 6's advice stands; only its
   reasoning needed correcting.
2. **Never filter on the magnitude.** "Drop zeros to be safe" discards the
   strongest signal on the board. `collect/mcp_capture.py` gates on the flag and
   refuses a percentile that arrives without one — absence is not a passed gate.

Denominators continue to be rolling per-instrument session counts and continue to
move: 63/127/251 and 62/126 on earlier dates, 64/127/252 for SPY and 255 for
ASML's 52-week window here. Two instruments do not share one, and one instrument
does not keep its own from day to day.

#### The futures ladder: shape settled, order emphatically not  [CONNECTOR ✓ 2026-09-09]

`search_futures(underlying_contract_id=17340715)` — CL, Light Sweet Crude, NYMEX
— returned an object with a `contracts` array, each element:

```json
{"contract_id_ex":"296574787@NYMEX","contract_id":296574787,
 "exchange":"NYMEX","symbol":"CLZ6",
 "last_trading_date":"20261120","contract_month":"202612"}
```

Three things a curve collector has to get right, all observed:

1. **The ladder is NOT in expiry order, and not close to it.** The first five
   rungs came back `202612, 202701, 202702, 202610, 202611` — the front two
   months arrive fourth and fifth. Sorting by `contract_month` is mandatory, and
   "take the first element as the front month" is wrong on the actual response.
2. **It is long.** 132 rungs for CL, out to `203612` — eleven years. A collector
   that snapshots "the ladder" without bounding it prices a decade of contracts
   nobody trades. `contract_month` is the only field to bound on.
3. **`last_trading_date` and `contract_month` disagree by design.** `CLZ6` is the
   December 2026 contract and stops trading `20261120`. The expiry a rung should
   be filed under is `contract_month`; `last_trading_date` is when it rolls.

> **The connector's own tool description is wrong about `symbol`.** It states
> "The `symbol` field is identical for every expiry rather than a per-contract
> code; it cannot be used to distinguish contracts". On this response every rung
> carries its own code — `CLZ6`, `CLF7`, `CLG7` — and they are unique. Recorded
> because the description is the kind of thing a future session would take as
> settled fact; the response is the authority, and it disagrees.

#### `_pct` is not a unit, and two fields prove it  [CONNECTOR ✓ 2026-09-09]

**This is the snapshot-field-units question, and the answer is that the suffix
cannot be trusted.** One `get_price_snapshot` on SPY (756733), `top-status:
REALTIME`:

| requested | response key | value | actual unit |
|---|---|---|---|
| `historical_vol` | `historical-vol` | `daily_pct 0.006024`, `annual_pct 0.09562…` | **FRACTION** — 9.56% annualised |
| `implied_vol_underlying` | `implied-vol-underlying` | `annual_iv 0.12523…`, `is_valid true` | **FRACTION** — 12.5% |
| `implied_volatility_percentile` | `implied-volatility-percentile` | `high_13w 0.31746…` | **FRACTION** — 31.7th percentile |
| `dividend_yield` | `dividend-yield` | `yield_pct 0.98` | **PERCENT** — 0.98%, not 98% |

`historical-vol.annual_pct` and `dividend-yield.yield_pct` carry the same `_pct`
suffix and are a hundred-fold apart in convention. Read either by its name and
the number is wrong by 100 in one direction or the other, silently, in a column
nothing downstream would question. Both were sanity-checked against the
instrument: SPY at ~9.6% realised and ~1% dividend yield are the plausible
readings; 956% and 98% are not.

Also confirmed: **request names are underscored, response keys are hyphenated**
(`implied_vol_underlying` -> `implied-vol-underlying`). A lookup by the requested
name finds nothing and looks exactly like an unavailable field.

#### The connector serves `dividend_yield`, which nothing else here does  [CONNECTOR ✓ 2026-09-09]

`dividend-yield` came back populated for SPY on the same call. Every
`universe.dividend_yield` in `universe.yaml` is `null` and marked UNVERIFIED
because no source reachable from a sandbox served one — marketdata.app does not,
and the CP route has never run. **This route does.**

It is not filled here. The gap is a per-instrument collection pass and a decision
about which route owns the column, and both belong to a phase of their own rather
than to a probe. What this entry changes is the reason the values are empty: it
is no longer "no source exists in a sandbox". `ROADMAP.md` carries it.

#### Bars are COLUMNAR, ISO-stamped, and dated at the session OPEN  [CONNECTOR ✓ 2026-09-09]

The 2026-09-03 entry above establishes that the whole window arrives in one call.
What it did not record is the response's shape, which differs from **both** other
routes in this repository:

```
get_price_history(756733 SPY, STK, step=ONE_DAY, period=ONE_WEEK, outside_rth=false)
{"chart_step":86400,
 "chart_start":"2026-09-03T00:00:00Z","chart_end":"2026-09-09T00:00:00Z",
 "expires":"2026-09-09T14:22:07Z","source":"Last",
 "time":["2026-09-03T13:30:00Z","2026-09-04T13:30:00Z","2026-09-08T13:30:00Z"],
 "open":[767.86,772.01,769.06], "close":[773.17,770.19,765.96],
 "high":[774.03,772.87,769.7], "low":[767.45,769,765.14],
 "volume":[28771837,21636580,26794949]}
```

* **Parallel arrays**, like marketdata.app's chain and unlike the Client Portal's
  `data[]` of `{t,o,h,l,c,v}` row objects. The same short-column hazard applies:
  zip only as far as the shortest column.
* **`time[]` holds ISO-8601 UTC strings, not epoch milliseconds.** The CP route's
  `t` is epoch ms. A decoder moved between the two without noticing lands in
  1970 or throws, depending on which direction it was moved.
* **A daily bar is stamped 13:30Z — 09:30 New York, the session OPEN**, not the
  close. Storing it as a close-stamped bar shifts every reading by a session's
  length, and on a market whose open crosses midnight UTC it shifts the date.
* **`chart_start`/`chart_end` bound the window explicitly** and `chart_step` is
  the bar size in seconds, so the window the server actually served is stated
  rather than inferred from the first and last bar.
* **The window is calendar time; only completed sessions come back.**
  `ONE_WEEK` spanning 2026-09-03..09 returned **three** bars. 2026-09-07 was
  Labor Day and 2026-09-09 was the day of the call, still open. So a bar count
  can never be used to detect a gap — a holiday and a missing day look the same.

## The dividend yield, and what a field name does not tell you

Measured while wiring the `cp_api` dividend-yield column. The snapshot that
produced these came from the MCP connector; **none of it verifies CP**, which is a
different transport with a different field-id scheme and no id resolved yet.

* **`dividend-yield` is served as a PERCENT, and `historical-vol` in the SAME
  response is a FRACTION.** [MCP ✓ 2026-09-09] One call, SPY (conid 756733):

  ```
  dividend-yield          {yield_pct: 0.98}                 -> 0.98 PERCENT
  historical-vol          {daily_pct: 0.006024,
                           annual_pct: 0.09562803538711856} -> a FRACTION (9.56%)
  implied-vol-underlying  {annual_iv: 0.13250859904114967}  -> a FRACTION
  ```

  The `_pct` suffix therefore states **nothing** about the unit — the same suffix
  carries both conventions inside one payload. This is why
  `ibkr.cp_api.marketdata.dividend_yield_units` exists rather than a name-based
  rule, and why its fallback is `percent` while implied vol's is `decimal`:
  reading a bare `0.98` on this field as a decimal prices a 1% payer as a 98% one.

* **The precision is TWO decimal places, and the tool description is wrong.**
  [MCP ✓ 2026-09-09] `get_price_snapshot`'s own text for `dividend_yield` says
  "Rounded to 1 decimal place". Two samples on the same day disprove it:

  | conid | symbol | `yield_pct` |
  |---|---|---|
  | 756733 | SPY | **0.98** |
  | 4215210 | XLP | **2.62** |

  No tolerance may be sized from the 1-dp claim, and nothing downstream may round
  to one place on the strength of it.

* **`implied-volatility-percentile` returns three windows, all fractions.**
  [MCP ✓ 2026-09-09] SPY: `{high_13w: 0.5079, high_26w: 0.2937, high_52w: 0.3705}`.
  Recorded because `underlying_snapshots` holds a 13w, a 26w and a 52w column and
  the mapping between them and these keys had never been observed.

* **`search_contracts` was returning `-32400` while `get_price_snapshot` worked.**
  [MCP ✓ 2026-09-09] Three consecutive calls with different queries ("SPY",
  "SPDR S&P 500 ETF Trust") each answered `{"code":-32400,"message":"An error
  occurred. Please try again later."}`, while snapshots against a conid passed
  directly succeeded on the same connection. A conid already held in
  `universe.yaml` is therefore usable when symbol resolution is not, which is
  worth knowing before concluding the connector is down.

**Still open on CP, and unchanged by any of the above:** no field id for the
dividend yield has been verified on the Client Portal Web API. `config.yaml`
declares `underlying_snapshot_fields.dividend_yield: null`, so the field is
dropped from the request and the column stays NULL rather than holding a guess —
the same treatment `implied_vol_percentile` and `market_data_status` get. Setting
it is a one-line change once a live CP snapshot resolves it.

### The Client Portal route, live for the first time (Phase 4, 2026-09-10, CP)

**Every line in this section is `[CP ✓ 2026-09-10]` unless it says otherwise.**
This is the first session in the project's history with a working OAuth 1.0a
credential, so it is the first evidence about `cp_api` that is not
documentation-derived. Where it corrects an earlier `[CP ?]` line, the correction
is applied in place above as well.

#### The handshake works, end to end

`obliskiq-collect preflight` passed all six steps on the first attempt:

| step | result |
|---|---|
| credentials | consumer key and three keys loaded; **DH prime 2048 bits** |
| decrypt-access-token-secret | **32 bytes** of prepend recovered |
| diffie-hellman-exchange | challenge **511 hex chars**, response received |
| live-session-token-signature | IBKR's signature over the derived token matches |
| brokerage-session | authenticated |
| tickle | session stays open |

Two things settle here that the code had flagged as open:

* **IBKR accepts an ODD-LENGTH hex challenge.** `preflight.py` logs a warning
  because `format(x, "x")` drops the top nibble and it was unverified whether
  IBKR would reject the result. The warning fired — 511 characters — and the
  exchange succeeded anyway. The warning is now known to be harmless.
* **`OBLISK_IBKR_CP_BASE_URL` is `https://api.ibkr.com`, with no path.**
  `preflight` appends `/v1/api/oauth/live_session_token` and `CpApiClient`
  appends `ibkr.cp_api.base_path`, so a value carrying `/v1/api` produces
  `/v1/api/v1/api/...`. `deploy/collect.env.example` has always been right; an
  environment pointing at `https://localhost:5000/v1/api` — the downloadable
  gateway, which `CLAUDE.md` puts out of scope — is wrong twice over and fails
  before the first request.

**`OBLISK_IBKR_REALM` and `OBLISK_IBKR_ACCOUNT_ID` are read by nothing.** The
realm comes from `ibkr.cp_api.oauth.realm` in config and the account id is
discovered at runtime through `/portfolio/accounts`. Setting them is harmless and
does nothing; only seven of the nine documented variables have a consumer.

#### `/iserver/secdef/search` takes a JSON BODY, and lies quietly if it does not

**The single most expensive finding of the session.** Sent as query parameters the
endpoint answers `HTTP 200`, matches the symbol, and returns every row with
`sections: []`. Nothing errors. `ContractMatch.option_months()` then returns `()`
and the chain collector reports *"reports no option months"* — for all 24 targets,
in a way that reads exactly like a market-data entitlement problem.

Measured on SPY:

| request shape | matches | sections on the first match |
|---|---|---|
| query parameters | **55** | **0** |
| JSON body | **3** | **6**, including the `OPT` months and exchange list |

The JSON body also returns *better* matches: 3 relevant rows against 55 ranked by
the API's own relevance.

#### Both section lists are SEMICOLON-separated, and one was split on a comma

The `OPT` section for SPY carries
`"months": "SEP26;OCT26;NOV26;..."` and
`"exchange": "SMART;AMEX;BATS;BOX;CBOE;..."` — **semicolons in both**.
`option_months()` already split on `;`. `option_exchanges()` split on `,`, so it
returned a single element containing the entire list, which travelled to
`/iserver/secdef/strikes` as `exchange=SMART;AMEX;BATS;...` and came back
**HTTP 410**. The fixtures that pinned this used commas for both fields, which is
why no test caught it. Both are now split on either separator.

#### `/iserver/marketdata/*` needs `/accounts`, and needs it more than once

`/iserver/marketdata/snapshot` answers
`HTTP 500 {"error":"Please query /accounts first"}` until `/iserver/accounts` has
been called on the brokerage session. That much is documented. Two things are not:

* **`/portfolio/accounts` does NOT satisfy it.** They are different endpoints;
  only the `/iserver` one arms the `/iserver` market-data routes. This build only
  ever called `/portfolio/accounts`, via `primary_account_id()`, which is why
  `universe` succeeded and `chain` did not.
* **The precondition is lost mid-run, and re-calling `/accounts` does not restore
  it.** In one run the session armed correctly at connect (`/iserver/accounts`
  returning one account) and every subsequent snapshot still refused. Calling
  `/iserver/accounts` and `/portfolio/accounts` again cleared nothing across
  **nine** attempts, while a fresh process served the same snapshot immediately.
  **Re-running `/iserver/auth/ssodh/init` is what restores it**, and doing that on
  the refusal took the chain collector from 24 failures to zero.

`/iserver/accounts` itself is unreliable as a *read*: it returned `{}` on one call
and `{"accounts": [...]}` minutes later on the same credential. It is a
precondition to call, not a portfolio source to trust.

#### Snapshot fields, measured

| id | config name | SPY | XLP | ASML | reading |
|---|---|---|---|---|---|
| `31` | last | `"763.38"` | `"C83.05"` | `"1718.05"` | the `C` prefix means previous close; `safe_float` already strips it |
| `84` / `86` | bid / ask | `"763.22"` / `"763.27"` | | | plain |
| `87` | volume | `"53.4K"` | `"135"` | `"8.28K"` | **abbreviated, see below** |
| `6509` | market_data_status | `"DP"` | `"DP"` | `"DPB"` | **CONFIRMED**; a letter code, not a number |
| `7283` | implied_vol | `"13.126%"` | `"15.970%"` | `"41.707%"` | **percent-suffixed STRING** |
| `7284` | historical_vol_30d | `"9.449%"` | `"16.474%"` | `"44.033%"` | likewise |

* **`implied_vol_units: auto` is correct for CP and needs no fallback.** The
  values carry an explicit `%`, so `auto` resolves them without ever reaching
  `implied_vol_units_fallback`. SPY decoded to `0.13126` and its 30-day realised
  to `0.09449`, which are plausible against each other. `7283` is an implied
  volatility on this route and **not** the `7633` ratio the playbook warns about.
* **`market_data_status` is now set to `6509` in config.** It was `null`.
* **Field `87` is abbreviated and the abbreviation is LOSSY.** `"53.4K"` does not
  parse, so `safe_float` returned `None` and the volume column was silently NULL
  for every liquid name — while BG, whose volume printed as a bare `"219"`,
  stored fine. **A NULL that appears only on liquid instruments is the worst
  shape a gap can take, because the sample that survives is biased.** IBKR
  supplies the exact value alongside it as `"87_raw": 53400.0`, and the decoder
  now prefers `<id>_raw` wherever it exists — which fixes the parse and the
  precision at once (`"53.4K"` is three significant figures; `53400.0` is not).

#### The dividend yield is NOT SERVED on this route, and the field id is not the problem

Part B's answer, and it is a negative one. A sweep of **7280–7299** plus
`7671`, `7672`, `7674`, `7675`, `7718`, `7719`, `7768` on SPY, then `7287` alone,
then `7286`+`7287`, never returned a dividend field. What the same snapshot *did*
return is the point:

| id | documented as | returned for SPY |
|---|---|---|
| `7286` | Dividend Amount | **absent** |
| `7287` | **Dividend Yield %** | **absent** |
| `7288` | Ex-date | **absent** |
| `7292` | Cost Basis | `"0"` |
| `7293` | 52 Week High | `"779.37"` |
| `7294` | 52 Week Low | `"629.29"` |
| `7295` | Open | `"0.00"` |

Their documented neighbours come back correctly, which means **the id mapping is
right and the data is withheld** — not that `7287` is the wrong id. Requesting a
narrow field list does not help: CP returns whatever the account is subscribed to
and ignores the narrowing.

So `underlying_snapshot_fields.dividend_yield` **stays `null`**, and
`underlying_snapshots.dividend_yield` stays NULL, for a reason that is now
measured rather than assumed. The connector's `[MCP ✓ 2026-09-09]` readings — SPY
`0.98`, XLP `2.62`, both percent, both two decimals — remain the only figures this
account has, and they cannot be cross-checked against a second route because the
second route does not serve them. **A single-route figure does not earn the right
to price with it**, so `_dividend_yield_for` still resolves to
`q_none_declared` and the solve says so.

#### `/iserver/marketdata/history`: the per-call cap is 257 bars

The backfill probes rather than trusting `ibkr.cp_api.history.max_bars_per_request`,
and the probe wrote its result into `collector_state`:

```json
{"bars": 257, "requested_days": 400, "chunk_days": 365, "probed_with": "FIND11"}
```

**257 bars for 400 calendar days requested** — about one year of trading sessions.
The chunking loop is therefore load-bearing on this route, not a precaution.
Separately, a 2-year daily request returned bars from **2023-08-08** against a
configured `data.daily_window_years: 2`, so **IBKR serves a span it derives from
the request rather than the window asked for** — the same behaviour already
recorded for `step_count` on the MCP route, now confirmed on CP.

#### HTTP statuses this route actually uses

* **`HTTP 429`** is real and frequent under the backfill's request rate. It is in
  `runtime.http.retry_status_codes` and the retries absorbed it.
* **`HTTP 500` is used for "no data".** `"Chart data unavailable"` and
  `"Contract details are not available"` both arrive as 500, which is a
  *permanent* answer wearing a *retryable* status: the collector retried each one
  three times before giving up. Worth knowing before reading a 500 as an outage.
* **`HTTP 410`** on `/iserver/secdef/strikes` means the request was malformed —
  in this case an exchange list where an exchange belonged.

#### Bars are unavailable for the three European listings

`ASML`, `SAN` and `TTE` all failed `/iserver/marketdata/history`, as did `BARN`,
`BOVA11`, `JDEP` and `LISN` — 15 of 167 backfill targets:

```
ASML:1d  HTTP 500 {"error":"Contract details are not available"}
SAN:1d   HTTP 500 {"error":"Chart data unavailable"}
TTE:1d   HTTP 500 {"error":"Chart data unavailable"}
```

This matters more than the count suggests: the chain collector sizes its strike
band from 30-day realised volatility computed off daily bars, so the three
listings whose chains cannot be re-bought are exactly the three that fall back to
the sigma ladder. `ASML` still collected a chain; `SAN` and `TTE` did not.

#### The European listings' options are US-listed — **SUPERSEDED**

**This entry's conclusion is wrong and its measurements are not.** See *The
European listings resolve to three different problems* at the end of this file:
the exchange list below belongs to whichever LISTING the symbol search matched,
which for SAN and TTE is their New York line and not the conid the universe
holds. Only ASML's universe conid was itself a US listing. The table's readings
stand exactly as taken.

`secdef/search` returns, for all three, an `OPT` section whose exchange list is
`SMART;AMEX;BATS;BOX;CBOE;...` — **US option exchanges**, not AEB, BM or SBF:

| symbol | universe conid | OPT months | strikes on SMART |
|---|---|---|---|
| ASML | 117902840 | 14 | **296 calls / 296 puts** |
| SAN | 30314144 | 5 | **0** |
| TTE | 29612193 | 9 | **0** |

`SAN` and `TTE` list months but serve no strikes against the conid the universe
holds: their US options sit under a *different* conid (`secdef/search` returns
`12442` for SAN alongside the Madrid line). So the "European chain" this build
collects for ASML is the US listing's chain, and for SAN and TTE there is
currently no chain at all. **This is a universe-resolution question, not an API
one**, and it is open.

#### A chain collected outside market hours has greeks and no prices

The run ran at 10:00–11:00 UTC, with the US pre-market and Europe open. Every
option row carried a **delta** — 671 distinct values, calls positive, puts
negative, ITM magnitudes near 1 — and **no bid, ask, last, iv, open interest or
volume**. IBKR serves model greeks when it is not serving quotes. A chain
photographed pre-open therefore cannot feed `obliskiq-compute solve-iv`, which
inverts a mid price. The 22:30 UTC schedule in `deploy/` is after the US close for
exactly this reason, and this run is not evidence against it.

### The European listings resolve to three different problems (Phase 5, 2026-09-10, MCP)

**This section corrects *The European listings' options are US-listed* above.**
That entry concluded, from `secdef/search` returning `SMART;AMEX;BATS;...` for all
three symbols, that "their US options sit under a *different* conid". Measured
against the connector on 2026-09-10, that is true of **ASML and only ASML**, and
in the opposite direction: it is the UNIVERSE that held the US conid.

Every line here is `[MCP ✓ 2026-09-10]` unless it says otherwise. The connector
and `cp_api` share IBKR's conid space, so a conid measured on one is valid on the
other — but the `cp_api` `secdef/search` OPT section for a European conid has
still never been read, and everything about how that route answers these three
stays `[CP ?]` until a live run.

| symbol | universe conid | what that conid IS | the European listing | option venues on it |
|---|---|---|---|---|
| ASML | `117902840` | **NASDAQ**, "ASML HOLDING NV-NY REG SHS", `country_code: US` | **`117589399`**, AEB, "ASML HOLDING NV", NL | `EUREX`, `FTA`; current `FTA` `[MCP ✓ 2026-08-24]` |
| SAN | `30314144` | BM, "BANCO SANTANDER SA", ES — **correct** | itself | **`EUREX`, `MEFFRV`**; current `EUREX` |
| TTE | `29612193` | SBF, "TOTALENERGIES SE", FR — **correct** | itself | **`EUREX`, `MONEP`**; current `MONEP` |

Three things follow, and they are separate faults:

* **ASML was bound to the wrong instrument.** `resolve_conid` takes the first
  exact ticker match, the NASDAQ row wins, and `exchange: AEB` / `currency: EUR`
  in the universe row never entered the decision. The 296 strikes Phase 4
  recorded were that listing's, correctly served.
* **SAN and TTE were never a conid problem.** Both already held the European
  line. What failed is that the OPT months and the exchange list came from a
  SYMBOL search — so the section describing New York travelled to
  `/iserver/secdef/strikes` alongside the Madrid and Paris conids, `SMART` was
  sent, and the ladder came back empty. `12442` (SAN's NYSE ADR) and `837140579`
  (TTE's NYSE line) are the rows that section belonged to.
* **The European chains are reachable**, on EUREX and on each market's own
  derivatives venue. Nothing about them required a US listing.

ASML's own option parameters could not be re-read in this session — the connector
answered `503 Service Unavailable` on three attempts — but the repository already
held the reading: `tests/fixtures/mcp_capture/raw/option_parameters_117589399.json`,
captured live in Session 7, carries `{"exchanges": ["EUREX", "FTA"],
"current_exchange": "FTA"}` against that conid, with expiration ids of the form
`117589399@FTA/OPT/FTA/20261218/AS9/1`.

#### What this says about the bars, and what it does not

Phase 4 recorded all three failing `/iserver/marketdata/history` with `HTTP 500`,
and asked whether a different conid would fix that too. It answers half:

* **ASML** requested `exchange=AEB` history against a NASDAQ conid, and
  `"Contract details are not available"` is what that looks like. Plausibly fixed
  by the conid; **UNVERIFIED** until a live run.
* **SAN and TTE** already held the right conid, so no conid change can fix
  `"Chart data unavailable"` for them. Whether `history_exchange: null` reaches
  them — the inversion recorded for TSEJ on 2026-08-29 — is untested, and is not
  declared here on a guess.

#### The venues are declared now, not inferred

`universe.yaml` pins `conid:` on all three rows and declares `option_exchange:`
(`FTA`, `EUREX`, `MONEP` — IBKR's own `current_exchange` for each conid). The
collector sends the declared venue verbatim and does **not** check it against the
search's exchange list, because that list is the thing that was wrong. A declared
venue serving no strike fails the target and names both lists.

#### `HTTP 500` with a no-data body is now a permanent answer

`"Chart data unavailable"` and `"Contract details are not available"` are matched
on the BODY, the way the `/accounts` precondition is, and raise
`PermanentUpstreamError`. A permanently absent target cost **three** HTTP attempts
and then **two** more per-target attempts — nine calls to be told the same thing
nine times. It costs one now. The status alone is deliberately not enough: every
genuine upstream fault also uses a bare 500.

### The European chains, read on CP at last (Phase 6, 2026-09-10, CP)

**Everything Phase 5 concluded from the connector holds on `cp_api`, and the one
thing it could not test is the thing that mattered.** Every line here is
`[CP ✓ 2026-09-10]` unless it says otherwise.

#### `secdef/search` serves the European `OPT` section, on European venues

The reading Phase 5 could not take. Sent as a JSON body, `secType: STK`:

| symbol | universe conid | has `OPT` | the exchange list on THAT conid |
|---|---|---|---|
| ASML | `117589399` AEB | yes | `EUREX`, `FTA` |
| **SAN** | **`30314144` BM** | **yes** | **`EUREX`, `MEFFRV`** |
| TTE | `29612193` SBF | yes | `EUREX`, `MONEP` |

Identical to what the connector reported for the same conids, to the venue. The
`cp_api` and MCP routes agree here, which is the first time that has been checked
for an option section rather than assumed from the shared conid space.

**The US line is in the same response, and it is ranked first in all three cases:**
ASML `117902840` NASDAQ, SAN `12442` NYSE, TTE `837140579` NYSE, each carrying
`SMART;AMEX;BATS;BOX;CBOE;…`. So *The European listings' options are US-listed*
above was reading the first row of a multi-listing answer, and
`references/ibkr-playbook.md` recorded that row's exchange list as the symbol's.

**This settles what Phase 5 left open: keying the section on the conid was the
WHOLE fix, not half of it.** The European sections were always in the response.

#### The declared venues serve strikes

`/iserver/secdef/strikes`, the first three in-window months on the declared venue:

| symbol | venue | SEP26 | OCT26 | NOV26 |
|---|---|---|---|---|
| ASML | `FTA` | 85 / 85 | 44 / 44 | 32 / 32 |
| SAN | `EUREX` | 57 / 56 | 27 / 26 | 24 / 24 |
| TTE | `MONEP` | 57 / 57 | 30 / 30 | 28 / 28 |

calls / puts. `FTA`, `EUREX` and `MONEP` had never been sent to this endpoint on
the collection route before this run.

#### The bars: one conid problem, and the TSEJ inversion again

`/iserver/marketdata/history`, `period=1m bar=1d`, each conid asked **twice**:

| symbol | with the native exchange | with `exchange` omitted |
|---|---|---|
| ASML `117589399` | **22 bars** (`AEB`) | 22 bars |
| SAN `30314144` | HTTP 500 `{"error":"Chart data unavailable"}` (`BM`) | **22 bars** |
| TTE `29612193` | HTTP 500 `{"error":"Chart data unavailable"}` (`SBF`) | **22 bars** |

* **ASML's bars were a conid problem, and the conid fixed them.** Phase 5 marked
  this *plausible, UNVERIFIED*; it is verified, and it needs no declaration —
  `AEB` and no exchange both answer.
* **SAN and TTE are the TSEJ inversion**, the one recorded on 2026-08-29 for the
  TOPIX-17 funds: the native exchange is refused and the omitted exchange serves.
  It is the second market where this holds, so it is a route behaviour rather than
  a Japanese peculiarity. `universe.yaml` declares `history_exchange: null` on
  both rows — probed both ways first, which is what the Phase 5 prompt asked for
  and what a declaration on a guess would have skipped.

`"Chart data unavailable"` is therefore **not** "this instrument has no history".
It is what this route says when the exchange on the request is one it will not
serve that contract on, and the same contract answers with the parameter absent.

#### `secdef/search` answers HTTP 200 with an OBJECT when nothing matches

Measured on `TOPIX` (`secType: IND`):

```
{"error": "No symbol found"}
```

A hit answers with a **list** of match objects. Iterating the miss yields its
**keys**, so a decoder looping `for row in body` gets the string `"error"` and
`row.get(...)` raises `AttributeError`. In this build that surfaced as
`conid resolution failed: AttributeError("'str' object has no attribute 'get'")`
— a message naming nothing an operator could act on — and cost two per-target
retries of a condition that could not change.

Same family as the four Phase 4 found: **HTTP 200, a plausible body, and a
decoder reading it as something it is not.** A symbol this route does not know is
a legitimate answer and now reads as no matches.

#### Every listing of one company answers the same bare ticker

The reading that explains the Phase 4 defect rather than describing it. The
`symbol` field — the one `resolve_conid`'s "exact ticker match" rule compares —
is the plain ticker on **every** row of a dual-listed search:

| search | rows, in the API's own order |
|---|---|
| `ASML` | `117902840` NASDAQ, `762277717` TSE, `117589399` AEB |
| `SAN` | `12442` NYSE, `889303007` MEXI, `815035193` AEQLIT, `80993135` LSE, `30314144` BM, `385055564` WSE, `199284645` ASX |
| `TTE` | `837140579` NYSE, `813330142` AEQLIT, `29612193` SBF |

Every one of those carries `symbol` equal to the bare ticker. **The listing venue
is in `description`** (`"NASDAQ"`, `"AEB"`, `"BM"`, `"SBF"`, …), which is the only
field that separates them.

So the exact-ticker rule matches every row and returns the first, and **the first
is the US listing for all three**. Measured directly, 2026-09-10 20:05 UTC, with
the pre-Phase-5 file that pins no conid:

| symbol | declared `exchange` | resolved without it | resolved with it |
|---|---|---|---|
| ASML | `AEB` | `117902840` NASDAQ | **`117589399`** AEB |
| SAN | `BM` | `12442` NYSE | **`30314144`** BM |
| TTE | `SBF` | `837140579` NYSE | **`29612193`** SBF |

A declared venue matching no row falls through to the previous answer
(`SAN` + `NOSUCH` → `12442`), so passing it can sharpen a resolution and never
refuse one.

#### Nineteen of seventy-four declared rows resolve to the wrong instrument

The three European rows are not the extent of it. Every declared row was searched
once and both rules applied to the same match list, 2026-09-10:

| | |
|---|---|
| declared rows | 74 |
| same conid under both rules | 54 |
| **different conid with the declared venue** | **19** |
| no match at all | 1 (`TOPIX`) |

**Eighteen of the nineteen were already pinned**, and in every one of those the
venue tiebreak returns *exactly the pinned conid* — an independent check on the
pins, arrived at from the search rather than from the file.

What the unpinned rule was picking is worth listing, because "a foreign listing"
undersells it:

| row | declares | resolves to | which is |
|---|---|---|---|
| `EEM` | ARCA | `690179584` | **CME, `IND`, EUR** |
| `XLP` | ARCA | `897516470` | COINDERIV |
| eleven TOPIX-17 funds | TSEJ | various | **SEHK** (Hong Kong) |
| `ISF`, `IUKP` | LSEETF | `90581012`, `107157253` | EBS (Swiss) |
| `STW` | ASX | `32583597` | SGX |
| `SLF` | ASX | `9662691` | NYSE |
| `ATEC` | ASX | `245164531` | NASDAQ |
| `ASML`, `SAN`, `TTE` | AEB, BM, SBF | US lines | above |

#### `EEM` resolved to a Micro Ether future, and was not pinned

The nineteenth. `contract_info` on both, 2026-09-10:

| conid | `company_name` | type | exchange | currency | options | daily bars on ARCA |
|---|---|---|---|---|---|---|
| `690179584` | **"Micro Ether Euro"** | `IND` | **CME** | **EUR** | no | HTTP 500 `"Contract details are not available"` |
| `6604766` | "ISHARES MSCI EMERGING MARKET" | `STK` | SMART (ARCA valid) | USD | **yes** | **20** |

The search returned `690179584` **first at 20:05 UTC** (and the correct row
first at 09:48 — see the ranking note below), so an unpinned `EEM` row declaring
`ARCA`, `USD`, `ETF`, `collect_chain: true` and `collect_bars: true` resolved to a
euro-denominated crypto index future on CME. `universe.yaml` pins `6604766` now.

Two things follow. **A wrong conid is not always another listing of the right
company** — the failure mode this route offers is a completely unrelated
instrument that happens to share a ticker, so "it resolved to the foreign line" is
the *benign* case. And **the row would have failed loudly in this build rather
than silently**: `690179584` carries no `OPT` section, so the conid-keyed chain
collector refuses it with *"reports no option months"* — Phase 5's failure doing
its job, on a symptom nobody would have traced to the resolver.


**The ranking is not stable within a day, and that is the finding.** The section
*The European listings resolve to three different problems* above recorded, from
MCP, that SAN and TTE "already held the correct European conid" and only ASML was
mis-resolved. The readings above say all three resolve to their US line on CP with
nothing pinned. Both are true, and the Phase 4 store (collected 09:48 UTC, opened
and read 2026-09-11) is what reconciles them:

| symbol | conid that run resolved | that is |
|---|---|---|
| ASML | `117902840` | NASDAQ — wrong, and it collected 192 US chain rows |
| SAN | `30314144` | BM — correct |
| TTE | `29612193` | SBF — correct |
| EEM | `6604766` | ARCA — correct |

So at **09:48 UTC** the search ranked the European and ARCA lines first, and at
**20:05 UTC** the same searches on the same account ranked the US and CME lines
first. `secdef/search` relevance is **time-varying**, not merely
listing-dependent.

The consequence is the opposite of reassuring. An unpinned row does not fail
consistently; it fails on some runs and not others, so the defect survives every
test anybody thinks to run and lands on the night nobody reads the log. A declared
`exchange:` tiebreak and a pinned `conid:` are both deterministic, and that is
their point.

**A withdrawal.** The Phase 6 entry above originally recorded that the Phase 4
store held EEM bars collected against `690179584` ("Micro Ether Euro"). Reading
that store: it does not. It holds `6604766` and no row against `690179584`. The
claim was an inference from the 20:05 reading presented as a fact about a store
that had not been opened.

#### The account ledger names no currency on its `BASE` row

`/portfolio/{id}/ledger` returns one row per currency plus the consolidated one:

| key | `currency` | `secondkey` | `exchangerate` |
|---|---|---|---|
| `BASE` | `"BASE"` | `"BASE"` | 1 |
| `EUR` | `"EUR"` | — | 1 |
| `USD` | `"USD"` | — | 0.8612968 |

**Both** keys on the `BASE` row read `"BASE"`. Phase 5 hoped `secondkey` carried
the real code and wrote the fixture that way; it does not. The consequence is that
the NAV currency check could never disagree with `account.base_currency` on this
route, because the label it compared was never a currency code at all.

What identifies the base currency is arithmetic already in the response: the
per-currency row whose `exchangerate` is **one** is the currency the consolidation
is into. Here that is `EUR`, and `account.base_currency` is `EUR`. `netliquidation
value` is a **float** on this route, not the string the per-currency amounts arrive
as.

#### The European chains collect, on their own venues

The run the phase existed for. `obliskiq-collect daily`, 22:58–23:59 UTC:

| symbol | conid | venue sent | rows | `sigma_source` |
|---|---|---|---|---|
| ASML | `117589399` | **`FTA`** | 112 | `realised_30d` |
| SAN | `30314144` | **`EUREX`** | 86 | `realised_30d` |
| TTE | `29612193` | **`MONEP`** | 44 | `realised_30d` |

`chain_snapshots.option_exchange` carries the venue per row, so a European chain
is distinguishable from a US one collected under a European row without inferring
anything. All three are on the **primary** realised-vol rung rather than the sigma
ladder, because the bars arrived in the same run.

#### `max_conids_per_snapshot` was 200 and the route's cap is 100

**QQQ lost its chain to `HTTP 429` in three consecutive live runs** — Phase 4, this
phase's `daily`, and a two-target re-run where it was alone with XLE. The body,
never recorded before, is the whole diagnosis:

```
{"error":"Exceed number of conids"}
```

Walked on QQQ option conids, one request at a time, 2026-09-10:

| conids in ONE request | result |
|---|---|
| 50 | ok |
| **100** | **ok** |
| 150 | `429 Exceed number of conids` |
| 200 | `429` |
| 233 | `429` |

So the cap is per-request, `>=100` and `<150`. It is **not** pacing, not the run's
length, and not accumulated subscriptions: QQQ failed alone in a two-target run,
and SPY — a **larger** collected chain, 418 rows against QQQ's 466 — succeeded two
targets after QQQ failed in the same sequence.

What singles QQQ out is the strike ladder. It lists **376 call strikes in the
front month**, far more than any other target, so it is the only one whose
volatility-scaled band actually reaches
`collectors.chain.max_strikes_per_expiry`; at both rights that is **120 conids for
one expiry**, which went out as a single request under the old cap of 200. Every
other target's band resolves to fewer than 100 and never touched the limit.

`ibkr.cp_api.marketdata.max_conids_per_snapshot` is **100** now, and QQQ collected
**466 rows** on the next run — the largest single chain in the set.

#### A bare `401` is the brokerage session dropping, not the token expiring

`XLE` failed its chain on `HTTP 401` 31 minutes into the run, with roughly 22
hours left on the live session token. `XLF` collected normally on the very next
target, and a re-run of `XLE` alone collected **130 rows**. An expired token does
not recover by itself and would have taken the rest of the run with it; a dropped
`/iserver` session does exactly this.

It is the same condition as `HTTP 500 {"error":"Please query /accounts first"}`,
wearing a different status. The repair — `ssodh/init` — was already written and
was one branch away, so a 401 now re-arms once and repeats the call. A **403** is
deliberately NOT re-armed: an entitlement answer does not change when the session
is re-opened.

#### A chain photographed at 22:30 UTC has greeks and no prices either

**This contradicts the reason the 22:30 schedule exists**, which is recorded above
as *"a chain photographed pre-open carries greeks and no prices"* with the 22:30
slot presented as the remedy. Measured on the same account, the same day:

| run | clock | rows | priced (bid/ask/last) | delta |
|---|---|---|---|---|
| ASML, US listing | **20:08 UTC** (16:08 ET, 8 min after the close) | 192 | **123** | 190 |
| the `daily` chain step | **23:04–23:57 UTC** (19:04 ET) | ~4,300 | **~34** | 3,542 |

`market_data_status` is `ZBd` on 4,478 of 4,502 rows either way, so the account's
entitlement did not change between the two — **the hour did**. Frozen closing
quotes are served for a while after the close and are gone by 19:00 ET.

The consequence is the one Phase 4 recorded for the pre-open run, and it is not
fixed by the schedule as deployed: `obliskiq-compute solve-iv` inverts a mid
price, so there is nothing for it to invert. **This is not evidence for any
particular replacement hour** — one reading at 20:08 and one at 23:00 bracket it
and no more. What would settle it is a chain step run at two or three hours
between 20:05 and 22:30 UTC and the priced fraction compared; until then
`deploy/obliskiq-collect@daily.timer` is left where it is, because moving a
production schedule onto an unmeasured hour is the move this repository refuses.

#### Four futures roots serve no ladder

`KC`, `NG`, `TTF` and `ZS` each returned no `/trsrv/futures` ladder — *"Futures
need the NATIVE exchange; SMART returns empty"* — while 12 of 16 roots collected
142 rungs. Unchanged from Phase 4's count and not investigated here.

#### `TOPIX` resolves to nothing on this route

`secdef/search` for `TOPIX` with `secType: IND` answers
`{"error": "No symbol found"}`. The row has never entered the store. Before the
decoder fix above this failed as an `AttributeError`; it now fails naming the
remedy, which is an explicit `conid:`. Not closed here — the conid was not
searched for under another term.

### What hour a chain can actually be photographed at (2026-09-11, CP)

**Phase 6 left the 22:30 timer in place because two readings bracketed the window
and neither named the hour.** This adds the middle of the bracket and one thing
neither reading anticipated.

Method: a fixed basket — 8 near-the-money call conids each on SPY, QQQ, ASML
(`FTA`), SAN (`EUREX`) and TTE (`MONEP`) — snapshotted on a schedule, counting the
fraction carrying any of `31`/`84`/`86`.

**An instrument note that matters, because it invalidated the first readings.**
`market_snapshot` polls until *any* requested field arrives
(`cp_client.py`, the settle loop), so asking for `6509` alongside the prices lets
it return a settled row with a status and no price. The first three samples were
taken that way and produced two isolated zeros that were artefacts. Asking for
`31`/`84`/`86` **alone** removes the ambiguity, and every reading below is taken
that way. A controlled test on cold conids with the market open found the
collector's own field set returned 12/12 priced either way, so this is a
measurement hazard rather than a demonstrated collection defect — but the settle
condition is the reason to be careful about what a zero means.

| UTC | state | SPY | QQQ | ASML `FTA` | SAN `EUREX` | TTE `MONEP` | `6509` |
|---|---|---|---|---|---|---|---|
| 15:00 | both open | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | `DPBd` / `RBd` |
| 15:10 | both open | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | `DPBd` / `RBd` |
| **21:08** | **US +1h08, EU +5h38** | **7/8** | **7/8** | **1/8** | **8/8** | **0/8** | **`ZBd`** |

Read against the two Phase 6 readings — ASML's US chain **123 of 192 priced at
20:08**, and the full chain step **~34 of ~4,300 at 23:04** — the shape is:

* **US option prices survive the close by hours, not minutes.** 87% of the basket
  is still priced at 21:08, and the quote state has gone to `ZBd` — frozen, which
  is what a served closing quote looks like. The collapse to ~1% happens somewhere
  between 21:08 and 23:04, and is not measured more finely than that.
* **So 22:30 was on the wrong side of a transition nobody had located**, and any
  hour from the close to roughly 21:00 is inside the priced window. `20:15 UTC`
  sits safely inside it with the whole decay ahead of it.
* **The European chains are a different problem, and one timer does not solve
  it.** EUREX, FTA and MONEP close at 15:30 UTC. By 21:08 — 5h38m later — ASML is
  1/8 and TTE 0/8 while SAN is still 8/8, so European option prices are partly
  gone at ANY hour chosen for the US close. This is not a schedule that can be
  tuned; it is two markets with different closes sharing one timer.

#### The run takes an hour, so the START time has to leave room for it

The reading that makes 20:15 the right answer rather than merely an acceptable
one. A full `daily` collected **2026-09-11 21:12–22:09 UTC** — the chain step
alone is 57 minutes across 24 targets — and produced **899 of 4,192 rows priced,
21.4%**, against a basket that was **87% priced at 21:08**.

Both are true and they are not in tension: the basket is one instant, the chain
step is an hour. A run beginning at 21:12 spends its first targets inside the
priced window and its last ones past the decay, so the fraction it records is an
average over the transition rather than a reading at its start.

| run | started | rows | priced |
|---|---|---|---|
| Phase 6 | 23:04 | ~4,300 | ~34 (**0.1%**) |
| this one | 21:12 | 4,192 | 899 (**21.4%**) |
| — | 20:15 | — | not yet measured |

**So the schedule must be chosen for where the run ENDS, not where it begins.**
A 20:15 start finishes around 21:15, which the basket says is still inside the
window; a 21:12 start does not. That is the argument for 20:15, and it is an
argument about duration that no instantaneous sample could have produced.

**Not measured, and the reason it is not declared:** no sample was taken between
15:30 and 20:00, so the hour at which European prices actually decay is unknown,
and nothing here says a second timer at (say) 15:45 UTC would serve them. That
probe is a basket sampled across the European close, which is a weekday job.

## Which wider-axis markets IBKR can actually reach — reference data only

**Measured 2026-09-13 on `cp_api`, exchanges closed.** `secdef/search` and
`iserver/contract/{conid}/info` are reference data and answer at the weekend;
`marketdata/history` served delayed daily bars on the same account and the same
day. No market data was requested and no vendor credit was spent. This exists so
the market-axis phase starts from measurement rather than from what anyone
believes about IBKR's country coverage — an assumption this repository has had
corrected three phases running.

| candidate | conid | ccy | listing | bars, `1m`/`1d` | options |
|---|---|---|---|---|---|
| `CAC` Amundi CAC 40 | 11183823 | EUR | SMART | **22** to 2026-09-11 | no |
| `ETFMIB` Amundi FTSE MIB | 26703763 | EUR | SMART | **22** to 2026-09-11 | no |
| `XIU` iShares S&P/TSX 60 | 74580921 | CAD | SMART | **20** to 2026-09-11 | **yes** |
| `XIC` iShares Core S&P/TSX Capped | 74580634 | CAD | SMART | **20** to 2026-09-11 | **yes** |
| `ZCN` BMO S&P/TSX Capped Comp | 114089048 | CAD | SMART | — | **yes** |
| `NIFTYBEES` Nippon India ETF Nifty | 396135052 | INR | **NSE** | **22** to 2026-09-11 | no |
| `SETFNIF50` SBI ETF Nifty 50 | 201495097 | INR | **NSE** | **22** to 2026-09-11 | no |
| `0050` Yuanta/P-shares TW Top 50 | 165197063 | TWD | **TWSE** | **22** to 2026-09-11 | no |
| `2222` Saudi Arabian Oil Co | 708373937 | SAR | **TADAWUL** | **22** to 2026-09-10 | no |

SPY on the same run returned 20 bars to 2026-09-11, so the counts are the route
working rather than a short window.

**The finding, and it is the opposite of the expected one.** Every candidate
resolved and every candidate served daily bars, India and Taiwan and Saudi
Arabia included. NSE, TWSE and TADAWUL are all reachable from this account for
reference data and delayed history. **Whatever blocks a wider axis, it is not
IBKR reachability** — see `references/macro-playbook.md` for what does.

**What this does NOT establish.** Delayed daily bars are not a snapshot quote and
not a chain. Whether this account is entitled to live or delayed *market data* on
NSE, TWSE or TADAWUL is a separate subscription question that a snapshot during
those exchanges' own hours would answer and this probe did not ask. Treat the
table as "the instrument exists and its history serves", nothing more.

**Two traps the probe walked into, both worth keeping.**

1. **`CAC` resolves first to CAMDEN NATIONAL CORP**, a USD US bank, ahead of the
   Amundi CAC 40 — the bare-ticker collision Phase 6 measured, in a market that
   does not yet exist on the axis. Any France row pins its conid.
2. **`CACX` is the SAME Amundi CAC 40 fund, quoted in GBP on the London line.**
   A France row declaring `CACX` would report sterling returns under France's
   name and hand indicator 11 a `EURFRF` cross that does not describe them. This
   is exactly the wrapper failure `universe.yaml`'s currency rule exists to
   refuse, and it is one relevance-ranked search row away.

`TASI` — the Tadawul All Share index — does not resolve at all, and `KSA` is the
iShares MSCI Saudi Arabia ETF, **USD and US-listed**: the same wrapper trap. So
Saudi Arabia has a reachable single stock and no native-currency market tracker
on this route.

### A second probe, and two corrections to the first

**2026-09-13, same route.** `CSMIB` was not in the first sweep and should have
been; `C40` was, and its description was read too quickly.

| ticker | conid | ccy | what it actually is |
|---|---|---|---|
| `CSMIB` | 74707801 | EUR | **iShares FTSE MIB ACC** — accumulating share class |
| `C40` | 305372184 | EUR | **Amundi CAC 40 *ESG* UCITS** — a DIFFERENT INDEX |
| `CAC` | 11183823 | EUR | Amundi CAC 40 — the one to pin |
| `ETFMIB` | 26703763 | EUR | Amundi FTSE MIB UCITS |

**`C40` is not the CAC 40.** It tracks the ESG-screened subset, so a france row
declaring it would report the screened index under France's name — the same class
of error as the GBP-listed `CACX`, and harder to spot because the currency is
right.

**`CSMIB` is the better italy candidate, not a duplicate.** An accumulating fund
reinvests dividends inside the NAV, so its price series is a total-return series
with **no ex-dividend gaps**. A distributing fund drops by the dividend on the ex
date, and that drop enters a realised-vol estimator as a real move. Verify it
against the bars before declaring it, but prefer it.

### The pinning run, and the Canadian chain nobody had asked about

**2026-09-14, `cp_api`, same account.** Every conid the axis widening intended to
declare was read back from `/iserver/contract/{conid}/info` before being written
into `universe.yaml`, because on this axis the bare ticker is the trap and the
venue is not.

| declared | conid | `/info` says | ccy | valid exchanges |
|---|---|---|---|---|
| `CAC` | 11183823 | AMUNDI CAC 40 | EUR | `SMART,SBF,CHIXEN,TRQXEN,BATEEN,GETTEX,DXEEN,TGHEEN,EUIBSI,AQEUEN,TRWBEN` |
| `CSMIB` | 74707801 | **ISHARES FTSE MIB ACC** | EUR | `SMART,VSE,FWB2,SWB2,IBIS2,BVME.ETF,GETTEX2,EUIBSI,TRWBIT` |
| `XIU` | 74580921 | ISHARES S&P/TSX 60 INDEX ETF | CAD | `SMART,TSE` |
| `NIFTYBEES` | 396135052 | NIPPON INDIA ETF NIFTY BEES | INR | `NSE` |
| `C40` | 305372184 | **AMUNDI CAC 40 *ESG* UCITS E-UE** | EUR | — not declared |
| `ETFMIB` | 26703763 | AMUNDI FTSE MIB UCITS ETF | EUR | — fallback only |

`C40`'s description is confirmed from the API rather than inferred: it is the
ESG-screened index, the currency is right, and it is the near-miss that a
currency check cannot catch.

**`/iserver/secdef/search` was asked for an `OPT` section on each of them, rather
than the absence being assumed from the 2026-09-13 sweep, which did not ask.**
`CAC` returns `['CFD', 'STK']`, and `CSMIB`, `ETFMIB` and `NIFTYBEES` return
`['STK']` alone. None carries an option chain on this route, and each row says so
in `options_unavailable_reason` with that call as its evidence.

**Daily bars served for all nine candidates with the exchange OMITTED**, 6m/`1d`:
CAC 125, C40 125, CSMIB 125, ETFMIB 125, XIU 122, XIC 122, ZCN 122, NIFTYBEES 123
(newest 2026-09-11), SPY 122 as the control. Every new row therefore declares
`history_exchange: null` — the path that was measured — rather than its listing
venue, which is the SAN/TTE inversion holding in Paris, Milan, Toronto and Mumbai
too.

#### `CSMIB` over `ETFMIB`, settled against the bars rather than the prospectus

Both track the FTSE MIB, so a day where one falls materially more than the other
is a fund event and not an index move. Over the **504 sessions they share** (2y
`1d`, both to 2026-09-14) the daily tracking difference has a standard deviation
of **0.28 percentage points**, and exactly two sessions sit far outside it:

| session | CSMIB | ETFMIB | gap |
|---|---|---|---|
| 2025-12-09 | **+0.715%** | **−3.357%** | −4.07pp |
| 2024-12-09 | −0.324% | −4.238% | −3.91pp |

The next largest gap in either direction is 0.56pp. Those two are the
distributing fund's annual ex-dividend dates, and on 2025-12-09 the index rose
while ETFMIB fell over three percent. A close-to-close realised-vol estimator
reads each as a real four-percent move once a year. The accumulating share class
has no such day, which is the reading the axis phase was told to take and did.

#### The only non-US chain on this axis that is both reachable AND priced

`/iserver/secdef/search` returns an `OPT` section on **`CDE`** — Bourse de
Montréal — for all three Canadian candidates, and `CDE` is the only venue any of
them names:

| ticker | conid | OPT months |
|---|---|---|
| `XIU` | 74580921 | **14**: `SEP26;OCT26;NOV26;DEC26;JAN27;FEB27;MAR27;JUN27;SEP27;DEC27;MAR28;JUN28;MAR29;MAR30` |
| `XIC` | 74580634 | 8, to `JUN27` |
| `ZCN` | 114089048 | 11, to `MAR30` |

Reference data is not a quote, so the entitlement question was asked directly.
Four near-ATM `XIU` calls per month, price fields `31`/`84`/`86` requested alone
and `6509` read on a separate call, 23:0x–23:2x UTC with the venue **closed**:

| month | DTE | strikes offered | priced | `6509` |
|---|---|---|---|---|
| `OCT26` | 18 | 44 calls | **4/4** | `ZBd` |
| `DEC26` | 95 | 65 calls | **4/4** | `ZBd` |
| `MAR27` | 186 | 66 calls | **4/4** | `ZBd` |

`DEC26` and `MAR27` both sit inside the configured `option.min_dte`/`max_dte`
band, and the `DEC26` quotes are tight — 53.00 call at 1.51/1.70 against a 53.03
close. **This account is entitled to Bourse de Montréal option data.**

#### A zero on the FIRST call for a conid is not evidence

Both the `OCT26` and `MAR27` baskets returned **0/4 with every price field
absent** on the first run and **4/4** on the second, with the same conids, the
same fields and the venue equally closed both times. The difference is
subscription state: `/iserver/marketdata/snapshot` answers a skeleton row for a
conid the session has never subscribed to, and `market_snapshot`'s settle loop
does not always cover a cold basket within one call — it retries within a call,
not across the separate price and status calls this method makes.

This is the same discipline Phase 7 reached from the other direction, where a
lone `0/8` taken with the venue OPEN turned out to be an artefact. **A zero must
survive a second call before it counts**, and a first-call zero read as an
entitlement failure would have refused a chain that works.

#### The same artefact reaches the COLLECTOR, and it is writing unpriced rows

**This is the larger finding of the run and it is not about the market axis.**
`obliskiq-collect chain` writes a full chain on its first pass over a set of
option conids and **every row is unpriced**. A second pass, same session, same
day, same conids, same closed venue, writes them all priced.

Measured 2026-09-14, 23:49–23:58 UTC, each name re-run with `--force`:

| name | venue | first pass | second pass |
|---|---|---|---|
| `XIU` | `CDE`, closed 21:00 UTC | 264 rows, **0 priced** | 264 rows, **264 priced** |
| `FCX` | US, closed 21:15 UTC | 170 rows, **0 priced** | 170 rows, **170 priced** |
| `ASML` | `FTA`, closed 15:30 UTC | 122 rows, 0 priced | 122 rows, **0 priced** |

`ASML` is the control and it is the important one: it does NOT recover, because
its zero has a different cause — the European post-close behaviour Phase 7
measured directly. Two causes, one symptom, and that is precisely why a single
zero decides nothing.

**The mechanism, read from the code rather than instrumented, so treat it as a
diagnosis and not as a measurement.** `CpApiClient.market_snapshot` polls up to
`ibkr.cp_api.marketdata.snapshot_settle_retries` times and breaks out of the
poll when `any(field in row for field in wanted)`. `wanted` is the whole of
`snapshot_fields`, and since Phase 6 that includes `6509`
(`market_data_status`) — which a cold skeleton row carries. So the loop counts
the batch as settled on the very first response and never spends its retries. A
basket asked for prices ALONE behaves differently: the hand probe above
requested `31`/`84`/`86` only, found none of them, and correctly exhausted all
four retries — and still needed a later call to settle, so the retry budget is
too small for a cold subscription even when the predicate is right.

**What it costs.** Every night, every option conid that is new that night — which
is every roll into a new expiry month — is written with no bid, no ask and no
last. The chain is the one series in this package that cannot be retrieved
retrospectively at any price.

**CLOSED 2026-09-15, and the diagnosis above was right in shape and wrong in
detail.** See *What a cold snapshot subscription actually does* below: it is not
only `6509`, and the retry budget was too small as well.

#### What a cold snapshot subscription actually does

**Timed 2026-09-15 on `cp_api`**, polling `/iserver/marketdata/snapshot` once a
second against option conids this session had never subscribed to, and recording
which requested field ids were present on each response:

| t | full field set (what the collector asks for) | price fields only |
|---|---|---|
| 0.15s | *(nothing)* | *(nothing)* |
| 1.30s | `6509` `7308` `7309` `7310` `7311` | *(nothing)* |
| 2.4s – 5.9s | unchanged | *(nothing)* |
| 4.78s | — | **`31` `84` `86`** |
| 7.03s | **`31` `84` `86`** + the above | — |

**Three things this settles, and the first one overturns the earlier reading.**

1. **It is not only `6509`. All four GREEKS arrive with it**, 5.7 seconds before
   any price. Excluding the status field — the obvious fix, and the one the
   defect's own write-up implied — would have left `delta` ending the poll just
   as early. Settlement is therefore defined by INCLUSION: a price arrived.
   `CpApiClient.QUOTE_FIELD_NAMES` is that list, and it is `last`, `bid`, `ask`.
2. **The retry budget was too small independently of the predicate.** The
   price-fields-only basket had a correct predicate by construction and still
   needed 4.78s, against a budget of four retries at one second. So a correct
   predicate alone would not have fixed this.
3. **Priming works.** The same full field set settled in **7.03s** cold and
   **4.78s** when the batch had been asked for once already. `market_snapshot`
   now issues one request per batch before polling any of them, so a multi-batch
   chain warms its subscriptions concurrently instead of one after another.

`snapshot_settle_retries` is 10 on that evidence. The cost is bounded and is only
ever paid by a batch that never quotes: the `curve` step, whose COMEX snapshots
are documented above as returning nothing, went from **31s to 54s** on the same
day with identical output.

#### The fix, proven live rather than on a test

| | first pass, fresh store |
|---|---|
| `market_snapshot` on 12 cold XIU conids, before | **0 of 12 priced** in 2.16s |
| the same, 12 cold FCX conids, before | **0 of 12 priced** in 1.28s |
| after, on a different still-cold slice — XIU | **12 of 12 priced** in 6.95s |
| after — FCX | **12 of 12 priced** in 4.65s |
| `obliskiq-collect chain --only XIU`, after | **264 rows, 264 priced** |

The last line is the one that matters: on `main` that same command against a
fresh store wrote 264 rows with no price on any of them.

### The four remaining backfill failures were three different things

**Measured 2026-09-15 on `cp_api`, each target asked BOTH ways** — with its
declared listing venue on the request and with the exchange omitted — because
"the error string matches the TSEJ inversion" is a resemblance and not a
diagnosis. It turned out to be right for two of the four and wrong for the rest.

| target | with the declared venue | exchange omitted | what it actually is |
|---|---|---|---|
| `BARN` `EBS` | HTTP 500 *Chart data unavailable* | **250 bars 1y / 499 2y** | the inversion, in Zurich |
| `LISN` `EBS` | HTTP 500 *Chart data unavailable* | **249 bars 1y / 498 2y** | the inversion, in Zurich |
| `BOVA11` `B3` | **122 bars at `6m`** | **122 bars at `6m`** | **NOT the inversion** |
| `JDEP` `AEB` | HTTP 500 *Contract details are not available* | 32 bars **ending 2026-04-29** | a dead listing |

`BARN` and `LISN` are the inversion holding in a third country, and both now
declare `history_exchange: null` on a measurement rather than on the pattern.

**`BOVA11` is not an exchange problem at all**, and reading it as one would have
changed a field that changes nothing. It serves `6m` and `1y` with `B3` on the
request; it refuses `2y` and `5y` with the exchange on the request *and* with it
omitted. SPY, STW, BARN and LISN all serve the full window on the same run, so
this is the instrument's own available history and not a route-wide cap:

```
BOVA11  1y/1d -> 248 bars      730d/1d -> HTTP 500     182d/1d -> 181 bars
        2y/1d -> HTTP 500      365d/1d -> HTTP 500      91d/1d ->  90 bars
```

**The endpoint refuses a window longer than it can serve rather than returning a
short series**, which is why the target collected nothing at all: `Backfill`
opens by asking for the whole window in one call. It now halves the span on a
refusal, down to `collectors.backfill.span_stepdown_min_days`.

**And a second layer underneath it.** With the step-down in, `BOVA11` still
failed: the backward walk sends `startTime`, and every span at every cursor
beyond the instrument's history is refused too. That walk already treats an EMPTY
chunk as the signal to stop — a refusal is the same fact served differently, so
it now stops the walk instead of failing the target and discarding the recent
bars the same run had already collected.

**`JDEP` is neither**, and is the reason the four were measured separately.
`/iserver/secdef/search` returns the name on `GETTEX` now; `/info` lists
`SMART,VSE,SWB,GETTEX,EUIBSI` with **`AEB` absent**; and the only series any
venue serves **stops on 2026-04-29**. Declaring `history_exchange: null` would
have made the target "succeed" while collecting a series that ended months ago.
It collects nothing instead, and its row stays: `JDEP` is the only `KC` entry in
`commodity_proxies` and the `KC` futures root names it in its own
`untradeable_reason`, so deleting it would quietly delete coffee's equity
expression. That is a universe decision, carried open in `ROADMAP.md`.

**What the run reports now.** `obliskiq-collect backfill` against live `cp_api`:
**171 of 171 targets ok, status `ok`**, where the same command on `main` reported
`partial` with eight failed targets. `BOVA11` holds 315 daily and 53 weekly bars
where it held none, and `brazil` went from **60 to 89** `ok` leaves on the board.

## What the European close actually does to a chain

**Measured 2026-09-14, six hourly ticks, one fixed `SEP26` basket of 8 near-ATM
contracts per name.** ASML on `FTA`, SAN on `EUREX`, TTE on `MONEP`, all three
closing at 15:30 UTC; SPY and QQQ as controls open throughout. Price fields
`31`/`84`/`86` requested **alone**; status read on a separate call.

| UTC | vs EU close | SPY | QQQ | ASML | SAN | TTE |
|---|---|---|---|---|---|---|
| 15:07 | −23m | 8/8 | 8/8 | **8/8** | 8/8 | **8/8** |
| 16:06 | +36m | 8/8 | 8/8 | 2/8 | 8/8 | 6/8 |
| 17:06 | +1h36m | 8/8 | 8/8 | 3/8 | 8/8 | 6/8 |
| 18:06 | +2h36m | 8/8 | 8/8 | 2/8 | 8/8 | **8/8** |
| 19:06 | +3h36m | 8/8 | 8/8 | 2/8 | 8/8 | 6/8 |
| 20:06 | +4h36m | 8/8 | 8/8 | 2/8 | 8/8 | 6/8 |

### It is a step at the close and then a plateau, not a decay

Every European name is fully priced 23 minutes before its close and none of them
falls progressively afterwards. ASML drops to 2/8 within 36 minutes and sits
there for four hours. SAN never moves at all. TTE alternates 6/8 and 8/8,
**including a return to fully priced at 18:06, nearly three hours after MONEP
shut** — which no decay model produces.

So "the European chains decay after their close" is the wrong description. What
happens is that the venue shuts, a partial set of frozen quotes is served from
then on, and **how partial is a property of the name, not of the elapsed time**.
The US controls hold 8/8 at every tick, which is what makes the step
attributable to the close rather than to the sampler.

### The status letter is a second, independent signal of the same event

The European names read `DPBd` / `RBd` while open and `ZBd` from the 16:06 tick
onward, flipping in the same tick their prices drop, while the US controls stay
`DPBd` all day. Whatever `Z` denotes, it tracks the venue being shut. It is
worth reading beside the price count because it does not share the price count's
failure mode — see the transient zeros below.

### A lone `0/8` with the market OPEN is noise

On 2026-09-11, TTE read 0/8 at 14:40 and SAN 0/8 at 14:55, each with its venue
open and each bracketed by 8/8 on either side. Two ticks later in the same run
they were fine. **Do not read a single zero as an event**; require it to persist
across consecutive ticks. Two of the five readings that survived that day's
sampler were this artefact, and a curve fitted through them would have been
fitted through noise.

### At the collection timer's own hour, the chains are PRICED

At 20:06 — the hour `deploy/obliskiq-collect@daily.timer` fires — the basket
reads **SAN 8/8, TTE 6/8, ASML 2/8: 16 of 24**. The 20:15 run is not reaching
dead chains.

**This contradicts the 2026-09-11 21:08 reading** of ASML 1/8, TTE 0/8, SAN 8/8,
which is the single observation the whole phase was built on. One hour later in
the day, and three days earlier, on the same basket. Both readings are real; the
day-to-day difference is larger than the within-day effect, and one hand-taken
sample was too thin to carry the conclusion drawn from it.

**ASML is the one name where the close costs something real.** 2/8 near-ATM from
half an hour after its close onward means an ATM mid price is unlikely to be
available for it, and `solve-iv` inverts a mid. That is the operative
consequence and it is confined to one of the three names. It is inferred from
the basket rather than from a stored chain: this store holds no ASML, SAN or TTE
chain rows to check it against.

<!-- append here -->
