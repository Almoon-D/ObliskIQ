# Research data sources

What the research loaders (`src/obliskiq/research/data.py`) read from each vendor, as seen when
the fixtures in `tests/fixtures/research/` were recorded. The endpoints are configuration
(`config/defaults/research.yaml`); this file records their observed formats and what they do
not promise.

| Vendor | Observed | Point in time |
|---|---|---|
| Yahoo through `yfinance` 1.7.0 | `Ticker(...).history(auto_adjust=False, actions=True)` returns a frame indexed by the session's timestamp at midnight in the exchange's zone (`Europe/Berlin` for `EUNL.DE`, `America/New_York` for `SPY`), with `Open`, `High`, `Low`, `Close`, `Adj Close`, `Volume`, `Dividends`, `Stock Splits`, `Capital Gains`; `Close` is split-adjusted, `Adj Close` also dividend-adjusted; an accumulating fund's two closes are equal. A direct `curl` to Yahoo's chart endpoint answered HTTP 429; `yfinance` warned that its crumb fetch was rate limited and still served the history `[Yahoo ✓ 2026-10-16]` | as observed (the close of its session); not point in time across splits or dividends, which re-scale history backwards |
| Ken French data library | a zip holding one CSV: free-text header lines, then tables that each open with a row starting with a comma (`,Mkt-RF,SMB,HML,RF`) and end at a blank line; the first is monthly (`YYYYMM`), the second annual (`YYYY`); values in percent; the file names the CRSP vintage it was built from ("created using the 202608 CRSP database"); missing values are `-99.99` or `-999` in the portfolio files `[Dartmouth ✓ 2026-10-16]` | revised without vintages; availability is a declared lag |
| AQR data sets | an `.xlsx` workbook; the data sheet (`TSMOM Factors`) has notes above a header row (row 18), Excel serial dates in column A (1900 system: 31078 is 1985-01-31) and decimal excess returns; other sheets hold definitions and disclosures `[AQR ✓ 2026-10-16]` | revised without vintages; availability is a declared lag |
| FRED | `fredgraph.csv?id=<series>`: header `observation_date,<series>`, one row per observation, `.` for a missing value; the latest revision only `[FRED ✓ 2026-10-16]` | a declared lag after the observation date |
| ALFRED | `fred/series/observations` with `realtime_start` and `realtime_end` (and `FRED_API_KEY`): one row per observation and vintage with `date`, `value`, `realtime_start`, `realtime_end`; UNRATE for October 2023 was first published at 3.9 on 2023-11-03 and revised to 3.8 on 2024-01-05. The answer does not echo the key `[ALFRED ✓ 2026-10-16]` | vintages: each value public from its `realtime_start` |
| ECB reference rates | `eurofxref-hist.zip`: one CSV, newest date first, a trailing comma on every row, units of each currency per euro, `N/A` for a currency not fixed (legacy currencies); no row on TARGET holidays (Good Friday and Easter Monday 2024 are absent) `[ECB ✓ 2026-10-16]` | as observed (published around 16:00 CET on its day) |
