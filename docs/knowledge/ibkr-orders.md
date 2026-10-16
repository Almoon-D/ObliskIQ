# IBKR Client Portal Web API — orders

The order endpoints as IBKR documents them and as ibind implements them. Nothing here has been
executed through ObliskIQ yet: every line is `[DOC 2026-10-13]` (IBKR's reference) or
`[ibind 0.2.2]` (read in its source), and every shape the code relies on is
`VERIFY-LIVE(paper order test)` until Session 4a's paper order test runs on a machine where the
paper Gateway login works (docs/knowledge/ibkr.md, "Session 3"). Tags as in `README.md`.

## Sources

- IBKR Web API reference, `.md` form of each page, fetched 2026-10-13:
  `endpoints/orders/{place-order, place-order-reply-confirmation, preview-order-what-if-order,
  modify-order, cancel-order, suppress-messages, suppressible-message-ids, order-error-details,
  cash-quantity-orders-in-the-web-api, respond-to-a-server-prompt}` and
  `endpoints/order-monitoring/{live-orders, order-status, order-status-value, trades}` under
  <https://www.interactivebrokers.com/docs/web-api/v1/>.
- ibind 0.2.2 (Voyz/ibind, Apache-2.0): `ibind/client/ibkr_client_mixins/order_mixin.py` and
  `ibind/client/ibkr_utils.py` (`QuestionType`, the message-id map, `handle_questions`,
  `OrderRequest` and its field mapping). Read as a reference, never a dependency.

## Endpoints

| Call | Endpoint | Body / query | Answer |
|---|---|---|---|
| Place | `POST /iserver/account/{accountId}/orders` | `{"orders": [order]}` | `[{"order_id", "order_status", "encrypt_message"}]`, or a warning, or `{"error"}` (HTTP 200) |
| Reply | `POST /iserver/reply/{replyId}` | `{"confirmed": true\|false}` | as Place; may carry a further warning |
| Preview | `POST /iserver/account/{accountId}/orders/whatif` | as Place | `amount`, `equity`, `initial`, `maintenance`, `position` blocks, `warn`, `error` |
| Modify | `POST /iserver/account/{accountId}/order/{orderId}` | one order (not wrapped), mirroring the original | as Place |
| Cancel | `DELETE /iserver/account/{accountId}/order/{orderId}` | — | `{"msg", "order_id", "conid", "account"}` or `{"error"}` |
| Live orders | `GET /iserver/account/orders` | `filters`, `force` | `{"orders": [...], "snapshot": bool}` |
| Status | `GET /iserver/account/order/status/{orderId}` | — | one order's state |
| Trades | `GET /iserver/account/trades` | `days` (at most 7) | list of executions |
| Reset suppressed | `POST /iserver/questions/suppress/reset` | — | — |

[DOC 2026-10-13] [ibind 0.2.2]

- **Pacing.** `/iserver/account/orders`, `/iserver/account/trades` and
  `/iserver/account/pnl/partitioned` are paced at one request per 5 s (docs/knowledge/ibkr.md,
  "Pacing"); each has its own rate group at one per 6 s (`config/defaults/ibkr.yaml`). The
  placement path (`/iserver/account/{id}/orders`) has no endpoint limit of its own and is
  paced by the global window. [DOC 2026-10-12]
- **Preconditions.** `/iserver/accounts` before modify and cancel; a `/iserver/marketdata/
  snapshot` of the instrument before a whatif. [DOC 2026-10-13]

## The order body

Fields ObliskIQ sends (`ibkr/orders.py: order_body`), from the reference and ibind's mapping
[DOC 2026-10-13] [ibind 0.2.2]:

- `acctId`, `cOID`, `side` (`BUY`/`SELL`), `tif` (`DAY`, `GTC`, `IOC`; `OPG` is never sent: no
  auction orders), `outsideRTH: false`.
- `conid`, or `conidex` (`"<conid>@<exchange>"`) to route to one venue. ibind refuses both in
  one order; the reference's example carries both. ObliskIQ sends `conid` for `SMART` and
  `conidex` otherwise. VERIFY-LIVE(routing by conidex).
- `orderType`: only `LMT` and `STOP_LIMIT` are ever built. A stop-limit carries `price` (the
  limit) and `auxPrice` (the stop). An IBKR algo is `LMT` with `strategy` and
  `strategyParameters` (from `/iserver/contract/{conid}/algos`), capped by `price`. The
  reference also lists `MKT`, `STP`, `MIDPRICE`, `TRAIL`, `TRAILLMT`; none can be expressed.
- Size: `quantity`, or `cashQty` (money; stocks, forex and crypto only; a stock order rounds
  down to whole shares and is rejected below one share; stocks accept `cashQty` since
  2026-03-27).
- **Currency conversion:** `isCcyConv: true` with `fxQty` (the reference's prose says `fxQTY`;
  its JSON example and ibind say `fxQty`). ObliskIQ sends `fxQty`.
  VERIFY-LIVE(conversion field spelling) — Session 5 converts.
- `manualIndicator` and `extOperator` are required for futures and futures options only (CME
  Rule 536-B); ObliskIQ never trades either and sends neither.

## The cOID

- `cOID` is "an arbitrary string" the client chooses; live orders and trades return it as
  `order_ref`; the order-status endpoint does not return it. [DOC 2026-10-13]
- A cOID already used is refused with `Order couldn't be submitted: Local order ID=… is already
  registered` — the text ibind matches. [ibind 0.2.2] Over what window IBKR keeps it is not
  documented. VERIFY-LIVE(duplicate cOID refusal and its window).
- No maximum length is documented. ObliskIQ sends the 36-character uuid7 and keeps a
  deterministic short form ready (`execution.orders.coid_max_length`).
  VERIFY-LIVE(cOID length).

## Warnings and replies

- A placement, modify or reply may answer with a warning:
  `[{"id": "<replyId>", "message": ["…"], "isSuppressed": false, "messageIds": ["o163"]}]`.
  The reference calls `messageIds` "internal use only"; ibind matches the message text instead.
  ObliskIQ decides by id and declines a warning that carries none. [DOC 2026-10-13]
- A warning must be answered **before any other request**: another order or request
  invalidates it, and confirming it afterwards times out with HTTP 503. After one confirmation
  further warnings may follow. `confirmed: false` declines and discards the order.
  [DOC 2026-10-13] [ibind 0.2.2] So ObliskIQ places one order at a time and decides from what
  the clearance recorded, reading nothing while a warning is open. Its own keepalive (`/tickle`)
  and the kill switch's sweep run beside a placement and could still invalidate a warning; the
  confirmation then fails and the outcome is reported as unknown. VERIFY-LIVE(whether a tickle
  invalidates an open warning).
- Suppression (`/iserver/questions/suppress`) accepts a warning automatically for every later
  order. ObliskIQ never suppresses; `OrderClient.reset_suppressed()` re-enables any a user
  suppressed, so no warning bypasses the reply policy. [DOC 2026-10-13]

### Message ids and the policy

From IBKR's suppressible-id list [DOC 2026-10-13]; the policy is `execution.reply_policy`.

| Id | Warning | Policy |
|---|---|---|
| `o163` | price exceeds the percentage constraint (3 %) | declined |
| `o354` | no market data | confirmed only under the quote policy and the collar (`execution/replies.py`) |
| `o382` | exceeds the tick size limit | declined |
| `o383` | size exceeds the size limit | declined |
| `o403` | will most likely trigger and fill immediately | declined |
| `o451` | value exceeds the total value limit | declined |
| `o2136`, `o2137` | mixed allocation, cross side | declined |
| `o2165` | no fractions outside regular hours | declined |
| `o10082` | called bond | declined |
| `o10138` | size modification limit | declined |
| `o10151`, `o10152`, `o10288` | market, stop (market) and crypto market order risks | declined (never sent) |
| `o10153` | mandatory cap price | confirmed for any order |
| `o10164`, `o10223` | cash-quantity notices | confirmed for cash-quantity orders |
| `o10331` | stop order risks | confirmed for stop-limit orders |
| `o10332`–`o10336` | crypto, exercise, omnibus, rapid entry, limited liquidity | declined |
| `p6`, `p12` | multiple accounts; "Use the Price Management Algo?" | declined |
| `o0` | the stop-order warning in the reference's **modify** example | declined (unlisted) |

`o0` appears in the reference's modify example for the stop-order text that `o10331` carries
on a placement. If live modifies of a stop answer `o0`, every protective-stop modify is
declined until the policy changes. VERIFY-LIVE(warning ids on a stop-limit modify).

## Answers and errors

- **Refusals with HTTP 200:** `{"error": "We cannot accept an order at the limit price you
  selected…"}`, `Invalid order price fields`, `null time in force is not supported for this
  order`, `no sec defs returned for…` (bad conid). **HTTP 400:** malformed JSON, a field of the
  wrong type, an invalid side, an unknown order type, no conid. **HTTP 500:** invalid request
  content. [DOC 2026-10-13]
- ObliskIQ sends each write once and classifies what came back: not processed (circuit open,
  the transport could not sign, 401, 429), refused (an error body, 400, 403), or **unknown** (a
  5xx, a transport failure mid-flight, a body that is not JSON) — the order may exist, and the
  cOID is looked up before anything else (DECISIONS D47).

## Live orders, status and trades

- **Live orders** list the account's orders of the day (cancelled, filled, working), at most
  1000. `snapshot: true` marks a complete list; ibind notes the endpoint "requires a
  pre-flight request", and `force=true` clears IBKR's cache and returns an empty list for that
  call. ObliskIQ treats a list without `snapshot: true` as incomplete: an absent cOID proves
  nothing, and the placement is refused. Fields read: `orderId`, `order_ref`, `conid`, `side`,
  `secType` (`CASH` for a conversion), `orderType`, `status`, `totalSize`, `filledQuantity`,
  `remainingQuantity`, `avgPrice`, `timeInForce`. VERIFY-LIVE(live-orders shape, snapshot flag
  on the first call of a session). [DOC 2026-10-13] [ibind 0.2.2]
- **Order status** answers 503 for an order cancelled or filled before the session with no
  cached information; its side is `B`/`S`; it carries no cOID. [DOC 2026-10-13]
- **Trades**: the current day and six before it (`days` ≤ 7); `side` `B`/`S` (the reference
  also says "Buy or Sell"), `size` a number, `price` and `commission` text, `trade_time_r`
  epoch milliseconds, `order_ref` the cOID. [DOC 2026-10-13]
- **Status values** and how ObliskIQ reads them (`execution/boundary.py: state_for`):
  `Inactive`, `PendingSubmit`, `PreSubmitted`, `Submitted`, `PendingCancel`, `PreCancelled`,
  `WarnState` → still working (an execution can still arrive while a cancel is pending);
  `Filled` → filled; `Cancelled` → cancelled ("could occur unexpectedly when the destination
  has rejected your order"). Any other status is read as working. IBKR's `Inactive` can also be
  an order the destination rejected after acknowledging it; read as working, such an intent
  waits for Session 5's reconciliation. VERIFY-LIVE(Inactive after acknowledgement).
  [DOC 2026-10-13]

## Preview (whatif)

`amount.amount` (`"1,977.60 USD (10 Shares)"`), `amount.commission`, `amount.total`, and
`{current, change, after}` for `equity`, `initial` and `maintenance` margin and `position`, as
display strings with thousands separators; `warn` like `"21/You are trying to submit an order
without having market data…"` (a numeric code, a slash, the text); `error` or null. Whatif is
affected by message suppression. [DOC 2026-10-13]

## Cancel

`{"msg": "Request was submitted", "order_id", "conid", "account"}`; `conid` is `-1` for an order
cancelled at once; an unknown order answers `{"error": "OrderID 1 doesn't exist"}`. `orderId` `-1`
cancels **every** open order — ObliskIQ sends only all-digit ids, so never `-1`. What a cancel of an
order that has just filled answers is not documented; ObliskIQ reads the order's status after every
cancel. VERIFY-LIVE(cancel of a filled order). [DOC 2026-10-13]

## What the pre-trade gate relies on (Session 4a)

Nothing below was executed through ObliskIQ: the paper Gateway login does not complete from a
cloud container (docs/knowledge/ibkr.md), so the paper order test (`obliskiq preflight
--paper-order-test`) waits for the deployment machine. VERIFY-LIVE(paper order test).

- **Ledger.** Each currency row carries `exchangerate`, the value of one unit in the base currency
  (the base row's is 1; USD 0.8612968 in a EUR account) [CP ✓ 2026-09-10, docs/knowledge/ibkr.md].
  The gate converts notional and the cash floor with it, and refuses a currency without one.
- **Whatif amounts** are display strings naming their currency after the number
  (`"1,977.60 USD (10 Shares)"`) [DOC 2026-10-13]; the gate refuses a whatif whose cost is in
  another currency than the order's. Whether a whatif registers its cOID is not documented; the
  gate previews with a cOID of its own. VERIFY-LIVE(whatif cOID), VERIFY-LIVE(whatif margin fields
  on a paper margin account: a cash-funded buy may still report an initial-margin change).
- **Positions after a fill.** IBKR's portfolio is a cache; how long it lags a fill is not
  documented. The gate takes the lower of the broker's read and the store's (the read before the
  fill, less the fill). VERIFY-LIVE(position lag after a fill).
- **Trades versus live orders.** A working sell whose `filledQuantity` exceeds the trades recorded
  under its cOID refuses further sells of the instrument. VERIFY-LIVE(trades lag behind live
  orders).
- **A modify and a cancel in the live orders.** The exit coordinator cuts a protective stop (a
  modify, or a cancel) and clears the exit on the next live-orders read; it counts the stop at the
  larger of what the broker lists and what the modify asked. VERIFY-LIVE(live orders show a modify
  or a cancel at once).
- **Zero positions.** A position closed during the day may be listed at zero; flatten skips it.
  VERIFY-LIVE(zero rows in the positions read).
