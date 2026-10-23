# The sources besides IBKR

Observed behaviour of the macro, positioning, insider, event, news, halt and earnings sources
`config/sources.yaml` declares, and of the LLM provider `config/llm.yaml` names: what each
endpoint answered, with where and when. Condensed from ObliskIQ-lite-MCP's
`references/macro-playbook.md` (legacy/lite, @b1ebd49), keeping every verified finding, plus what
Session 10 measured. The values the code uses live in `config/sources.yaml`; why they were chosen
is `docs/DECISIONS.md` (D124–D129).

Tags follow `docs/knowledge/README.md`: `[<source> ✓ <date>]` read live on that date,
`[lite ✓ <date>]` read live by lite, `[DOC <date>]` documentation only, `VERIFY-LIVE(<what>)`
built against fixtures and queued in `docs/ROADMAP.md`. Session 10's readings were taken from a
cloud session, whose egress is shared with other users.

## Status, at a glance

Every built source but GDELT collected live on 2026-10-23 into a fresh store, one
`obliskiq sources run <source>` each (the halt feed was read by hand):

| Source | Key | Area | Rows on 2026-10-23 |
|---|---|---|---|
| FRED and ALFRED | `FRED_API_KEY` | macro | 33,595 |
| ECB Data Portal | — | macro | 33,598 |
| EIA v2 | `EIA_API_KEY` | macro | 521 |
| ONS website | — | macro | 119 |
| Statistics Canada WDS | — | macro | 119 |
| Bank of Japan | — | macro | 2,574 |
| BIS Data Portal | — | macro | 36,436 |
| Federal Reserve Board (EBP) | — | macro | 240 |
| CFTC (Socrata) | — | positioning | 11,935 reports (2006 on, 11 contracts) |
| SEC EDGAR Form 4 | `SEC_USER_AGENT` | insiders | 0 (no Form 4 of a followed issuer in the ten days read) |
| Polymarket Gamma | — | events | 178 markets |
| Kalshi | — | events | 91 markets |
| GDELT DOC 2.0 | — | events | none: HTTP 429 on this egress (below) |
| RSS and Atom feeds | `SEC_USER_AGENT` for SEC | news | 15 items within three days of publication, two of them Santander 6-Ks read with their filing |
| Yahoo earnings (yfinance) | — | calendar | 90 dates; LISN and JDEP none |
| Nasdaq Trader halts | — | halts | 18 halts listed, none of a universe instrument |

## FRED and ALFRED

Only `series/observations` with `output_type=4` yields a publication date; the plain call returns
none, and a `from_api` series is never stored without one.

- **The real-time window is required, and today is not one.** Without `realtime_start` and
  `realtime_end` FRED defaults the window to today and fails: `HTTP 400 No vintage dates exist
  for the specified real-time period: <today> to <today>` (DFF, SOFR, DGS2, DGS10, CPIAUCSL)
  [lite ✓ 2026-09-04]. A vintage is a release day, so most days have none.
- **The window is capped at 2,000 vintage dates**, not a span: `There are 5101 vintage dates …
  This exceeds the maximum number of vintage dates allowed for this file type (2000)`. DGS10 has
  about 250 a year, so ten years exceed it; monthly series never do. The fetcher splits the window
  by the series' frequency against `max_vintages_per_call` [lite ✓ 2026-09-04]; DGS10 over ten
  years in three windows matched a single five-year call on 1,302 of 1,303 rows.
- **A window with no vintage in it answers about the series**: `The series does not exist in
  ALFRED but may exist in FRED` (DHHNGSP before its first vintage, 2014-12-10). The fetcher asks
  `series/vintagedates` and clamps the window to them. SOFR (2019-03-29), IUDSOIA (2021-07-28),
  DCOILWTICO and DCOILBRENTEU (2011-04-06), DHHNGSP and PCOPPUSDM (2015-11-06) start later than
  the window [lite ✓ 2026-09-09]. `output_type=4` serves nothing before a series' second vintage;
  that history has no first-publication date anywhere and is reported as a shortfall.
- **Retired series** answer `The series does not exist`: IRLTLT01CNM156N (China) and
  IRLTLT01INM156N (India) [lite ✓ 2026-09-14].
- **The OECD's CPI redistribution is frozen.** `CPALTT01*` (and `CPALCY01*`, `CPGRLE01*`,
  `CPHPTT01*`) stop between 2022-04 (Japan) and 2025-04; the real effective exchange rates of the
  same release (`CCRETT01*`) are current [lite ✓ 2026-09-15]. Outside the US, no market has a CPI
  print newer than 2025 from FRED's OECD series.
