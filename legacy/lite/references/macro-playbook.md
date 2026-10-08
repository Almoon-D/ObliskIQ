# The macro sources — observed behaviour

This file records what each API *did*, not what its documentation says.
`config.yaml` owns the thresholds and `docs/DECISIONS.md` owns why the design
responds as it does.

## Status, at a glance

**Every source is exercised live**, most recently on 2026-09-09 against
**reissued** keys, and **every declared series still collects**: 53 of 53
targets, 59,160 rows, no failed target.

| Source | Key | Held | Exercised live | State |
|---|---|---|---|---|
| FRED / ALFRED | yes | ✅ | 2026-09-09 | 35 of 35 series |
| ECB Data Portal | no | — | 2026-09-09 | 13 of 13 series |
| CFTC (Socrata) | no | — | 2026-09-09 | 4 of 4 series |
| EIA v2 | yes | ✅ | 2026-09-09 | 1 of 1 series |

The 2026-09-04 sweep returned 59,165 rows against this one's 59,160. The
difference is the rolling `macro.collection.default_window_years` window sliding
five days, not a regression: no target failed in either run. **The keys are not
the same keys** — the FRED, EIA and marketdata.app values once committed to
`.env.template` were reissued, and this run is the first evidence the reissued
FRED and EIA keys work.

There were five. **The OECD is gone**, with the one series it served — see below
for what it did while it was here, which is why the SDMX decoder it taught this
build survives it.

The IBKR and marketdata.app routes are covered by their own playbooks; the
Client Portal route has no credentials in any environment and has never run.

---

## FRED / ALFRED

Everything in this section concerns `series/observations` with `output_type=4`,
which is the only route that yields a publication date — see `macro/fetchers.py`.
The plain observations call returns no release date at all, and this build
refuses to align a macro series on its reference period.

---

## `output_type=4` requires a real-time window, and today is not one

Sent without `realtime_start` and `realtime_end`, FRED defaults the vintage
window to today and **fails** rather than returning the current vintage:

```
HTTP 400  {"error_code":400,"error_message":"Bad Request.  No vintage dates exist
for the specified real-time period: 2026-09-04 to 2026-09-04."}
```

Reproduced on `DFF`, `SOFR`, `DGS2`, `DGS10` and `CPIAUCSL` — every series tried.
A day with no vintage is most days: FRED publishes vintages on release days, not
daily. So the window is not optional and cannot be left to the default.

## The window is capped at a number of VINTAGE DATES, not a span

Widening the window to FRED's full range fails a different way, and the ceiling
is named in the error:

```
HTTP 400  There are 5101 vintage dates in the specified real-time period:
1776-07-04 to 9999-12-31.  This exceeds the maximum number of vintage dates
allowed for this file type (2000).
```

A vintage is a release, so the count is the window length times the series'
publication rate. Measured:

| series | frequency | window | vintages | result |
|---|---|---|---|---|
| `DGS10` | daily | 5 years | ~1,300 | 200, 1,303 observations |
| `DGS10` | daily | 10 years | 2,472 | **400**, over the cap |
| `DGS10` | daily | 20 years | 4,931 | **400**, over the cap |
| `CPIAUCSL` | monthly | 10 years | — | 200, 119 observations |
| `IRLTLT01DEM156N` | monthly | 10 years | — | 200, 118 observations |
| `IRSTCI01AUM156N` | monthly | 10 years | — | 200, 118 observations |

`macro.collection.default_window_years` is ten, so **every daily FRED series
exceeds the cap** and no monthly one comes close. The fetcher splits the window
by the series' declared frequency against
`macro.sources.fred.max_vintages_per_call`.

Chunking was verified end to end: `DGS10` over ten years in three windows
returned 2,605 observations, and its release dates matched an independent
single-call five-year window on 1,302 of 1,303 overlapping rows.

## A vintage-free window answers with a message about the SERIES

**This section previously said two declared series exist in FRED but not in
ALFRED. Neither claim was true, and both were written from an error message
rather than from a probe.** What follows is what the API answered when asked.

`IRLTLT01CNM156N` (China long rate) and `DHHNGSP` (Henry Hub natural gas spot)
both failed with:

