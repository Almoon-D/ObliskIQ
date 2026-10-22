# IBKR Client Portal Web API

Observed behaviour of IBKR's Web API and the routes around it, with where and when each fact was
seen. Condensed from ObliskIQ-lite-MCP's `references/ibkr-playbook.md` (legacy/lite, @b1ebd49),
keeping every verified finding, plus what IBKR documents and what Session 3 measured. Values the
code uses live in `config/defaults/ibkr.yaml`; why they were chosen is `docs/DECISIONS.md`.

## Routes and tags

Three IBKR routes behave differently, so every line names the one it was seen on:

- **CP** — the Client Portal Web API (`/v1/api`), over OAuth 1.0a or through the Client Portal
  Gateway. The route this package speaks.
- **MCP** — the IBKR MCP connector's tools inside a chat session.
- **CONNECTOR** — the same tools at IBKR's Custom Connector endpoint
  (`https://api.ibkr.com/v1/api/mcp-public`), an OAuth 2.0 resource.

`[CP ✓ <date>]` verified live on that date; `[DOC <date>]` read in IBKR's documentation on that
date and not executed; `VERIFY-LIVE(<what>)` built against fixtures (queued in
`docs/ROADMAP.md`). A finding on MCP or CONNECTOR is **not** a CP fact: the response shapes
differ (columnar bars with ISO stamps on MCP, `data[]` of `{t,o,h,l,c,v}` with epoch ms on CP).

Unless a line says otherwise, `[CP ✓ 2026-09-…]` readings were taken by lite over OAuth 1.0a on
the owner's live account; ObliskIQ's own sessions use the paper account.

## Sources

- Web API reference: <https://www.interactivebrokers.com/docs/web-api/v1/> (each page has a `.md`
  form; fetched 2026-10-12), in particular *Pacing Limitations*, *Regular Server Maintenance*,
  *Market Data Availability*, *Market Data Fields*, *Live Market Data Snapshot*, *Historical
  Market Data*, *Authentication Status*, *Initialize Brokerage Session*, *Ping the server*,
  *Trading Schedule (NEW)*, *Trading Schedule by Symbol*, *Search Contract Rules*, *Search Algo
  Params by Contract ID*, *Iserver Market Scanner*, *Iserver Scanner Parameters*, *Portfolio
  Accounts*, *Positions*, *Portfolio Ledger*, *Search Contract by Symbol*, *Search Bond Filter
  Information*, *Contract information by Contract ID*.
- Web API changelog: <https://www.interactivebrokers.com/docs/web-api/changelog/> — entries
  2025-11-18, 2025-12-10, 2026-01-06, 2026-03-27, 2026-04-14, 2026-09-09.
- Market-data users and sharing:
  <https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-users/introduction>,
  <https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-users/market-data-sharing>,
  *Market Data Considerations* <https://www.ibkrguides.com/kb/article-1719.htm>.
- Client Portal Gateway: download <https://download2.interactivebrokers.com/portal/clientportal.gw.zip>,
  *Client Portal Gateway FAQ* and *Limitations of the Client Portal Gateway*
  (<https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/>).
- IBeam (Voyz/ibeam, Apache-2.0), read as a reference for the Gateway's automated login, never a
  dependency.

## What IBKR documents

- **Pacing.** A global limit of 10 requests a second; per endpoint: `/iserver/marketdata/history`
  10/s **and** 50/min; `/iserver/marketdata/snapshot` 10/s; `/portfolio/accounts` and
  `/portfolio/subaccounts` 1 per 5 s; `/iserver/scanner/params` 1 per 15 min;
  `/iserver/scanner/run` 1/s; `/tickle` 1/s; `/iserver/account/orders`, `/trades` and
  `/pnl/partitioned` 1 per 5 s; `/sso/validate` 1/min; `/fyi/*` 1/s; `/pa/*` 1 per 15 min. A
  violation answers `429` and puts the IP in a **15-minute penalty box**; repeat violators can be
  blocked permanently. [DOC 2026-10-12]
