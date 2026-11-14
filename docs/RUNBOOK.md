# Runbook

How to run ObliskIQ on the deployment machine: in shadow, then on the IBKR paper account; how to
stop it, recover it and read what it tells you. The design is `docs/MASTER_PLAN.md` §5.14; why the
service works as it does is `docs/DECISIONS.md` D102–D105. Commands run from the checkout with
`uv run obliskiq …` (or `obliskiq …` inside the environment).

## What runs

`obliskiq serve` is one process that runs until it is stopped:

- a **heartbeat** every 5 minutes: it works the open orders (reprices, fills, retries), checks the
  protective stops, reconciles with the broker, rewrites the health file and pings
  `OBLISK_HEALTH_PING_URL` when set;
- four **decision cycles** each trading day, each an hour after a venue's open (Tokyo, Xetra, New
  York) and one an hour before New York's close: the core engine's target portfolio is compared
  with the book and its trades become orders;
- the **daily digest** half an hour after New York's close;
- each **data source** besides IBKR at the moments `config/sources.yaml` gives it (the macro
  series, the CFTC's positioning, SEC Form 4 filings, prediction markets, GDELT, the news feeds,
  earnings dates), each run recorded as the source's health; after each news run the
  **EventExtractor** reads the new items, when `GEMINI_API_KEY` is set;
- the **halt feed** (Nasdaq Trader) every 5 minutes, in the heartbeat: a halted instrument of the
  universe is not traded until it resumes; its venue and the venue's other instruments are.

Times follow each venue's own calendar, daylight saving and holidays included
(`config/defaults/runtime.yaml`). The **core** runs the variant `core.variant` names
(`config/owner.yaml` sets D50, global equity with the trend brake; the expert default in
`config/defaults/core.yaml` is A, the static risk balance). Each role is held in the fund
the vehicle selector ranks first (`uv run obliskiq universe vehicles <role>` shows the ranking).
The core trades when it starts, when a role leaves its outer band (the only time it sells), and
when the book holds cash beyond its targets of at least the minimum trade. A role without a
vehicle that may be bought keeps its weight in cash; the digest names it and says why.

Only one obliskiq process may talk to the broker. `serve`, `run-cycle`, `preflight`, `reconcile`
and `flatten` take a lock in the data directory; while the service runs, the others refuse.
`status`, `intents list`, `holdings`, `mode` and `kill` read the store only and work beside it.

## Before the first start

1. The machine has passed T1 (`docs/SESSION_PROMPTS.md` §A.5): the Gateway logs in unattended and
   `uv run obliskiq preflight --read-only` passes.
2. `.env` at the checkout's root (from `.env.example`): `OBLISK_IBKR_PROFILE=paper`, the paper
   account id (`DU…`), `OBLISK_IBKR_TRANSPORT=gateway`, the paper username and password,
   `OBLISK_DATA_DIR`. Optional: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (without them every
   message goes to the log), `OBLISK_HEALTH_PING_URL` (a healthchecks.io check, say). The data
   sources: `FRED_API_KEY`, `EIA_API_KEY` and `SEC_USER_AGENT` (a contact string SEC requires);
   without one, the source or the feeds that need it report the missing variable as a failure. `GEMINI_API_KEY` starts the EventExtractor, and
   with it the clock of hypothesis H7 (`research/preregistrations/llm-events.md`); without it the
   news items wait.
3. `config/owner.yaml`: how much the bot manages. The default is a capped book of 1,000 euros; a
   paper account holds about a million, so keep a cap there:

   ```yaml
   capital:
     mode: cap
     cap_base: "800"
   ```

   Then `uv run obliskiq config validate`.

## Run in shadow

Shadow is where a deployment starts: orders are simulated with the cost model and never sent
(a buy in dollars waits in shadow unless the account already holds dollars: shadow converts
nothing).

```
uv run obliskiq mode show                          # execution mode shadow
uv run obliskiq run-cycle --mode shadow --dry-run  # the plan; nothing written or sent
uv run obliskiq serve                              # runs until SIGTERM or Ctrl-C
```

Leave it under a supervisor so it restarts after a reboot. A systemd user unit, for example:

```
[Unit]
Description=ObliskIQ service
After=network-online.target

[Service]
WorkingDirectory=%h/ObliskIQ
ExecStart=%h/.local/bin/uv run --locked obliskiq serve
Restart=on-failure
RestartSec=60
KillSignal=SIGTERM
TimeoutStopSec=120

[Install]
WantedBy=default.target
```

