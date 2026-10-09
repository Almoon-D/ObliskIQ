# Knowledge

Observed behaviour of the outside world: the broker, data sources, exchanges and tax
authorities, as measured, with where and when. One owner per fact — configuration holds
values, code holds mechanics, `docs/DECISIONS.md` holds why, `docs/MASTER_PLAN.md` holds
the design; this directory holds what was seen.

## What goes here

- How an API actually behaves: endpoints, fields, units, error bodies, rate limits, quirks.
- Exchange and venue facts the configuration cites (sessions, auctions, fees, taxes).
- Legal and tax rules a jurisdiction profile encodes, with the article and an official
  source per rule (marked "not tax advice").
- Findings carried over from the legacy repositories when their files are retired.

Values the code uses live in `config/` with a source; a knowledge file explains them.

## Tags

Every finding carries its route and date:

| Tag | Meaning |
|---|---|
| `[CP ✓ 2026-09-10]` | verified live through the IBKR Client Portal Web API on that date |
| `[MCP ✓ <date>]`, `[CONNECTOR ✓ <date>]` | verified through IBKR's MCP or Custom Connector routes |
| `[<source> ✓ <date>]` | verified against that data source live |
| `VERIFY-LIVE(<what>)` | built against fixtures, not yet seen live; also queued in `docs/ROADMAP.md` |

A fixture is evidence about our parsing, never about the vendor's format: until a finding is
seen live it stays `VERIFY-LIVE`. Correct a file when reality differs, and keep the date of
the observation that settled it.

## Files

Files are added by the sessions that verify them (`ibkr.md`, `ibkr-orders.md`,
`ibkr-capabilities.md`, `macro-sources.md`, `tax-es.md`, …); none is written ahead of the
evidence.
