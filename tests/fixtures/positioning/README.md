# Recorded CFTC answers

A fixture is evidence about this build's parsing, never about the CFTC's format; observed
behaviour lives in `docs/knowledge/macro-sources.md`.

| file | origin |
|---|---|
| `cot_annual_sample.csv` | Ported from the old ObliskIQ (`legacy/old/ObliskIQ/tests/fixtures/cot_annual_sample.csv`): rows of the legacy futures-only annual archive `deacot2024.zip` (`annual.txt`), the header trimmed to the columns read. The tests zip it as the archive is served. |
| `socrata_6dca.json` | `GET https://publicreporting.cftc.gov/resource/6dca-aqww.json` with `$where=cftc_contract_market_code in('020601','13874A','088691') AND report_date_as_yyyy_mm_dd >= '2026-10-01T00:00:00.000'`, captured **2026-10-23**; the first three rows, each cut to the fields read. |
