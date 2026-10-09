---
name: obliskiq-lite
description: Runs a global volatility-dislocation workflow over a precomputed market/sector/indicator grid, using the full IBKR toolset for account, market, options and order data, plus FRED, ECB, CFTC and EIA for macro. Produces portfolio thesis reviews, the market-by-sector grid with cross-market correlations, implied-versus-realised volatility screens, and a trade table with sized positions and observable invalidation triggers. Use this whenever the user asks about their portfolio, position sizing, what to buy or sell, market or sector analysis, implied volatility, option chains or strategy, macro regime, correlations, "what's coiled", "where's the next big move", or wants market strategy work of any kind — even if they don't mention volatility or options explicitly. Also use for single-position questions like "should I sell X" or "is Y cheap", which require the same gates and verification.
---

# ObliskIQ lite

Find where large moves are most likely to happen next, anywhere in the world, and position for them with defined risk. Cheap quality companies are a bonus, not the objective.

The user is an EU retail client with a global, high-return mandate. Three failure modes dominate and every rule below exists to counter one of them: recommending things they cannot execute, viewing the world through a US lens, and buying strength then capitulating into weakness.

---

## Where the numbers live

**`config.yaml` owns every threshold, cap, window and tenor. This file owns judgment, method and evidence.**

Rules below name config keys rather than restating their values. Read the value
from `config.yaml` at runtime, and quote it in the output so the user can see
what bound. Never copy a number out of config into this file: a copy is a future
contradiction, and changing a threshold must never require editing this file.

Historical examples, worked illustrations and the failure modes below carry
figures deliberately. They are evidence of what happened, not gates — they do
not change when config does.

---

## The tools

The `obliskiq-lite-mcp` server owns the store, the grid, the ledgers and the risk
maths. It holds no live market data and makes no network requests: quotes,
positions and NAV come from the IBKR connector and are **passed in** as
arguments. Every response carries the date it was measured on.

Call them in this order. The order is not a style preference — leading with the
budget is what stops the failure mode below it.

| Step | Tool |
|---|---|
| 1. Budget first, always | `get_sleeve_status` |
| 2. The book, thesis verbatim | `get_portfolio_review` |
| 3. Per position, with live quotes | `check_position_risk` |
| 4. Where the moves are | `get_grid_rankings`, then `get_grid_cell` for a specific reading — and `get_grid_cell(..., history=N)` to see that one cell MOVE |
| 5. Currency discipline | `get_fx_real_returns` |
| 6. Commodity views | `get_commodity_state` |
| 7. Candidates | `screen_options` |
| 8. Dates | `get_catalyst_calendar` |
| 9. Decisions | `log_entry`, `log_exit`, `log_reunderwrite` |
| Manual entry | `upsert_external_position` for a holding at a broker with no API |
| Catch-up | `run_collectors` when a scheduled run was missed |
| When a read comes back thin | `get_system_health` — how far behind the collectors and the grid build are |

**Reconcile before trusting the sleeve.** `get_account_trades` defaults to **today**, and a window that is too short reports an empty book rather than an error. A trade executed outside a session that never reached the ledger leaves the sleeve overstating what is available — by twice the loss, for an option. Pull at least year-to-date at session start, and reach further back with the quarterly periods when the ledger looks emptier than the account.

**`get_sleeve_status` comes first.** Discovering the budget last is the most
repeated failure mode in this account: whole global analyses have been produced
and then found unaffordable. Report available budget, penalty balance and the
gains still required at the top of every session, and filter the universe by the
affordability ceiling **before** screening.

**`check_position_risk` records.** It writes each review so the zombie test's
consecutive-session confirmation can accumulate, and it books the penalty at
detection. Call it on every option position at every review, not only on the ones
that look troubled — a session skipped is a confirmation lost.

**Grid reads are precomputed.** `get_grid_rankings` reads what
`obliskiq-compute` built; it does not compute anything now. If it reports no grid,
the collectors or the compute run are behind — say so rather than reasoning over
the gap. A cell returned `n/a` carries the reason it is missing: quote the reason,
never substitute a proxy.

**When a grid read comes back THIN OR EMPTY, call `get_system_health` before
anything else.** It is the only tool that says how far behind the pipeline is,
and without it staleness has to be inferred from absence — which cannot tell a
market with nothing to report from a collector that has not run since Tuesday.
It reads last run and status per collector, what the last sequence planned
against what it completed, grid coverage with the date it was built for, NAV
age, how long since a collector last authenticated against IBKR, and what the
vendor route has spent. Three verdicts, and the first is the one to read out
loud: `empty` means nothing has ever run in this store, so there is no evidence
the pipeline works at all — it is never "all clear"; `attention` means something
is behind or failing; `ok` is a claim about the COLLECTORS and never about the
market. **Quote the `concerns` list rather than the verdict alone** — it names
each reason in words, including the one a run's own status cannot carry: a
collector whose last run was clean and three months ago is `ok` about that run
and silent about everything since. A reading taken while any concern stands is
indicative at best, and a grid that is thin for one of those reasons is a
collection gap rather than a market fact.

**And you cannot build one from here.** `run_collectors` runs the COLLECTORS;
`obliskiq-compute` is a separate entrypoint with no tool, deliberately, because it
is scheduled on the machine rather than driven from a session. So "no grid has
been built" is a thing to report and ask for — *run `obliskiq-compute all`* — and
never a thing to work around by reading raw tables and computing in your head.

**One snapshot is not a trajectory.** Every reading in a ranked row is measured
INSIDE one session; the windows and percentiles describe that session's history,
not the grid's. To see a cell actually move, ask for it:
`get_grid_cell(market=…, sector=…, indicator=…, history=N)` returns that one cell
across the sessions the store has accumulated, newest first, with unmeasured
sessions present as gaps rather than skipped. Two cautions it states itself:
compare rows only where `window` and `units` match, because the grid accumulates
history rather than re-measuring it; and on a young store there may be one row,
which is an answer about the store and not about the market.

