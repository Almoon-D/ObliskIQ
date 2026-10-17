# Selection data audit: H1, H2 and H4

Session 4d, Step 1 (DECISIONS D71, D84). Measured on 2026-10-17. This audit decides which
selection hypothesis is pre-registered and tested; it computes no effect of any signal and is not
evidence for or against one (D64). No IBKR call.

## The choice

| | H2 insider-purchase clusters | H4 stock-level composite | H1 country momentum through UCITS |
|---|---|---|---|
| Point-in-time source | SEC insider transaction data sets, by filing date | prices: none free without survivorship; fundamentals: SEC XBRL, by filing date | MSCI end-of-day country indices, as observed |
| Span | 2006Q1 to 2026Q3 | XBRL from 2009Q2, every filer from mid-2011 | 29 Dec 2000 to 16 Oct 2026 |
| Lost to survivorship, by year | 74.5 % (2006) falling to 11.6 % (2025) of events | 76.5 % (2006) falling to 12.5 % (2025) of sampled issuers | 4.1 % (2001–2005), 2.0–2.9 % (2006–2021), none from 2023 |
| Independent observations | about 1,030 effective events over 42 half-years | one observation a month, 183 months | one observation a month: 224 in the confirmation sample |
| Effect detectable at power 0.8 | 3.6 % abnormal return over six months | 3.7 % to 7.5 % a year | 2.5 % to 3.4 % a year |
| Adequate | **no** | **no** | **yes** |

Among the candidates with adequate data the preference is H2, then H4, then H1 (D71). H2 and H4
fail the survivorship criterion in every year that matters; H1 passes it. **H1 is chosen**, tested
as country momentum, with H5 (the same signal with and without a volatility-scaled stop) beside it.
H2 and H4 are recorded as not evaluable with free data, with the data that would make them
evaluable and its cost (below), for Session 21 or the owner.

## The criteria, fixed before measuring

A hypothesis has adequate data when all of these hold (D62's adequacy checks, made concrete):

1. **Point in time.** Every input carries the date it became public: a filing's acceptance date,
   an index level's calculation day, or a declared publication lag. A value restated later is not
   read as if it had been known.
2. **Survivorship.** The share of the point-in-time universe (the events, or the members on each
   date) without usable outcome data is at most **5 % in every year** of the sample. The bias a
   lost share m can cause is about m times the gap between the lost names' outcomes and the rest;
   stocks that delist on performance lose about 30 % in their delisting month alone (Shumway
   1997), so 5 % already allows a bias of about 1.5 points, of the order of the effects tested.
   A name counts as lost when no history exists today, when the history starts after the date,
   or when the history under its symbol is another security (its unadjusted close more than
   about 22 % away from the price the record states).
3. **Coverage.** The signal's inputs exist for the universe it ranks.
4. **Span.** At least ten years, so the sample holds a development period and a confirmation
   period with two subperiods for the sign check (D63).

Power is reported beside these and is not a criterion (D62): low power makes a test inconclusive,
not inadequate.

## H2: insider-purchase clusters (SEC Form 4)

**Source.** The SEC's insider transaction data sets: 83 quarterly files, 2006Q1 to 2026Q3, every
Form 3, 4 and 5 as filed, with its filing date (SHA-256 of the sorted file hashes
`4bbd1e86ffd50aa8fc0aae6b9c5116634912901d2c331e25fcfd5939d91569ef`). Point in time: yes, a
cluster becomes public on the filing date of the filing that completes it. The structured sets
start in 2006; EDGAR holds the XML from mid-2003, which would add three years.

**Events.** 802,018 open-market purchase rows; 622,932 by officers and directors on original
Forms 4. A cluster is the first filing date on which at least three distinct officers or
directors of one issuer have reported open-market purchases traded in the 30 days before it, at
most one per issuer in 182 days (the audit's counting rule; a pre-registration would fix its
own): **17,257 events in 6,262 issuers**, 441 to 1,621 a year, in every one of the 249 months
the data sets cover.

**Survivorship, measured.** For every event, Yahoo's daily history was looked up under the symbol
the filing reported and under the issuer's current ticker (the SEC's list), and accepted only if
its unadjusted close around the trade dates was within about 22 % of the insiders' average price
(a whole split factor away counted as covered). 9,202 symbols were asked for; 4,762 had a history.