- **Eurostat's HICP is current on FRED** (release 251: `CP0000{DE,ES,FR,IT}M086NEST`,
  `CP0000EZCCM086NEST`, the changing-composition euro area) while Eurostat's own API and the
  ECB's `ICP` flow stop at 2025-12. The indexes were **rebased to 2025 = 100 during 2026**:
  ALFRED's initial releases hold two bases (2025-12 at 132.8, 2026-01 at 100.6), and FRED refuses
  `units=pc1` with `output_type=4`. Hence `value_vintage: latest`: the vintage walk supplies the
  dates, one plain call values on one base [lite ✓ 2026-09-15]. 2026-01 to 2026-03 were first
  published together on 2026-04-16.
- **Canada has no daily short rate on FRED**: IRSTCI01CAM156N (monthly) is the live one;
  IRSTCB01CAM156N and INTGSTCAM193N are dead [lite ✓ 2026-09-14].

## SDMX (the ECB, the BIS)

- **SDMX-JSON 1.0 and 2.0 differ**: the ECB answers 1.0 (`header`, `dataSets`, `structure`); the
  OECD answered 2.0 (`meta`, `data.dataSets`, `data.structures` as a list). A decoder reading one
  shape finds no data set in the other and must refuse rather than return nothing
  [lite ✓ 2026-09].
- **The time dimension is `TIME_PERIOD` or `TIME`, matched exactly**: `startswith("TIME")` picked
  the OECD's `TIME_HORIZ` first [lite ✓ 2026-09].
- **A key that leaves a dimension empty addresses several series** (the OECD CLI key returned 23
  countries in one flat list); it is refused with the dimension named.
- **The ECB's empty window is HTTP 200 with a zero-length body**, refused as no observations. The
  ECB serves no publication date: its series state their own rule (`same_as_reference` for the
  policy rate and the reference rates, `unknown` otherwise). Its `ICP` HICP flow stopped at
  2025-12 in both `.ANR` and `.INX` forms while its policy rate and FX are current
  [lite ✓ 2026-09-15].
- **The BIS Data Portal** serves CSV with an `UNIT_MEASURE` code per series (368 for policy
  rates, 770 for credit gaps, 882 for effective exchange rates), which the units guard compares
  [BIS ✓ 2026-10-23]. Credit-to-GDP gaps run about three quarters behind (2026-Q1 the newest on
  2026-10-23, hence a 290-day lag); monthly broad effective rates for September were out by
  2026-10-23 (52 days). Both are revised in place.

## EIA, ONS, Statistics Canada

- **EIA v2** (`WCESTUS1`, US crude stocks, weekly): the week ends on Friday and is published at
  10:30 Eastern the Wednesday after; after a Monday holiday on Thursday at noon; the week ending
  2025-12-19 on Monday 2025-12-29, ten days later (the Weekly Petroleum Status Report's release
  schedule) [EIA ✓ 2026-10-23]. Lite's lag of 3 days dated every week before its release; the lag
  is 10.
- **ONS**: the API was retired on 2024-11-25 (`api.ons.gov.uk` answers 404 with that sentence);
  the website's time-series JSON (`www.ons.gov.uk/…/timeseries/d7g7/mm23/data`) answers without a
  key. Each month carries its own `updateDate`, a last-updated stamp no earlier than publication.
  **The timezone is load-bearing**: `2026-08-18T23:00Z` is 19 August in London, the day ONS
  published, so the date is read in `Europe/London`. Python's urllib user agent gets 403; httpx's
  passes [lite ✓ 2026-09-15].
- **StatCan WDS** (`getDataFromVectorsAndLatestNPeriods`): a `releaseTime` per observation
  (Eastern, 08:30, so the date is the same in UTC); HTTP 200 with a per-vector `status`, read
  before the points; no unit string [lite ✓ 2026-09-15].

## Bank of Japan, Federal Reserve Board

- **The BoJ Time-Series API** (`stat-search.boj.or.jp/api/v1/getDataCode`) pages with
  `NEXTPOSITION` and `startPosition` and serves one last-update date per series, never per
  observation. **Its database holds no JGB yields** [BoJ ✓ 2026-10-23]: the Ministry of Finance
  publishes them, declared as `mof_japan`; the macro board reads the monthly ten-year yield
  through FRED. USDJPY (`FXERD01`) and the monetary base (`MABS1AN11`) are collected.
