# The MCP server

`obliskiq-lite-mcp` is the third entrypoint, alongside `obliskiq-collect` and
`obliskiq-compute`, and **the only one that binds a port**. It wraps the store,
the grid, the ledgers and the risk engine as MCP tools, so the workflow runs from
Claude Code *or* claude.ai — scripts inside a skill folder only run where there
is a shell.

It **reimplements nothing**. Every tool composes the engines in
`compute/` and the ledger writes in `store/ledger.py`; a rule has one home. It
also **makes no network requests of its own**: live quotes, positions and NAV are
passed in as tool arguments by whoever holds the IBKR connector.

```
obliskiq-mcp stdio     local transport for Claude Code; binds nothing
obliskiq-mcp http      streamable HTTP on OBLISK_MCP_HOST:OBLISK_MCP_PORT
```

There is no default transport. Guessing one from the environment means a
misconfigured deployment binds silently to the wrong thing.

---

## The tools

| Tool | Returns | Writes |
|---|---|:--:|
| `get_sleeve_status` | Available option budget net of the penalty AND of premium committed to open positions, what closed the sleeve when it is closed, gains still required, affordability ceiling | |
| `get_grid_rankings` | The four ranked views — each row with its cell's trajectory — flagged correlation pairs, grid coverage | |
| `get_grid_cell` | Specific cells with window, units, status and — when absent — the reason | |
| `get_portfolio_review` | Positions joined to `position_ledger`: thesis verbatim, stops, caps, days held | |
| `check_position_risk` | Greeks, N(d₂) on the strike's own smile, the zombie test, theta budget, stops, caps, breaker | ● |
| `get_commodity_state` | Curve and roll, COT percentile, cycle position, inventory deviation, proxy beta/R², wrapper | |
| `get_fx_real_returns` | Four-way decomposition, flags, carry-illusion result | |
| `screen_options` | Candidates surviving budget, liquidity, PRIIPs and tenor gates, split into four buckets | |
| `get_catalyst_calendar` | Derivable dated events, plus the classes that cannot be verified offline | |
| `log_entry` | Opens a position — after every entry gate — and commits its premium to the sleeve pool | ● |
| `log_exit` | Closes one, and settles the sleeve consequence | ● |
| `log_reunderwrite` | Appends a dated re-underwrite | ● |
| `upsert_external_position` | Manual entry for a non-IBKR holding | ● |
| `record_iv_percentile` | Stores a vendor IV percentile against an underlying, with its route | ● |
| `run_collectors` | Spawns `obliskiq-collect` as a separate process | ● |
| `get_system_health` | Last run per collector, the last sequence's planned versus completed steps, grid coverage with its `as_of`, NAV age, handshake age against its TTL, vendor credit spend | |

Read tools carry `readOnlyHint`. The writers do not, and three of them are worth
knowing about:

**`log_entry` writes twice, in one transaction, and the second write is the
sleeve's cap.** After the
position row it books a `premium_committed` event for what the entry ties up, and
available budget is `budget_pct × NAV − penalty_balance − committed_premium`
from then until the position closes. That is why `contracts` is required on an
option: a commitment nobody sized cannot be subtracted, and an uncounted one lets
`option_sleeve.max_concurrent_positions` entries each spend the whole sleeve.
There is no release event — closing the position is the release.

The premium is booked in `account.base_currency`, converted through the same
rate the affordability gate uses, and an entry whose currency has no rate in the
store is **refused**: a commitment that cannot be converted cannot be subtracted
from a base-currency pool, and booking it raw moves available budget by the
exchange rate. The row keeps the quoted amount and the rate beside the converted
figure, so the conversion is auditable rather than reconstructed.

`log_entry` also records what the position IS — `is_option`, `contracts`,
`strike`, the right, `expiry`, `entry_currency` — so no later gate has to be told
again from memory. `expiry` is required on an option for that reason: without it
the mandatory-close date is never computed, and the gate used to disappear from
both the checked and the untested list.

**`log_exit` reads the sleeve from the ledger.** `is_option` was a caller
argument defaulting to true, so closing an equity position without saying so
booked an option penalty against a loss the sleeve never carried. It is now read
from the row, a contradicting value is refused, and a row written before the
field existed makes the tool ask rather than guess. `realised_pnl` is converted
to base the same way the commitment is.