- **Changelog.** 2025-12-10: `/iserver/marketdata/snapshot` takes at most **100 conids** per query
  and **50 fields**. 2026-09-09: history pacing is 10/s or 50/min; `/hmds/scanner` is deprecated;
  **the `secType` parameter was removed from `/iserver/secdef/search`**. 2026-01-06: the
  fundamental tags — dividend amount, dividend yield %, ex-date, P/E, market capitalisation, EPS,
  beta — are deprecated and no longer served. 2025-11-18: `/hmds/history` is deprecated (use
  `/iserver/marketdata/history`). 2026-03-27: stocks accept `cashQty`. 2026-04-14: websocket
  `smd` subscriptions end after 10 minutes. [DOC 2026-10-12]
- **Daily maintenance.** Web API clients are disconnected at midnight of their connecting region
  (North America 00:00 US/Eastern, Europe 00:00 CEST, Asia 00:00 HKT). The page's UTC row
  (20:00, 02:00, 08:00 UTC) contradicts its local row, so neither is configured: a reset is
  detected when the session reports unauthenticated, and re-established. [DOC 2026-10-12]
- **Session.** `POST /iserver/auth/ssodh/init` opens the brokerage session (`publish`, `compete`;
  compete disconnects other brokerage sessions of the user); everything but `/portfolio` needs it.
  `POST /iserver/auth/status` and `POST /tickle` (whose `iserver.authStatus` nests the same
  object) report `authenticated`, `competing`, `connected`, `message`, `fail`. IBKR asks for a
  tickle about every 60 s; an idle session times out. `/iserver/accounts` must be called before
  `/iserver/marketdata/snapshot`, and `/iserver/secdef/search` before a derivative's snapshot.
  [DOC 2026-10-12]
- **Market data availability (field 6509).** First character: `R` real time, `D` delayed (15–20
  minutes), `Z` frozen (last data at the close, relayed in real time), `Y` frozen delayed, `N` not
  subscribed, `O` the annual market-data API acknowledgement is not signed; then `P` snapshot or
  `p` consolidated, `B` book, `d` performance details. [DOC 2026-10-12]
