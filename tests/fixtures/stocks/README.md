# Stock collector fixtures

Answers the stock collector parses (`src/obliskiq/data/stocks/`), recorded on 2026-11-17 from
the cloud container with a declared User-Agent for the SEC (its value is not part of any answer).
A fixture is evidence about our parsing, never about the source's format: where a later download
disagrees, the download wins and the fixture is re-recorded. None carries a key or personal data.

| File | Source | Trimmed |
|---|---|---|
| `sec_company_tickers_exchange.json` | `https://www.sec.gov/files/company_tickers_exchange.json` (10,400 rows) | the rows of nine companies (Apple, NVIDIA, Microsoft, Alphabet, Berkshire Hathaway, Walmart, JPMorgan, Bank of America, the SPDR S&P 500 trust) with every ticker each lists, and one row each of an OTC, a Cboe, a blank-exchange, a `-P`, a `-UN`, a `-WT` and a Nasdaq `W` listing; fields and order unchanged |
| `sec_frame_eps_cy2025.json` | `https://data.sec.gov/api/xbrl/frames/us-gaap/EarningsPerShareDiluted/USD-per-shares/CY2025.json` (5,081 points) | the rows of the nine companies above; `pts` set to the rows kept |
| `sec_frame_gross_profit_cy2025.json` | `.../frames/us-gaap/GrossProfit/USD/CY2025.json` | the same |
| `sec_frame_assets_cy2026q3i.json` | `.../frames/us-gaap/Assets/USD/CY2026Q3I.json` | the same |
| `sec_frame_absent.xml` | `.../frames/us-gaap/GrossProfit/USD/CY2027.json`: HTTP 404, a frame not built yet | no |
| `sec_submissions_aapl.json`, `sec_submissions_spy.json` | `https://data.sec.gov/submissions/CIK0000320193.json` and `CIK0000884394.json` | the top-level `cik`, `entityType`, `sic`, `sicDescription`, `name`, `tickers` and `exchanges` only |
| `yahoo_aapl.csv` | `yfinance` 1.7.0 `Ticker("AAPL").history(start="2026-11-03", end="2026-11-18", interval="1d", auto_adjust=False, actions=True)`, the frame written with `to_csv()` | no (a dividend on 2026-11-12; the 17th read after the close) |
| `yahoo_nvda_split.csv` | the same for `NVDA` from 2024-06-03 to 2024-06-14 | no (the ten-for-one split of 2024-06-10) |
| `yahoo_twtr_empty.csv` | the same for `TWTR` from 2026-11-03: Yahoo answers an empty frame | no |
