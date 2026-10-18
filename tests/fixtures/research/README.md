# Research data fixtures

Downloads the research loaders parse, recorded from each vendor's public endpoint on the date each
row names.
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
| `msci_world_netr_eur.json` | `https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?currency_symbol=EUR&index_variant=NETR&start_date=20240101&end_date=20240131&data_frequency=DAILY&index_codes=990100` (MSCI World, net total return, EUR) | 2026-10-17 | by the request's own dates |
| `msci_error.json` | the same request with `start_date=19691231`: the endpoint's refusal of a start before 1997 | 2026-10-17 | no |
| `ecb_eonia.csv` | `https://data-api.ecb.europa.eu/service/data/EON/D.EONIA_TO.RATE?format=csvdata&startPeriod=2021-12-20&endPeriod=2021-12-31` | 2026-10-17 | by the request's own dates |
| `ecb_estr.csv` | `https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?format=csvdata&startPeriod=2022-01-01&endPeriod=2022-01-14` | 2026-10-17 | by the request's own dates |
| `bundesbank_r05.csv`, `bundesbank_r04.csv` | `https://api.statistiken.bundesbank.de/rest/data/BBSIS/D.I.ZST.ZI.EUR.S1311.B.A604.R05XX.R.A.A._Z._Z.A?format=csv&lang=en&startPeriod=2024-01-01&endPeriod=2024-01-31` and the same for `R04XX` (zero-coupon yields of German Federal securities, 5 and 4 years) | 2026-10-17 | by the request's own dates |
| `ken_french_developed_daily.zip` | `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Developed_3_Factors_Daily_CSV.zip` ("created using the 202608 Bloomberg database") | 2026-10-18 | the CSV keeps its header lines, the first ten rows (from 1990-07-02) and the last five (to 2026-08-31), re-zipped under its own name |
| `aqr_century_factor_premia_monthly.xlsx` | `https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets/Century-of-Factor-Premia-Monthly.xlsx` | 2026-10-18 | the data sheet cut to rows 1–24 (five months from July 1926) and two of the empty-string rows below the table (1216, 1217); embedded images and printer settings emptied; every other part unchanged |