- **Field ids** (IBKR's table): 31 last (prefix `C` previous close, `H` halted), 84 bid, 86 ask,
  85/88 sizes, 87 volume abbreviated (`K`/`M`; 7762 is the exact volume), 7283 option implied vol
  of the underlying (30-day at-the-money), **7633 implied vol of the specific strike**, **7638
  option open interest**, **7089 option volume**, 7087 30-day historical vol (**7284 is deprecated
  in favour of 7087**), 7308 delta, 7309 gamma, **7310 theta, 7311 vega**, 6509 availability,
  7295 open, 7296 close, 7741 prior close, 7768 trading permission. [DOC 2026-10-12]
- **History.** At most 1000 points per call; `period` and `bar` combinations per a published
  table; answers carry `data[]` of `{o,c,h,l,v,t}`, `mdAvailability`, `mktDataDelay` (processing
  delay, ms, not the feed delay). [DOC 2026-10-12]
- **Trading schedule.** `GET /contract/trading-schedule?conid&exchange` returns the six days
  around today: per `YYYYMMDD`, `liquid_hours` (the regular session) and `extended_hours` as
  epoch seconds, with `exchange_time_zone`; non-trading days are omitted. The older
  `GET /trsrv/secdef/schedule` returns up to a month as local `HHMM` strings per weekday code.
  VERIFY-LIVE(trading schedule shape and omitted holidays). [DOC 2026-10-12]
- **Contract rules.** `POST /iserver/contract/rules` (`conid`, `exchange`, `isBuy`) returns order
  types (in and outside regular hours), cash-quantity types (`cqtTypes`), TIF types, size
  increment, increment rules (`lowerEdge`, `increment`), price magnifier, algo eligibility. Algos:
  `GET /iserver/contract/{conid}/algos` (`addParams=1`). Scanner: `GET /iserver/scanner/params`,
  `POST /iserver/scanner/run` (at most 50 contracts). Bonds: search rows carry `issuers`, and
  `/iserver/secdef/bond-filters?symbol=BOND&issuerId=` lists maturities, coupons and currencies.
  [DOC 2026-10-12]
- **Market data is per user.** A subscription on one username does not reach another user of the
  same account. A live username may share its market data with one paper account, so both read
  data under one subscription, billed once; IBKR's *Market Data Considerations* describes how
  shared data behaves. Whether the paper session receives real-time data while the owner is
  logged in live elsewhere is VERIFY-LIVE(shared data with the owner logged in live). API data
  needs an IBKR PRO account, the subscription and the Market Data API acknowledgement (code `O`
  when it is missing). [DOC 2026-10-12]
- **The Client Portal Gateway.** IBKR "does not support an automated authentication process for
  Client Portal Gateway"; clients must log in daily; the login must happen in a browser on the
  Gateway's own machine, and API calls must come from that machine. It ships without a valid
  certificate. [DOC 2026-10-12]

## Verified in lite (routes as tagged)

### The OAuth 1.0a handshake

- The handshake passed every step first time: credentials (DH prime 2048 bits), decrypt (32
  bytes of prepend), DH exchange (a **511-character, odd-length** hex challenge is accepted), the
  live-session-token signature matched, brokerage session authenticated, tickle kept it open.
  [CP ✓ 2026-09-10]
- `OBLISK_IBKR_CP_BASE_URL` is `https://api.ibkr.com` with no path; `/v1/api` is appended.
  [CP ✓ 2026-09-10]
- The DH shared secret is a signed big integer (a leading zero byte when the top bit is set); the
  token is `HMAC-SHA1(K, prepend)` and is verified against `live_session_token_signature`. On
  OpenSSL builds with implicit rejection a wrong encryption key decrypts to random bytes instead
  of raising, so the signature check is the backstop and is never skipped (seen across lite's CI
  matrix).

### Session

- `/iserver/marketdata/*` answers `HTTP 500 {"error":"Please query /accounts first"}` until
  `/iserver/accounts` is called; `/portfolio/accounts` does not satisfy it. [CP ✓ 2026-09-10]
- The precondition is **lost mid-run**: re-calling both accounts endpoints cleared nothing across
  nine attempts, re-running `/iserver/auth/ssodh/init` restored it at once. [CP ✓ 2026-09-10]
- A bare `401` 31 minutes into a run with ~22 hours left on the token was a dropped brokerage
  session (the next target collected; a re-run collected 130 rows): re-arm on 401. A `403` is an
  entitlement answer: never re-armed. [CP ✓ 2026-09-10]
- `/iserver/accounts` is unreliable as a read (`{}` once, the list minutes later): a
  precondition to call, not a source to trust. [CP ✓ 2026-09-10]

### Contract search

- `/iserver/secdef/search` takes its terms as a **JSON body**: as query parameters it answers 200
  with every row's `sections: []` (SPY: 55 matches, 0 sections, against 3 matches and 6 sections
  as a body). [CP ✓ 2026-09-10]
- Both section lists (`months`, `exchange`) are **semicolon**-separated; splitting the exchange
  list on a comma sent the whole list as one exchange and `/secdef/strikes` answered `410`.
  [CP ✓ 2026-09-10]
- A miss answers 200 with an **object** `{"error": "No symbol found"}` (TOPIX, `IND`); a hit a
  list. [CP ✓ 2026-09-10]
- Every listing of one company answers the same bare ticker (ASML: NASDAQ, TSE, AEB; SAN: NYSE,
  MEXI, AEQLIT, LSE, BM, WSE, ASX; TTE: NYSE, AEQLIT, SBF); the venue is in `description`. The
  ranking is **time-varying**: at 09:48 UTC the European lines came first, at 20:05 UTC the US
  ones; 19 of 74 declared rows resolved to another conid without the declared venue, one of them
  (`EEM`) to "Micro Ether Euro", a CME index future in EUR. [CP ✓ 2026-09-10]
- A short ticker returns leveraged ETPs, foreign listings and unrelated issuers (`HG` → Hamilton
  Insurance second); `security_type` filters were ignored on MCP. [MCP ✓ 2026-08-23/24]
