# Recorded SEC EDGAR answers

Captured live on **2026-10-23** with `SEC_USER_AGENT` set. A fixture is evidence about this
build's parsing, never about EDGAR's format; observed behaviour lives in
`docs/knowledge/macro-sources.md`. The filers' mail and business addresses (SGML header and
`reportingOwnerAddress`) are removed; everything else is as EDGAR served it.

| file | route |
|---|---|
| `form4_purchase_mfm.txt` | `GET https://www.sec.gov/Archives/edgar/data/801961/0001104659-26-112638.txt`: a director's open-market purchase (code P), filed 2026-10-01 |
| `form4_award_fcx.txt` | `GET https://www.sec.gov/Archives/edgar/data/831259/0001214659-26-012436.txt`: a director's award (code A), accepted after 17:30 ET |
| `form_index_20261001.idx` | `GET https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261001.idx`: the header and a handful of lines (a 1-K/A, the two filers above, a 4/A, an SC 13G) |
| `company_tickers.json` | `GET https://www.sec.gov/files/company_tickers.json`: seven entries |