**The server enforces the gates, and its refusals are answers.** `log_entry`
rejects a prohibited instrument, an entry outside the tenor window, an illiquid
contract, a premium the sleeve cannot afford, a book already at its concurrency
cap, a mandatory close date that disagrees with the trading calendar, an add
to a losing position with no logged re-underwrite, a target that does not pay
`option_sleeve.min_payoff_ratio`, a top-decile entry with no dated catalyst while
`discipline.entry_top_decile_requires_catalyst` holds, a tenor that decays
below the floor before the thesis can resolve while
`tenor.require_theta_budget_check` holds, an implied vol above
`screen.cheap_iv_percentile_max`, a `positions` concentration cap breached with
this entry added to the book, and any entry at all while the drawdown breaker in
`portfolio_circuit_breaker` is tripped. `log_exit` rejects any reason outside
the permitted set. When a tool refuses, quote its reason to the user and stop —
do not restructure the request until it passes.

**Money carries its currency.** The sleeve is denominated in
`account.base_currency`, so a premium or a realised P&L in another one is
converted at the stored rate before it is booked, and **refused** when no rate
for that pair is held. Pass `realised_pnl_currency` on `log_exit` whenever the
figure is not in the base currency; the entry's own currency is the default.

**`log_exit` no longer takes the sleeve on trust.** Which sleeve a position is in
is recorded at entry and read from the ledger, so a close cannot redeclare it.
Positions opened before that was stored are the one case where the tool asks.

**An option entry must state its own terms.** `log_entry` requires `expiry`,
`strike`, `is_call`, `target`, `entry_range_pctile` and either
`expected_resolution_days` or `time_window_end`, alongside `contracts`. Pass
`positions` and, on a non-option entry, `quantity`: without them the
concentration caps are reported untested rather than measured.

The required ones are what the gates above are computed from; omitted, they would
not be gates that passed but gates nobody ran. A gate reported `untested` is a
different thing — that is the store lacking an input, and it is reported rather
than refused.

**A thesis cannot be edited.** No tool exposes `thesis_text` or
`invalidation_trigger` for update. Changes append through `log_reunderwrite`, and
that is what makes drift visible.

**`get_catalyst_calendar` returns what it cannot verify.** Earnings, index
events, central-bank dates and corporate actions are not held by any collector.
The tool names those classes on every call; confirm each at source before relying
on it, and never read their absence from the calendar as absence in the world.

---

## Gates

Apply to every idea individually, not once per session.

### Option budget and the penalty balance

The option sleeve is capped at `option_sleeve.budget_pct` of current NAV. The remainder trades stocks, funds and bonds under its own rules and is not a cash buffer — it carries the return mandate.

Losses in the option sleeve carry a penalty multiple (`option_sleeve.loss_penalty_multiple`), held as a continuous balance rather than a binary lock:

```
penalty_balance += loss_penalty_multiple × realised_option_loss
penalty_balance −= qualifying_realised_gains        (1:1, floored at 0)
available_option_budget = max(0, budget_pct × NAV − penalty_balance − committed_premium)
```

**The budget is a shared pool.** `committed_premium` is the premium tied up by option positions that are still open. `budget_pct` caps the sleeve as a whole, not each position in it: without that subtraction, `option_sleeve.max_concurrent_positions` entries could each be sized against the entire sleeve. Budget left unspent stays unspent — it is never redistributed across the remaining slots. A commitment is released by the position closing, not by a second entry in the ledger, so `log_entry` requires `contracts` on an option and `get_sleeve_status` reports what is committed alongside what is available.

**Qualifying gains** are realised profits on closed non-option positions exited for one of the reasons in `option_sleeve.qualifying_exit_reasons`. Gains from an `override` exit never count. This prevents unlocking the sleeve by cutting winners early to bank profit.

**Recovery is measured on realised gains, never on NAV.** A NAV-based rule measures market beta rather than decisions: a general drawdown would extend the penalty indefinitely for reasons unrelated to the option, and a beta rally would clear it without anything being demonstrated.

Report at the top of every session: available budget in base currency, penalty balance outstanding, premium already committed to open positions, and the realised gains still required to clear the penalty. When the sleeve is closed, say **which** of the two closed it — `get_sleeve_status` returns `locked_by`, and the remedies are opposite: a penalty lock clears only on realised qualifying gains, a commitment lock clears the moment a position closes.

Consequences for every option recommendation:

- **The true cost of a loss is the premium multiplied by the penalty multiple.** Require asymmetric payoff of at least `option_sleeve.min_payoff_ratio` **at a realistic, named target price**. Reject anything that is merely "cheap vol." `log_entry` measures the named target against the ratio and refuses below it — do not restate the target to make the gate pass.
- **Not spending is normal.** Unspent budget carries forward at no cost. Never pad a recommendation to fill the sleeve.
- **Concentration beats diversification here.** Splitting does not reduce the penalty, it raises the probability of paying it, and `option_sleeve.max_concurrent_positions` is what bounds fragmentation — on its own. A position is refused for being small only when its costs dominate it: `option_sleeve.min_premium_base_currency` is an absolute amount in base currency, derived from real IBKR round-trip commission, and it is deliberately not a share of the budget. One position at a fiftieth of the sleeve beside another at four fifths, with the rest unused, is allowed by design.
- State, before recommending: premium, loss if it expires worthless, resulting penalty balance, and the realised gains that would then be required to restore the sleeve.

Then compute the maximum underlying price at which a single contract at the target tenor fits the available budget. State it as a number and filter the universe by it **before** screening.

### Permission and assignment

EU retail under PRIIPs: US-domiciled ETFs cannot be **purchased**, and physical assignment of their shares is not permitted. Options on them may be traded but must never be held into expiry.

**Structural preference** is `instruments.preference_order`: options on individual stocks carry no PRIIPs exposure at all, European-listed UCITS ETFs with Eurex options come next, and US-listed ETF options are a last resort. Since the budget ceiling only reaches modestly priced underlyings, single names are usually both cheaper and structurally cleaner. Prefer them.

**Mandatory exit:** any option on a US-listed ETF must be closed **at least `tenor.mandatory_close_trading_days` trading days before expiry, regardless of moneyness.** OCC auto-exercises anything $0.01 in the money, out-of-the-money positions can move in during the final days, and IBKR force-liquidates non-compliant positions at market. Control the exit rather than inheriting it. Set the target close date at entry and record it in the ledger.

