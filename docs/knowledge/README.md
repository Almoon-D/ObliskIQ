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
| `[DOC <date>]` | read in the vendor's documentation on that date, never executed |
| `[MCP ✓ <date>]`, `[CONNECTOR ✓ <date>]` | verified through IBKR's MCP or Custom Connector routes |
| `[ibind <version>]` | read in a reference implementation's source at that version, never executed |
| `[<source> ✓ <date>]` | verified against that data source live |
| `VERIFY-LIVE(<what>)` | built against fixtures, not yet seen live; also queued in `docs/ROADMAP.md` |

A fixture is evidence about our parsing, never about the vendor's format: until a finding is
seen live it stays `VERIFY-LIVE`. Correct a file when reality differs, and keep the date of
the observation that settled it.

## Files

- `calendars.md` — what `exchange_calendars` reports, the venue facts `venues.yaml` cites,
  and how expiry and daylight saving behave.
- `costs.md` — the commission schedules, the transaction taxes and the friction assumptions
  behind `costs.yaml`, and how well each was confirmed.
- `ibkr-capabilities.md` — what the Web API searches, quotes and previews per asset class and
  venue, the order types, algos and trading permissions seen, the capability probe, and what
  IBKR's contract search returns for the core's UCITS funds and ETCs.
- `ibkr.md` — the Client Portal Web API: pacing, sessions, field ids, units, search, history,
  the ledger, the trading schedule, the Client Portal Gateway, lite's live readings and
  Session 3's Gateway trial, and the Gateway archive's Java runtime.
- `ibkr-orders.md` — the order endpoints, the order body, the cOID, warnings and their message
  ids with the reply policy, answers and errors, live orders, status, trades, whatif and
  cancel, as IBKR documents them and ibind implements them, and what the pre-trade gate relies on
  (the ledger's rates, whatif amounts, position and live-order lag); live shapes join it with the
  paper order test.

- `macro-sources.md` — the sources besides IBKR `config/sources.yaml` declares (FRED and ALFRED,
  the ECB, EIA, ONS, StatCan, the BoJ, the BIS, the Fed's excess bond premium, the CFTC, SEC
  EDGAR, Polymarket, Kalshi, GDELT, the news feeds, Nasdaq Trader's halts, Yahoo's earnings dates)
  and the LLM provider: what each answered and how each is placed in time; condenses lite's macro
  playbook.
- `research-sources.md` — the formats the research loaders read from Yahoo (through `yfinance`),
  the Ken French and AQR libraries (daily developed markets and the *Century of Factor Premia*
  included), FRED and ALFRED, the ECB and its Data Portal, MSCI and the Bundesbank, and how each
  is placed in time; the daily continuity of the core's proxies; what the selection data audit
  read from the SEC and Tiingo; the vendors that refused the container.
- `runtimes.md` — Python, Java, Ubuntu, Playwright and uv: releases, support windows and
  platform support, with the publisher's page and the date each was read.
- `universe-sources.md` — the core's candidate vehicles per role: their identifiers and the two
  sources that confirmed them, their conids, their measured tracking and partner correlations.
- `tax-es.md` — Spain's rules on the savings base as the `es_irpf` policy applies them, each with
  its article and the official text it was read in (not tax advice).

More files are added by the sessions that verify them; none is written ahead of the evidence.