**`record_iv_percentile` exists because the routes disagree.** The IBKR MCP
connector serves `implied_volatility_percentile`; the collectors run on Client
Portal, where no field id for it has been verified. Without this tool a session
can read the number and has nowhere to put it, and grid indicator 8 stays `n/a`.
It refuses anything outside 0–1 rather than rescaling it, and writes only the
fields it was given, so it cannot blank what a collector wrote that morning.

**`check_position_risk` writes on purpose.** The zombie test needs the four
conditions to hold across `zombie_test.consecutive_sessions_required` consecutive
review sessions, and it books the penalty at *detection* rather than at close.
Neither works without recording each review, so a read-only version would be a
zombie test that could never fire. Pass `record: false` for an explicit dry read.

**`run_collectors` spawns rather than imports.** The collectors hold IBKR
credentials, a chain pull runs for minutes, and the same image has to deploy to a
VM running no server at all.

**`get_system_health` answers the question a thin grid raises.** SKILL.md tells
the model to report a missing grid rather than reason over the gap — and until
this tool existed, no tool said how far behind the collectors were, so staleness
had to be inferred from absence. It composes `collect.status.collector_status`
and `compute.build_grid.coverage_report` rather than counting anything itself,
touches no network, and works when every upstream is down, which is the case it
is for.

**The verdict is derived from named concerns, and AGE is one of them.** The
payload carries a `concerns` list and the verdict is `attention` whenever it is
non-empty, so every verdict can be checked against the reasons that produced it
rather than trusted as a score. Age matters most: `collector_status` reports the
newest run per collector with no window on it, so a collector that last ran
cleanly three months ago arrives as a tidy `ok` row — which is exactly what a
machine whose timer stopped firing looks like. A run's status describes that
run; the concern describes the silence since. The bound is
`server.stale_after_days`, the same one the response envelope applies, and every
age is measured against the `as_of` asked for rather than the wall clock.

Three things it will not do. It **reports an empty store as empty**: a store no
collector has ever run against has produced no evidence that the pipeline works,
and that is a different claim from evidence that it does. It reports the age of
the **last successful handshake**, never a live token — collectors are
`Type=oneshot` and mint a fresh one per run, so the TTL comparison answers "has
anything authenticated inside the window a token would have lasted". And it
lives **behind the bearer token**: `/healthz` is exempt because it reveals
nothing, while last-run timestamps, coverage counts and credit spend are
operational intelligence about a private account.

### What `log_entry` refuses

The brief's rule is that **the skill must never be the only thing between a bad
trade and the ledger**. `store.ledger.open_position` enforces ledger integrity —
a thesis exists, an invalidation trigger exists, the entry price is positive —
and can enforce nothing about the mandate, because it sees neither the chain nor
the sleeve. `server/guards.py` closes that gap before the write, composing the
same `CandidateGates`, `PenaltyLedger` and `TradingCalendars` the census and the
screener use:

- a **prohibited instrument** — futures are never a candidate, in any code path
- an **entry outside the tenor window**, or one that does not clear the catalyst
  buffer
- an **illiquid contract**: spread, open interest, session volume
- a **premium above the affordability ceiling**, or below the premium floor
- a **book already at `option_sleeve.max_concurrent_positions`**, counting the
  option sleeve alone: cash equity holdings do not consume slots in a sleeve they
  are not in
- a **mandatory close date that disagrees with the trading calendar** — it is
  recomputed here rather than trusted, which is how an option on a US-listed ETF
  avoids being held into an OCC auto-exercise
- **averaging down** without a logged re-underwrite carrying a new dated catalyst
- an **implied vol above `screen.cheap_iv_percentile_max`**. The screener applied
  this to every candidate it surfaced and the write path did not, so an entry the
  screen would never have shown could still be logged by naming its contract
- a **concentration cap breached with this entry added to the book** — position,
  sector, country and single foreign currency, under `positions`. The caps govern
  what is bought, and they were computed for reporting only. They need the book
  (`positions`) and the size (`contracts` or `quantity`); without either they are
  reported untested rather than measured against the part of the book the store
  happens to know