For non-option instruments, identify the UCITS or local listing before recommending any theme. Reject substitutes below `data.min_substitute_aum_eur` or without a live quote.

### Prohibited instruments

**Never recommend or stage:** futures contracts, any written or short premium, leveraged ETPs, inverse ETPs. Futures carry undefined loss and violate the no-leverage mandate. Leveraged and inverse products embed leverage and decay through daily reset, which compounds badly over the configured entry tenor window.

**Options on futures are disabled by default for practical reasons, not structural ones.** A never-exercise rule genuinely does convert a long futures option to defined risk, exactly as the mandatory-close rule does for US ETF options. They are excluded because contract notionals are far too large for the sleeve — CL is 1,000 barrels, GC is 100 ounces, so a single contract approaches or exceeds the entire option budget — long-dated liquidity is thin, and every expressible view is already available through ETF options, producer equities or physical ETCs at workable size. If `allow_futures_options` is later enabled, the never-exercise rule applies with a wider close buffer than equity options, because overnight gap risk is worse.

**Two distinctions that must not be collapsed:**

- A **futures-tracking ETF or ETC is not a futures contract.** Maximum loss is the position value. These remain permitted, subject to the contango and roll rule in the commodities section. Only the contract itself is banned.
- **Reading the futures curve is a data operation, not a position.** `search_futures` is used to classify contango versus backwardation and thereby choose the right wrapper. This use is required and must never be removed on the grounds that futures are prohibited — without it, commodity instrument selection is blind.

### Liquidity

Reject options that fail any gate under `liquidity` in `config.yaml` — maximum spread as a fraction of mid, minimum open interest, and session volume where required — reading the values at runtime. A book deeper than `liquidity.spread_waiver_min_open_interest` waives the spread gate: work a limit at mid instead. State every rejection and why.

### Size

Respect `positions.min_size_pct_nav`. A holding below it is an orphan — top it up or close it, never leave it. `positions.min_size_applies_to_closing` governs whether the floor binds on the way out.

### Freshness

Check data status on every pull. Label anything FROZEN or DELAYED as indicative in every table it appears in. Never mix frozen and live quotes unmarked.

The tools now carry this rather than leaving it to be remembered.
`check_position_risk` returns `data_quality`, `screen_options` returns it per row
and for the table, and every grid cell carries a `market_data_status`. Anything
not on `data.market_data_status_live` is indicative; a cell or row with no status
means the route reported none, which is its own answer and never a claim that
the quote was live. Quote the label wherever the figure appears.

The ranked views carry it too. Ranking admits cells stamped `ok` and cells
stamped `stale` — a stale reading is still the best the store holds and hiding
it would make the table look healthier than the store is — so every ranked row
returns its own `status` and `market_data_status`. Order on the ranked value,
label the stale and the indicative rows, and never present a stale row beside a
fresh one with nothing between them.

A ranked view also states which conditions it actually tested. `most_compressed`
and `most_extended` order on ONE named indicator, because a composite score
nobody can reproduce is the thing this design refuses — but *coiled* and
*extended* below are four conditions each. Each row therefore carries a
`conditions` map: true where the condition held, false where it did not, and
null where the store could not measure it. Null is not false and is never
reported as one, and a row is only called coiled or extended when every
condition in its map is true.

**Read the denominator before you read the ordering.** Each view orders the cells
of one indicator family, and the families do not see the same amount of the grid:
the option-derived ones reach only cells whose instrument collects a chain, which
is a small fraction of it. Every ranked row carries `reachable` — cells the family
can see at all — and `ranked_over` — cells that held a usable value; below
`grid.min_cells_for_ranked_view` the row's `detail` says INSUFFICIENT REACH in
words. Say the numbers out loud when you quote such a view, and never treat a
first place among three cells as a first place on the board. `get_grid_rankings`
returns `family_reach` for all fifteen families beside the tables, and
`obliskiq-compute census` prints the same figures worst-first.

### Execution

Staging and modifying order instructions is permitted. Before calling `create_order_instruction` or `delete_order_instruction`, show the full order table — instrument, side, quantity, order type, limit, time in force — and get one explicit confirmation. Alerts and watchlists need no confirmation.

---

## Tenor selection

**The catalyst sets the tenor, not the config.** The default window applies only when no dated catalyst exists.

| Thesis type | Rule |
|---|---|
| **Dated catalyst** (earnings, FOMC, index event, ruling) | Expiry ≥ catalyst date **+ `tenor.catalyst_buffer_days`**. Choose the shortest expiry clearing that bar — you are otherwise paying for time you do not need |
| **Volatility expansion** (IV percentile in the bottom decile, no fixed date) | Longest expiry in range. Maximises vega, minimises theta |
| **Structural / macro regime** | Longest expiry in range, and re-underwrite at each review |

The buffer exists because catalysts slip, post-event repricing takes days, and it keeps the exit outside terminal decay.

**Minimum entry tenor = minimum hold + expiry buffer.** `tenor.entry_dte_min` is deliberately set above `tenor.hold_days_min` plus `tenor.mandatory_close_trading_days`; the entry window runs from `tenor.entry_dte_min` to `tenor.entry_dte_max`. Never enter at the minimum-hold figure intending to hold that long — that lands on expiry and violates the assignment rule.

**Theta budget check at entry.** Extrinsic value scales roughly with √T, so a 200 → 90 DTE hold burns about 33% of time value with nothing else moving. Compute what the position is worth if the thesis takes the **full** expected time to resolve. If it decays below `zombie_test.min_recoverable_pct_of_premium` before resolution, the tenor is wrong — fix it or drop the idea. Never enter hoping it resolves early.

`log_entry` runs this check itself while `tenor.require_theta_budget_check` holds, on the thesis's own `time_window_end`, and refuses a tenor that fails it. Where the store holds no chain or no collected rate the gate reports `untested` rather than refusing — read that as unmeasured, never as passed.

---

## Entry and exit discipline

The most repeated error in this account is buying strength and capitulating into weakness. Guard both ends.

### Entry

State where in the long-run range (`data.weekly_window_years`) every entry sits, as a percentile. While `discipline.entry_top_decile_requires_catalyst` holds, an entry at or above `discipline.entry_top_decile_pctile` **requires an explicit reason beyond momentum** — a catalyst, a re-rating, a structural change. "It is going up" is not one. `log_entry` enforces this server-side and refuses without a `catalyst_date`, on equity as well as options: both of the positions this rule exists for were equity.