`systemctl --user enable --now obliskiq`, and `loginctl enable-linger` so it runs without a login.

## Move to paper

L1 of the promotion ladder (MASTER_PLAN §5.19) asks for ten trading days of clean shadow first;
the promotion gate that checks it is Session 18's, so today it is the owner's judgement.

```
uv run obliskiq run-cycle --mode shadow --dry-run   # still sensible?
systemctl --user stop obliskiq                      # the lock: stop the service first
uv run obliskiq mode set paper --reason "start paper trading"
uv run obliskiq run-cycle --mode paper --dry-run
uv run obliskiq run-cycle --mode paper              # one real paper cycle, supervised
uv run obliskiq status
systemctl --user start obliskiq
```

`run-cycle --mode` must name the mode the store holds; it refuses otherwise.

## Stop

- **Stop the service**: `systemctl --user stop obliskiq` (SIGTERM) or Ctrl-C. The job in hand
  finishes, the health file says `stopped`. Resting orders stay at IBKR: day limits expire at the
  close, protective stops (the core carries none) keep resting.
- **Kill switch**: `uv run obliskiq kill on --reason "…"` blocks every order that adds risk at
  once, even with the configuration broken; the running service's sweep cancels working buys and
  conversions within seconds and keeps protective exits. `kill off --reason "…"` lifts it.
- **Flatten**: `uv run obliskiq flatten --confirm` sells every managed position. It is separate
  from the kill switch and needs the service stopped (the lock).

## Recover

The service recovers by itself when it starts: runs a killed process left open are marked
`interrupted`, every order that may be at IBKR is looked up by its id (a crash never orders twice),
and a cycle missed in the last two hours runs at once; an older one is recorded as missed.

