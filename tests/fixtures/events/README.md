# Event fixtures

Read live on 2026-10-23 from a cloud session and trimmed to the fields the parsers read.

| File | Source | Trimmed |
|---|---|---|
| `polymarket_event_fed_december.json` | `https://gamma-api.polymarket.com/events?slug=fed-decision-in-december-20260729232808632` | the event's first three markets and five tags; market fields the collector reads |
| `polymarket_tag_recession.json` | `https://gamma-api.polymarket.com/events?tag_slug=recession&closed=false&limit=3` | as above (one event, one market, below the discovery liquidity floor) |
| `kalshi_kxfeddecision.json` | `https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXFEDDECISION&status=open&limit=3` | market fields the collector reads; the cursor kept |
| `gdelt_middle_east_volraw.json` | GDELT DOC 2.0, `mode=timelinevolraw&format=json&timespan=7d`, the `middle_east` query of `config/sources.yaml` | whole answer; GDELT leaves out some hours (17 Oct: 1, 18 Oct: 3, 19 Oct: 1) |
| `gdelt_latam_tone.json` | GDELT DOC 2.0, `mode=timelinetone&format=json&timespan=7d`, the `latam` query | whole answer; the same hours are left out as in the volume answer of another query |
| `rss_fed_press.xml` | `https://www.federalreserve.gov/feeds/press_all.xml` (RSS 2.0, with a byte-order mark) | first three items |
| `rss_boc_press.rdf` | `https://www.bankofcanada.ca/content_type/press-releases/feed/` (RSS 1.0, RDF) | first two items |
| `atom_fcx_filings.xml` | EDGAR company feed, Freeport-McMoRan 8-K (`browse-edgar?action=getcompany&CIK=0000831259&type=8-K&output=atom`) | first two entries; the company's addresses and the feed's contact e-mail removed |