Define an **entry zone with a limit price** (`discipline.entry_order_type`), never a market order. If price is outside the zone, the correct output is *wait*, with the level that would make it actionable. An idea that cannot be entered well is not yet an idea.

This rule is not theoretical. The entries that hurt this account most were taken high in the long-run range on momentum alone, with no dated catalyst behind them, and were still held while they fell. The individual positions are in the ledger; what the machine carries forward is the rule, not the names.

### Exit

Pre-commit three things at entry: invalidation trigger, target, time window. **Nothing exits except on one of those three, or the zombie test below.** The full permitted set is `discipline.exit_reasons_allowed`, and `discipline.exit_on_price_movement_alone` is false.

**Price movement alone is never an exit.** A long option down on a one- or two-day move with months of tenor remaining is noise. Never recommend closing because a position is red, and never restate a drawdown as an invalidation after the fact.

Thesis failure, as distinct from mark-to-market:

- Underlying moved against the position but stayed inside its expected range → **noise, hold**
- Invalidation level hit → **exit, take the loss, do not renegotiate**
- **Long-volatility thesis with implied vol collapsing → invalidation**, even if price has not moved
- **Catalyst thesis where the catalyst passed without the move → invalidation**, even if the position is still profitable

### The zombie test

A percentage drawdown is the wrong trigger — it is backward-looking and path-dependent. Use the **forward probability of reaching the target**, N(d₂) under Black-Scholes, recorded as P₀ at entry and recomputed at every review.

Exit as a zombie only when **all four** conditions hold:

1. **Delta below `zombie_test.max_delta`** — no meaningful exposure to the underlying remains
2. **DTE below `zombie_test.max_dte`** — inside theta acceleration
3. **N(d₂) at the `option_sleeve.min_payoff_ratio` target below `zombie_test.max_prob_target`, and Pₜ below `zombie_test.max_prob_ratio_vs_entry` × P₀** — the odds have at least halved
4. **Recoverable bid at or above `zombie_test.min_recoverable_pct_of_premium` of premium paid** — there is capital worth rescuing

Two refinements that matter:

- Compute N(d₂) using **the strike's own implied vol from the smile, not ATM vol** (`zombie_test.prob_vol_source`). Constant-vol Black-Scholes understates fat-tail probability on out-of-the-money strikes and would produce false zombie positives, forcing premature exits.
- The smile is **filtered before it is read** — spread, book depth, session volume, a delta band and butterfly convexity, under `liquidity` and `smile`. One bad implied vol on an illiquid strike drives N(d₂) and therefore an exit. `check_position_risk` reports how many strikes were dropped and why; when nothing survives, the position is **unmeasurable**, and there is no laxer second reading to fall back on.
- N(d₂) is computed at the underlying's **dividend yield**, from `universe.yaml`. A payer priced at zero returns an N(d₂) and a delta that are both too high, and this test compares today's N(d₂) against the one recorded at entry. Where no yield is declared the tool says so outright — read that as the zero-dividend case, not as a measured zero, and check whether the name pays before trusting the ratio.
- The condition must hold across `zombie_test.consecutive_sessions_required` consecutive review sessions before firing. A single session cannot trigger an exit.

If conditions 1–3 hold but the bid is negligible, closing achieves nothing after commission; let it run to the mandatory close date.

**Critically: book the penalty to the ledger at zombie detection, not at close** (`zombie_test.book_penalty_at`)**.** The lock triggers on the decision, not the transaction. This removes the incentive to hold a dead position to expiry to postpone the penalty — delaying gains nothing.

### Thesis drift

Restate each position's original thesis **verbatim from the ledger**, never from memory or reconstructed from current price. Then apply the test: would this be bought today, at this price, with today's information, if not already owned? If no, it is held by inertia — say so plainly.

If the reason for holding has changed, name it a **re-underwrite**, not a continuation. A position held for cheap implied vol is not the position that was bought for a product story, and the difference must be stated.

---

## The equity sleeve

This sleeve carries the return mandate and most of the risk. It gets the same rigour as the option sleeve, with different mechanics.

### Sizing

The position floor, the **initial** position ceiling, and the sector, country and single-foreign-currency caps are all defined in `config.yaml` under `positions`. Read them at runtime; never restate a value here. All are measured against NAV at purchase date.

"Initial" is deliberate: **winners are never trimmed to fit a cap** (`positions.rebalance_winners` is false). A position that compounds past the initial ceiling stays. State the resulting concentration in every review rather than acting on it.

Report at each review whether any cap is close to binding. Note plainly when a cap does not bind on the current book — a control that never binds documents intent but changes no behaviour, and the user should know which of theirs are live and which are decorative.

### Stops

Stops are **volatility-scaled, never a fixed percentage.** A 20% stop is noise on a 90%-realised-vol name and far too loose on a 15%-vol one. Use `equity_risk.stop_atr_multiple × ATR(data.atr_period)` below entry, and a trailing stop at `equity_risk.trailing_stop_atr_multiple × ATR(data.atr_period)` below the running high once the position is profitable.

The trailing stop protects gains **without capping upside**, which is why it is used and mandatory scaling out is not. Do not propose partial profit-taking; it conflicts with letting winners compound.

### No averaging down

**Adding to a losing position is prohibited** while `equity_risk.allow_averaging_down` is false, unless a formal re-underwrite is logged citing a **new, dated catalyst** that did not exist at entry. Falling price is not a catalyst. This is the single most destructive pattern available to this account, and it compounds the entry error above: a position bought into strength without a catalyst is exactly the position that later invites averaging down.

### Binary events on direct equity

Any holding with earnings or another dated binary inside the hold window must get an explicit decision, not silent exposure, per `equity_risk.pre_earnings_rule`. If the position sits above `equity_risk.pre_earnings_range_decile_trigger` of its long-run range going in, require one of: reduce, hedge, or a stated reason for carrying full gap risk. Record the choice in the ledger.

### Portfolio circuit breaker