- The European listings serve their option sections on their own venues: ASML `117589399` AEB →
  `EUREX`, `FTA`; SAN `30314144` BM → `EUREX`, `MEFFRV`; TTE `29612193` SBF → `EUREX`, `MONEP`;
  strikes served on `FTA`, `EUREX`, `MONEP`. [CP ✓ 2026-09-10]
- Conids worth pinning (ambiguous symbols): SLF 45208985 ASX, ATEC 407597432 ASX, XLF 4215220
  ARCA, 3191 438186903 SEHK; the nine Select Sector SPDRs on ARCA. [MCP ✓ 2026-09-03]
- `/iserver/contract/{conid}/info` returns `currency` and the valid exchanges (CAC: `SMART,SBF,…`;
  XIU: `SMART,TSE`); `C40` is the CAC 40 *ESG* fund and `CACX` the same CAC fund in GBP — the
  currency check cannot catch the first. [CP ✓ 2026-09-14]
- The search matches one token and silently truncates (`KSM.F` reported 210, returned 15).
  [MCP ✓ 2026-09-03]

### Snapshots

- Field ids [CP ✓ 2026-09-10]: `31` last (`"C83.05"` on XLP: previous close), `84`/`86` plain
  bid/ask strings, `87` volume **abbreviated** (`"53.4K"` with `"87_raw": 53400.0` beside it),
  `6509` availability as letters (`"DP"` SPY and XLP, `"DPB"` ASML), `7283` implied vol and
  `7284` 30-day historical vol as **percent-suffixed strings** (`"13.126%"`, `"9.449%"`).
- `7286`–`7288` (dividend amount, yield, ex-date) are never returned while neighbours `7292`–`7295`
  are; the data is withheld (and deprecated by IBKR since 2026-01-06). [CP ✓ 2026-09-10]
- **150 conids in one request answer `429 {"error":"Exceed number of conids"}`; 100 is
  accepted.** A request-shape refusal, not pacing. [CP ✓ 2026-09-10]
- A cold subscription answers a skeleton first; on a cold option the status and **all four
  Greeks** arrive at ~1.3 s and the prices at ~7 s (4.8 s when the batch was primed first): a
  poll settles on a PRICE field only, with ten polls a second apart. [CP ✓ 2026-09-15]
- A zero on the first call for a conid is not evidence; a lone 0/8 with the venue open is noise.
  [CP ✓ 2026-09-11, 2026-09-14]
- After the close: US option quotes survive for hours (87 % priced at 21:08 UTC, `ZBd`), ~1 % by
  23:04; European chains step down at their close and plateau, by name (ASML 2/8, SAN 8/8, TTE
  6–8/8). A chain photographed outside hours carries Greeks and no prices. [CP ✓ 2026-09-10..14]
- `6509` reads `DPBd`/`RBd` while a European venue is open and `ZBd` once it closes.
  [CP ✓ 2026-09-14]
- Units: implied vol, historical vol and the IV percentile are fractions on MCP while
  `dividend-yield.yield_pct` is a **percent** in the same response — a `_pct` suffix states
  nothing; `-1.0` is IBKR's "no implied vol" sentinel (seen on US, EUREX and FTA contracts, ~27 %
  of one ASML expiry's strikes); volatility is annualised with √252. [MCP ✓ 2026-08-23..09-09]
- The underlying can read `REALTIME` while its options read `FROZEN_DELAYED`; `REALTIME` is about
  entitlement, not the session; a closed market cannot be told from a missing entitlement by a
  snapshot. [MCP ✓ 2026-08-23/24]

### History

- **257 bars for 400 calendar days requested**; a 2-year request returned bars from 2023-08-08:
  IBKR serves a span derived from the request, never "the count asked for". [CP ✓ 2026-09-10]
- `HTTP 500 {"error":"Chart data unavailable"}` and `{"error":"Contract details are not
  available"}` are final answers wearing a retryable status. [CP ✓ 2026-09-10]
- "Chart data unavailable" with the native exchange, and bars with the exchange **omitted**: SAN
  (`BM`), TTE (`SBF`), BARN and LISN (`EBS`), TSEJ funds; ASML answers both ways; hence
  `history_exchange`. [CP ✓ 2026-09-10, 2026-09-15; MCP ✓ 2026-08-29]
