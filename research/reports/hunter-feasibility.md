# Hunter feasibility: what to buy, for how much, to decide what

Session 23 (DECISIONS D150, D154). Measured and read on 2026-11-13. **Recommendation: do not
buy a price history for the Hunter's test now.** No test in the fixed sequence reaches a
probability of a sufficient outcome of 0.5: the first, the composite H12 on the whole universe,
has at most 0.009 (0.015 on the free-fundamentals span), because the net effect the literature
supports for it, from −0.8 % to +0.8 % a year after publication decay, costs and Spanish tax,
lies below θ = 2 %, while a sufficient outcome at even odds needs 3.6 % to 4.8 % a year. Every
later test, the tiers and H2, H3, H4, H10 and H11 alone, stays at or below 0.04. If the owner buys
anyway, the cheapest history that would make the sequence evaluable is **Sharadar's Core US
Equities Bundle, full history, US$499 a year** (prices from December 1997 with delisted
companies, point-in-time fundamentals from January 1998, a personal licence, an API a cloud
session can read); Norgate US Platinum (US$630 a year, Windows only) is the alternative. The
next cheapest route to evidence costs nothing: the thesis book's forward record (H13, locked)
and H2's insider events collected forward from the SEC, where survivorship cannot arise; both
are slow and decide only a large effect. This report computes no return of any candidate and is
not evidence for or against one (D64).

## The decision rule, fixed before any number

- **The run rule** (D154, `research.decision.min_sufficient_probability`). A test is registered
  only if its probability of a sufficient outcome (L ≥ θ, D62), before data and at the net effect
  the literature supports after publication decay, costs and tax, is at least **0.5**. θ is the
  smallest effect worth having and is never raised, or lowered, to meet it.
- **The sequence** (D154). H12 on the whole universe; then H12 in each size tier (micro, small,
  mid, large); then H2, H3, H4, H10 and H11 each alone. A test runs only if the one before it
  was sufficient, so none carries a multiplicity adjustment: each is one test at α = 0.05.
- **The purchase.** If the first test does not reach 0.5, the recommendation is not to buy, and
  the report names the next cheapest route.

## The sequence before data

The effect is the book's annualised after-tax log growth minus that of its own universe bought
and held: long-only, a broad book, costs at each tier's own cost tier (`costs.friction.equity`),
the `es` tax. θ = 2 % a year for every test, the theta of the last top group against its
universe (H1, `research/preregistrations/selection-country-momentum.md`): a top group adds
tracking error, turnover and model risk, and below two points the excess is inside the
uncertainty of the cost model itself. Months: the Sharadar span as one confirmation sample
(January 1999 to September 2026, 332 months; a specification taken as published needs no
development sample, §5.9) and, beside it, the free XBRL span (July 2011 on, 183 months).

| # | Test | Supported net effect a year: central / generous | P(sufficient), central / generous | Power at θ | Detectable at 0.8 | Effect for P = 0.5 |
|---|---|---|---|---|---|---|
| 1 | H12 composite, whole universe | −0.77 % / +0.79 % | 0.001 / 0.009 | 0.32–0.68 | 2.4–4.3 % | 3.6–4.8 % |
| 2 | H12, micro caps | −1.01 % / +1.76 % | 0.000 / 0.037 | 0.32–0.68 | 2.4–4.3 % | 3.6–4.8 % |
| 3 | H12, small caps | −1.01 % / +1.76 % | 0.000 / 0.037 | 0.32–0.68 | 2.4–4.3 % | 3.6–4.8 % |
| 4 | H12, mid caps | −1.01 % / +0.60 % | 0.000 / 0.007 | 0.32–0.68 | 2.4–4.3 % | 3.6–4.8 % |
| 5 | H12, large caps | +0.34 % / +1.73 % | 0.005 / 0.036 | 0.32–0.68 | 2.4–4.3 % | 3.6–4.8 % |
| 6 | H2 insider-purchase clusters | −1.85 % / −0.27 % | 0.000 / 0.004 | 0.22–0.32 | 4.3–5.7 % | 4.8–5.8 % |
| 7 | H3 spin-offs and index deletions | −1.09 % / +1.48 % | 0.001 / 0.030 | 0.22–0.32 | 4.3–5.7 % | 4.8–5.8 % |
| 8 | H4 52-week high, 12-1 momentum, profitability | −1.90 % / +0.17 % | 0.000 / 0.003 | 0.32–0.68 | 2.4–4.3 % | 3.6–4.8 % |
| 9 | H10 cheapness with quality | −0.35 % / +0.89 % | 0.001 / 0.011 | 0.32–0.68 | 2.4–4.3 % | 3.6–4.8 % |
| 10 | H11 post-earnings drift | −7.38 % / −5.90 % | 0.000 / 0.000 | 0.22–0.32 | 4.3–5.7 % | 4.8–5.8 % |