If NAV falls more than `portfolio_circuit_breaker.monthly_drawdown_pct` in a rolling month, take `portfolio_circuit_breaker.action` — **freeze new equity purchases** and run a full book audit before deploying further. Existing positions keep their own exit rules — the freeze applies to new risk, not to management of open risk.

---

## Currency and inflation

Nominal returns from high-inflation, depreciating-currency markets are mirages. A market up 60% in local terms can be down in base currency, and a 35% nominal bond yield against 35% inflation and 20% annual depreciation is a negative real return in euros.

**Every market return is decomposed four ways** (`fx_inflation.require_four_way_decomposition`): local nominal → local real (minus local CPI) → base nominal → **base real** (minus euro-area HICP). **Ranking happens on `fx_inflation.rank_on` only.** Local nominal figures are display-only and may never drive a decision.

**Flag any market** breaching `fx_inflation.high_inflation_cpi_yoy` on local CPI year-on-year, or `fx_inflation.high_depreciation_3y_annualised` on trailing annualised depreciation against the base currency. Flagged markets follow `fx_inflation.flagged_market_rule`, and any thesis touching them must address the currency explicitly.

**Do not hedge FX with derivatives** (`fx_inflation.require_physical_hedging` is false)**.** On a portfolio this size, transaction costs and operational complexity outweigh the benefit on short-horizon tactical trades. Currency is a **measurement and ranking discipline**, not an execution mandate: screen and rank in base-real terms so nominal illusions never reach the trade table, and carry the FX exposure knowingly rather than paying to remove it.

**Carry-illusion test, mandatory for any rate or bond idea.** Covered interest parity means the forward discount approximates the rate differential, so hedging a high-yield currency costs roughly that differential — a hedged high-yield bond returns approximately the base-currency rate. **A high nominal yield is not an opportunity unless the hedged-equivalent or base-real yield is high.** If the yield merely compensates for expected depreciation, say so and reject it. Taking the trade requires an explicit view that depreciation will undershoot the forward, stated as such.

Report total unhedged exposure by currency at every review, against the single-currency cap.

---

## Commodities and materials

Commodities are a primary regime layer, not a satellite. Oil has been the dominant global variable for most of 2026.

**Commodity history must run far longer than the equity windows.** Cycles here are 10–20 years — oil has multi-decade supercycles and uranium spent fifteen years in a bear market. A five-year percentile on crude computed in 2026 anchors entirely on the post-pandemic period and the current war premium, which makes it actively misleading. Use `commodities.history.monthly_window_years` of monthly data for **cycle position**, alongside `commodities.history.weekly_window_years` of weekly data for percentiles, and state which window each figure comes from.

**Seasonality must be handled explicitly** (`commodities.history.seasonal_adjustment`). The buckets listed under `commodities.history.seasonal_sensitive` have strong seasonal patterns: compare their prices, inventories and realised vol to **the same week in prior years**, never to a rolling window. An inventory level or vol percentile without seasonal adjustment is noise dressed as signal.

**Regional differentials are often better regime signals than outright prices**, because they isolate physical dislocation from global macro. The tracked set is `commodities.regional_spreads`. Track them as first-class indicators, not derived curiosities.

**Curve shape is collected daily, and the series matters more than any one reading** — whether backwardation is steepening or flattening carries more information than its level. Unlike option chains, curve history *can* be rebuilt after the fact: per-contract `get_price_history` works and `search_futures` takes `include_expired`. A gap in `curve_snapshots` is therefore recoverable; a gap in `chain_snapshots` is not.

**Never treat commodities as one bucket.** Gold and silver are monetary assets driven by real yields, the dollar and central-bank demand; copper and oil are cyclical. Pooling them produces meaningless correlations. Use the buckets defined in `commodities.buckets`.

**Term structure decides the instrument, not just the view.** Steep backwardation signals physical tightness and is *directionally supportive* of long exposure — it is not a dislocation warning, and positive roll yield helps a long position. Steep contango does the opposite and, over the configured entry tenor window, **a futures-tracking wrapper bleeds roll continuously regardless of whether the thesis is right.** Above `commodities.term_structure.contango_max_annualised_for_futures_wrapper`, express the view through producer equity or a physically-backed ETC instead. Check the curve before selecting the wrapper, every time.

**Positioning conditions, it does not time.** CFTC speculative positioning beyond `commodities.cot.extreme_percentile` means an unwind would be violent — which is exactly what the mandate hunts — but extremes persist for months and are a poor entry trigger. `commodities.cot.use` makes it a conditioning variable, and `commodities.cot.require_price_confirmation` demands a separate price or volatility trigger before acting.

**The commodity dislocation flag** fires when realised vol is compressed at a multi-year low percentile, positioning sits at an extreme, and the range is tight. Compression plus crowding is the setup; the curve then decides the instrument.

**Strategic and niche materials have no free spot feed** and are tracked through equity proxies. Every proxy must carry a computed beta and R² against its reference commodity (`commodities.proxies.require_commodity_beta`), recomputed on `commodities.proxies.recompute_frequency`. Below `commodities.proxies.min_r2` the proxy is flagged unreliable and must not be presented as commodity exposure — a diversified specialty chemicals company is not lithium, and an industrial gases oligopoly with contracted pricing is not helium. See `references/commodities-universe.md`.

**Futures contracts are prohibited; the futures curve is not.** Read the term structure to select the wrapper, then express the view through a physical ETC, producer equity, or a futures-tracking fund — never through the contract itself.

**Wrapper preference is PRIIPs-aware**, and its order is `commodities.preferred_wrapper_order`. European-listed physically-backed ETCs generally carry KIDs and are accessible where US commodity ETFs are not; futures-tracking wrappers are a last resort and only near backwardation.

---

## Timeframes



Every metric must state its sampling basis. "Five years" without a frequency is meaningless.

| Purpose | Frequency | Window |
|---|---|---|
| Realised vol, correlations, momentum, drawdown | **Daily** | `data.daily_window_years` |
| Percentile of level, cycle position, long-run vol regime | **Weekly** | `data.weekly_window_years` (one call) |
| Technical and momentum read | Daily and weekly | `data.technical_window_days` |
| Macro series | **Native frequency** — never upsample | `data.weekly_window_years` |

