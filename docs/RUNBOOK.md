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
  York) and one an hour before New York's close: the skeleton's targets are compared with the
  book and the gaps become orders;
- the **daily digest** half an hour after New York's close;
- each **data source** besides IBKR at the moments `config/sources.yaml` gives it (the macro
  series, the CFTC's positioning, SEC Form 4 filings, prediction markets, GDELT, the news feeds,
  earnings dates), each run recorded as the source's health; after each news run the
  **EventExtractor** reads the new items, when `GEMINI_API_KEY` is set;
- the **halt feed** (Nasdaq Trader) every 5 minutes, in the heartbeat: a halted instrument of the
  universe is not traded until it resumes; its venue and the venue's other instruments are.

Times follow each venue's own calendar, daylight saving and holidays included
(`config/defaults/runtime.yaml`). The **skeleton** book is three UCITS funds of the core's variant
A (`config/defaults/core.yaml`): world equity on the LSE in dollars, emerging-market equity and euro
government bonds on Amsterdam in euros. It is a walking skeleton: Session 13 replaces it.

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
  close, protective stops (none on the skeleton) keep resting.
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
| `service.stuck_intent` | An order with no answer, or open past its expiry. `uv run obliskiq intents list`; compare with IBKR's open orders; `reconcile run`. |
| `service.target_unconfirmed` | IBKR resolves a skeleton fund to another conid: that role is not traded. Report it; the conid in `config/universe.yaml` needs a correction (a corrected pin supersedes the old conid). |
| `order.warning_declined` | IBKR asked to confirm a warning the reply policy does not confirm; the order was cancelled. The alert and `intents list --all` carry IBKR's own words. |

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

## Before a Claude session with live IBKR checks

IBKR allows one brokerage session per username. A Claude session that logs in to the paper
account (a preflight, a paper order test) takes the session over from the running service
(`compete`), and the service takes it back at its next keepalive: each breaks the other's work
mid-order. The service lock only guards processes on this machine. So before such a session:

```
systemctl --user stop obliskiq
```

and tell the session the bot is paused. Start the service again when the session is done.

## Session 6's live paper cycle (owner, on the machine)

One supervised paper cycle with a managed-capital cap of a few hundred euros, the cap this test is
allowed. Inside the European order window (Amsterdam and London both open: 09:30 to 17:15 Madrid
time):

1. `config/owner.yaml`: `capital: {mode: cap, cap_base: "800"}`; `uv run obliskiq config validate`.
2. No service running (`systemctl --user stop obliskiq`; `uv run obliskiq status` shows no fresh
   heartbeat).
3. `uv run obliskiq mode set paper --reason "Session 6 live cycle"`
4. `uv run obliskiq run-cycle --mode paper --dry-run`: the plan; the three funds confirmed (no
   `not confirmed` line). With 800 euros at October 2026's prices: about one IWDA, two EMIM and
   three IBGM.
5. `uv run obliskiq run-cycle --mode paper`: the conversion into dollars first, then the three
   buys. Expect `o10164` on the conversion: the reply policy declines it (D101) and the IWDA buy
   waits; the intent history (`uv run obliskiq intents list --all`) and the alert now carry IBKR's
   own text of the warning.
6. Ten minutes later, `uv run obliskiq run-cycle --mode paper` again: reprices or fills, the
   holdings registered to the core, reconciliation.
7. `uv run obliskiq status`, `uv run obliskiq holdings list`, `uv run obliskiq reconcile run`.
8. With Telegram set, `uv run obliskiq serve` until the digest (half an hour after New York's
   close: 22:30 Madrid time, 21:30 from 26 to 30 October while New York is still on summer time),
   then stop it; without Telegram the digest is in the log.
9. `uv run obliskiq mode set shadow --reason "Session 6 live cycle done"`
10. Paste the masked outputs into a short session: *"Record Session 6's live paper cycle per
    SESSION_PROMPTS §A.5."*

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
