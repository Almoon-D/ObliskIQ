# Build brief — ObliskIQ-lite-MCP

**Architecture, the store schema and the tool surface.** Nothing else: judgment
lives in `.claude/skills/obliskiq-lite/SKILL.md`, thresholds in `config.yaml`,
observed API behaviour in `references/*-playbook.md`, what is open in
`ROADMAP.md`, and why the design is as it is in `docs/DECISIONS.md`.

This file states no threshold and restates no config value.
`tests/test_skill_config_consistency.py` fails the build if one appears here.

---

## Two components, and conflating them is how this goes wrong

**The MCP server (`obliskiq-lite-mcp`) owns state and computation.** The SQLite
store, the collectors, the grid, the ledgers, the risk maths. It has to be a
server rather than scripts inside a skill because the design depends on **state
that persists between sessions and collectors that run on a schedule with no chat
session open**. A skill is a stateless instruction set; it cannot maintain a
penalty balance or accumulate chain snapshots.

**The skill (`SKILL.md`) owns judgment.** Gates, tiering, the output contract,
failure modes, the discipline rules. It says how to think and which tools to call
in what order. It holds no data.

The payoff: with the pipeline behind an MCP server the workflow runs from **either**
Claude Code or claude.ai, because the server is reachable from both. Scripts in a
skill folder only run where there is a shell.

### Three entrypoints, deliberately separate

| Entrypoint | Binds a port | Touches the network | What it is |
|---|---|---|---|
| `obliskiq-collect` | no | yes — IBKR, marketdata.app, the macro sources | the unattended collectors |
| `obliskiq-compute` | no | **no** | the grid and the engines, over the store |
| `obliskiq-mcp` | yes | no | the tool surface |

The split is what lets the collectors run on a small always-on VM under a systemd
timer with no human present, lets the compute unit enforce `PrivateNetwork=true`,
and keeps the server free of IBKR credentials — live quotes, positions and NAV are
passed in by whoever holds the connector.

### Scheduling

`chain_collector`, `curve_collector` and `macro_pull` run on a **scheduler
independent of any chat session**. For the three European listings a day not
collected is gone permanently; everything else is recoverable. See
`docs/COLLECTORS.md` for the sequence and `deploy/` for the units.

---

## The MCP tool surface

| Tool | Returns |
|---|---|
| `get_sleeve_status` | Available option budget, penalty balance, realised gains still required, affordability ceiling |
| `get_grid_rankings` | Top N by dimension: most extended, most compressed, widest IV−HV, largest correlation shift. Each row carries a named set of trajectory leaves from its own cell, so a level is read against the direction it is moving in |
| `get_grid_cell` | Specific market / sector / indicator with its window, sample count and status |
| `get_portfolio_review` | Positions joined to `position_ledger` — thesis verbatim, invalidation, stops, caps, days held |
| `check_position_risk` | Volatility-scaled stop, trailing stop, zombie test with two-session state, cap utilisation |
| `get_commodity_state` | Curve shape and roll, COT percentile, inventory seasonal deviation, proxy beta/R², wrapper recommendation |
| `get_fx_real_returns` | Four-way decomposition, inflation and depreciation flags, carry-illusion result |
| `screen_options` | Candidates surviving budget, liquidity, PRIIPs and tenor gates |
| `get_catalyst_calendar` | Verified dated events across holdings and watchlist |
| `log_entry` / `log_exit` / `log_reunderwrite` | Ledger writes; `log_exit` requires a permitted `exit_reason` |
| `upsert_external_position` | A holding at a broker with no readable API, so NAV and every concentration cap see the whole book |
| `record_iv_percentile` | A caller-supplied vendor IV percentile against an underlying, with its source |
| `run_collectors` | Manual trigger for chain, curve and macro pulls |
| `get_system_health` | How far behind the collectors and the grid build are: last run per collector, the last sequence's planned versus completed steps, grid coverage and its `as_of`, NAV age, the age of the last Client Portal handshake against its TTL, vendor credit spend. An EMPTY store reports empty |

`tests/test_mcp_tools.py` asserts this list exactly: a tool renamed without the
brief being updated is a tool the skill will call and not find.

**`get_system_health` is on the authenticated surface, and `/healthz` is not it.**
That endpoint is exempt from the bearer check precisely because it reveals
nothing — an unauthenticated caller learns the process is alive and no more.
Last-run timestamps, coverage counts and vendor credit spend are operational
intelligence about a private trading account, so they sit behind the token with
every other tool. The exempt path is not widened.