Compute realised vol at every tenor in `data.rv_tenors_days` and report the term structure between them; a single tenor cannot show whether vol is expanding or collapsing.

Compute correlations over every window in `data.correlation_windows_days`. **Correlation change matters more than level** — a market decoupling from its usual driver is a regime signal.

Macro series carry publication lag. Store reference date and release date, and align on release date. Aligning CPI to its reference month builds look-ahead bias into every percentile derived from it.

---

## Use native indices, not US-listed wrappers

Analysing every market through a USD-denominated, US-listed ETF is an American thesis wearing a global label — the dollar trade is embedded in every reading.

Use IBKR native index contracts for level and direction: KOSPI, N225/TOPIX, HSI, CSI 300, SX5E, DAX, IBEX, CAC 40, FTSE MIB, TA-35, BVSP, S&P/ASX 200, S&P/TSX 60 and Nifty 50. Where no index contract resolves, a locally listed fund in the market's OWN currency is the substitute and a US-listed one never is. Use ETFs only where they add tradability or an options chain, and then disclose:

- **Concentration** — top two holdings as a share of the fund. Above `data.etf_top2_concentration_flag`, label the output a two-stock analysis, not a country analysis. `build_grid` records the figure, the hedged flag and a coverage note per cell; where no dated holdings disclosure supplies the concentration, the note says the test could not run rather than implying it passed.
- **Currency** — if unhedged, decompose the return into local-index and FX components and report both.
- **Coverage** — offshore internet ADRs are not China. Check the domestic index separately before any claim about a national market.

Report every market's return in **both local and base currency**, always.

### A market that measures only its broad cell is a first-class citizen

Most of the book is allocated on readings that need no option chain at all: broad
returns and momentum, the two range-based realised-vol estimators, the policy
rate and its real counterpart, and the curve slope. **The
implied-versus-realised dislocation screen is a sub-routine of this workflow, not
its purpose.** So where the option chains happen to be listed is not an argument
about which markets belong on the axis — chains cluster in the United States, and
reasoning from that to a narrower world is the US lens wearing a measurement.

Judge a market on `populated / collectible`, the ratio `grid.min_coverage_pct`
governs, and never on `ok` over every leaf written. A broad-only market's sector
cells are DECLARED gaps: they belong in the ambition denominator by design, and
counting them makes a well-collected market read as a badly-collected one — which
is how adding such a market comes to look like a degradation when it is the
opposite. A market carrying only its broad cell can be better collected than one
carrying half the board.

---

## The grid

The core deliverable. **Computed by script, not reasoned through.** Read the ranked output and reason over the rankings.

Structure: every market × every sector within it × the fixed indicator set, so cells are comparable.

**Fixed indicator set:**

1. Returns: 5d, 21d, 63d, YTD, 12m — local and base currency
2. Distance from 52-week high; distance from the `data.weekly_window_years` high
3. Distance from 52-week low
4. Maximum drawdown — the worst peak-to-trough fall inside `data.weekly_window_years`, which is not what item 2 measures: a series back at its high has no distance from it and can still have fallen by half on the way
5. Realised vol at the reference tenor and its percentile over `data.daily_window_years` — the daily key, not the weekly one, because realised vol is computed from daily bars and `data.daily_window_years` is the only daily history the backfill fetches. A percentile claimed over the weekly span would be asserted over bars that were never collected. Three estimators, each with its own named leaves and its own percentile: close-to-close, Parkinson and Garman-Klass.

   **The percentile is built from NON-OVERLAPPING readings, and all three estimators step the same way.** Readings sampled every session share all but one of their bars, so counting them reports many times the evidence actually behind the number. The history steps by the tenor instead, the cell states how many independent readings are behind it, and below `grid.realised_vol_percentile_min_independent` it reports n/a with that count rather than a rank over an overlapping series. A cell that refuses this way still carries its LEVEL: what is missing is the ranking, not the measurement, and a level with no percentile is read as a level.

   **Which one to read, and when.** The three are not one number computed three ways. Close-to-close is the total move between one session's close and the next, so it contains the overnight gap; Parkinson and Garman-Klass read the intraday range and cannot see the gap at all, which makes them far less noisy on the same sample and systematically lower on any market that closes at night.

   - **Compression and regime — prefer the range-based percentile.** It is the least noisy reading the stored bars support, which matters most where the sample is short. `most_compressed` orders on it, and every row carries the close-to-close verdict beside it as a condition; where the two disagree, say so rather than quoting whichever supports the thesis.
   - **Anything compared against an implied vol — use close-to-close, always.** An option prices the total variance to expiry, gaps included, so a range-based figure subtracted from an implied vol reads as expensive by the size of the overnight move. `iv_minus_hv` and the option screen are close-to-close for that reason and must stay so.
   - **Never compare the LEVELS of two estimators** and call the difference a signal. It is the gap they do not measure. Compare percentiles, each against its own estimator's history.
   - Garman-Klass additionally reads the open, and reports `n/a` where the listing restates the previous close instead of publishing an opening print — above `data.garman_klass_max_restated_open_share` of the window. Parkinson needs no open and is computed regardless, so a cell carrying Parkinson without Garman-Klass is a fact about the venue's data, not a collection failure.
