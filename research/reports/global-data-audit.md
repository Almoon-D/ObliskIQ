# Global data audit: what an adaptive core and a global search need

A desk audit, first pass (owner request, 2026-10-17). It asks which markets, references and
variables the system needs to adapt its allocation and to find opportunities across the world,
and whether the research so far leans on US data because of what the data say or because US
data are easier to get. Availability was probed on 2026-10-17: each source was checked for whether
it answers and over what span. **No return, correlation or other statistic was computed**, so no
later pre-registration is contaminated, and nothing here is evidence for or against a signal
(D64). No IBKR call.

## Rules this audit follows

- **The vendor's country is not the exposure.** FRED, Yahoo, CFTC and AQR are US vendors that
  serve euro, Japanese and global series. A series counts as US-biased when its **economic
  exposure** is the US and the role or the signal does not call for the US.
- **Data follow a consumer** (MASTER_PLAN §5.18, D66). A block or variable is listed when a
  mechanism, a component or a safety rule would read it. Collecting a country "for coverage" is
  not a reason.
- **Redundancy is measured, not assumed.** Measuring it takes statistics, so it is done on a
  development window that later confirmations exclude. That is how Session 4d kept 2001–2007
  for development. This audit states only the prior.
- A source that answers is not a validated series. Before a pre-registration names a source,
  someone checks its adjustments, distributions, continuity, currency, revision policy and how
  closely it matches the vehicle.

## Where the research so far leans on the US

| Place | Series used | Exposure needed | Verdict |
|---|---|---|---|
| 4c, IL role | TIP (US TIPS, USD) | a euro investor's inflation hedge: euro HICP-linked government bonds | **convenience bias**; the prose admits it ("US inflation and the dollar rather than euro inflation") |
| 4c, UST role | IEF | US Treasuries, a role MASTER_PLAN §5.4 names | intended exposure, not bias |
| 4c, GOLD and COMMOD | GLD, CRSOX (US funds) | gold and broad commodities, global by nature | the vendor is in the US but the exposure is global; the problems are proxy quality (fees, active management, start dates), not region |
| 4c, structure check | Ken French US market from 1926 | the trend tilt on a long, multi-market history | **convenience bias**: AQR's *Century of Factor Premia* holds market returns of equity indices, bonds and commodities across countries from 1926-07 (verified below) |
| 4d, H2 and H4 | SEC Form 4, SEC XBRL, US prices | single-name signals anywhere | US only because free point-in-time data exist only there; recorded as not evaluable rather than run on the US alone |
| 4d, H1 | 51 MSCI country indices | country momentum | global; no bias |
| Batch 2, H6 (D71) | the variance risk premium (VIX) and the excess bond premium (Federal Reserve) | the core's risk in euro, US and global markets | **at risk of bias**: both are US measures; a global core also needs euro-area measures |
| Long-history table (owner conversation, 2026-10-17) | PCRIX, VIPSX, VFITX, GC=F | longer history for IL, UST, GOLD, COMMOD | VIPSX repeats the TIP bias for IL; GC=F and PCRIX are vendor-only cases |

## When a region adds information

A region or country belongs in the data when at least one of these holds, measured on the
development window:

1. **Distinct drivers.** The other blocks do not span it: little of its variance is explained by
   them, and that stays so across subperiods.
2. **Breadth.** A cross-sectional signal needs many independent members. H1 used 51 countries.
3. **The investor's own currency.** For a EUR investor, euro rates and euro inflation are core
   inputs, not extras.
4. **Investable under the profile.** A UCITS fund or an eligible ETC exists, at a cost the
   expected benefit pays for (market data, conversion, transaction taxes). Otherwise the region is
   data only (§5.3).
5. **Point-in-time quality.** The series has release dates, or a lag can be declared and
   defended.

Prior to be tested, not assumed:

- At a monthly horizon, developed equity regions are largely spanned by one another for risk
  purposes.