| You see | Do |
|---|---|
| `status` shows the heartbeat STALE | The service is down or hung: `systemctl --user status obliskiq`, its journal, then restart it. |
| `reconciliation drift` (digest, alert) | New buys are frozen. `uv run obliskiq reconcile run` (stop the service first) shows the findings. If IBKR is right (a dividend, a manual trade), `uv run obliskiq reconcile accept --reason "…"`. |
| `reconciliation incomplete` | IBKR's data was partial; the next heartbeat retries. If it persists, check the Gateway. |
| `session.reauthentication_failed` | The Gateway could not log in again (after IBKR's daily reset, say). Protective stops rest at IBKR, so this costs cycles, not protection. Check the Gateway's log; the keepalive retries once its re-login interval passes. |
| `session.competing_session` | Another login took the paper username: stop it (a Claude session, a browser). |
| `service.downgraded` | Three cycles in a row failed; the mode is shadow. Read the run log (`status`, the digest's errors), fix the cause, then `mode set paper` again. |
| `breaker.frozen` | The managed book fell more than 15 % beyond global equity within a month: new buys are frozen while that holds; sales and stops go on, and nothing is sold for it. `status` shows the fall and the limit. Look at what fell (a holding, a price, a fill) before anything else. |
| `breaker.hard_tripped` | The book fell more than 25 % beyond global equity from a daily close: the mode is shadow and stays there. Nothing was sold. Find the cause, then `uv run obliskiq breakers reset --reason "…" --confirm` and `mode set paper`; the hard breaker measures again from the closes after the reset. If the alert says the `FORCED_SHADOW` file holds the mode, the store could not record the change: repair the store first; the next `mode set` removes the file. |
| `breaker.unreadable` | The book's value could not be read, or its last daily close is more than five days old (the service was down, or was in another mode): new buys wait until the next close is recorded at the day's last cycle. |
| `service.stuck_intent` | An order with no answer, or open past its expiry. `uv run obliskiq intents list`; compare with IBKR's open orders; `reconcile run`. |
| `service.target_unconfirmed` | IBKR does not confirm a core fund's conid (it resolves to another, or is not found): that fund is never bought, and the role uses the next-ranked vehicle or keeps its weight in cash. Report it; the conid in `config/universe.yaml` needs a correction (a corrected pin supersedes the old conid). |
| `order.warning_declined` | IBKR asked to confirm a warning the reply policy does not confirm; the order was cancelled. The alert and `intents list --all --history` carry IBKR's own words. |

## Read the digest

- **Mode, kill switch, reconciliation**: what may trade, and whether new risk is frozen.
- **NAV**: the account's net liquidation value, read once a day, and the time-weighted return
  over the last `runtime.status_report_days` with your deposits and withdrawals neutralised.
- **Book**: managed capital, then each role: what is held, its share, its target and what the plan
  does now (`hold`, `buy`, `sell`, `skip` with why).
- **Intents, fills**: today's orders and executions (`simulated` in shadow).
- **Runs, errors**: today's cycles; an `error`, `partial` or `interrupted` run is listed with the
  step that failed.
- **Venues**: 🟢 regular, 🔵 auction, 🟣 extended hours, 🔘 lunch, ⚫️ closed, ⚪️ no session today;
  a venue that should trade but cannot: 🚨 incident, ♦️ inconsistent schedule, 🔶 IBKR's schedule
  disagrees, ❔ not covered.
- **Health and open alerts**: the last heartbeat, the broker session, the last cycle, and the
  conditions still holding (each was sent once when it began).
- **Thesis book**: its capital, each open idea with what the plan does (`enter`, `hold`, `exit`,
  `close`, or `wait` with the reason it is withheld), the ideas that ended today and why, and the
  record of the closed ones for the book, each source and each conviction level.
- **Withdrawals, questions, unexplained cash**: each open reservation and how it is met, the
  questions waiting for your answer, and any cash change the bot could not explain (new buys are
  frozen until you classify it).

The subject line counts what needs attention; "nothing needs attention" is a full digest with no
failure in it, never an empty one.

## Money in and out, questions, tax

None of these opens a brokerage session, so all are safe beside the running service. Each one
that records something first says what it would do; add `--confirm` to record it.

```
uv run obliskiq cash deposit 1237.57                    # money you added
uv run obliskiq cash reserve 1000 --by 2026-11-15 --reason "car"   # money you will take out
uv run obliskiq cash list                               # R<n> reservations, F<n> flows, E<n> events
uv run obliskiq cash withdrawn R1                       # you took the reserved money out
uv run obliskiq cash cancel R1                          # no longer needed
uv run obliskiq cash classify E1 dividend               # explain a cash change the bot froze on
uv run obliskiq questions list                          # what the bot needs you to decide
uv run obliskiq questions answer 3 postpone             # by its deadline, or the default applies
uv run obliskiq holdings external add --broker DEGIRO --symbol VWCE --quantity 10 \
    --currency EUR --value 1100                         # a holding at a broker with no API
uv run obliskiq tax report --year 2026 --out tax-2026.csv   # not tax advice
```

A reservation lowers investable capital at once; the bot raises the cash before the date and asks
before selling a protected position. Without an answer by the deadline, the withdrawal wins.

## Your ideas (the thesis book)

None of these opens a brokerage session, so all are safe beside the running service. An idea is
judged at the service's next cycle; with no idea the book holds nothing.

```
uv run obliskiq idea add FCX --expect "copper tightens as mines close" \
    --target 60 --invalidation 38 --horizon 2027-06-30       # a symbol of config/universe.yaml
uv run obliskiq idea add SGLD --bearish --expect "equities fall" \
    --target 400 --invalidation 300 --horizon 2027-03-31     # a bearish view: a defensive instrument
uv run obliskiq idea add --candidate jp:toyota_motor --isin JP3633400001 --expect "..." \
    --target 4000 --invalidation 2500 --horizon 2027-06-30   # a share outside the universe file
uv run obliskiq idea list                                    # open ideas (--all: every idea)
uv run obliskiq idea show T1                                 # one idea, its log and its grade
uv run obliskiq idea withdraw T1                             # before its first fill only
```

- **What an idea needs.** The instrument, what you expect and why, a target price, an
  invalidation price (what proves it wrong) and a horizon. Without one of them it is refused and
  nothing is stored. The target lies above the invalidation: the book never shorts. Prices are in
  the instrument's own currency.
- **Options.** `--conviction high` or `max` multiplies the size (1.5 and 2 times a normal idea)
  inside the position cap. `--entry-max` enters only at or below a price. `--enter-by` is the last
  day to enter; without it the idea may be entered for `thesis.entry_window_days` (20).
- **A share outside the universe file** is named by its stock candidate's key (the holdings files
  `obliskiq collect candidates` reads) with its ISIN, which the tax lots are kept by. The service
  resolves it through IBKR's contract search at its next cycle. A fund, ETF or ETC enters through
  a reviewed entry in `config/universe.yaml`, because its eligibility is judged on its declared
  attributes.
- **What happens next.** The gate judges it: the mandate, eligibility, liquidity, the caps, the
  ratio of the target's gain to the invalidation's loss (at least `thesis.min_gain_to_loss`), a
  free slot. A refusal is final and says why; anything else withholds the idea, with the reason
  in the digest, until its entry window ends. Once approved its text never changes. It leaves at
  its target, its invalidation or its horizon; on paper a stop rests at the broker at the
  invalidation. A closed idea is graded against what it wrote.
- **Capital.** In shadow and on paper the book works on `thesis.capital_share` of investable
  capital (a tenth), an eighth of it for a normal idea; set it to `"0"` in `config/owner.yaml` to
  give the reserve everything. On the live account the book has no capital until the capital
  allocation rule exists.

## Before a Claude session with live IBKR checks

IBKR allows one brokerage session per username. A Claude session that logs in to the paper
account (a preflight, a paper order test) takes the session over from the running service
(`compete`), and the service takes it back at its next keepalive: each breaks the other's work
mid-order. The service lock only guards processes on this machine. So before such a session:

```
systemctl --user stop obliskiq
```

and tell the session the bot is paused. Start the service again when the session is done.

## From the order test to the reserve on paper (owner, on the machine)

One list, in order: the paper order test, the execution test, the history the reserve's trend
brake reads, the reserve's supervised paper cycle, then `serve` under the supervisor for the ten
shadow days that open paper (MASTER_PLAN §5.19, L1). The ROADMAP's live verification queue calls
part A "T1 (the paper order test)" and part D "the core's live paper cycle".

Rules for every step:

- The service is stopped (`systemctl --user stop obliskiq`) until part E, and no Claude session
  is logged in to the paper username.
- `preflight`, `collect`, `run-cycle` and `reconcile` each log in once: leave ten minutes between
  the starts of two of them. A locked username or a refused password ends the day's logins.
- Keep each command's printed report and its log (JSON lines on stderr) together. Reports mask
  the account id; before pasting anything else, replace it with `DU*******`, and paste no
  balance beyond what a report prints.
- A step that does not match its expected output is pasted as it is. Do not repeat it, and do
  not go on to the next part.

### A. The paper order test, inside both order windows

On a day Xetra and the NYSE are both open, between 16:00 and 17:15 Madrid time: the NYSE's order
window opens 30 minutes after its open and Xetra's closes 15 minutes before its close
(`calendars.order_offsets`).

1. `uv run obliskiq mode set paper --reason "T1 paper order test"` prints the change to `paper`.
2. `uv run obliskiq preflight --paper-order-test`. Expect `Every step passed.` under these lines:
   - `mode: paper`;
   - EXV1 (Xetra, euros): `identity`, `algos`, `quote`, `whatif`, `pre-trade: cleared`,
     `place: placed` with the ids of IBKR's replies, `cOID: listed as order_ref`, `status`, and
     `cancel: … then Cancelled`;
   - FCX (NYSE, dollars): `identity`, `algos`, `quote`, `whatif`, then
     `pre-trade: refused by the cash check, as expected` (the account holds no dollars);
   - `live orders: the `snapshot` flag of this session's first reads: <first>, then true`. The
     first is the pre-read's flag (`absent` on 2026-10-21); the second must be `true`, or the
     gate has refused with `account_unreadable` and nothing was placed.

   Paste the whole report, and from the log the two lines `live orders pre-read` and
   `live orders, the read after the pre-read` with their `snapshot` values.
3. `uv run obliskiq mode set shadow --reason "T1 paper order test done"`.
4. The Java 21 and 25 side runs of SESSION_PROMPTS §A.5 item 6, each ten minutes after the
   previous login: a second day adds those versions to the Gateway's list. Paste each masked
   report with its `java -version`.

### B. The execution test

The same day or a later one, inside the NYSE's order window (16:00 to 21:45 Madrid time). It
converts 100 euros into dollars, buys FCX's size increment at the touch and protects it with a
stop-limit (`ibkr.preflight.execution_test`).

5. `uv run obliskiq mode set paper --reason "execution test"`.
6. `uv run obliskiq preflight --execution-test`. Expect `mode`, `FCX identity` and
   `(a) conversion whatif` to pass. Then one of two outcomes:
   - `[FAIL] (a) conversion: failed: declined: … (IBKR: '…')`, and the test stops. The reply
     policy does not confirm `o10164` for a conversion (DECISIONS D101), so this is the outcome
     expected today and not a fault of the machine. The quoted text is IBKR's own.
   - the conversion fills: `(a) both balances`, `(c) attached orders: not probed`, then the
     `(b)` lines (the buy, its fill, the stop placed and listed) and the `(d)` lines (how long
     IBKR took to show the fill in the live orders, the trades and the positions).

   Paste the whole report.
7. `uv run obliskiq intents list --all --history` (no login). Under the conversion's intent, the
   line that names `o10164` holds IBKR's text of the warning, kept in the intent's history.
   Paste the conversion's intent and its history lines.
8. Ten minutes later, `uv run obliskiq reconcile run`. Expect `green`. On 2026-10-20 IBKR kept
   the declined conversion listed as `Inactive` and refused its cancel: if the run reports it,
   paste the findings, do not `reconcile accept`, and run it again the next trading day. Part D
   needs a green run first, because drift freezes new buys.
9. Only if step 6 reached the `(b)` lines: the next trading day, inside the NYSE's window,
   `uv run obliskiq preflight --execution-test --finish`. Expect the stop still listed after the
   restart, the sale, and the position and the stop gone. Paste the report.
10. `uv run obliskiq mode set shadow --reason "execution test done"`.

### C. What the reserve's trend brake needs on the machine

The reserve (`core.variant: D50`) scales its equity by a trend it reads from stored history.
Without it a role says `untilted` and is held whole.

- **The market store's daily bars** (`engines/core/history.py`): 13 month-end closes of the
  equity vehicle and of the cash vehicle (`core.tilt`: 12-month momentum against the month
  before it), each the last bar at most 7 days before its month end
  (`research.simulation.max_price_age_days`). Today the vehicles are SPPW or XDWD and XEON, all
  on Xetra in euros. The collectors' backfill holds two years of bars
  (`collectors.backfill.window_years`). A vehicle in another currency would also need the ECB's
  rate series (`uv run obliskiq sources run ecb`).
- **The research store** (`orchestration/correlations.py`): each candidate's tracking of its
  role's series and the swap partners' correlation. Without it the selector ranks without
  tracking and the reserve has no swap partner.

11. `uv run obliskiq collect universe --venue XETR`, then ten minutes later
    `uv run obliskiq collect backfill --venue XETR` (read-only). Expect `universe: ok`, then
    `backfill: ok`, each ending `run: ok`. Paste both.
12. `uv run obliskiq research data load-universe` (Yahoo; no login, a few minutes). Expect
    `loaded N of N listings`. Paste that line and any listing it names as failed.
13. `uv run obliskiq universe vehicles world_equity` and `uv run obliskiq universe vehicles cash`
    (no login). Expect a number on each `tracking` and `liquidity` line where they said
    `unmeasured`, and under `swap partners of SPPW` either XDWD with its correlation or the
    reason it is not one. Paste both.
14. Optional, after New York's close: `uv run obliskiq collect daily` (every venue and step,
    read-only, up to two hours). It is the collectors' live round of the verification queue;
    paste its step lines.