Ranges run over the tracking errors below (the larger probability is shown); 332 months. On
the free XBRL span (183 months) every probability rises a little with the wider interval and
none exceeds 0.041; the effect for P = 0.5 rises to 4.1–5.8 % (5.8–7.1 % for the event books).
The probability of a sufficient outcome is Φ((μ − θ)/se − z₀.₉₅): when the supported effect μ
is below θ it is below α = 0.05 whatever the tracking error, which is why the verdict does not
hang on the dispersion assumed. The probability that the whole sequence reaches a test is the
product of the probabilities before it, at most 0.009 for the second.

**The verdict under the rule:** the first test is not registered, so no test is, and the
recommendation is not to buy. `obliskiq research power` reports this column for any
cross-section pre-registration that states its supported effect (`supported_effect`), and names
each test below the floor.

## How the supported effects are built

Net effect = gross long-short premium in the source's own sample × what survives publication ×
½ − turnover × round-trip cost − tax. Central and generous differ in the decay and, for the
composites, the gross premium. `research/explore/hunter_feasibility/power.py` holds the inputs.

- **Gross premia, in-sample.** The published factor returns of the Fama and French data library
  (5 factors 2×3 and momentum, read 2026-11-13), July 1963 to December 1991, the sample before
  the value, size and momentum papers: value (HML) 4.53 % a year, profitability (RMW) 2.48 %,
  investment (CMA) 3.51 %, momentum 10.09 %. H12 averages value, quality and momentum standing
  for industry momentum (Moskowitz and Grinblatt 1999 find industry momentum accounts for much
  of stock momentum; abstract read 2026-11-13 through OpenAlex): 5.70 %; generous 8 %, as if the
  intersection added to its parts, and 1.5 times that in micro and small caps, where anomalies
  concentrate (Hou, Xue and Zhang 2020: with microcaps mitigated, 65 % of 452 anomalies fail
  |t| ≥ 1.96; McLean and Pontiff 2016: returns are higher in stocks with high idiosyncratic risk
  and low liquidity). H10: value and profitability, 3.51 %; generous 6 % (Piotroski 2000 and
  Greenblatt 2005 report larger in-sample spreads; not re-read this session). H4: momentum,
  the 52-week high (George and Hwang 2004: it dominates past returns; abstract read) and
  profitability, 7.55 %; generous 10 %. H2: Cohen, Malloy and Pomorski 2012, "value-weight
  abnormal returns of 82 basis points per month" for opportunistic insider trades (abstract
  read), 9.84 % a year. H3: no figure re-read this session (Cusatis, Miles and Woolridge 1993's
  page did not resolve); central 0, generous 8 %. H11: Martineau 2022, "For large stocks, PEAD
  have been non-existent since 2006 but has only disappeared recently for microcap stocks"
  (abstract read): central 0, generous 4 %.
- **Publication decay.** McLean and Pontiff 2016: "Portfolio returns are 26 % lower out-of-sample
  and 58 % lower post-publication" (abstract read). Central keeps 42 %, generous 74 %.
- **Long only.** A top group against the whole universe holds half of a long-short spread whose
  return is linear in the score's rank: × ½.
- **Costs**, from `config/defaults/costs.yaml` (labelled assumptions, D63): a round trip costs
  0.4 % in large caps (≥ US$10 billion, the liquid tier) and 2.0 % below; 1.73 % for the whole
  universe, whose names are 17 % large. One-way turnover a year from each holding period: H12
  90 % (annual fundamentals, a six-month industry trend), H10 50 %, H3 50 %, H4 175 %, H2 200 %
  (six-month holds), H11 400 % (sixty-day holds). Novy-Marx and Velikov 2016: most anomalies
  with less than 50 % turnover a month keep significant net spreads when designed against
  costs, few with more do (abstract read through a summary). Commissions (below) and impact are
  left out, which favours every test.
- **Tax** (`config/jurisdictions/es.yaml`, the 21 % band). The excess is taxed (× 0.79 when
  positive), and gains the book realises early lose the deferral the universe bought and held
  keeps: 0.45 % a year at full turnover on 7 % equity growth over fifteen years, scaled by
  turnover up to one.