| Year | Events | Lost | 95 % interval | No history | Wrong security or price | Lost if Tiingo's listed tickers were recovered |
|---|---|---|---|---|---|---|
| 2006 | 929 | 74.5% | 71.6% to 77.2% | 71.4% | 3.1% | 53.4% |
| 2007 | 1,248 | 72.0% | 69.4% to 74.4% | 69.5% | 2.5% | 47.7% |
| 2008 | 1,621 | 70.6% | 68.4% to 72.8% | 68.1% | 2.5% | 40.9% |
| 2009 | 964 | 64.3% | 61.2% to 67.3% | 61.6% | 2.7% | 35.0% |
| 2010 | 759 | 66.3% | 62.8% to 69.5% | 64.0% | 2.2% | 32.9% |
| 2011 | 1,000 | 61.0% | 57.9% to 64.0% | 58.8% | 2.2% | 26.2% |
| 2012 | 790 | 58.6% | 55.1% to 62.0% | 56.5% | 2.2% | 24.2% |
| 2013 | 653 | 60.6% | 56.8% to 64.3% | 55.7% | 4.9% | 22.8% |
| 2014 | 795 | 56.4% | 52.9% to 59.8% | 53.1% | 3.3% | 15.8% |
| 2015 | 909 | 54.6% | 51.3% to 57.8% | 50.9% | 3.6% | 17.4% |
| 2016 | 716 | 53.2% | 49.6% to 56.8% | 49.6% | 3.6% | 14.2% |
| 2017 | 637 | 50.2% | 46.4% to 54.1% | 46.9% | 3.3% | 16.8% |
| 2018 | 776 | 44.3% | 40.9% to 47.8% | 41.0% | 3.4% | 14.0% |
| 2019 | 776 | 43.6% | 40.1% to 47.1% | 39.4% | 4.1% | 15.1% |
| 2020 | 981 | 40.8% | 37.7% to 43.9% | 31.0% | 9.8% | 8.4% |
| 2021 | 714 | 42.2% | 38.6% to 45.8% | 32.6% | 9.5% | 11.3% |
| 2022 | 736 | 26.8% | 23.7% to 30.1% | 22.6% | 4.2% | 7.7% |
| 2023 | 687 | 23.1% | 20.1% to 26.4% | 18.8% | 4.4% | 5.4% |
| 2024 | 529 | 17.4% | 14.4% to 20.9% | 11.3% | 6.0% | 5.1% |
| 2025 | 596 | 11.6% | 9.3% to 14.4% | 6.0% | 5.5% | 3.5% |
| 2026 | 441 | 13.8% | 10.9% to 17.4% | 3.6% | 10.2% | 3.9% |

- The loss falls with time because Yahoo keeps listed companies and drops the ones that left:
  three quarters of the 2006 events are gone. Even the 2025 and 2026 events lose 12–14 %: Yahoo
  lacks some over-the-counter issuers, and a purchase coded "P" can be a private placement at a
  negotiated price, which the identity check then rejects. Either way the outcome is missing.
- Tiingo's public ticker list names 4,949 of the 8,933 lost events with a span over the filing
  date (unverified identity). Even if every one were recovered, the loss stays above 5 % in
  every year to 2022.
- **Not adequate**: above 5 % in every year. The lost events are not random — they are the
  companies that later failed, were acquired or went dark — so the survivors' abnormal return is
  biased in a direction the data cannot tell.

**Independent observations and power.** Six-month abnormal returns of sampled stocks (log return
over SPY, 4,318 non-overlapping observations, measured on the H4 sample in the year after its
trade, never on an event window) have a standard deviation of 41.7 % and correlate on average by
0.038 among stocks of the same half-year. With events clustered by half-year, the 17,257 events
carry about 1,028 effective observations (965 for the matched ones alone); with a second test (H5)
in the confirmation run, the effect detectable at power 0.8 is **3.6 % over six months** (3.8 % on
the matched events). The literature's estimates for cluster purchases are of that order, so H2
would have useful power if its data were complete.