### D. The reserve's paper cycle

One supervised paper cycle under a managed-capital cap of a few hundred euros, the cap this test
is allowed. Inside Xetra's order window (09:30 to 17:15 Madrid time), after a green
`reconcile run`.

15. `config/owner.yaml`: `capital: {mode: cap, cap_base: "800"}`; `uv run obliskiq config
    validate` passes.
16. `uv run obliskiq mode set paper --reason "reserve paper cycle"`.
17. `uv run obliskiq run-cycle --mode paper --dry-run`: the plan, nothing written or sent.
    It shows `variant D50: start`, then each role with its fund, its target and why:
    `world_equity` in SPPW, `cash` in XEON. No role says `IBKR did not confirm`, and
    `world_equity` does not say `untilted` (if it does, part C is incomplete: the reason names
    the months it has). Under an 800-euro cap the minimum trade is 50 euros, so the plan buys
    only the roles whose target is worth at least that and at least one unit, and says why for
    each of the others. Paste the plan.
18. Ten minutes later, `uv run obliskiq run-cycle --mode paper`: the buys, in euros, so no
    conversion comes first. Paste the output.
19. Ten minutes later, `uv run obliskiq run-cycle --mode paper` again: it reprices or fills,
    registers the holdings to the core and reconciles. Paste the output.