**Tracking error** (the dispersion of the monthly series). Against its own universe, a broad
book's tracking error is half a long-short factor's volatility plus the idiosyncratic risk left
in its names. The same library's long-short volatilities, July 1963 to September 2025: value
10.3 %, profitability 7.7 %, investment 7.1 %, size 10.5 %, momentum 14.5 % a year; half of a
composite's is 3.5–5 %. With 40 to 80 names, firm-level volatility (which rose relative to the
market's, Campbell, Lettau, Malkiel and Xu 2001, abstract read; its level is not re-read here)
leaves several points more. Assumed: 5 % to 9 % a year for the stock books, 9 % to 12 % for the
event books (H2, H3, H11), which hold fewer names at a time. D154's 10 % to 14 % was the same
book against the global equity reference, which adds the size and country tilt. No dispersion
or correlation was estimated on candidate data.

## Vendors: US daily prices that keep delisted companies

Read on each vendor's own pages on 2026-11-13. None publishes a delisting return as CRSP does;
the best give the last traded price and a delisting date and reason, so a failed company's last
move after its final trade stays unmeasured (D84).

| Vendor | Price, one private user | Span | Delisted | Down in size | Format | Licence | Where it is read |
|---|---|---|---|---|---|---|---|
| **Sharadar** Core US Equities Bundle | US$69 a month or **US$499 a year** full history; prices alone US$39 / US$299 (sharadar.com/subscribe) | prices from December 1997; fundamentals from January 1998, as reported, indexed to the filing date (sharadar.com/prices, /fundamentals, /docs/fundamentals) | "active and delisted securities", 21,000 stock tickers; an actions table with delisting dates and reasons, `isdelisted`, `lastpricedate` (sharadar.com/prices, /docs/actions, /docs/tickers) | NYSE, Nasdaq, NYSE American; OTC not stated | REST API and bulk compressed CSV (`years=full`) | personal, non-professional; no redistribution; delete within 30 days of the end, derived results kept (sharadar.com/terms) | a cloud session (an HTTPS API; the host must be allowed and the key given as a credential) or the owner's machine |
| **Norgate Data** US Stocks Platinum | US$346.50 for six months, **US$630 a year**; Diamond (from 1950) US$787.50; current OTC add-on US$90 (norgatedata.com/stockmarketpackages.php, /prices.php) | from 1990 | at Platinum or Diamond only: securities that traded on a major exchange; OTC-only never-listed excluded; delisted symbols carry a year-month suffix (norgatedata.com/data-content-tables.php) | NYSE, Nasdaq, NYSE American; formerly listed OTC names | the Norgate Data Updater, a Windows application with a proprietary database; Python on Windows; TXT/CSV export of prices (norgatedata.com/ndu-overview.php, /accessibility.php) | one natural person, personal use, two computers, any processing environment; no refunds; delete on expiry, derived data kept (EULA dated 2026-11-07, norgatedata.com/subscribe/eula.php); a three-week Platinum trial with two years of history (norgatedata.com/freetrial.php) | the owner's machine (Windows host) |
| **EODHD** EOD Historical All-World | US$19.99 a month, US$199 a year (eodhd.com/pricing) | US from 1962 | delisted symbols listed and served; before 2018 prices only; no delisting date in the list (eodhd.com/financial-apis/delisted-stock-companies-data-2) | PINK and OTCBB symbols mentioned | API, CSV or JSON; bulk endpoint | personal use (eodhd.com/financial-apis/commercial-vs-personal-license-use) | a cloud session or the owner's machine |
| **Massive** (formerly Polygon) | Starter US$29 (5 years), Developer US$79 (10 years), Advanced US$199 a month (20+ years) (massive.com/stocks) | 5 to 20+ years by plan | "delisted tickers keep their full history"; OTC included | OTC included | API and daily flat files | individual, non-professional | a cloud session or the owner's machine |
| **FirstRate Data** | not found on its pages | January 2000 | "over 7000 delisted tickers" (firstratedata.com/stock-data) | OTC venue codes listed | CSV download | private use, perpetual (firstratedata.com/about/license) | either |
| **Tiingo** Power | US$30 a month, US$300 a year (tiingo.com/about/pricing) | 30+ years | its public ticker list names delisted tickers (Session 4d); serving them not stated in the docs | not stated | REST, CSV | internal use only | either |
| **CRSP** | not published; sold to "academic institutions, government agencies, and investment practitioners" (indexes.morningstar.com/research-data-products/crsp-us-stock-databases) | from 1925 | yes, with delisting returns | listed stocks | WRDS, Snowflake, file transfer | institutional | not available to one private user |

Databento keeps "8+ years" of US equities at US$199 a month (databento.com/catalog/us-equities);
delisted symbols not stated. QuantConnect's dataset pages did not render.

