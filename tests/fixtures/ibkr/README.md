# IBKR Client Portal fixtures

Shapes of `/v1/api` answers the read client decodes. A fixture is evidence about our parsing,
never about IBKR's format: where a live reading and a fixture disagree, the reading wins and the
fixture is corrected (docs/knowledge/ibkr.md).

Every account id is the placeholder `DU_PAPER`; session ids, MAC addresses, server names and user
ids are placeholders; amounts and prices are invented. `tests/unit/test_ibkr_fixtures_scrubbed.py`
enforces it.

| File | Endpoint | Source of the shape | Date |
|---|---|---|---|
| `accounts.json` | `GET /portfolio/accounts` | IBKR Web API reference, "Portfolio Accounts" | 2026-10-12 |
| `positions_page0.json` | `GET /portfolio/{account}/positions/0` | IBKR reference, "Positions"; string amounts with separators added on purpose | 2026-10-12 |
| `ledger.json` | `GET /portfolio/{account}/ledger` | lite live reading [CP ✓ 2026-09-10]: BASE row labelled `BASE` on `currency` and `secondkey`, base identified by the rate-1 row, NAV a float, per-currency amounts strings | 2026-09-10 |
| `search_asml.json` | `POST /iserver/secdef/search` | lite live reading [CP ✓ 2026-09-10]: three listings answer the bare ticker, the venue is in `description`, lists separated by `;` | 2026-09-10 |
| `search_miss.json` | `POST /iserver/secdef/search` | lite live reading [CP ✓ 2026-09-10]: a miss is an object | 2026-09-10 |
| `contract_info_asml.json` | `GET /iserver/contract/{conid}/info` | IBKR reference example, values for ASML on AEB from lite's readings | 2026-10-12 |
| `trading_schedule.json` | `GET /contract/trading-schedule` | IBKR reference, "Trading Schedule (NEW)"; sessions 09:30-16:00 New York for four weekdays around 2026-10-12, computed, not captured | 2026-10-12 |
| `rules.json` | `POST /iserver/contract/rules` | IBKR reference, "Search Contract Rules" | 2026-10-12 |
| `algos.json` | `GET /iserver/contract/{conid}/algos` | IBKR reference, "Search Algo Params by Contract ID" | 2026-10-12 |
| `scanner_params.json`, `scanner_run.json` | `/iserver/scanner/params`, `/iserver/scanner/run` | IBKR reference | 2026-10-12 |
| `tickle.json` | `POST /tickle` | IBKR reference, "Ping the server" | 2026-10-12 |

None of these was captured live through ObliskIQ: the paper session could not be opened from the
cloud container on 2026-10-12 (docs/knowledge/ibkr.md, "The Client Portal Gateway in a cloud
container"). VERIFY-LIVE(fixture shapes) in docs/ROADMAP.md.