## H4: a stock-level composite (52-week high, 12-1 momentum with a crash guard, profitability)

**Sources.** Prices: the same free source as H2 and the same survivorship. Fundamentals: the
SEC's XBRL filings, point in time through the Financial Statement Data Sets (each filing's own
date), from 2009Q2 for the largest filers and for every filer from fiscal periods after
15 June 2011; nothing free and point in time before 2009. Gross profit (Novy-Marx's
profitability) is reported, or computable as revenue minus cost of revenue, by 38 % of the filers
with total assets in 2010 and 50 % in 2024 (the XBRL frames, which hold the last value filed and
so are not point in time; banks, insurers and funds report no cost of goods).

**Survivorship, measured.** The universe on a date is approximated by the issuers with at least
one priced open-market trade in a Form 4 that year (4,000 to 6,100 a year). A seeded random
sample of 200 issuers a year was looked up as for H2:

| Year | Sampled issuers | Lost | 95 % interval |
|---|---|---|---|
| 2006 | 200 | 76.5% | 70.2% to 81.8% |
| 2007 | 200 | 72.0% | 65.4% to 77.8% |
| 2008 | 200 | 62.5% | 55.6% to 68.9% |
| 2009 | 200 | 61.5% | 54.6% to 68.0% |
| 2010 | 200 | 58.5% | 51.6% to 65.1% |
| 2011 | 200 | 57.5% | 50.6% to 64.1% |
| 2012 | 200 | 58.0% | 51.1% to 64.6% |
| 2013 | 200 | 53.0% | 46.1% to 59.8% |
| 2014 | 200 | 48.0% | 41.2% to 54.9% |
| 2015 | 200 | 40.5% | 33.9% to 47.4% |
| 2016 | 200 | 42.0% | 35.4% to 48.9% |
| 2017 | 200 | 43.5% | 36.8% to 50.4% |
| 2018 | 200 | 43.0% | 36.3% to 49.9% |
| 2019 | 200 | 35.0% | 28.7% to 41.8% |
| 2020 | 200 | 32.0% | 25.9% to 38.8% |
| 2021 | 200 | 35.0% | 28.7% to 41.8% |
| 2022 | 200 | 26.5% | 20.9% to 33.0% |
| 2023 | 200 | 18.5% | 13.7% to 24.5% |
| 2024 | 200 | 15.5% | 11.1% to 21.2% |
| 2025 | 200 | 12.5% | 8.6% to 17.8% |
| 2026 | 200 | 2.5% | 1.1% to 5.7% |

