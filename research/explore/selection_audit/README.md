# Selection data audit (Session 4d, Step 1)

The scripts behind `research/reports/selection-data-audit.md`. They are exploration (DECISIONS
D64): they measure the data each selection hypothesis would stand on and never compute an effect
of any signal. Run them in order, each with the same output directory, outside the repository:

1. Download every quarterly `*_form345.zip` listed on the SEC's "Insider Transactions Data
   Sets" page, and `company_tickers_exchange.json`, with `SEC_USER_AGENT` as the User-Agent
   (the SEC's fair-access rule); put Tiingo's public `supported_tickers.csv` beside them.
2. `form4.py <zip dir> <out dir>`: open-market purchases by officers and directors on original
   Forms 4, and one priced open-market trade per issuer and year.
3. `events.py <out dir>`: insider-purchase clusters (three insiders within 30 days, one event per
   issuer per 182 days).
4. `yahoo.py <out dir>`: Yahoo daily histories for every event's and every sampled issuer-year's
   symbols and the issuers' current tickers (about 9,200 symbols; Yahoo throttles, so it waits
   and resumes from what it kept).
5. `coverage.py <out dir>`: the share of events and of sampled issuer-years with no matching
   history, by year, and the dispersion the power calculation reads.
6. `msci.py <out dir>`: which point-in-time MSCI ACWI countries MSCI's endpoint serves, and the
   dispersion of their monthly returns.
7. `power.py <out dir>`: independent observations and detectable effects.

Yahoo removes delisted symbols over time, so a later run measures a larger loss for the same
events: the report states the day it was measured.