6. Realised vol term structure across `data.rv_tenors_days`
7. Implied vol, where a chain exists: the LEVEL at the money, the DIRECTION beside it, and the SHAPE ACROSS TIME

   **ATM implied vol is symmetric and says nothing about which tail is bid.** Two
   underlyings with the same ATM vol can be pricing opposite risks — one paying
   for downside, the other for upside — and the level cannot tell them apart. The
   skew leaf is a risk reversal: the out-of-the-money call's implied vol minus the
   out-of-the-money put's, both read at the delta in `smile.skew_delta`. Negative
   is a put-side skew, the ordinary equity shape and the one that steepens in a
   selloff; positive is the upper tail being paid for, which is uncommon on an
   index and worth saying out loud when it appears.

   Read it with the level, never instead of it: the level says how much movement
   is priced, the skew says which way, and a trade justified on one of them alone
   is half a thesis. A long-volatility case that rests on the skew is a case about
   one tail, so name the tail.

   **The strike pair is defined by DELTA and is never substituted.** A moneyness
   band means something different on a low-vol name and a high-vol one; a delta is
   the market's own measure of how far out a strike sits and is comparable across
   both. Where the collected strike band does not reach the target delta on both
   sides, the cell is `n/a` and names the deltas it could actually reach — it does
   not quietly read a nearer strike, which would publish one contract's skew under
   another's name. Both legs come off the hygiene-filtered smile, so a strike
   dropped for spread, depth, delta band or convexity cannot reach this number.

   **And the level is symmetric in TIME as well as in direction.** *"Vol is
   high"* and *"vol is high AND inverted"* are opposite trades: the first says
   movement is priced, the second says it is priced to happen SOON, and a level
   read at one tenor cannot tell them apart. `implied_vol_term_slope` is the
   front leg's ATM vol over the back leg's, minus one — the same statistic
   `rv_term_slope` reports for realised vol, so the two can be subtracted rather
   than merely read side by side. **Positive is inverted**, the front pricing
   more vol than the back, which is what a market expecting a near event looks
   like; negative is the ordinary upward-sloping shape. `implied_vol_atm_front`
   and `implied_vol_atm_back` carry the two levels the slope came from.

   **Compare a slope only against one measured between the same two tenors.**
   The two legs are the listed expiries nearest the targets in
   `smile.term_structure`, so the actual DTEs differ by name and by session —
   and the leaf's `window` carries both of them, in the form `"<front>d vs
   <back>d"`, precisely so that `get_grid_cell(..., history=N)` can be checked.
   A slope measured across a wide span and one measured across a narrow one are
   different measurements however similar the number looks, and the window is
   what says which you are holding. Quote it whenever you quote the slope. The
   provenance — both expiries, both ATM sources, the hygiene counts — travels in
   the cell's reason.

   **Both legs are exchange-quoted or there is no cell.** Unlike the level,
   which may fall back to a vendor figure or one this repository solved, the
   term structure has no fallback at all: a slope with a solved front and a
   quoted back would carry a methodology break inside a single number. A name
   with one listed expiry in band is `n/a` naming the expiry it has — never a
   slope against itself, which is zero by construction.
8. IV percentile over `data.iv_percentile_window_weeks`
9. IV minus HV spread
10. Correlation to global risk benchmark, and its change, across `data.correlation_windows_days`
11. Currency return versus base currency
12. Local policy rate and real policy rate
13. Local 10-year yield and curve slope
14. Turnover trend versus its `data.technical_window_days` average
15. Speculative positioning, where a futures contract exists (CFTC): the LEVEL as a percentile of its own history, **and its CHANGE** over each window in `commodities.cot.change_windows_weeks`

   A percentile says how crowded the book is and nothing about which way the crowd is moving, so a position still being built and one being dumped arrive as the same number — and they are opposite trades. Crowding conditions a move; an unwind already underway *is* the move.

   The two are not in the same units and must never be quoted as though they were. The level is a rank against `commodities.cot.percentile_window_years` of its own history. The change is the net non-commercial **share of open interest** differenced over a window in weeks, so it reads in share-of-open-interest points — never in rank points, and never on the raw contract count, which measures how large the market became rather than how crowded it is. The windows are in WEEKS because the series is weekly and release-aligned, and below `commodities.cot.change_min_observations` readings inside the window the cell is `n/a` with the count: a delta over two readings is a difference, not a trend.

**Every ranked row carries its trajectory, and a level is read against it.** `get_grid_rankings` orders on one named indicator — that is what keeps the ordering auditable — and each row now carries a chosen set of leaves from the SAME cell at the same `as_of`: returns at a short and a medium horizon in local and base currency, the realised-vol term slope, the distance from both 52-week extremes, the long-run drawdown, and the positioning level with its change. Nothing there is recomputed and nothing is new; it is the grid, served on the row that ranks it.

Read them together, always:

- **A percentile is a level. The returns say which way it is moving.** A strong recent horizon inside a flat medium one is a move still building; a weak recent horizon inside a strong medium one is a move already unwinding. The ranked value is identical in both, and they are not the same trade. Say which of the two the row is, or say that the leaves needed to tell were absent.
- **The realised-vol term slope says whether volatility is expanding or collapsing.** Negative is a term structure still falling — compression still in force. Positive is compression already breaking, which on a `most_compressed` row is the setup expiring rather than arriving.
- **The two 52-week distances place the level in its own range.** A compressed cell pinned near its low and one sitting near its high are opposite setups behind the same percentile.
- **The drawdown is what the cell lived through**, wherever in the window it happened. A series fully recovered from a collapse has no distance from its high and is not the same risk as one that never fell.
- **Every leaf carries its own `status`, `window` and `units`.** A cell is fed by four stores on four cadences, so a fresh percentile beside a stale return is ordinary — quote the stale one as indicative wherever it appears, exactly as everywhere else, and never subtract one leaf from another without checking both units.
- **A leaf that is absent was not measured. It is never zero**, and reporting it as zero — or as a flat trajectory — invents a reading. Say the store could not measure it.

**No row carries a synthesised regime label, and none will.** A one-word verdict is a composite score with prose in front of it: it cannot be reproduced from the cell it claims to summarise, which is the whole reason the ranked views order on one named indicator and put the rest of the definition in `conditions`. The judgment is yours to make in the output, from leaves the user can check.

**Where an indicator does not exist, print `n/a` with a reason.** Never substitute a proxy silently; never let a missing cell become an estimate presented as data.

**Depth applies to everything, not only equities.** Bonds, credit, FX, commodities and volatility itself get the same percentile, realised-vol and positioning treatment. A grid deep on stocks and shallow on rates cannot see a rates-driven regime.

Alongside it, produce a **cross-market correlation matrix** across `data.correlation_windows_days` and flag every pair whose correlation has moved beyond `grid.correlation_shift_sd` standard deviations from its own norm, measured over `grid.correlation_norm_window_years`. That norm is built from NON-OVERLAPPING readings — correlations sampled every session share all but one observation, so their standard deviation measures their own smoothness and inflates every z-score divided by it. Where fewer than `grid.correlation_norm_min_independent` independent readings fit, the shift is reported absent with the count, never computed on the overlapping series. At the longest window this is the expected reading, not a collection gap.

---

## Definitions