- **The excess bond premium** (Gilchrist and Zakrajšek, `ebp_csv.csv`): September 2026 was
  published on 2026-10-06 (the file's Last-Modified), 35 days after the month's first day; the
  lag is 45 days. Each update re-estimates the whole history [FRB ✓ 2026-10-23].

## Japan's and others' CPI: probed and rejected

e-Stat (Japan) carries one update date per table across all its history; Israel's CBS carries
none; Eurostat is frozen; the ABS dataflow was not found; IBGE, KOSIS/ECOS, MoSPI and NBS are key-
gated or undated [lite ✓ 2026-09-15]. None has a defensible per-observation release date, and a
day-of-month rule (Japan's CPI the Friday before the 26th) is not expressible as a fixed lag.

## CFTC

- **Socrata** (`publicreporting.cftc.gov/resource/6dca-aqww.json`, legacy futures-only): 1,085
  weekly reports per contract from 2006 [CFTC ✓ 2026-10-23]. Contract codes are the dataset's
  `cftc_contract_market_code`; the 30-year bond is `020601`.
- **The annual archives** (`files/dea/history/deacot{year}.zip`, one `annual.txt` each) refuse
  httpx's default user agent with **HTTP 403**; a named agent (`sources.user_agent`) passes. The
  2026 archive held 451 reports of the registry's contracts [CFTC ✓ 2026-10-23].
- **Release dates**: Tuesday positions, published the Friday after at 15:30 Eastern; the 2025
  government shutdown delayed the October–November reports, which the fixed lag dates early
  (Follow-ups).

## SEC EDGAR

- **Every request carries `SEC_USER_AGENT`**; the fair-access limit is ten requests a second.
- **The daily form index** (`daily-index/{year}/QTR{q}/form.YYYYMMDD.idx`) is absent on weekends,
  holidays and before EDGAR posts it in the US evening. Absence is **HTTP 403 with an XML
  `<Code>AccessDenied</Code>`** from its storage, not a 404; a 403 HTML page refusing the client
  is different and is a failure [EDGAR ✓ 2026-10-23].
- **A Form 4 submission** (`<accession>.txt`) is EDGAR's SGML header (`FILED AS OF DATE`,
  `ACCEPTANCE-DATETIME` in New York time) then the `ownershipDocument` XML; a price left to a
  footnote is absent, not zero.
- **Company feeds** (`browse-edgar?action=getcompany&CIK=…&type=8-K&output=atom`) list filings
  with metadata only (form, items, accession, a link to the `-index.htm`), no text. The press
  release is in the full submission: an 8-K's `EX-99.1`, a 6-K's own documents (Cameco's 6-K of
  2026-10-01 carried an `EX-99.1` too) [EDGAR ✓ 2026-10-23]. A 6-K without an exhibit carries
  its release in its main document after the form's cover page (Santander's of 21 and 22
  October), so the cover's boilerplate opens the text read. An 8-K's full submission can be a
  megabyte with its graphics.
- **The issuers' investor-relations pages** refused or lacked a feed at the usual paths (403 at
  investors.fcx.com, investors.mosaicco.com, asml.com; 404 at Albemarle, Bunge, Cameco, Nutrien,
  TotalEnergies, Santander) [web ✓ 2026-10-23].

## Prediction markets

- **Polymarket Gamma** (`gamma-api.polymarket.com/events?slug=…`): a list of one event, or `[]`
  for an unknown or ended slug. A market carries `outcomes` and `outcomePrices` as **JSON lists
  encoded in strings** (`'["Yes", "No"]'`, `'["0.065", "0.935"]'`), `bestBid`, `bestAsk`,
  `lastTradePrice`, `volumeNum`, `liquidityNum`, `endDate`, `closed`, `active`. Event slugs often
  end in a creation stamp (`fed-decision-in-december-20260729232808632`). Tag discovery:
  `events?tag_slug=<tag>&closed=false&limit=<n>` [Polymarket ✓ 2026-10-23].
- **Kalshi** (`api.elections.kalshi.com/trade-api/v2/markets?series_ticker=…&status=open`): prices
  as strings in dollars (`yes_bid_dollars`, `yes_ask_dollars`, `last_price_dollars`), volume as
  `volume_fp`, a `cursor` to page; a listed market's `status` reads `active`. The recession series
  is `KXRECSSNBER`; `RECSSNBER` has no open market [Kalshi ✓ 2026-10-23].