20. `uv run obliskiq status`, `uv run obliskiq holdings list`, `uv run obliskiq intents list
    --all --history`, and ten minutes later `uv run obliskiq reconcile run`. Expect
    `reconciliation: green`, the holdings owned by `core`, and each intent `filled` or still
    working. Paste all four.
21. With Telegram set, `uv run obliskiq serve` until the digest, then stop it with Ctrl-C. The
    digest comes half an hour after New York's close: 22:30 Madrid time, or 21:30 while New York
    is on summer time and Madrid is not. Without Telegram the digest is in the log. Paste the
    digest.
22. `uv run obliskiq mode set shadow --reason "reserve paper cycle done"`.
23. Paste parts A to D into a short session: *"Record T1's order test, the execution test and
    the core's live paper cycle per SESSION_PROMPTS §A.5."* That session decides `o10164` on a
    conversion from IBKR's text, in a pull request of its own.

### E. `serve` under the supervisor, and the ten shadow days

24. `uv run obliskiq mode show` says `shadow`. Install the unit of "Run in shadow", then
    `systemctl --user enable --now obliskiq` and `loginctl enable-linger`.
25. Each trading day, for ten in a row, after the digest:
    - `uv run obliskiq status`: `mode shadow · kill switch off`; the heartbeat under ten minutes
      old, not `STALE`, `ok`; `reconciliation: green`; `open intents: 0`; `open alerts: 0`; no
      run marked `!`.
    - The digest's subject says `nothing needs attention`.
    - `journalctl --user -u obliskiq --since yesterday | grep reauthenticat`: one
      `reauthenticated` after IBKR's daily reset, and no `reauthentication_failed`.

    A day that fails any line starts the count again: L1 asks for ten consecutive trading days
    with no unhandled error, a clean reconciliation, no stuck intent and every daily reset
    logged in again unattended. Keep each day's `status` output.