**Ledger writes are the only mutating tools**, and they validate against
`config.yaml` and reject gate violations **server-side**. `server/guards.py`
composes the same gate objects the census, the screener and the risk engine use,
so the skill is never the only thing standing between a bad trade and the ledger.
No tool can edit a thesis. There is **no order-staging tool**: staging runs
through the IBKR MCP connector under the rule SKILL.md states.

An input only the caller can state is required and its absence is a refusal; an
input the *store* may not hold leaves its gate UNTESTED and reported. An untested
gate is never a passed gate, and the two are never conflated.

`docs/SERVER.md` covers transports, auth, deployment and what each refusal means.

---

## The persistent store

SQLite. The grid is markets × sectors × fifteen indicator families — a table to
compute, not to reason through. Total footprint is a few hundred MB; storage is
not the constraint, API rate limits and backfill time are.

**`src/obliskiq/store/migrations/` is authoritative for the schema.** The sketch
below is the shape, for orientation:

```sql
bars(contract_id, ts, freq, open, high, low, close, volume)

chain_snapshots(snapshot_date, underlying_id, expiry, strike, right,
                bid, ask, last, iv, delta, open_interest, volume,
                source, option_exchange, market_data_status)

macro(series_id, source, reference_date, release_date, value)

grid(as_of, market, sector, indicator, value, status,   -- ok | n/a | stale | inapplicable
     n_obs, market_data_status)

universe(contract_id, symbol, exchange, currency, asset_class,
         market, sector, is_native_index, listing_domicile, priips_restricted,
         dividend_yield, dividend_yield_as_of, history_exchange, option_exchange)

nav_history(as_of, nav_base, source, recorded_at)

underlying_snapshots(snapshot_date, contract_id, last, bid, ask, volume,
                     implied_vol, implied_vol_source,
                     implied_vol_solved, implied_vol_solved_source,
                     dividend_yield, dividend_yield_basis,
                     iv_percentile_13w/26w/52w, source, market_data_status)

position_ledger(contract_id, opened_at, entry_price, entry_range_pctile,
                thesis_text, invalidation_trigger, target, time_window_end,
                mandatory_close_date, prob_target_at_entry,
                reunderwrite_log, closed_at, exit_reason, realised_pnl,
                is_option, contracts, strike, option_right, expiry,
                entry_currency)

option_ledger(event_at, event_type, contract_id, premium_paid, proceeds,
              realised_pnl, penalty_added, penalty_balance_after, resolved_at,
              amount_currency, amount_quote, fx_to_base)
```

Migrations are forward-only and applied in order; `tests/test_migration_chain.py`
runs the chain against a populated store.

**`correlation_matrix` is keyed on a LEG, not on a market.** A market is not an
identifier: it has an equity index, a policy rate and a long rate, and those are
three series. Each side carries an asset class (`equity | fx | rate`) and a
series id, with the market it belongs to beside it. For an `fx` or `rate` leg
the series is the macro id the market declares; for an `equity` leg it is the
market NAME, because a market is measured on exactly one index contract and so
names that series uniquely. `n_obs` is the overlapping observations behind the
correlation and `norm_independent` the non-overlapping readings behind its norm —
both were previously stated only inside the reason prose, which is where a
number goes to stop being checkable.

### Columns whose meaning is not obvious from their name

**`universe.dividend_yield`** is a continuous yield q as a decimal, **declared in
`universe.yaml`**, and carries `dividend_yield_as_of` because a yield is a fact
about a date. `NULL` means nobody has declared one — which is not the claim that
the instrument pays nothing.

**`underlying_snapshots.dividend_yield`** is the **collected** twin of it, and the
two are different facts rather than a duplication. The declaration says which
instruments need a yield; this says what one was measured at on a session. It is
dated because `universe` is current-state, and a collected yield written there
would let a grid rebuilt for a past session reprice that session with today's
number. `dividend_yield_basis` records which basis the figure is —
`trailing_12m` is what IBKR serves, and BSM wants a continuous q, so the column is
what stops an approximation passing as the model's q. `cp_api` owns the column;
the MCP connector cross-checks it and cannot own it, because it cannot run
unattended. Precedence wherever a yield is read is **caller → collected →
declared → none-declared**, and `check_position_risk` and the entry guard both
report which of the four they used, so an undeclared yield never reads as a
measured zero.

