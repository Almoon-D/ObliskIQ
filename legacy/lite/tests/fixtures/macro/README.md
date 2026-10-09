# Recorded macro responses

Captured live on **2026-09-15**, unedited except for trimming the history to
keep the files readable. What each one is, and the exact route it came from:

| file | route |
|---|---|
| `ons_d7g7_mm23.json` | `GET https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/d7g7/mm23/data` — CPI ANNUAL RATE 00: ALL ITEMS. `description` verbatim, last 30 `months`. |
| `statcan_v41690973.json` | `POST https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods`, body `[{"vectorId":41690973,"latestN":30}]` — CPI all-items Canada. |
| `fred_hicp_de_initial_release.json` | `GET .../fred/series/observations?series_id=CP0000DEM086NEST&output_type=4&realtime_start=2000-01-01&realtime_end=2026-09-15&observation_start=2024-11-01` |
| `fred_hicp_de_latest_vintage.json` | the same window with no `output_type` and no real-time bounds |
| `fred_hicp_de_series.json` | `GET .../fred/series?series_id=CP0000DEM086NEST` |

The two FRED files are **the same series over the same months** and they
disagree, which is the point of keeping both: Eurostat rebased HICP to 2025=100
during 2026, so the initial-release file changes base between 2025-12 and
2026-01 while the latest-vintage file is on one base throughout. That pair is
what `test_macro_pull.py` reads to hold the rebase regression.

A fixture is evidence about this build's parsing, never about the publisher's
format. `references/macro-playbook.md` is where the format findings live.