```
HTTP 400  The series does not exist in ALFRED but may exist in FRED.  Try setting
realtime_start and realtime_end to today's date or removing the realtime_start
and realtime_end variables.
```

They failed for two entirely different reasons, and the message is a good
description of neither.

### `DHHNGSP`: the window had no vintage in it, and the series is fine

ALFRED holds `DHHNGSP` from **2014-12-10**, weekly, 605 vintages. Asked for a
real-time window inside that range it answers normally:

| realtime window | result |
|---|---|
| `2026-01-01 .. 2026-09-04` | 200, 174 observations |
| `2016-01-01 .. 2018-01-01` | 200, 519 observations |
| `2014-08-14 .. 2022-07-22` | 200, 1,983 observations |
| `2006-09-05 .. 2014-08-13` | **400, "does not exist in ALFRED"** |
| `2010-01-01 .. 2012-01-01` | **400, "does not exist in ALFRED"** |

The failing windows are the ones that contain **no vintage date at all**, and
that is the whole condition. It is the same failure as the `today to today`
default recorded above, at the other end of the history: `output_type=4` is a
vintage query, and a vintage query over a span with no vintage in it is an error
rather than an empty result.

`DHHNGSP` is in the `commodity_history` group, so its window is
`commodities.history.monthly_window_years` — twenty years — and the **first
chunk** of that window ended in 2014-08, four months before ALFRED's first
vintage. The first chunk raised, and the target failed. `DCOILWTICO` and
`DCOILBRENTEU` are chunked identically and survive that same first chunk only
because their own first vintage, **2011-04-06**, happens to fall inside it.

The fix is to ask ALFRED where its vintages are, on `fred/series/vintagedates`,
and intersect the window with the answer. Measured before and after on the same
day: `DHHNGSP` 0 rows → **2,967 rows** from 2014-12-09, every one carrying a real
first-publication date; `DCOILWTICO` 3,858 rows → 3,858 rows, unchanged, which is
what a clamp inside an answerable range should do.

Six of the thirty-five FRED series have a first vintage later than the window
this build asks for, and are clamped: `SOFR` (2019-03-29), `IUDSOIA`
(2021-07-28), `DCOILWTICO` and `DCOILBRENTEU` (2011-04-06), `DHHNGSP`
(2014-12-10), `PCOPPUSDM` (2015-11-06). Clamping changes what the other five
return **not at all**: `output_type=4` already serves only observations whose
initial release falls inside the vintage range, so a window that starts earlier
buys nothing. Verified by probing `SOFR` and `PCOPPUSDM` with the window
clamped, half-clamped and unclamped — identical counts and identical first rows
in all three.

**What is genuinely lost is ALFRED's, not the request's.** `output_type=4`
serves nothing before a series' SECOND vintage: `DHHNGSP`'s earliest returned
observation is 2014-12-09, first released 2014-12-17, so the fourteen years of
Henry Hub history before that carry no first-publication date anywhere in
ALFRED. The twenty-year commodity window is short by that much, reported as the
shortfall it is, and not filled with a synthetic date.

### `IRLTLT01CNM156N`: FRED does not have it

Not "in FRED but not in ALFRED" — not in FRED:

```
GET fred/series?series_id=IRLTLT01CNM156N
HTTP 400  {"error_code":400,"error_message":"Bad Request.  The series does not exist."}
```

`fred/series/search` returns **nothing** for a China long-term government bond
yield. The other nine `IRLTLT01*` series are alive and unaffected —
`observation_end 2026-06-01`, last updated 2026-07-16 — so this is one series
retired, not the family. No release-date policy and no request shape brings back
a series the source no longer serves, so it is deleted and `china` declares no
`long_rate_series`.

### The fallback that stays rejected

Reissuing either as a plain observations call would return values with no
publication date, and this source's policy is `from_api` — so the rows would
store a NULL release date, be excluded from every point-in-time read by
`known_at`, and still count as collected. That is still refused, and
`tests/test_macro_pull.py` now asserts it rather than describing it: a series
ALFRED holds no vintages for fails, and no observations call is made.

## Why none of this was visible without a key

`macro_pull` abandons a source after
`macro.collection.max_consecutive_failures` consecutive failures and lets the
other sources continue — which is correct, and was added deliberately. The
consequence is that a keyless run and a *broken keyed* run print the same summary
line: nineteen CFTC and ECB targets collected, FRED abandoned. The defect
survived two sessions inside that symmetry.