## GDELT DOC 2.0

- **Timelines**: `mode=timelinevolraw` gives hourly `value` (matching articles) and `norm` (all
  articles monitored); `mode=timelinetone` hourly `value` (mean tone); `date` as
  `YYYYMMDDTHHMMSSZ`; `timespan=7d` starts mid-day seven days back [GDELT ✓ 2026-10-23].
- **Omitted hours**: both timelines leave out the same hours for every query (17 October: 1,
  18 October: 3, 19 October: 1), a gap in GDELT's data rather than hours without a match; an
  omitted hour is left out of both totals.
- **Rate limit**: `HTTP 429 Please limit requests to one every 5 seconds`. From this cloud
  session's shared egress it answered 429 to most requests even 75 to 240 seconds apart, and a
  run with 12 s spacing and two 60 s backoffs collected nothing. VERIFY-LIVE(GDELT collection from
  the deployment machine).

## News feeds

| Feed | Format | Date field | Observed 2026-10-23 |
|---|---|---|---|
| Federal Reserve press releases | RSS 2.0, byte-order mark, CDATA links | `pubDate` (GMT) | 20 items |
| ECB press | RSS 2.0, one line | `pubDate` (+0200) | 15 items, titles only |
| Bank of England news | RSS 2.0 with an Atom namespace | `pubDate` | 50 items |
| Bank of Japan what's new | RSS 2.0 | `pubDate` (+0900) | 51 items, empty descriptions |
| SNB press releases | RSS 2.0 | `pubDate` | 20 items |
| Bank of Canada press releases | RSS 1.0 (RDF), `rdf:about` as id | `dc:date` | 10 items |
| RBA media releases | RSS 1.0 (RDF) | `dc:date` (+1100) | 1 item |
| SEC press releases | RSS 2.0, a UUID `guid` | `pubDate` | 25 items |
| EDGAR company feeds (8-K, 6-K) | Atom | `updated` | 40 entries each, metadata only |

None declared a DTD. ESMA's feeds (`esma.europa.eu/rss.xml`, its news feed) answered 403 to the
container [web ✓ 2026-10-23], so the regulator read is the SEC.

## Nasdaq Trader trade halts

`www.nasdaqtrader.com/rss.aspx?feed=tradehalts`: RSS 2.0 with a byte-order mark and `ndaq:` fields
per item (`HaltDate` MM/DD/YYYY, `HaltTime` with or without milliseconds, `IssueSymbol`,
`IssueName`, `Market`, `ReasonCode`, `ResumptionDate`, `ResumptionQuoteTime`,
`ResumptionTradeTime`), times in New York [Nasdaq ✓ 2026-10-23]. It listed 18 halts: 16 `NASDAQ`
and 2 `AMEX` (NYSE American); reason codes T12, T1, H11, H10; no resumption shown; the oldest a
Sinovac halt of 2019-02-22 still in force. **No `NYSE` listing appeared**:
VERIFY-LIVE(an NYSE-listed halt in the feed, with its `Market` code).

## Yahoo earnings dates

`yfinance.Ticker(symbol).get_earnings_dates(limit=n)` returns a frame indexed by report instants
in `America/New_York` (FCX: 2026-10-27 08:00 EDT, the next one; reported EPS beside the past
ones); it returned more rows than the limit asked. `LISN.SW` and `JDEP.AS` had none ("may be
delisted") [Yahoo ✓ 2026-10-23]. Research-grade: an upcoming date is Yahoo's estimate.

## The LLM provider

Gemini's OpenAI-compatible endpoint (`generativelanguage.googleapis.com/v1beta/openai`,
`/chat/completions`, bearer key, `response_format` of type `json_schema`) and
`gemini-3.1-flash-lite`'s paid-tier price (US$0.25 input, US$1.50 output per million tokens) were
read in Google's documentation [DOC 2026-10-23]. No key was available:
VERIFY-LIVE(the first extraction: the reply's shape, `usage`, and a schema the endpoint accepts).

## Catalyst calendars

The decision days in `catalysts.events` were read on the pages each names (the Fed's FOMC
calendar, the ECB's monetary policy meetings, the BoE's MPC dates, the BoJ's monetary policy
meetings, the BLS release schedules for CPI and employment) on 2026-10-23.