**`underlying_snapshots.implied_vol_solved`** is an implied vol this repository
inverted out of a mid price, kept in a **separate column** from `implied_vol`
rather than tagged inside it. `iv_percentile` ranks a reading against its own
history, so a series mixing solved and exchange-quoted values has a methodology
break at the join and reports a regime shift that did not happen. Two columns make
that impossible rather than discouraged. `implied_vol_solved_source` carries
`solved_bsm_european/q_<tier>`: the model is European, SPY and the sector ETFs are
American-style, and nothing here handles early exercise —
`numerics.implied_vol.solve` bounds where the number is produced and the label
travels with it to the grid cell. `data.atm_iv_prefer` decides which source
indicator 7 reads when more than one exists. q enters d₁ and discounts
the spot, so pricing a payer at zero overstates both N(d₂) and delta, and the
zombie test compares today's N(d₂) against the one recorded at entry — which puts
the bias directly on the exit gate.

**`macro.units`** records what a row carries, in the source's own words. Written
by every fetcher that has one to give — FRED's `units_short`, EIA's per-row
`units`, CFTC's `net_noncommercial_share_of_open_interest` — and NULL where the
publisher states none, which is a gap rather than a claim. It exists because a
percentile ranks a value against others of the SAME quantity and a year-on-year
rate divides two of them, so a definition change inside a window is not a thin
sample: it is two measurements compared as one. `MacroSeries.units_known_at`
refuses the positioning window, and `cpi_yoy` refuses the ratio, when the units
inside disagree. A NULL never contradicts a declared unit.

The declaration side of the same fact is **`macro.series[].value_vintage`** in
config.yaml: `initial` (the default, every existing series) stores the number as
first printed, and `latest` stores it as it stands now while still dating it from
the initial release. `latest` is only ever right for a REBASED index — Eurostat's
HICP moved to 2025=100 during 2026 — where the first-print column holds two bases
and a ratio across them is arithmetic between units. `docs/DECISIONS.md` carries
the trade-off; the FredFetcher docstring carries the mechanics.

**`chain_snapshots.market_data_status`** records the quote state a row was
captured in — FROZEN, DELAYED, REALTIME, or NULL where the route reported
nothing. It travels through `ChainRow` and `UnderlyingSnapshot` into
`check_position_risk`, `screen_options` and every grid cell, because SKILL.md
requires anything frozen or delayed to be labelled indicative wherever it appears.
`data.market_data_status_live` says which values count as live. NULL is
*unreported*: a third answer, not either of the others in disguise.

**`chain_snapshots.source`** records the route that collected the row (`mcp |
cp_api | marketdata_app`), so collection can be migrated wholesale if one route
turns out to exclude the other.

**`chain_snapshots.option_exchange`** records the VENUE that answered, which is a
different fact from the route that asked. A ticker names several listings and
their option venues differ; without this column a US-listed chain collected under
a European universe row is invisible in the data and survives only in a run log
that rotates away. `universe.option_exchange` is its declared twin — the venue an
option request must send, which is not the listing venue: measured 2026-09-10,
the Madrid Santander line lists its options on EUREX and MEFFRV and the Paris
TotalEnergies line on EUREX and MONEP. NULL there means undeclared, and is not
the claim that SMART is right.

**`nav_history` is written by the collectors**, from the consolidated `BASE` row
of the account ledger, and `as_of` is the day the reading was TAKEN. It is a
series rather than a current value because the drawdown circuit breaker measures
across a rolling month; back-dating a figure read today onto an earlier session
would put a number the account never had into the window it reads. A figure whose
currency is not `account.base_currency` is refused rather than converted.

**`position_ledger.is_option` has three states.** 1 and 0 say which sleeve a
position belongs to; NULL says the row was written before the fact was recorded.
It decides whether a realised loss carries `option_sleeve.loss_penalty_multiple`,
and it used to be re-declared by the caller at every close with nothing able to
check the answer — so an equity close could book an option penalty, and an option
close could escape one. `log_exit` now reads it and refuses a caller value that
contradicts it; on a NULL row it asks rather than guessing, because the two
mistakes are not symmetric and a booked penalty cannot be reversed.

**Every amount in `option_ledger` is in `account.base_currency`**, and
`amount_currency` / `amount_quote` / `fx_to_base` record what it was before
conversion. The affordability gate has always converted a premium before
comparing it against the sleeve; the write did not, so a non-base position moved
available budget by the exchange rate. A figure that cannot be converted is
refused rather than booked — the same direction the gate refuses in.

**`grid.status` has four values, not three.** `inapplicable` covers a cell no
collector could ever fill — no instrument on the axis, no macro series in the
market spec, no futures contract — and it is **declared** at each site rather than
inferred from an absence. Two coverage numbers follow from that: populated over
collectible, which is collection health, and collectible over total, which is the
ambition gap.