**Other markets the account trades.** Norgate serves Australia (delisted from 1992) and Canada
(from 1990) at AUD or CAD 630 a year; it names no European market (norgatedata.com). EODHD
serves Xetra, the LSE and Euronext, but no page states that delisted European companies are
kept. No vendor read here offers survivorship-free European daily prices to a private user, so
a European tier would need its own data audit (§5.5) and has no source yet.

**Option-surface history for H8.** HistoricalOptionData.com: 2002 to date, US$1,150 (level 1),
US$1,495 with greeks, US$2,035 with IV-surface metrics, one-off; CSV (historicaloptiondata.com).
DiscountOptionData: 2005 to 2026, US$245, US$295 with greeks and IV, 4,552 symbols, about 32 GB
of CSV (discountoptiondata.com). ORATS: from 2007, US$599 one-off backfill (about 500 GB) and
US$99 a month for an individual licence, with greeks and IV (orats.com/near-eod-data). Cboe
DataShop's Option EOD Summary starts in January 2012, price not listed
(datashop.cboe.com/option-eod-summary); OptionMetrics IvyDB US (from 1996) and iVolatility (from
2005) publish no price; ThetaData sells 6 to 14 years at US$40 to US$160 a month, personal use
(thetadata.net/pricing). H8 tests an option against a share for a sufficient signal, so it waits
for one: none of this is worth buying before the Hunter or the thesis book has one.

## Free point-in-time fundamentals and events (SEC)

Measured from EDGAR's quarterly full indexes (every filing with its form, filer and filing
date) and the SEC's submissions API, the way Session 4d measured the insider data.