Measured on the same fixture-seeded store, same day:

| | before the fix | after |
|---|---|---|
| macro targets collected | 19 of 55 | **53 of 55** |
| macro rows | 33,721 | **56,194** |
| grid cells populated | 1 | **215** |
| collection coverage | 0% | **10%** |
| cells reading `macro_series_absent` | 216 | **44** |

---

## OECD — deleted, and what it taught the decoder on its way out

**The OECD is no longer a source here.** `OECD_CLI` was the only series it
served, it returned no row in its life, nothing in `src/` read it and no market
declared it. `docs/DECISIONS.md` carries the argument and what reopening it would
cost.

This section stays because the three shapes below are **properties of SDMX, not
of that publisher**, the decoder that handles them is the one the ECB's thirteen
live series run on, and the tests that pin them were re-pointed at the ECB rather
than deleted with the fetcher. The next SDMX source will meet all three.

### SDMX-JSON **2.0**, and the ECB's is **1.0**

Both "speak SDMX-JSON" and they do not speak the same one. Observed bodies:

| | ECB | OECD |
|---|---|---|
| top-level keys | `header`, `dataSets`, `structure` | `meta`, `data`, `errors` |
| where the data sets are | `dataSets` | `data.dataSets` |
| where the structure is | `structure` (object) | `data.structures` (**list**) |

The decoder read only the 1.0 shape, so an OECD response yielded no data sets at
all. Finding nothing is not an error, so it returned an empty list, the fetcher
reported the target collected, and the run said **ok with zero rows**. That is
how it survived undetected.

### `TIME_HORIZ` is not the time axis, and it comes first

The CLI dataflow's observation dimensions, in order, live:

```
[0] REF_AREA  [1] FREQ  [2] MEASURE  [3] UNIT_MEASURE  [4] ACTIVITY
[5] ADJUSTMENT  [6] TRANSFORMATION  [7] TIME_HORIZ  [8] METHODOLOGY
[9] TIME_PERIOD
```

The decoder selected the time dimension with `startswith("TIME")`, which matches
**`TIME_HORIZ` at position 7** before `TIME_PERIOD` at position 9. It then took
`TIME_HORIZ`'s single non-date value as the period list and its position as the
time position.

Every observation was dropped for an unparseable date, and **that was the lucky
outcome**: had that value parsed as a date, all 2,574 observations would have
been stamped with it. The decoder's own comment already warned that assuming
time is the last dimension attaches values to the wrong dates; the prefix match
reintroduced the same failure from the other end. Matched on the exact SDMX ids
now — `TIME_PERIOD` and `TIME`.

### The configured key returns 23 countries, not one series

`OECD.SDD.STES,DSD_STES@DF_CLI,4.1/.M.LI...AA...H` leaves `REF_AREA` empty, so
one response carries 23 countries × 117 periods = 2,574 observations.

Under `dimensionAtObservation=AllDimensions` there is no `series` map, so the
existing "more than one series came back" warning never fired — every country's
observations land in one flat dictionary and merging them was the default. That
is now **refused** with the dimension named, because there is no "first series"
to take as there is in the 1.0 shape.

That refusal is what turned the series from a clean zero into a visible failed
target — and a series that has never returned a row, that nothing in `src/`
reads and that no market declares is the same shape as `COT_USD`, deleted in
PR 6 for exactly that reason. Pinning `REF_AREA` per market was the alternative;
deleting it is what was done. The refusal survives it, because a key that
addresses several series at once is a mistake any SDMX source allows.

---

## ECB, CFTC, EIA

All three answered on the first live run and needed no change.

**ECB Data Portal** — SDMX-JSON 1.0, no key, 13 of 13 series. Carries no
publication date of any kind beyond the daily reference rates that are public on
the day they describe, so those rows store a NULL release date and
`MacroSeries.unaligned_count` reports them. That is the honest answer and not
a gap to paper over.

**CFTC (Socrata)** — no key, 4 of 4 series. All contract codes re-verified
against the dataset in PR 6.

**EIA v2** — keyed, 1 of 1 series (`WCESTUS1`).