- Japan, emerging Asia, commodity exporters, euro-periphery spreads (2010–12), gold and
  commodities are less spanned.
- Germany, the Netherlands and Austria are largely redundant with one another; Italy and Spain
  are not.

## Blocks an adaptive core needs (risk estimation and allocation)

Daily or monthly returns in EUR, one series per block.

| Block | Why | Source (vendor) | Span verified 2026-10-17 | Cost | Point in time | Gap |
|---|---|---|---|---|---|---|
| Equity: world, EM, 51 countries | growth exposure; country breadth | MSCI end-of-day endpoint | daily from 2000-12-29, the regional indices included (North America, Europe, EMU, Pacific ex Japan, EM Asia, EM Latin America, EM EMEA); monthly or pre-1997 requests refused (`error_code` 100, 300) | free | as observed; restated without vintages | nothing before 2000-12-29 |
| Equity: developed regions, long | development window before 2001 | Ken French data library (Bloomberg-based) | Developed and Europe monthly from 1990-07, Emerging from 1989-07 | free | revised without vintages | a factor-model market return, not an investable index |
| Euro government, core | duration in the base currency | Bundesbank Svensson curve | daily from 1997-08 (in hand) | free | as observed | Germany only |
| Euro government, periphery | the spread risk Bunds miss (2010–12) | ECB Data Portal, `IRS` (convergence 10-year yields) | Italy from 1991-03, Spain from 1991-11, monthly; the all-issuer curve (`G_N_C`, every euro-area government) daily from 2004-09-06 | free | as published | before 2004, monthly 10-year yields only, so returns would have to be approximated from yields and duration (a stated bias) |
| Euro inflation-linked | the IL role for a EUR investor | iShares € Inflation Linked Govt Bond UCITS ETF (`IBCI`) on Yahoo from 2008-01-02 (Amsterdam, Milan, London; Xetra from 2009); before 2008 only candidates: the Bundesbank's monthly price tables of linkers (PDF), Agence France Trésor (OAT€i); HICP index from the ECB (`ICP`, from 1996-01) | IBCI and HICP verified; no series before 2008 found | free | as observed (IBCI) | **the main gap**: a euro proxy exists from 2008 only; before that, the role has a US proxy (a stated bias) or a later start |
| US Treasuries | UST role | FRED H.15 constant-maturity yields | vendor verified; spans not probed | free | latest revision (yields are not revised) | none expected |
| Japanese government bonds | data for risk and regime (not a §5.4 role) | Ministry of Finance, `jgbcme_all.csv` | daily from 1974-09-24 | free | as observed | a role would need an owner decision |
| Gold | GOLD role | GLD (from 2004-11), GC=F continuous futures (from 2000-08); LBMA refused the container (403); the Bundesbank serves only the Frankfurt fixing in DM to 1998 | as stated | free | as observed | LBMA or WGC daily prices need registration and a licence check |
| Broad commodities | COMMOD role | CRSOX (2004-12), PCRIX (2002-07), `^SPGSCI` (first row 1984-01-03 today, variant unknown) | as stated | free | as observed | a total-return index history (BCOM, S&P GSCI) is licensed; GSCI is energy-heavy, BCOM caps sectors |
| FX | conversion into EUR | ECB reference rates (in hand) | from 1999 | free | as observed | BIS effective rates not probed |
| Cash | the cash role | EONIA, then €STR (in hand) | from 1999 | free | as observed | none |

## Variables with a documented mechanism

