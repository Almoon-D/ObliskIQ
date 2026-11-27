# Recorded SEC filings (the events collector and the dossier's text)

Captured live on **2026-11-27** with a declared User-Agent. A fixture is evidence about this
build's parsing, never about EDGAR's format; observed behaviour lives in
`docs/knowledge/research-sources.md`. Filers' business and mail addresses are removed from the
headers. The `.htm` files are excerpts: each `<p>` holds one span of the document's text as
served (tags removed, entities decoded, white space collapsed), in the document's order.

| file | route |
|---|---|
| `form_index_20261119.idx` | `GET https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261119.idx`: the header and three lines (two 424B4, an S-1) |
| `form_index_20261120.idx` | `GET …/form.20261120.idx`: the header and six lines (a Form 4, a 10-K, three 8-Ks, an F-1) |
| `hdr_bj_8k_results.sgml` | `GET https://www.sec.gov/Archives/edgar/data/1531152/000153115226000083/0001531152-26-000083.hdr.sgml`: an 8-K with items 2.02 and 9.01 |
| `hdr_agilent_8k_departure.sgml` | `GET …/data/1090872/000119312526470278/0001193125-26-470278.hdr.sgml`: an 8-K with item 5.02 |
| `hdr_anthropic_424b4.sgml` | `GET …/data/2130357/000162828026074944/0001628280-26-074944.hdr.sgml`: a pricing prospectus |
| `index_anthropic_424b4.json` | `GET …/data/2130357/000162828026074944/index.json`: the filing's documents |
| `prospectus_ipo_anthropic.htm` | excerpt of `…/000162828026074944/anthropicpbc-424b4.htm`: the cover, the prospectus's date, the dealers' delivery period (25 days, not a lock-up) and the lock-up paragraph (180 and 365 days) |
| `prospectus_follow_on_medline.htm` | excerpt of `…/data/2046386/000119312526468586/trident_iv_424b4.htm`: a listed company's follow-on offering (a 75-day lock-up), and its date |
| `submissions_bj.json` | `GET https://data.sec.gov/submissions/CIK0001531152.json`: the header fields and eight rows of `filings.recent` (columns kept: accession, dates, form, items, primary document) |
| `bj_10k_excerpt.htm` | excerpt of `…/data/1531152/000153115226000007/bj-20260131.htm`: the contents, the start of items 1, 1A and 7, a cross-reference, and the headings that follow |
| `bj_10q_excerpt.htm` | excerpt of `…/000153115226000086/bj-20261031.htm`: the contents, Part I item 2's start, Part II items 1A and 2 |
| `bj_8k_results.htm` | the whole text of `…/000153115226000083/bj-20261120.htm` (item 2.02) |