> `ROADMAP.md` and this file's predecessor both said EIA had "never been
> exercised with its key". **That was wrong** — it was written in the same PR
> whose own measurement run collected EIA successfully, by inferring EIA's status
> from the shape of the FRED bug instead of reading the run log. Recorded here
> because it is the second time in one session that a status was inferred from an
> absence rather than read, and the first time cost two sessions.

---

## No market but the US has a CPI print newer than 2025, on either source

**Measured 2026-09-13** against both live sources with the build's own keys. This
is the finding that governs how far the market axis can usefully widen, and it is
larger than the two markets an earlier reading named.

**FRED / OECD `CPALTT01*`**, which eleven of thirteen declared markets read.
Asked for the newest observation of each, `sort_order=desc&limit=1`:

| market | series | observations | newest |
|---|---|---|---|
| japan | `CPALTT01JPM659N` | 796 | **2022-04** (and its value is `.`) |
| korea | `CPALTT01KRM659N` | 827 | **2023-11** |
| australia | `CPALTT01AUQ659N` | 303 | 2025-01 |
| south africa | `CPALTT01ZAM659N` | 805 | 2025-01 |
| germany, spain, uk, israel | `CPALTT01{DE,ES,GB,IL}M659N` | 831–841 | 2025-03 |
| china, brazil | `CPALTT01{CN,BR}M659N` | 460, 533 | 2025-04 |
| **us** | `CPIAUCSL` | 956 | **2026-08** |

**ECB HICP**, the obvious replacement for the four euro-area markets. Asked with
`startPeriod=2026-01`, `ICP/M.U2.N.000000.4.ANR` and the DE, FR, IT and ES
national series **all return HTTP 200 with a zero-length body** — the ECB's
"no observations in this window", which PR 9 taught the decoder to refuse. Their
newest observation is **2025-12** on every one of the five.

**So the euro-area replacement is not a replacement.** Nine months stale beats
seventeen and is still not a current inflation print.

> **Superseded in part, 2026-09-15.** True of the ECB's `ICP` flow, which is
> still dead in both its `.ANR` and `.INX` forms. Not true of the statistic: FRED
> redistributes Eurostat's HICP INDEX current to 2026-07. The euro-area
> replacement is `CP0000{DE,ES,FR,IT}M086NEST` and `CP0000EZCCM086NEST`, on FRED.
> See the Phase 9 section at the end of this file.

**What is NOT affected, and the contrast is the useful part.** On the same run
and the same keys: `IRLTLT01{FR,IT,CA}M156N` and `IRSTCI01{CA,IN}M156N` all serve
to **2026-06**; `EXR/D.CAD.EUR.SP00.A` serves to **2026-09-11**; and
`FM/D.U2.EUR.4F.KR.DFR.LEV` — the ECB policy rate — serves to **2026-09-13**.
The long-rate and short-rate families are alive and the FX is alive. It is the
**CPI prints specifically** that stop, on both sources, for every market except
the United States.

Ten series stopping within two months of each other on one source, and five more
stopping on the same month on another, reads like a redistribution cutoff rather
than ten independent discontinuations — but the cause is not observable from
here, and the observable fact is enough: **this build cannot obtain a CPI print
newer than 2025-12 for any market but the US, from either source it uses.**

**What it costs, read off the grid rather than reasoned about.** On the
2026-09-11 board, `policy_rate_real` is `ok` on **us alone**. It is `stale` on
australia, brazil, germany, israel, japan, korea, south africa, spain and uk,
each naming its own vintage and age in the reason — `japan` at 1,550 days.
`eurozone` reads `n/a` for an absent `HICP_YOY`, and `china` and `hong_kong` are
`inapplicable` for a policy rate they do not declare. **The grid is honest about
this and the indicator is dead anyway.** A market added to the axis today
inherits that on arrival: the equity half of a new market is real, the real-rate
half is stale before it is collected.

## What a wider market axis can and cannot source

Asked in the same pass, for the markets a top-GDP / top-exchange axis would add:

| candidate | CPI | 10y | short rate | EUR cross |
|---|---|---|---|---|
| france | `CPALTT01FRM659N` → 2025-03 | `IRLTLT01FRM156N` → 2026-06 | ECB_DFR (live) | — (euro) |
| italy | `CPALTT01ITM659N` → 2025-03 | `IRLTLT01ITM156N` → 2026-06 | ECB_DFR (live) | — (euro) |
| canada | `CPALTT01CAM659N` → 2025-03 | `IRLTLT01CAM156N` → 2026-06 | `IRSTCI01CAM156N` → 2026-06 | `EXR/D.CAD…` → 2026-09-11 |
| india | `CPALTT01INM659N` → 2025-03 | **`IRLTLT01INM156N` does not exist** — HTTP 400 | `IRSTCI01INM156N` → 2026-06 | `EXR/D.INR…` → 2026-09-11 |
| taiwan | **`CPALTT01TWM659N` does not exist** | not probed | not probed | **`EXR/D.TWD…` stops 2020-10-30** |
| saudi | **`CPALTT01SAM659N` does not exist** | not probed | not probed | **`EXR/D.SAR…` is HTTP 404** |

`IRLTLT01INM156N` answers *"The series does not exist"*, which is the same
failure `IRLTLT01CNM156N` gave in PR 10 — a declared null is the honest reading,
not a bug to fix.

**Taiwan's euro cross exists and stopped six years ago**, which is worse than
absent: `lastNObservations=1` returns a 2020 rate with no error, so a row
declaring it would publish a stale FX reading rather than refusing one. Saudi
Arabia has no `EXR` series at all and fails loudly. Both mean indicator 11 cannot
be computed for those markets from the source this build uses.

### Every series the axis widening declares, re-measured before declaring it

**2026-09-14, `api.stlouisfed.org`, this build's own key.** The 2026-09-13 table
above was taken one day earlier and is the reason these ids were chosen; this run
is the one that put them in `config.yaml`. Asked for the series metadata and then
`sort_order=desc&limit=1`:

| id | freq | newest | note |
|---|---|---|---|
| `CPALTT01FRM659N` | M | 2025-03 | the CPI cutoff, as everywhere |
| `CPALTT01ITM659N` | M | 2025-03 | |
| `CPALTT01CAM659N` | M | 2025-03 | |
| `CPALTT01INM659N` | M | 2025-03 | |
| `IRLTLT01FRM156N` | M | **2026-06** | |
| `IRLTLT01ITM156N` | M | **2026-06** | |
| `IRLTLT01CAM156N` | M | **2026-06** | |
| `IRSTCI01CAM156N` | M | **2026-06** | |
| `IRSTCI01INM156N` | M | **2026-06** | |
| `IRLTLT01INM156N` | — | — | **HTTP 400, `The series does not exist`** |
| `IRLTLT01CNM156N` | — | — | HTTP 400, unchanged since PR 10 |

So india declares `long_rate_series: null` and `slope_label: null`, on the same
evidence and for the same reason china does. The four new markets inherit the CPI
cutoff on arrival, exactly as the entry below says any new market would.

#### There is no daily Canadian short rate on FRED, and canada collects a chain

`iv_solve` fails an underlying whose currency has no risk-free rate rather than
discounting it at a number nobody chose, so declaring `canada` with
`collect_chain` meant declaring a CAD rate. Asked on the same run:

| candidate | freq | range | verdict |
|---|---|---|---|
| `IRSTCI01CAM156N` | M | .. **2026-06** | live — **declared** |
| `IR3TIB01CAM156N` | M | .. 2026-06 | live, 3-month interbank rather than overnight |
| `IRSTCB01CAM156N` | M | .. 2023-12 | **dead** |
| `INTGSTCAM193N` | M | .. 2017-04 | **dead** |
| `IR3TCD01CAM156N` | — | — | **does not exist** (HTTP 400) |

`DTB3` and `IUDSOIA`, the declared USD and GBP rates, are DAILY and serve to
2026-09-11 and 2026-09-10. **CAD is the only monthly entry in
`macro.risk_free_rate.series_by_currency`, and it is a sourcing limit rather than
a preference.** It is not a hidden input: `risk_free_rate_as_of` returns the rate
with its own `reference_date` and `stale=True`, so the vintage a canada solve
discounted at is readable off the answer.

## Eurostat serves the same HICP as the ECB, and stops on the same month

**Probed 2026-09-13**, `ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr`,
`coicop=CP00`:

| geo | observations | range | newest value |
|---|---|---|---|
| EA | 348 | 1997-01 .. **2025-12** | 2.0 |
| DE | 348 | 1997-01 .. **2025-12** | 2.0 |
| FR | 348 | 1997-01 .. **2025-12** | 0.7 |
| IT | 348 | 1997-01 .. **2025-12** | 1.2 |
| ES | 348 | 1997-01 .. **2025-12** | 3.0 |

Those are the **same values and the same cutoff month** the ECB Data Portal
returned for `ICP/M.{U2,DE,FR,IT,ES}.N.000000.4.ANR` on the same day. The ECB is
redistributing Eurostat faithfully; it is not lagging its source. **Eurostat is
therefore not a fresher route and not a fix** — a euro-area replacement has to be
a national statistics office.

> **The conclusion held; the inference from it did not.** Eurostat's own API is
> frozen on every unit and every dataset, re-measured 2026-09-15. But the
> replacement was not a national statistics office: it was FRED, which serves the
> same Eurostat statistic current. "The publisher's own API is stale" and "the
> statistic is stale" are different claims, and this entry ran them together.

**The caveat that outranked this entry is now CLOSED — see the Phase 9 section
at the end of this file.** It asked for a re-run from a different egress before
treating the cutoff as a property of the data. That re-run never happened and is
no longer what would settle it: on 2026-09-15 the same two hosts served a
same-day policy rate and a nine-month-old HICP in the same call sequence, which
no path artefact and no shared cache produces. What follows is the original
caveat, kept because the commands under it are still the cheapest reproduction.

```
curl -s 'https://data-api.ecb.europa.eu/service/data/ICP/M.U2.N.000000.4.ANR?startPeriod=2026-01&format=jsondata' | head -c 200
curl -s 'https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr?geo=EA&coicop=CP00&format=JSON' | head -c 200
```

A zero-length body from the first is the ECB's "no observations in this window",
which PR 9 taught the decoder to refuse.

---

## Phase 9, 2026-09-15: the cutoff is real, and FRED is where the euro area lives

Everything below was measured on **2026-09-15** with this build's own keys. The
entries above are left standing; where one is now narrower than it reads, it is
corrected here and named.

### The egress caveat above is closed, and not by a second egress

The entry before this one says both probes ran from a cloud sandbox and cannot
tell *absent at source* from *absent on this path*, and asks for a re-run from
the VM. That re-run has not happened. **It is no longer the question**, because
the same hosts were asked both kinds of question in the same call sequence:

| host | live, on 2026-09-15 | dead, in the same seconds |
|---|---|---|
| `api.stlouisfed.org` | `CPIAUCSL` → **2026-08** | `CPALTT01{JP,DE,GB,CA}M659N` → 2022-04 / 2025-03 / 2025-03 / 2025-03 |
| `data-api.ecb.europa.eu` | `FM/D.U2.EUR.4F.KR.DFR.LEV` → **2026-09-15**, `EXR/D.CAD.EUR.SP00.A` → **2026-09-14** | `ICP/M.{U2,DE,FR,IT,ES}.N.000000.4.ANR` → 2025-12 |

A path that staled data would not serve one host's same-day policy rate beside
the same host's nine-month-old HICP. A shared cache would not hold a series-
specific `realtime_start` per FRED series (2026-07-06 for DE, 2026-09-02 for GB,
2026-08-22 for JP). **The cutoff is at the publishers.** A VM run is still owed
for the backfill's stable-egress item; it is not owed for this.

### The ECB's HICP is dead in both its forms

`ICP/M.U2.N.000000.4.INX` — the INDEX rather than the annual rate — also stops at
**2025-12** (129.54), as does `ICP/M.DE.N.000000.4.INX` (132.8). So the flow is
not serving one representation and withholding another; it has stopped.

### Eurostat's own API is frozen, on every unit

Correcting nothing above and narrowing it: `prc_hicp_midx` is stale the same way
`prc_hicp_manr` is, and the whole dissemination endpoint reports itself stale.

| dataset | unit | newest | `updated` |
|---|---|---|---|
| `prc_hicp_midx` (DE) | `I15` | 2025-12 | 2026-02-06 |
| `prc_hicp_midx` (DE) | `I05` | 2025-12 | 2026-02-06 |
| `prc_hicp_midx` (DE, EA20) | `I25` | **empty response** | 2026-02-06 |
| `prc_hicp_manr` (DE, EA20) | — | 2025-12 | 2026-02-06 |