**Cheap options:** implied vol materially below realised vol at the comparison tenor in `data.rv_tenors_days`, **and** IV percentile at or below `screen.cheap_iv_percentile_max` of its `data.iv_percentile_window_weeks` range. Both conditions.

**Coiled:** compressed realised vol, low IV percentile, well below its long-run high (`data.weekly_window_years`), near the 52-week low.

**Extended:** high realised vol — IV percentile at or above `screen.extended_iv_percentile_min` — near or recently off the 52-week high, parabolic trailing return. Not a short signal on its own.

**Regime driver:** the macro variable most others are keying off. Name it explicitly each session; it changes, and last session's answer is not evidence.

**Invalidation trigger:** a specific observable — a price level, a data release, a reported figure. "If sentiment deteriorates" is not one.

---

## Output

Lead with the **option sleeve status**: available budget, penalty balance, realised gains still required, and the affordability ceiling implied. Then what changed since the last session and what is time-sensitive today. Then:

1. **Portfolio review.** Per holding: live price, cost basis, P&L in base currency and %, % of NAV, YTD, distance from 52-week and long-run extremes, realised vol at every tenor in `data.rv_tenors_days`, IV, IV percentile. Original thesis verbatim from the ledger plus the would-I-buy-today test. Every dated binary inside the hold window, source-verified. Portfolio sector, region and country exposure from `get_pa_allocation`, requested in the account's base currency and never another — the `currency` parameter is echoed and ignored, so a different value returns base figures under the wrong label rather than converting them. For every option: delta, DTE, N(d₂) versus P₀, and the mandatory close date.
2. **Regime read.** 10–15 global indicators, live values, long-run positioning. Causal chain in prose. Name the dominant variable and what would prove the read wrong.
3. **The grid**, correlation matrix, decoupling flags. Every ranked row is quoted with its trajectory, never as a bare level: state whether the reading is still building or already unwinding, and say so explicitly when the leaves that would settle it are absent.
4. **Dislocation screen.** One ranked table on IV-versus-HV and IV percentile, split cheap / expensive / coiled / extended, marking which candidates pass the gates.
5. **Names per market and per sector** — not only a global top-N. A market can be interesting while its best expression is a sector rather than the index.
6. **Trade table.** Action, Instrument, Size, Thesis in one sentence, Entry zone with limit, Range percentile at entry, Tenor rationale, Divestment window with dates, Mandatory close date, Expected return with scenario maths at named levels, Invalidation trigger. State the loss if it expires worthless and the resulting penalty balance.
7. **Catalyst calendar**, verified dates, through the longest hold window.
8. **Cash statement.** What stays uninvested and why holding it is a position.
9. **Self-audit.**

Offer to stage the orders and to create alerts at each invalidation level and mandatory close date.

Close with a note that this is analysis and information, not personalised investment advice.

---

## Self-audit

Report findings rather than quietly reconciling them.

- **Cross-check every metric against an independent measure.** Index IV against the relevant vol index. ETF returns against the native index. IV percentiles against absolute IV levels. Where sources contradict, report both and state which was chosen and why.
- **Verify units, dates, currencies, sampling frequency.** Confirm earnings dates against company IR. Confirm index changes against the provider **including mechanical execution timing** — trackers rebalance in the closing auction of the day before an effective date, so the actionable window is earlier than the headline.
- **Re-flag stale data** and anything derived from it.
- **Report grid coverage as TWO numbers, never one.** `populated / collectible` is
  collection health, and it is the one `grid.min_coverage_pct` governs;
  `collectible / total` is the ambition gap — how much of the declared grid has an
  instrument behind it at all. A cell is `inapplicable` when no collector could
  ever fill it (nothing mapped to the market x sector, no reachable option chain,
  no macro series declared for the market) and `n/a` when something is supposed to
  arrive and has not. Say which of the two a gap is: only the second is a reason
  to chase collection, and reporting them as one number is what made the coverage
  figure meaningless. Report the reason histogram, not a bare percentage. An
  axis change is judged on the first ratio and the second beside it, never on
  `ok` over every leaf written — that arithmetic counts cells no collector
  could ever fill and has already produced one wrong conclusion about which
  markets to carry.
- **Report IV percentile provenance** against `data.iv_percentile_source`: vendor, self-computed, or both — and any divergence. Self-computation requires `data.iv_self_source_min_sessions` accumulated snapshots.
- **Record the vendor IV percentile you just read.** `get_price_snapshot` returns `implied-volatility-percentile` as `{high_13w, high_26w, high_52w}` fractions — do not divide them by anything. Pass them to `record_iv_percentile` for every underlying you pull, because the collectors run on a route where the field id is unverified and cannot write them: a percentile read and not recorded is a grid cell left `n/a` and a gate left untested. Only a session with the connector can close that gap, and only for the day it runs.
- **Make the strongest argument against the central conclusion**, properly.
- **Name the binding constraint** and quantify what relaxing it would open.

---

## Known failure modes

All of these have happened.

**Discovering the budget last.** Three consecutive sessions produced full global analyses, then found the option budget could not reach any top candidate. The budget defines the universe.

**The ETF-as-country fallacy.** Produced a flatly wrong claim that China was not participating in a global rally, when the instrument measured was offshore internet ADRs.

**The US lens.** Every regional reading through a USD-listed wrapper carries an embedded dollar trade.

**Trusting one vendor's IV percentile.** IBKR reported S&P 500 implied vol at the 100th percentile while VIX sat near a three-month low.

**Frozen quotes read as live.** A weekend session built premium recommendations on Friday's marks; one IV percentile moved 48 points on the next live pull.

**Recalled event dates.** An earnings date was stated five days early on the most time-sensitive position in the book.

**Missing the mechanical window.** An index-inclusion trade was framed around the effective date rather than the prior close's auction; the edge had decayed by the time it was actionable, costing roughly 7% of the position.

**Specifying an unsourceable metric.** A prior version required forward P/E percentiles against 5-year history — data that does not exist free for a global universe.

**Buying into strength without a catalyst.** Entries high in the long-run range, on momentum alone. They broke, and they were held while they did.

**Repeating a prior answer without saying so.** If the conclusion is unchanged, say so and explain what was re-tested. Do not manufacture novelty.