- BOVA11 refuses 730 and 365 days and serves 181 bars for 182: the endpoint refuses a window
  longer than it can serve; JDEP is a dead listing whose only series ends 2026-04-29.
  [CP ✓ 2026-09-15]
- `HTTP 429` is frequent under backfill load and absorbed by retries with backoff; `410` on
  `/secdef/strikes` means a malformed request. [CP ✓ 2026-09-10]
- Option price history is unavailable ("No historical market data available"); futures contract
  history is available (GC 123 daily bars, delayed 600 s); an empty series is not an error.
  [MCP ✓ 2026-08-23/24]

### Portfolio

- The ledger's `BASE` row reads `currency: "BASE"` **and** `secondkey: "BASE"`; the base
  currency is the per-currency row whose `exchangerate` is exactly 1 (EUR; USD 0.8612968);
  `netliquidationvalue` is a float while per-currency amounts arrive as strings.
  [CP ✓ 2026-09-10]
- `get_pa_allocation` ignores its currency and returns base-currency figures under another label:
  read allocation in base currency only. [MCP ✓ 2026-08-23]

### Futures (data only)

- The ladder is unsorted (front month ninth of 34, fourth of 132) and long (CL to 2036); sort by
  expiry and bound by contract month. KC, NG, TTF and ZS returned no `/trsrv/futures` ladder.
  [CP ✓ 2026-09-10; CONNECTOR ✓ 2026-09-09]

### The Custom Connector

- An OAuth 2.0 protected resource (RFC 9728 metadata at the URL the challenge names, not the
  canonical path); `client_credentials` is advertised and wired; it answers a headless
  container with a token minted elsewhere; registration and token lifetime untested.
  [CONNECTOR ✓ 2026-09-03]

## Session 3 (2026-10-12): the Client Portal Gateway in a cloud container

Tried in this order, with the paper username and password from the environment:

1. The archive at the download URL is 10,542,956 bytes, SHA-256
   `2f2d380b2f9424520ff5f9c11fe45e82ef39459329ac056258a3274bea6f76f9`, `Last-Modified` 2023-04-24,
   build `ibgroup.web.core.iblink.router.clientportal.gw-20230424154245`; the beta archive is of
   the same date. [CP ✓ 2026-10-12]
2. The shipped `root/vertx.jks` certificate (`CN=Client Portal Web API`) **expired 2019-05-11** and
   names no host. A per-install key pair for `localhost` (keytool, SAN `localhost`,
   `127.0.0.1`), set through `sslCert`/`sslPwd` in `root/conf.yaml`, serves TLS that verifies with
   hostname checks against the pinned certificate. [CP ✓ 2026-10-12]
3. The Gateway starts in about 2 s on Java 21 and on Java 11, and answers every API path with
   `401` before login. [CP ✓ 2026-10-12]
4. Headless Chromium (Playwright 1.56, build 1194) loads `/sso/Login?forwardTo=22&RL=1&ip2loc=on`
   in ~1.3 s, trusting the Gateway by its pinned public key
   (`--ignore-certificate-errors-spki-list`). The form has `input[name=username]`,
   `input[name=password]`, the paper toggle `label[for=toggle1]` ("Paper trading mode selected",
   the button turns to "Simulated Login") and the submit `.btn.btn-lg.xyz-button-login`; the page
   now says "Client Portal is now mobile-optimized". [CP ✓ 2026-10-12]
5. Filled and submitted at once, the form answered **"Authentication failed"**. Typed key by key
   with a 5-second pause before submitting, IBKR **accepted the password**: the SRP exchange at
   `/sso/Authenticator` returned the server proof `M2` with `reached_max_login: false`.
   [CP ✓ 2026-10-12]