### FRED serves fresher Eurostat HICP than Eurostat does

This is the finding the phase turned on, and it is odd enough to state plainly.
**FRED release 251, "Harmonized Indices of Consumer Prices (HICP)", sourced from
Eurostat, is current.** Every EU/EEA member and candidate is there; the ids used
are the all-items totals.

| id | newest reference | first published | `last_updated` |
|---|---|---|---|
| `CP0000DEM086NEST` | 2026-07 | 2026-08-19 | 2026-08-19 |
| `CP0000ESM086NEST` | 2026-07 | 2026-08-19 | 2026-08-19 |
| `CP0000FRM086NEST` | 2026-07 | 2026-08-19 | 2026-08-19 |
| `CP0000ITM086NEST` | 2026-07 | 2026-08-19 | 2026-08-19 |
| `CP0000EZCCM086NEST` | 2026-07 | 2026-08-19 | 2026-08-19 |
| `CP0000GBM086NEST` | **2020-11** | — | the UK left the HICP |

`EZCC` is the **changing-composition** euro area, which is what the ECB's `U2`
that it replaces was. `CP0000EZ19M086NEST` and `CP00MI15EA20M086NEST` are equally
current and both fix a membership that has changed twice.

**Cross-validated against the publisher rather than against itself.** Year on
year off the FRED German index gives 2.04% for 2025-12 and 2.55% for 2025-11;
Eurostat's own `prc_hicp_manr` for DE says 2.0 and 2.6.

### ALFRED serves these, once the real-time window is supplied

A bare `output_type=4` answers `400 No vintage dates exist for the specified
real-time period: 2026-09-15 to 2026-09-15` — condition 1 in the fetcher's
docstring, already handled. With bounds, the initial releases are real and
monthly: 2026-07 → 2026-08-19, 2026-06 → 2026-07-17, 2026-05 → 2026-06-17.
`series/vintagedates` holds 165 vintages for DE.

### The rebase, which is the trap in the above

These are index LEVELS and **Eurostat rebased them to 2025=100 during 2026**.
ALFRED's initial-release column therefore holds two bases:

```
ref 2025-12  first released 2026-01-19   132.8    (2015 = 100)
ref 2026-01  first released 2026-04-16   100.6    (2025 = 100)
```

Note also the three-month hiatus: 2026-01, 2026-02 and 2026-03 were all first
published on 2026-04-16, backfilled together.

A year-on-year ratio across that join divides one unit by another and comes out
near **-22%**, on two fresh, release-dated observations. **FRED refuses the
obvious fix**: `units=pc1` with `output_type=4` answers *"If output_type is '3'
or '4', then units must be 'lin'."* On the current vintage `units=pc1` is clean
and correct (2.30, 2.55, 2.04 … 2.84, matching Eurostat), but carries no release
dates. Hence `value_vintage: latest`: the vintage walk supplies the dates, one
plain call supplies values that are all on one base.

### Within FRED release 205, the freeze is partial

`CPALTT01*`, `CPALCY01*`, `CPGRLE01*` and `CPHPTT01*` — the OECD MEI CPI
sub-families — are frozen. `CCRETT01{JP,KR,AU,GB,IL,CN,BR,CA,IN}M661N`, the
CPI-BASED REAL EFFECTIVE EXCHANGE RATES in the same release, serve to **2026-07,
updated 2026-08-17**. Whatever stopped is narrower than "FRED's OECD
redistribution". Searching all of FRED for a fresh monthly CPI outside Eurostat
returns only the IMF WEO `*PCPIPCPPPT` annual series, which run to **2031** and
are therefore projections.

### ONS: the API is gone, the website's JSON is not

* `api.ons.gov.uk/timeseries/D7G7/dataset/mm23/data` → **HTTP 404** with a
  sentence: *"This API has been decommissioned … fully retired on 25/11/2024."*
* `api.beta.ons.gov.uk/v1/datasets/cpih01/editions/time-series/versions` answers,
  and carries an ONS **alert**: *"This page is no longer being updated. Data are
  available up to January 2026 on this page. For more recent Consumer Price Index
  (CPI) data, please use our published tables and time series datasets on the ONS
  website."*