26. After the tenth clean day, "Move to paper" above: `serve` then runs the reserve on paper.
    Paste the ten `status` outputs into the session that records the promotion.

## Session 9's capability probe (owner, on the machine)

Reads what the Web API can do per asset class and venue on the paper account: a search, a
snapshot, the contract rules, the algos and a whatif preview per check. Nothing is placed: the
preview is the only order endpoint it calls, and the paper guard refuses it on an account without
the paper prefix. A preview needs a bid, so run it while Europe and New York are both open (15:30
to 17:30 Madrid time); ASX's check reports no bid then and is read in its own session.

1. The bot paused (see "Before a Claude session with live IBKR checks").
2. `uv run obliskiq universe capabilities`: the readings recorded today, and which venues the
   vehicle selector may propose.
3. `uv run obliskiq universe capabilities --probe`: one block per check, with the account masked.
4. `uv run obliskiq universe vehicles world_equity` (and any other role): the ranking as the
   recorded capabilities stand.
5. Paste the masked output into a short session: *"Record Session 9's capability probe per
   SESSION_PROMPTS §A.5."* The readings enter `config/defaults/capabilities.yaml` and
   `docs/knowledge/ibkr-capabilities.md` by a reviewed change; the probe writes nothing itself.

## Session 10's sources (owner, on the machine)

Reads the sources besides IBKR once by hand, so their first state is known before `serve` runs
them on schedule. No broker session is opened; it can run beside the service.

1. `uv run obliskiq sources list`: every source, its consumers, how its rows are dated, and the
   condition a declared one waits for.
2. `uv run obliskiq sources run all`: each built source once (a few minutes; GDELT paces itself).
   Then `uv run obliskiq sources health`: every source `ok`, or the reason it is not.
3. `uv run obliskiq sources catalysts`: the central-bank and release dates ahead, with the
   earnings dates labelled research-grade.
4. With `GEMINI_API_KEY` set: `uv run obliskiq llm extract` once, then `uv run obliskiq llm
   status` (the items waiting, the month's spend against `config/llm.yaml`'s budget, the prompt
   hash H7 is bound to).
5. Paste the masked output of steps 2 and 4 into a short session: *"Record Session 10's source
   runs per SESSION_PROMPTS §A.5."* GDELT and the first extraction close their VERIFY-LIVE rows.