6. The session never became usable. In nine of ten attempts the `/sso/Dispatcher` step
   re-rendered the login form with no error; once (Java 11) it went on to
   `/sso/Dispatcher?hardware_info=…` and showed "Client login succeeds", after which the Gateway's
   own `/v1/api/sso/validate?gw=1` answered `401` "Access Denied" every three seconds. `tickle`
   and `iserver/auth/status` stayed `401` throughout. The Gateway's cookie manager keeps IBKR's
   cookies (`JSESSIONID`, `x-sess-uuid`, `AKA_A2`, `web`, `partnerID`) and passes none to the
   browser; IBKR issued a fresh `JSESSIONID` mid-flow. Pointing `proxyRemoteHost` at
   `ndcdyn`/`gdcdyn.interactivebrokers.com` and removing the container's JVM proxy settings
   (`JAVA_TOOL_OPTIONS`) changed nothing. [CP ✓ 2026-10-12]

So in a cloud container the Gateway route stops at the login: the password is good, the
Gateway session is not established, and IBKR documents that it does not support automating this
login. The read-only preflight therefore stopped at its transport step, and no CP market-data
reading was taken through ObliskIQ this session. What remains to be seen, on the owner's
machine: VERIFY-LIVE(Gateway headless login on the deployment machine), then every reading the
Session 3 prompt lists (the US stock, the European ETF, the US and Eurex options with and without
the owner logged in live, a scanner run, the trading schedule).

**Second attempt, same day (16:20 UTC), with the merged client.** The container's outbound
address is not stable: ten fresh HTTPS connections left from four addresses in one /24, while one
kept-alive connection kept a single address. The likely cause of the failure, not observed
directly: the Gateway opens several connections to IBKR during the login, which would leave from
different addresses and fits the fresh `JSESSIONID` IBKR issued mid-flow in the first attempt.
One run of `obliskiq preflight --read-only` (gateway, paper, Java 11, two login attempts) ended
the same way: the SRP proof came back (`M2`, password accepted)
and the form then showed "Authentication failed". No further login was tried, to keep clear of a
lock-out. [CP ✓ 2026-10-12] So on both attempts the Gateway login did not complete from this
cloud container; the next attempt is on the deployment machine, where a stable outbound address is
expected but not yet seen.

The decoders keep lite's live readings as their tests; the new field ids (`7633`, `7638`,
`7087`, theta `7310` and vega `7311`) are IBKR's table and VERIFY-LIVE(snapshot field ids), as is
VERIFY-LIVE(secdef search without secType).

## The Gateway archive and its Java runtime (2026-10-19)

- The download URL still serves the archive of Session 3: 10,542,956 bytes, the same SHA-256,
  `Last-Modified` 2023-04-24. The beta archive (`clientportal.beta.gw.zip`) is also dated
  2023-04-24. IBKR has published no later build. [DOC 2026-10-19]
- The archive's own `doc/GettingStarted.md`: "The gateway requires Java 1.8 update 192 or higher to
  run, and has been tested successfully with OpenJDK 11." It names no upper version. IBKR's current
  *Installation & Authentication* page says only to download Java from java.com if none is
  installed. [DOC 2026-10-19]
- The jars are compiled for Java 8 (class-file version 52; the Gateway jar's manifest: built
  2023-04-24 with Azul 1.8.0_332, `Javac-Target: 1.8`) and bundle vert.x 3.5.0, netty 4.1.15
  (with `netty-tcnative-boringssl-static` 2.0.6), jackson 2.9 and snakeyaml 1.17. `bin/run.sh`
  starts `java -server -Djava.net.preferIPv4Stack=true …` with no module options. [DOC 2026-10-19]
- IBeam 0.5.13 (Docker Hub `voyz/ibeam`, 2026-09-20), the open-source automated Gateway login,
  installs Debian bookworm's `default-jre`, which is OpenJDK 17 (`runtimes.md`). Whether its users'
  logins succeed on it was not read. [DOC 2026-10-19]
- What Session 3 shows about Java: the ten attempts of the first trial are not recorded by Java
  version; the one that reached "Client login succeeds" ran on Java 11 and still had no session
  (`401` on `sso/validate`, `tickle` and `auth/status`); the second trial, on Java 11, failed after
  the password was accepted. No Java version produced a usable session, and the likely cause, the
  container's changing outbound address, applied to every attempt whatever the Java. So the
  readings do not separate Java 11 from Java 21.
  VERIFY-LIVE(Gateway login on Java 21 and 25 on the deployment machine): one day observed, see
  *T1, second day* below.

