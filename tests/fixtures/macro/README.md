# Recorded macro responses

A fixture is evidence about this build's parsing, never about the publisher's format; observed
behaviour lives in `docs/knowledge/macro-sources.md`.

Ported from lite (`legacy/lite/tests/fixtures/macro`, captured live on **2026-09-15**, history
trimmed):

| file | route |
|---|---|
| `ons_d7g7_mm23.json` | `GET https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/d7g7/mm23/data`: CPI annual rate, `description` verbatim, the last 30 `months` |
| `statcan_v41690973.json` | `POST https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods`, body `[{"vectorId":41690973,"latestN":30}]`: CPI all-items Canada |
| `fred_hicp_de_initial_release.json` | `GET .../fred/series/observations?series_id=CP0000DEM086NEST&output_type=4&realtime_start=2000-01-01&realtime_end=2026-09-15&observation_start=2024-11-01` |
| `fred_hicp_de_latest_vintage.json` | the same window with no `output_type` and no real-time bounds |
| `fred_hicp_de_series.json` | `GET .../fred/series?series_id=CP0000DEM086NEST` |

The two FRED observation files are the same series over the same months and disagree: Eurostat
rebased HICP to 2025=100 during 2026, so the initial releases change base between 2025-12 and
2026-01 while the latest vintage is on one base.

Captured live on **2026-10-23** by Session 10 (no key in any file):

| file | route |
|---|---|
| `ecb_dfr.json` | `GET https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV?format=jsondata&startPeriod=2026-10-15&detail=dataonly` |
| `eia_wcestus1.json` | `GET https://api.eia.gov/v2/petroleum/stoc/wstk/data/?data[0]=value&facets[series][]=WCESTUS1&start=2026-09-25&sort...` (the key removed from the echoed request) |
| `boj_fxerd01.json` | `GET https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=en&db=FM08&code=FXERD01&startDate=202610` (gzip-encoded on the wire) |
| `boj_mabs1an11.json` | the same with `db=MD01&code=MABS1AN11&startDate=202606` |
| `bis_cbpol_jp.csv` | `GET https://stats.bis.org/api/v1/data/WS_CBPOL/D.JP?startPeriod=2026-10-14&format=csv`: three rows, the long `COMPILATION` and `SUPP_INFO_BREAKS` texts cut |
| `bis_credit_gap_us.csv` | `GET https://stats.bis.org/api/v1/data/WS_CREDIT_GAP/Q.US.P.A.C?startPeriod=2025-01&format=csv` |
| `frb_ebp.csv` | `GET https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv`: the header and the last four months |