| Mechanism | Global data | Verified | Cost | US-only trap |
|---|---|---|---|---|
| Trend (time-series momentum) | prices of the blocks above; research evidence in AQR TSMOM (1985, in hand) and *Century of Factor Premia* (1926-07, verified: equity indices, bonds, currencies and commodities, value, momentum, carry, defensive and market columns) | yes | free | none |
| Value across countries and asset classes | AQR *Value and Momentum Everywhere* (from 1972-01, verified: equity-index, bond, currency and commodity value and momentum, long–short); Ken French international portfolios by valuation (not probed); an investable signal needs point-in-time country valuations (CAPE, price to book, dividend yield) | evidence yes; a point-in-time signal **no** | MSCI fundamentals history is paid (price not verified) | Shiller's CAPE is free but US only |
| Carry | bond term spreads from the curves above (free); equity dividend yields (paid with MSCI fundamentals); commodity curves (IBKR live as data; history paid, not verified) | partly | mixed | none |
| Growth and inflation regime | OECD composite leading indicators: 22 areas, the US, Canada, Japan and the UK from 1960, Spain from 1985, China from 1992, India from 1994 (verified) | yes, latest revision only | free | US nowcasts (Atlanta and New York Fed) are free and US only; a backtest needs CLI vintages (OECD's original-release database, not probed) |
| Financial stress | ECB CISS: euro area and US daily from 1980-01 (verified) | yes, latest revision only | free | the excess bond premium is US only; FRED's ICE BofA spreads now hold only three years (euro high-yield OAS from 2023-10-17, verified), so they cannot back a backtest |
| Variance risk premium (H6) | VIX (Cboe); VSTOXX for the euro area; Nikkei VI | VIX yes; `stoxx.com` failed TLS verification from the container (the proxy's CA bundle did not help), Nikkei VI not probed | free (expected) | VIX alone measures the S&P 500 |
| Positioning | CFTC COT: US exchanges, global commodities, FX and index futures (in hand); EU commodity positions under MiFID II (from 2018, not probed) | partly | free | none: the vendor is in the US, the exposure is global |

## Single names outside the US (the satellite)

Free point-in-time data are thin outside the US:

- **Insider dealings.** EU disclosures under MAR article 19 are published regulator by regulator,
  with no common history.
- **Fundamentals.** EU ESEF XBRL starts in 2021; Japan has EDINET.
- **Prices without survivorship bias.** Paid everywhere; Norgate US Platinum is US$630 a year
  (D84).

H2 and H4 stay US-only or not evaluable until the owner decides to buy data. A pre-registered
hypothesis has to come first.

## Long history for structure checks

| Source | Content | Verified |
|---|---|---|
| AQR *Century of Factor Premia* | multi-country markets and premia, monthly from 1926-07 | yes |
| Ken French | US market from 1926 (in hand); developed regions from 1990-07 | yes |
| Jordà–Schularick–Taylor Macrohistory, R6 | 18 countries, annual from 1870 | the download link answers; content not read |
| Dimson–Marsh–Staunton | 35 markets from 1900 | paid; not probed |

## What to gather first, in order

1. **For the static-against-adaptive test of the core** (Session 4e, total-risk scaling,
   DECISIONS D92). The bases' equity roles and the cash role in EUR, and the longest equity
   and cash histories for the long-history check (MSCI from 2000, Ken French's developed
   regions from 1990, AQR from 1926). The IL role's proxy if a base holds IL: a euro series
   from 2008, or a US one before it with its bias stated. The euro periphery (all-issuer curve
   from 2004) if a base holds euro bonds. The equity regions (MSCI, from 2000), if a
   pre-registration estimates risk by region rather than by world and EM.
2. **For a regime-conditioned core** (H6 made global). CISS, OECD CLI with vintages, term
   spreads from the curves, VSTOXX beside VIX.
3. **Country valuations.** Bought only if a pre-registered hypothesis needs them.
4. **Not now.** Single-name data outside the US.

## To verify before a pre-registration names a source

- Euro linker prices before 2008 (AFT, the Bundesbank's tables).
- The variant of `^SPGSCI`.
- OECD CLI vintages.
- VSTOXX (from a host the container can verify) and Nikkei VI histories.
- BIS effective exchange rates.
- For each, its adjustments, continuity, currency and how closely it matches the vehicle.