The declaration site for "no instrument measures this cell" is `unmapped_cells:`
in `universe.yaml`, and the loader refuses a universe where a market × sector
pair is neither measured nor declared. A cell whose instrument IS declared and is
missing from the store is a different status again — `instrument_not_seeded`,
which is `n/a` and stays in the collection denominator, because a universe
collector that never ran must not be able to shrink the denominator until the
coverage floor passes.

**`grid.n_obs`** is the sample the cell actually got, not the window it asked for.

**Freshness is per input, not per cell.** A market × sector cell is fed by four
stores on four cadences. `grid.freshness_days` carries one window per input and
`indicators.FAMILY_INPUTS` declares what each family reads as requirement groups:
every group must hold, and within a group any one source will do. A family reading
nothing but macro does not go stale because an equity bar is old.

**Every store read is bounded at `as_of`**, the snapshot `COUNT(*)` included, so a
grid rebuilt for a past date cannot read data recorded after it.

### `position_ledger`

`thesis_text` and `invalidation_trigger` are written once at open and **never
edited** — changes append to `reunderwrite_log` as dated entries, which makes
thesis drift detectable rather than a matter of recollection. This is enforced by
the SQL trigger `trg_position_thesis_frozen`, not by application code and not by a
config flag. `exit_reason` is constrained to `discipline.exit_reasons_allowed`;
`override` should be rare enough to be conspicuous.

`prob_target_at_entry` stores P₀ = N(d₂) at the target, computed at entry from the
**strike's own smile IV, not ATM vol**. The zombie test compares against it.

### `option_ledger` and the penalty balance

This table enforces the sleeve budget and is not optional. The skill must be able
to answer "what is my available option budget" at session start without the user
remembering anything.

- `event_type` ∈ `{premium_committed, loss_detected, loss_closed, gain_realised,
  qualifying_equity_gain}`
- On **entry**, a `premium_committed` row carries what the position actually costs
  (`entry_price × multiplier × contracts`). This is what makes the sleeve a
  **shared pool** rather than a per-position allowance, and it is not optional:
  without it the budget does not cap.
- On **zombie detection or invalidation**, a `loss_detected` row applies the
  penalty. **At detection, not at close** — booking at close would reward
  deferring the close to postpone the penalty.
- `loss_closed` reconciles the realised figure against the detected estimate,
  adjusting by the delta only.
- `qualifying_equity_gain` rows are written only for exits in
  `option_sleeve.qualifying_exit_reasons`.
- `available_option_budget = max(0, budget_pct × NAV − penalty_balance −
  committed_premium)`, where `committed_premium` sums `premium_committed` rows
  whose position is still OPEN. A commitment needs no reversal row — it stops
  counting the moment the position closes. **Budget left unspent stays unspent**:
  it is never redistributed across the remaining concurrency slots.
- Reconcile against `get_account_trades` at session start, so trades executed
  outside a session still register.

---

## The components, and what each owns

**Collection** — `collect/`, entrypoint `obliskiq-collect`. `universe` seeds the
tracked instruments; `backfill` fills historical and incremental bars; `chain`
snapshots the European option chains; `vendor-chain` collects and backfills the
US chains from marketdata.app; `curve` snapshots futures term structure; `macro`
pulls FRED, ECB, CFTC, EIA, the ONS and Statistics Canada into `macro` at native
frequency with **both** dates; `daily` runs the scheduled sequence in order. `docs/COLLECTORS.md` owns
operation, the strike band, the sigma ladder, exit codes and deployment.

**Computation** — `compute/`, entrypoint `obliskiq-compute`. `build_grid` computes
every cell and the coverage report; `rankings` emits the four ranked views and the
correlation matrix; `risk_engine` carries the greeks, the zombie test, the theta
budget, the equity stops and the drawdown circuit breaker; `fx_engine` the
four-way decomposition and the carry-illusion test; `commodity_engine` the curve,
positioning, inventory and proxy regressions with a wrapper recommendation;
`universe_census` counts how many contracts clear every gate at once.
`docs/COMPUTE.md` owns the engines and the grid axes.

**The server** — `server/`, entrypoint `obliskiq-mcp`. It reimplements nothing:
every tool composes the engines above, and `server/guards.py` composes the gates.

**Shared** — `store/` (schema and migrations), `quant/bsm.py` (Black-Scholes,
greeks, the implied-vol solver), `ibkr/` (OAuth 1.0a and the Client Portal
endpoints), `macro/` (series declarations, fetchers, the risk-free rate),
`config.py`, `env.py`, `errors.py`, `logging_setup.py`, `util/`.