- **Not adequate**: above 5 % in every year with a full year of history, and the lost names are
  the ones a momentum and 52-week-high signal ranks at the bottom before they delist, which is
  where its crash guard and its spread come from. Free published portfolios that are
  survivorship-free (the Ken French library's momentum and operating-profitability sorts, Chen and
  Zimmermann's open-source signals) test single components, not the pre-registered composite,
  and hold none of its 52-week-high leg in Ken French's case.
- **Power** (if the data existed): one Fama–MacBeth observation a month for 183 months from July
  2011; for a top group whose monthly excess over the universe has a standard deviation of
  1.5 % to 3 % (not measured: no survivorship-free panel exists to measure it on), the effect
  detectable at power 0.8 is **3.7 % to 7.5 % a year**.

## H1: country momentum through UCITS

**Source.** MSCI's end-of-day standard (large and mid cap) country indices, net total return in
EUR, as observed and restated without vintages. The universe on each month end is the countries
in MSCI ACWI then (developed and emerging), from MSCI's market classification history: 49
countries in 2001, 44 to 50 since, 53 in all. MSCI's endpoint serves 51 of them, from 29 December
2000 (Qatar, the United Arab Emirates and Kuwait from 1 June 2005, Saudi Arabia from 1 September
2014, all before they joined); all 51 loaded through the harness's MSCI loader on 2026-10-17
(their datasets are configured with the cross-section harness).

**Survivorship, measured.** Venezuela (a member until May 2006) and Russia (until March 2022) are
not served under any code; MSCI's search lists only a "Russia Value" index that serves nothing.
Share of the month's members not served, the mean over each year's month ends:

| Years | Lost |
|---|---|
| 2001–2005 | 4.1% |
| 2006 | 2.9% |
| 2007–2021 | 2.0% to 2.2% |
| 2022 | 0.5% |
| 2023–2026 | 0.0% |

- **Adequate**: at most 4.1 % (two of 49 members) on any date. Russia's exit is the one large
  loss the test cannot see: MSCI removed it from MSCI ACWI in March 2022 at a price close to zero;
  a top group that held it then would have lost that weight. Its 12-1 momentum at the end of
  February 2022 is not known from these data, so the pre-registration names Russia and the report
  states the share.
- **The cross-asset half of H1.** Through UCITS the candidates are government bonds of a few
  markets (the free daily curves are Germany's, the United States', the United Kingdom's and
  Japan's) and commodity ETCs, whose long histories are US funds from 2004 to 2011 or spot prices
  without the futures' roll. Four bond markets and six to nine commodities are too few for a
  cross-section, and ranking asset classes against one another is the core's trend tilt, which
  Session 4c tested. H1 is therefore tested on country equity, where the breadth is.

**Independent observations and power.** One Fama–MacBeth observation a month: 224 in a
confirmation sample from February 2008 to September 2026, beside 72 in a development sample from
2002 to 2007. Country returns correlate on average by 0.43, so the 47 members of a month carry
about 2.3 independent observations, and their monthly returns spread by 4.9 % across countries.
A random third of the members, against all of them, has a monthly standard deviation of 1.09 %
(2,000 draws): the floor for a top group without a signal's tilt. At that floor the effect
detectable at power 0.8 is **2.5 % a year**; at 1.5 %, the figure a top group with momentum's tilt
is declared at, **3.4 % a year**.

## What would make H2 and H4 evaluable, and its cost

Both need daily US stock prices that keep delisted companies, with their delisting returns, from
2006 (and, for H4, point-in-time fundamentals, which the SEC's XBRL sets give for free from 2009).
Read on 2026-10-17; buying any of them is the owner's decision (AGENTS.md stop conditions).

| Source | Delisted companies | Cost |
|---|---|---|
| CRSP through WRDS | yes, with delisting returns, from 1925 | institutional subscription |
| Norgate Data, US Platinum | yes, from 1990 | US$630 for 12 months |
| Sharadar (Nasdaq Data Link) | yes, prices from 1998 | behind a login; a third party quotes US$9–69 a month for personal use (unverified) |
| EODHD, EOD Historical Data | listed as a feature | about US$18–20 a month; depth of delisted US coverage not confirmed |
| Tiingo | its public list names delisted tickers, 95 % of those that ended before 2026 in 2014 or later | an API key; free-plan symbol limits not confirmed |

None of them records the delisting return everywhere CRSP does; a source without it would still
leave the last month of a failed company unmeasured. Until the owner buys one, H2 and H4 are
`idea` entries in `research/pattern_registry.yaml`, and H2's events can be collected forward from
the SEC (Session 10), where survivorship cannot arise.

## Method and reproduction

The scripts are in `research/explore/selection_audit/` (exploration, D64; their README gives the
order). Inputs: the SEC files above (hash above), the SEC's ticker list, Tiingo's ticker list,
Yahoo's histories as served on 2026-10-17 (Yahoo drops delisted symbols over time, so a later
run measures more loss for the same events) and MSCI's levels and index search as served on
2026-10-17. Sampling seed 20261017. Only standard deviations and correlations were computed from
returns, never on an event window or a signal's ranking: no effect of any hypothesis was looked
at before its pre-registration.

## What this audit cannot conclude

- Anything about the size or sign of any hypothesis's effect.
- That H2 or H4 lack an effect: they are not evaluable with free data, which says nothing about
  them.
- The exact loss under another source: the matching rule (symbol, current ticker, price within
  about 22 %) can reject a covered event (a private placement at a negotiated price, a wrong split
  adjustment) or, rarely, accept a reused symbol whose price happens to match.