* `www.ons.gov.uk/economy/inflationandpriceindices/timeseries/d7g7/mm23/data` →
  **HTTP 200, `application/json`**, no key, no User-Agent needed (a default
  `curl` and httpx both get 200; `python-urllib` gets **403**, so the route does
  filter on User-Agent and httpx's default passes). 451 monthly observations,
  *CPI ANNUAL RATE 00: ALL ITEMS*, 2026 JUL = 2.9. `d7bt/mm23` is the index form
  of the same series if a level is ever wanted.

`description` carries `unit` (`%`), `releaseDate` and `nextRelease` (*"16
September 2026"*). **Each month carries its own `updateDate`**: 2026 JUL →
`2026-08-18T23:00:00.000Z`, 2025 JUL → `2025-08-19T23:00:00.000Z`, 2022 MAY →
`2022-06-21T23:00:00.000Z`. Everything before 2015-10-12 carries that one date, a
bulk restatement — later than first publication, which is the safe direction.

**The timezone is load-bearing.** `2026-08-18T23:00Z` is 2026-08-19 00:00 in
London, and 2026-08-19 is the Wednesday ONS published it — as is the 2026-09-16
the payload names next. Reading `.date()` off the UTC instant dates every UK CPI
print **one day early**. The fetcher converts to `Europe/London` first, and
`test_an_ons_release_date_is_the_london_day_not_the_utc_one` holds it.

### Statistics Canada WDS: per-observation release times, no key

`POST https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods`
with `[{"vectorId":41690973,"latestN":N}]`. Vector 41690973 is *Canada;All-items*
in product 18100004, `frequencyCode: 6` (monthly), `terminated: 0`.

* 2026-08 = 169.8, `releaseTime` `2026-09-14T08:30`; 2026-07 = 169.9,
  `2026-08-17T08:30`. **59 distinct release times over 164 points** — genuinely
  per observation. The oldest points carry `2021-07-28T08:30`, a basket
  restatement, later than first publication as with the ONS.
* `latestN` accepts at least 400, so a ten-year window is one call.
* `releaseTime` has no offset. It is Eastern; 08:30 Eastern is the same calendar
  day in UTC year-round, so the date part is taken as-is.
* WDS answers **HTTP 200 with a per-vector `status`**, so a rejected vector looks
  like a successful request. The envelope is read before the points.
* `getSeriesInfoFromVector` gives `memberUomCode: 17` and no unit string, so
  these rows are stored with NULL units.

### What was probed and rejected, and exactly why

Every one of these serves a CURRENT CPI. None of them serves a publication date
per observation, which is what `MacroSeries.known_at` needs and what
`release_date_policy: from_api` means.

| route | current? | why it is not usable |
|---|---|---|
| e-Stat (japan), key in `ESTAT_APP_ID` | yes — 2025-base table `0004052037`, updated 2026-08-28 | an observation is `{"@tab","@cat01","@area","@time","$"}`. The only date is `TABLE_INF.UPDATED_DATE`, ONE value for the whole table across all history. Stamping it would assert that thirty years of Japanese CPI was published on 2026-08-28. |
| CBS (israel), `api.cbs.gov.il/index/data/price?id=120010` | yes — 2026-07, `percentYear` 1.5 | no date of any kind beside the value: not a release date, not an update stamp. |
| Eurostat direct | no | frozen, see above |
| ABS (australia) SDMX | not reached — `ABS,CPI_M,1.0.0` is *"Could not find Dataflow and/or DSD"* | SDMX carries no per-observation publication date in any case |
| IBGE SIDRA (brazil), KOSIS/ECOS (korea), MoSPI (india), NBS (china) | — | key-gated or no per-observation publication date |

**The key was never the blocker for Japan.** `ESTAT_APP_ID` works;
`getStatsList` and `getStatsData` both answer. What is missing is a
`release_date_policy` able to express a day-of-month publication rule — Japan's
national CPI lands on the Friday preceding the 26th — which `reference_plus_lag`
cannot, and which this project will not approximate with a day count. That is a
decision of its own, not a series to declare. The key is not added to any env
template until something reads it.