| | Span | Coverage measured |
|---|---|---|
| XBRL financial statements (Financial Statement Data Sets, each filing dated) | from 2009Q1 (sec.gov financial statement data sets page) | filers of periodic reports with XBRL: 5.1 % in 2009, 16.2 % in 2010, 82.5 % in 2011, 87–93 % since 2012 (the rest are mostly asset-backed and other filers without the obligation); of today's listed common stocks, 96.6 % of micro, 94.5 % of small, 98.9 % of mid and 99.2 % of large caps filed a periodic report with XBRL in the last four quarters |
| SIC industry, point in time (H12's industries) | with every filing | carried by each submission |
| Form 4 insider transactions (H2) | data sets from 2006Q1, XML on EDGAR from mid-2003 | 17,257 clusters in 6,262 issuers, 441 to 1,621 a year (Session 4d) |
| Form 8-K, item 2.02 (earnings release, H11) | item codes from August 2004 | of the periodic reports of a seeded sample of 200 filers a year, preceded within 60 days by an item 2.02: 63.8 % (2006), 44.9 % (2011), 55.0 % (2016), 60.2 % (2021), 64.3 % (2025); the rest are mostly filers that never issue a release, whose report's own filing date is the point-in-time date |
| Form 10-12B (spin-off registrations, H3) | counted from 2001 | 8 to 45 original registrations a year, 2001–2026 (19 in 2026 to date); index deletions have no free dated source |

H3's events are too few for a book of 20 to 80 names to hold spin-offs alone: at 20 to 40 a
year, a book fills only with holds of two to three years, which is why its tracking error is
the event books'.

## The universe floors computed from the account

US common stocks listed on 2026-11-13 (Nasdaq's screener: Nasdaq, NYSE, NYSE American; OTC left
out), with a market capitalisation and twenty sessions of Yahoo bars: **4,832**, of which micro
(below US$300 million) 1,790, small (to US$2 billion) 1,237, mid (to US$10 billion, the cost
model's liquid bound) 995, large 810. The bounds are the usual ones; Session 24's registration
may use NYSE percentile breakpoints instead. EUR/USD 1.1028.

A stock passes when (a) the position is at most 1 % of its median daily traded value over twenty
sessions (`thesis.liquidity`, the same judgement as the thesis book), and (b) one leg's
commission on IBKR's fixed US schedule (US$0.005 a share, US$1 minimum, 1 % cap) is at most the
spread the cost model charges that leg in its tier (0.1 % liquid, 0.5 % otherwise): the
commission-derived minimum (D66). The Hunter holds 50 % of managed capital on a sufficient test,
up to all of it later (D149, D155).

| Managed capital | Hunter share | Names | Position | Daily value floor | Commission at the US$1 minimum | Pass: micro / small / mid / large | All |
|---|---|---|---|---|---|---|---|
| €10,000 | 50 % | 20 | US$276 | US$27,600 | 0.36 % | 1,100 / 1,200 / 879 / 0 | 3,179 |
| €10,000 | 50 % | 40 | US$138 | US$13,800 | 0.73 % | 0 / 0 / 0 / 0 | 0 |
| €10,000 | 50 % | 80 | US$69 | US$6,900 | 1.45 % | 0 / 0 / 0 / 0 | 0 |
| €10,000 | 100 % | 20 | US$551 | US$55,100 | 0.18 % | 961 / 1,214 / 988 / 0 | 3,163 |
| €10,000 | 100 % | 40 | US$276 | US$27,600 | 0.36 % | 1,100 / 1,200 / 879 / 0 | 3,179 |
| €10,000 | 100 % | 80 | US$138 | US$13,800 | 0.73 % | 0 / 0 / 0 / 0 | 0 |
| €50,000 | 50 % | 20 | US$1,378 | US$137,900 | 0.07 % | 765 / 1,206 / 991 / 779 | 3,741 |
| €50,000 | 50 % | 40 | US$689 | US$68,900 | 0.15 % | 920 / 1,212 / 989 / 0 | 3,121 |
| €50,000 | 50 % | 80 | US$345 | US$34,500 | 0.29 % | 1,059 / 1,213 / 961 / 0 | 3,233 |
| €50,000 | 100 % | 20 | US$2,757 | US$275,700 | 0.04 % | 616 / 1,196 / 992 / 805 | 3,609 |
| €50,000 | 100 % | 40 | US$1,378 | US$137,900 | 0.07 % | 765 / 1,206 / 991 / 779 | 3,741 |
| €50,000 | 100 % | 80 | US$689 | US$68,900 | 0.15 % | 920 / 1,212 / 989 / 0 | 3,121 |
| €250,000 | 50 % | 20 | US$6,892 | US$689,300 | 0.01 % | 400 / 1,174 / 993 / 806 | 3,373 |
| €250,000 | 50 % | 40 | US$3,446 | US$344,600 | 0.03 % | 556 / 1,192 / 992 / 806 | 3,546 |
| €250,000 | 50 % | 80 | US$1,723 | US$172,300 | 0.06 % | 721 / 1,203 / 991 / 798 | 3,713 |
| €250,000 | 100 % | 20 | US$13,785 | US$1,378,500 | 0.01 % | 247 / 1,103 / 988 / 806 | 3,144 |
| €250,000 | 100 % | 40 | US$6,892 | US$689,300 | 0.01 % | 400 / 1,174 / 993 / 806 | 3,373 |
| €250,000 | 100 % | 80 | US$3,446 | US$344,600 | 0.03 % | 556 / 1,192 / 992 / 806 | 3,546 |

- Liquidity binds on the smallest names only: the micro tier shrinks from about 1,100 names to
  250 as positions grow, while small, mid and large caps barely change.
- The commission binds at small positions. Below US$200 the US$1 minimum exceeds 0.5 % of the
  position and nothing passes (€10,000 in 40 or 80 names); below US$1,000 it exceeds a large
  cap's 0.1 % spread and large caps drop out. At the tier's median price the per-share rate adds
  little except in micro caps (about 0.1 % a leg at positions of US$3,000 and more).
- A spot conversion from euros precedes each purchase (§2); its cost is outside this table.

## What the owner is asked

One question, in `docs/ROADMAP.md` Follow-ups: buy Sharadar's Core US Equities Bundle, full
history, at US$499 a year, for the Hunter's test: yes or no? The recommendation is no. A yes
would buy a test whose rule (D154) says it should not be registered; Session 24 then has nothing
to run and waits.

## Method and reproduction

The scripts are in `research/explore/hunter_feasibility/` (exploration, D64; its README gives
the order), their summaries in its `results/`. Inputs: Nasdaq's stock screener and Yahoo's daily
bars as served on 2026-11-13 (traded value only, close times volume); EDGAR's full indexes,
2001Q1 to 2026Q4 to date, and the submissions API (seed 20261113); the SEC's ticker list; the
Fama and French data library as served on 2026-11-13 (factor means before 1992 and volatilities
only); paper abstracts read through OpenAlex and Semantic Scholar on 2026-11-13. Vendor pages were
read on 2026-11-13 through a fetching tool; a price or a term is to be checked on the vendor's
page before paying.

## What this report cannot conclude

- That any hypothesis lacks an effect: the literature's figures, decayed and net of costs and
  tax, put the expected effect below θ; a single history could still show more, which is what
  a probability of 0.01 to 0.04 means.
- The exact figures behind H3's and H10's generous cases, and the level of firm-level
  volatility, which were not re-read this session; the verdict does not hang on them, since a
  supported effect below θ leaves the probability below 0.05 at any dispersion.
- How a vendor's history measures against survivorship: that is the adequacy check of Session
  24's loader (at most 5 % lost a year, D84), run only on a bought history.