## T1, first day (the owner's report, 2026-10-20)

The owner ran T1's first steps (SESSION_PROMPTS §A.5) on a home machine. Masked report, read as
written; nothing below was observed by a session.

- **Windows is not a deployment target.** Native Windows could not start the Gateway: the
  installer looks for `bin/java` (Windows has `java.exe`), and IBKR's `run.sh` runs through
  `bash`, which on Windows is the WSL stub. The test suite on Windows: 3,428 passed, 49 failed
  and 8 errors, from `fcntl` (Linux only). The owner's two local patches for `java.exe` are not
  committed. The deployment OS stays Ubuntu (D96).
- **Ubuntu under WSL2** (user-level install; the release was not recorded as 26.04), Java 11,
  Chromium with its libraries: `obliskiq preflight --read-only` **passed every step**: the
  unattended Gateway login, the account guard, positions (none), cash in EUR, SPY prices
  (delayed), history, contract rules, algos and the trading schedule. [CP ✓ 2026-10-20, the
  owner's T1 report] So the login that failed from a cloud container completes from this
  machine, which supports Session 3's reading that the container's changing address was the
  cause.
- **`--paper-order-test` placed no order.** EXV1 (conid 89005163) passed its identity, quote and
  whatif, then was refused `venue_not_tradable` (XETR outside its window, at about 22:58 local
  time): the test ran after Xetra's close. FCX resolved through IBKR's contract search to conid
  **7089** on the NYSE, where the allowlist said 8158 (lite's fixture), and the test stopped
  there. The allowlist now says 7089. [CP ✓ 2026-10-20, the owner's T1 report]
- Not yet run: the second day's login after IBKR's reset, the Java 21 and 25 side test, and the
  paper order test inside both venues' order windows, which overlap from about 16:00 to 17:15
  Madrid time (the NYSE's window opens 30 minutes after its open, Xetra's closes 15 minutes
  before its close; from 15:00 in the week of 26–30 October, when Europe has left summer time
  and the US has not).

## T1, second day (2026-10-21, run by the owner's session on the same machine)

The same machine as the first day: Ubuntu 26.04.1 LTS under WSL2, Temurin 11.0.32.1, Playwright
1.64 with its Chromium, paper profile over the Gateway transport, no OAuth variable in the
distribution's environment, execution mode `shadow` before and after every run. Account masked
`DU*******`; every run is one login, at least ten minutes after the previous one's start. Times are
UTC. Native Windows was not tried again.

- **Day-2 login and read-only preflight passed, every step** (21:55). Gateway started to logged in
  in 13 s on the first attempt; `ssodh/init` answered `authenticated: true`, `competing: false`;
  accounts, guard, positions (none), cash (EUR), SPY on ARCA conid 756733 (delayed, flag `DP`, 1200 s),
  21 daily bars, contract rules (12 order types, size increment 40.0), 10 algos and 6 regular
  sessions of the trading schedule. The login is a fresh unattended one on the calendar day after
  day 1, with a Gateway process that was started and stopped by the command.
  [CP ✓ 2026-10-21]
- **Playwright 1.64's headless Chromium logs in on Ubuntu 26.04.1** with the form flow
  (`keystroke_delay_ms`, `presubmit_wait_seconds`) unchanged: six logins that day, each on the first
  attempt, no "Authentication failed", no session-less acceptance. `ssodh/init` was retried once
  after a backoff of 0.7 s in the ASX order run and answered authenticated on the second try.
  [CP ✓ 2026-10-21]
- **Java 21 and Java 25 each passed the whole read-only preflight** with the same report (Temurin
  21.0.12.1 at 22:18, Temurin 25.0.4.1 at 22:29), run from a copy of `config/` outside the clone
  whose `owner.yaml` listed that version in `ibkr.gateway.java_major_versions`, with
  `OBLISK_IBKR_GATEWAY_JAVA_HOME` pointing at that JDK. No `--add-opens` option and no Java 17 run was
  needed. This is **one day** for each version (day 1 had no side run), so the list stays `[11]`
  until a second day passes (D96). [CP ✓ 2026-10-21]
- **`--paper-order-test` placed no order on any of three venues.** The command ran in `paper` mode
  and the mode went back to `shadow` after each run.
  - *Xetra and NYSE, 22:07 UTC (outside both windows, by the owner's choice).* EXV1 (conid 89005163)
    and FCX (conid 7089) passed identity, algos, quote and whatif; EXV1's quote was `frozen` and
    FCX's `unreported`; limits 36.960 and 65.14 (0.05 below the bid); whatif commission 3 EUR and
    1.00 USD, initial margin change 11 and 2021, no warning. The **pre-trade gate then refused each
    with `venue_not_tradable` (`outside_window`)** before anything was sent to IBKR. That refusal is
    ObliskIQ's own order window (`calendars.order_offsets`: 30 minutes after the open to 15 before
    the close), not an IBKR answer. [CP ✓ 2026-10-21]
  - *ASX, 23:33 UTC (10:33 in Sydney, inside the window), a dry run with a config copy.* BHP resolved
    through the same contract search to **conid 4036812** on ASX in AUD (22:40, read-only preflight
    with `ibkr.preflight` set to BHP: snapshot `frozen` flag `ZB` with the prior close as last, 22 daily
    bars, 9 order types, tick bands 0.001, 0.005 from 0.1 and 0.01 from 2, size increment 1.0,
    algos as for SPY, schedule in `Australia/NSW`). In the window the order test passed identity,
    algos, quote (`delayed`, limit 54.96) and whatif (commission 6 AUD, initial margin change 12,
    **warning 4**: the report prints the count, not the text), and the calendar gate passed. The gate
    then refused with **`account_unreadable: the live orders are not a complete snapshot`**.
    [CP ✓ 2026-10-21]
- **The live orders of a session's first read lack `snapshot: true`.** This is the same refusal the
  v0.10.1 execution test met once at its first `reconcile run` (ROADMAP), now met by the pre-trade
  gate on the first live-orders read after a login. The gate and the cOID lookup treat the list as
  incomplete by design (*Live orders, status and trades* in `ibkr-orders.md`); nothing retries or
  pre-reads it, so **a fresh process cannot place an order until the live orders are read once before
  the read that counts**. The in-window rerun on Xetra and the NYSE will meet the same refusal after the
  calendar gate passes. The fix is a code change on a money path (follow-up in ROADMAP), not done here.
  [CP ✓ 2026-10-21]
- **Not observed:** an order placed, read, replied to and cancelled; the cOID's presence in the live
  orders and the trades; any answer to a `o354`/`o10164`-class reply; European and Australian
  commissions of an executed order.
- **T1's own criterion is met** ("both logins complete unattended and both preflights finish",
  SESSION_PROMPTS §A.5): day 1 on 2026-10-20 and day 2 on 2026-10-21, both under WSL2 Ubuntu with
  Java 11. The paper order test, the other half of what §A.5 says T1 proves, is still open.

## Session 7 (2026-10-21): cash activity, not yet observed

Session 7 had no paper credentials and makes no Gateway login from a cloud container (Session 3),
so how IBKR reports dividends, interest, fees, withholding and conversions in the ledger and the
account's activity was **not observed**. What the code assumes instead, and fails closed on:

- Reconciliation compares each currency's cash with the last reading plus the bot's fills
  (`execution/fill_cash.py`) and the owner's recorded flows; any other change beyond the
  tolerance is an unexplained cash event that freezes new buys until the owner classifies it
  (`obliskiq cash classify E<n> <kind> --confirm`). Nothing is classified from IBKR's own
  activity text yet.
- The ledger's per-currency `exchangerate` (the base row's is exactly 1, see *Portfolio* above)
  is the rate the tax lots value a fill of that day at; a day with no currency row has no rate,
  and a fill of that day waits for one.

VERIFY-LIVE(ledger and account-activity shapes of dividends, interest, fees, withholding and
conversions) is queued in `docs/ROADMAP.md`: the first session with a working paper login records
them here and builds the classifier's fixtures from them.