- a **tripped drawdown circuit breaker**. An entry is new risk, which is exactly
  what `portfolio_circuit_breaker.action` freezes; managing open risk is
  untouched, so nothing here reaches `log_exit` or `check_position_risk`

`log_exit` requires an `exit_reason` from `discipline.exit_reasons_allowed`.
Price movement alone is not on that list and a caller cannot add it.

`thesis_text` and `invalidation_trigger` are frozen at entry. **No tool exposes
either for update** — a test asserts it across the whole surface — so thesis
drift stays detectable rather than becoming a matter of recollection.

---

## Authentication

Bearer token, from `OBLISK_MCP_AUTH_TOKEN`, compared in constant time.

**The server refuses to start when `OBLISK_MCP_HOST` is not loopback and no token
is set.** The refusal happens while resolving the environment, before anything
binds. On loopback with no token it starts and logs a prominent warning: local
development is permitted, but anything that can reach that port can read the
ledger and write to it.

`GET /healthz` is the single unauthenticated path. It returns `{"status":"ok"}`
and nothing else — an unauthenticated caller learns that the process is alive and
no more. Every other path, `/mcp` above all, answers `401` with
`WWW-Authenticate: Bearer`.

**And it stays that way.** Operational detail — what ran last night, how full the
grid is, what the vendor route has spent — is served by `get_system_health` on
the authenticated surface instead. Putting it on `/healthz` would trade the one
property that makes an exempt path safe for the convenience of a `curl`.

The token is never logged, at any level.

Generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Prefer loopback plus a reverse proxy** that terminates TLS and connects over
`127.0.0.1`. Binding `0.0.0.0` directly makes the token the only thing between
the internet and the ledger.

---

## Deploying on a small always-on VM

The server, the collectors and the computation layer are **three processes with
three schedules**, and keeping them apart is deliberate:

| Process | Shape | Credentials | Network |
|---|---|---|---|
| `obliskiq-collect` | oneshot, on a timer | IBKR OAuth + macro API keys | yes |
| `obliskiq-compute` | oneshot, on a timer | none | **none** (`PrivateNetwork=true`) |
| `obliskiq-mcp` | long-running, binds a port | auth token only | listens only |

```bash
# One user, one database, three units.
useradd --system --home /opt/obliskiq obliskiq
install -d -o obliskiq -g obliskiq /var/lib/obliskiq /etc/obliskiq

python3 -m venv /opt/obliskiq/.venv
/opt/obliskiq/.venv/bin/pip install /path/to/obliskiq_lite_mcp-*.whl
install -m 0644 config.yaml universe.yaml calendars.yaml /opt/obliskiq/

# Two environment files, on purpose: the server needs no IBKR credentials and
# the collectors need no auth token.
install -m 0600 -o obliskiq deploy/collect.env.example /etc/obliskiq/collect.env
install -m 0600 -o obliskiq deploy/server.env.example  /etc/obliskiq/server.env
$EDITOR /etc/obliskiq/collect.env /etc/obliskiq/server.env

install -m 0644 deploy/*.service deploy/*.timer /etc/systemd/system/
systemctl daemon-reload

# The collectors first, and today. Option-chain history cannot be retrieved
# retrospectively from IBKR: for the European listings a day not collected is
# gone permanently. US chains come from marketdata.app, which serves history.
systemctl enable --now obliskiq-collect@daily.timer
systemctl enable --now obliskiq-collect@macro.timer
# The weekly universe sync. Without it a closed position keeps consuming API
# budget forever, and the census has no NAV to compute a ceiling from.
systemctl enable --now obliskiq-collect@universe.timer
systemctl enable --now obliskiq-compute@all.timer
systemctl enable --now obliskiq-compute@census.timer

# Then the server.
systemctl enable --now obliskiq-mcp.service
curl -s localhost:8000/healthz
```

`deploy/crontab.example` carries the same schedules for a machine without
systemd. Nothing in the pipeline assumes a human or a chat session is present.

### Containerised

