# Research data fixtures

Downloads the research loaders parse, recorded on 2026-10-16 from each vendor's public endpoint.
A fixture is evidence about our parsing, never about the vendor's format: where a later download
and a fixture disagree, the download wins and the fixture is re-recorded. None carries a key or
any personal data; the ALFRED request's API key is not part of its answer.

| File | Source | Recorded | Trimmed |
|---|---|---|---|
| `yahoo_spy.csv` | `yfinance` 1.7.0 `Ticker("SPY").history(start="2024-01-01", end="2024-04-01", interval="1d", auto_adjust=False, actions=True)`, the frame written with `to_csv()` | 2026-10-16 | no (one dividend, 15 March 2024) |
| `yahoo_eunl_de.csv` | the same for `EUNL.DE` (iShares Core MSCI World UCITS ETF on Xetra, accumulating) | 2026-10-16 | no |
| `ken_french_ff3_factors.zip` | `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip` ("created using the 202608 CRSP database") | 2026-10-16 | no |
| `aqr_tsmom_factors_monthly.xlsx` | `https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets/Time-Series-Momentum-Factors-Monthly.xlsx` | 2026-10-16 | the data sheet cut after row 42 (24 months from January 1985); embedded images and printer settings emptied; every other part unchanged |
| `fred_unrate.csv` | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE&cosd=2023-10-01&coed=2024-03-01` | 2026-10-16 | by the request's own dates |
| `alfred_unrate_vintages.json` | `https://api.stlouisfed.org/fred/series/observations?series_id=UNRATE&file_type=json&observation_start=2023-10-01&observation_end=2024-03-01&realtime_start=2023-11-01&realtime_end=2024-06-30` | 2026-10-16 | by the request's own dates |
| `ecb_eurofxref_hist.zip` | `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip` | 2026-10-16 | rows from 2023-12-20 to 2024-04-05 kept, header and column order unchanged, re-zipped |