```bash
docker build -t obliskiq-lite-mcp .
docker run -d --name obliskiq-mcp \
    -p 127.0.0.1:8000:8000 \
    -v obliskiq-data:/var/lib/obliskiq \
    -e OBLISK_MCP_AUTH_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
    obliskiq-lite-mcp
```

The image sets `OBLISK_MCP_HOST=0.0.0.0`, so **omitting the token fails at
startup** rather than serving the ledger unauthenticated. `OBLISK_DB_PATH` is the
only filesystem assumption; everything else comes from the environment.

The collectors are installed in the image but are not its job. Either run them on
the host on a timer against the same volume, or schedule
`docker exec obliskiq-mcp obliskiq-collect daily` — which is also what
`run_collectors` does from inside.

---

## Connecting a client

**Claude Code, locally** — `.mcp.json` in the repository, stdio, no token:

```json
{
  "mcpServers": {
    "obliskiq": {
      "command": "obliskiq-mcp",
      "args": ["stdio"],
      "env": { "OBLISK_DB_PATH": "/var/lib/obliskiq/obliskiq.db" }
    }
  }
}
```

**A remote server** — the streamable-HTTP endpoint is `/mcp`, with the token as
a bearer credential:

```
https://your-host.example/mcp
Authorization: Bearer <OBLISK_MCP_AUTH_TOKEN>
```

`--stateless` serves each request without a persistent session, for a deployment
behind a load balancer that will not pin a client to one instance.

---

## Operating it

**Every response carries the date it was measured on.** `as_of` is what was
asked for, `source_as_of` is what the data was actually computed for, and a read
older than `server.stale_after_days` comes back marked `stale` with a note. A
weekend session built on Friday's marks is a documented failure mode of this
account; the envelope is what makes it visible.

**No tool returns an unbounded table.** Results are capped at
`server.max_rows_per_response` and say so when truncated.

**A gate closing is the answer, not a fault.** Refusals arrive as tool errors
carrying the gate's own reason verbatim. Quote the reason rather than working
around it.

**A missing value is missing.** Non-finite floats are serialised as `null`: a
NaN that arrives as a number is a lie in the shape of data.

**Schema migrations run once at startup.** A store that cannot migrate stops the
process rather than surfacing on whichever tool the session happens to call
first. Each tool call then takes its own short-lived SQLite connection — `Store`
is not thread-safe — and WAL, set by `runtime.sqlite.journal_mode`, is what makes
that safe while the collectors are writing.

---

## Verified, and not

Verified offline in the sandbox, against a store built from recorded fixtures:

- Both transports. The HTTP path was driven by the MCP SDK's own
  streamable-HTTP client — initialize, `tools/list`, real tool calls.
- The auth rules: `/healthz` unauthenticated, `401` with
  `WWW-Authenticate: Bearer` for a missing or wrong token, and a startup refusal
  when the host is not loopback and no token is set.
- Every tool end to end against a populated store, including the honest `n/a`
  paths where the fixtures do not reach.
- The full write path: a gated entry, a mandatory close date recomputed against
  the venue calendar, an averaging-down refusal on a second entry, a penalty
  booked at the configured multiple of a realised loss, and the sleeve reduced
  accordingly.
- The wheel: it builds, installs into a clean virtualenv with all three
  entrypoints, and carries the migrations and the `py.typed` marker.
- The container's `HEALTHCHECK` command, run against a live server.

**Not verified**, and marked as such:

- **The image build itself.** There is no Docker daemon in the sandbox, so the
  `Dockerfile` was never built. Every command inside it was exercised
  individually — the wheel build, the install, the entrypoint, the no-token
  refusal under `OBLISK_MCP_HOST=0.0.0.0`, the healthcheck — but the layering
  is unproven. Build it once before relying on it.
- **claude.ai's remote-MCP client specifically.** The transport is exercised by
  the reference client, not by that one. If it rejects the connection, the thing
  to check first is whether it requires OAuth resource-server metadata rather
  than a static bearer token.
- **Anything touching live IBKR.** The server makes no IBKR calls, so nothing
  here belongs in `references/ibkr-playbook.md` — but the data it reads was
  collected against fixtures, and the collection route itself is still unverified.
