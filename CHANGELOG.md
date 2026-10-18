# Changelog

All notable changes to ObliskIQ are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every version below 1.0.0 is a
pre-release; the release workflow publishes each version's section as its release notes.

## [0.9.0] - 2026-10-18

Session 4e, Step 2: the harness for scaling total risk, merged before the pre-registration.
No IBKR call.

### Added

- `quant/covariance.py`: an EWMA, a window and their blend, Ledoit–Wolf shrinkage toward a
  declared target for weighted observations, and a prior in equivalent observations.
- `research/daily.py`: a study's daily history on its calendar, paths of whole months with their
  days, daily marks and drawdown statistics.
- The `total_risk` strategy, its declared twin, `study.twins` and secondary `comparisons`.
- Pre-registration fields: `marks`, `owner_point`, `breach_horizons_years`,
  `bootstrap.sensitivity`, `forecast_check`, `long_history` and `development`.
- `research/forecast.py`: the forecast check, never an outcome.
- `research/long_history.py`: the long-history check.
- Reports: comparisons, the owner's point, block lengths and their sensitivity, the forecast and
  long-history checks.
- `docs/DECISIONS.md` D94.

### Changed

- A candidate outside the owner's point cannot be sufficient (label `outside the owner's point`).
- D92 rule 3 is refused at validation for a total-risk study.
- `core.tilt` and `core.tax_aware` are needed only by the strategies that use them.
- The simulator's weight quantum is 1e-24.
- `obliskiq research power` prints each comparison.

## [0.8.6] - 2026-10-18

Session 4e, Step 1: the data checks, before the harness. No IBKR call.

### Added

- Research datasets `kf_developed_daily` (Ken French's developed markets and the US bill, daily
  from July 1990) and `aqr_century_market` (AQR's equity-index and fixed-income market returns,
  monthly from July 1926).
- `research/explore/risk_scaling_data/`: the checks of every series Session 4e reads, and their
  output of 2026-10-18.
- `docs/DECISIONS.md` D93: the sources, the daily grid, the span and the long histories, chosen
  on the checks alone.
- `docs/knowledge/research-sources.md`: the formats of the two new files and the daily continuity
  of the core's proxies.

### Changed

- The Ken French loader reads daily tables (eight-digit stamps); the AQR loader reads text dates
  as month/day/year (`date_format`) and skips empty cells; a stamp that names no date is refused.

## [0.8.5] - 2026-10-18

Session 4e decided on the objective, at the owner's request. Documents only; no code or
configuration changed. No IBKR call.

### Added

- `docs/DECISIONS.md` D92: an adaptation is sufficient on after-tax growth against static A at the
  owner's point, a forecast's accuracy being a check; total-risk scaling is tested first, without
  the trend tilt and beside its declared twin; a pre-registration whose candidates read a lookback
  uses blocks of at least twice it with a sensitivity over block lengths; the breach is also
  measured on the longest history the candidates allow.

### Changed

- `docs/SESSION_PROMPTS.md`: Session 4e becomes total-risk scaling (study `core-risk-scaling`);
  Session 13 reads its report and uses `quant/covariance.py` to scale total risk.
- `docs/MASTER_PLAN.md` §5.4 (adaptation, covariance), §5.9 (block length), §10 (Session 4e) and
  §11 (row 18); D89 points to D92.
- `docs/ROADMAP.md`: Session 4e's row, the owner's follow-ups, the log entry.
- `research/reports/global-data-audit.md`: what Session 4e gathers first.

## [0.8.4] - 2026-10-18

The owner's point on Session 4c's frontier. Documents only; no code or configuration changed.
No IBKR call.

### Added

- `docs/DECISIONS.md` D91: the 30 % tolerance reached on at most 5 % of 20-year paths, which
  resolves to A, the static risk balance; Session 13's precondition is met.

### Changed

- `docs/MASTER_PLAN.md` §11, `docs/SESSION_PROMPTS.md` (Sessions 4e and 13), `docs/ROADMAP.md`
  and `research/reports/drawdown-tolerance-implications.md` record the point; D88 says that D91
  sets what it left open.

## [0.8.3] - 2026-10-17

The owner's drawdown tolerance and adaptation requirement, D80's data limitation narrowed, a desk
audit of global data, and a new research session for adaptive risk estimation. Documents and one
exploration script; no code or configuration changed. No IBKR call.

### Added

- `docs/DECISIONS.md` D88 (a 30 % drawdown tolerance on the value the bot manages, over a very
  long horizon, measured from a peak taken once a day at the last cycle's NAV, neither a
  guaranteed maximum loss nor an order to sell, with its probability and how that probability is
  stated open; optional exposure caps by asset class;
  the option budget and penalty unchanged), D89 (adaptation as a requirement, earned by test one
  mechanism at a time) and D90 (D80's limitation holds for 4c's proxies only).
- `research/reports/global-data-audit.md`: where the research leans on US data, the criteria for
  a region to add information, the blocks and variables a global adaptive core needs, with
  sources, spans, costs and gaps.
- `research/reports/drawdown-tolerance-implications.md` and
  `research/explore/drawdown_horizon/horizon.py`: what the open probability and horizon imply.
- `docs/SESSION_PROMPTS.md`: Session 4e, adaptive risk estimation.
- `docs/knowledge/research-sources.md`: the sources the audit probed.

### Changed

- `docs/MASTER_PLAN.md`: adaptation in §1 and §5.4, the covariance line, optional caps in §2,
  breaker thresholds apart from the tolerance in §5.11, Session 4e in §10, §11's owner decisions
  and open items.
- `docs/SESSION_PROMPTS.md`: Sessions 13, 17 and 21 after D88–D90.
- `docs/ROADMAP.md`: Session 4e, the owner's follow-up, the log entry.

## [0.8.2] - 2026-10-17

A documentation review at the owner's request, after Session 4d: the documents that steer later
sessions now agree with each other and with the code. Documents only; no code or configuration
changed. No IBKR call.

### Changed

- `AGENTS.md` and `README.md` describe the core as the variant the owner picks from Session 4c's
  frontier, and the transports as the Gateway for paper and OAuth for live (DECISIONS D42, D60);
  the README's status lists what is built.
- `docs/SESSION_PROMPTS.md`: no subagents in §B and §C (the owner removed `.claude/agents/`);
  the opening count of sessions; Session 6's skeleton roles; Sessions 10 and 15 after Session
  4d's verdict; the feature-session template's path to weight follows D62, D65 and D66.
- `docs/MASTER_PLAN.md`: the two references can be the owner's pick from the frontier (§5.4);
  no selection hypothesis is sufficient yet (§5.5); Session 3 built both transports (§10).
- `docs/DECISIONS.md`: D5, D9 and D12 name the entries that superseded them, D22 is marked
  withdrawn, and D61 cites the right entry for the pricing of both legs.
- `docs/ROADMAP.md`: T1's status; the review's log entry.
- `docs/knowledge/README.md` lists every research source.

## [0.8.1] - 2026-10-17

Session 4d, last part — the first selection verdict. The locked pre-registration
`selection-country-momentum` ran once on its confirmation sample, after an exploration on its
development sample (DECISIONS D87). No IBKR call.

### Added

- `research/reports/selection-country-momentum.md`: H1, the top third of MSCI ACWI countries by
  12-1 momentum held through UCITS, against every member equally, 2008–2026 after costs and
  Spanish tax: −1.04 % a year, interval [−3.76 %, 1.39 %], insufficient at theta 2 %. H5, the
  same group with a trailing volatility stop: −5.46 % a year, [−7.92 %, −3.92 %], insufficient
  and negative. The report gives survivorship by year, every portfolio before and after tax,
  the excess year by year, robustness, the placebo and selection across trials.
- `research/reports/selection-country-momentum-exploration-20261017T174052Z.md`: the exploratory
  run on 2001–2007, labelled never evidence.
- `research/preregistrations/selection-country-momentum.lock`.
- `research/pattern_registry.yaml`: H1 and H5 `retired` with their results, H2 and H4 `idea`
  with the data they need; a test keeps its entries well formed.

### Changed

- `docs/SESSION_PROMPTS.md`: Session 8 no longer builds a universe for Session 4d's hypothesis
  first.

## [0.8.0] - 2026-10-17

Session 4d, second part — the harness for the chosen selection hypothesis. A selection signal is
tested as a cross-section, and the simulator carries a protective stop for H5. The
pre-registration and its run follow in their own pull requests. No IBKR call.

### Added

- The cross-section study kind (`kind: cross_section`, DECISIONS D85): members of a universe over
  stated spans, a member the vendor does not serve named without a series and counted as lost to
  survivorship on every date; the universe and the momentum top group (enter within the top
  fraction, kept within a wider one, a lagged signal as the placebo), trading only what changes
  and reset to equal weights once a year; each portfolio simulated with costs and tax and
  liquidated; each test the Fama–MacBeth series of the candidate's monthly excess log return,
  its mean with a block-bootstrap interval and Newey–West beside it, the sign in each subperiod,
  the break-even cost multiplier and the placebo; exploration on the development sample and the
  confirmation run on the later one. `research/cross_section.py`, `research/cross_section_report.py`,
  `research/preregistrations/TEMPLATE-cross-section.md`.
- A trailing volatility stop in the simulator (`Simulator.run(..., stop=StopRule)`, D86): watched
  on daily closes between rebalance dates, filled at the first close at or below it, so a gap is
  borne in full.
- Datasets for the MSCI standard country indices, net total return in EUR, of the 51 countries
  in MSCI ACWI since 2001 that MSCI serves.

### Changed

- `obliskiq research power`, `run` and `data coverage` read either kind of pre-registration.

## [0.7.3] - 2026-10-17

Session 4d, first part — the selection data audit. Before any pre-registration, the data each
batch-1 selection hypothesis would stand on are measured, and the hypothesis is chosen by them
(DECISIONS D84). No IBKR call.

### Added

- `research/reports/selection-data-audit.md`: point-in-time sources, spans, the share of each
  universe lost to survivorship by year, independent observations and the effect detectable at
  power 0.8 for H1, H2 and H4. Free prices miss most insider-purchase events and most stocks of
  the early years, so H2 and H4 are not evaluable with free data; MSCI's country indices lose at
  most 4.1 % of the countries in MSCI ACWI on a date, so H1 (country momentum through UCITS) is
  chosen, with H5 beside it. The report states what would make H2 and H4 evaluable and its cost.
- `research/explore/selection_audit/`: the scripts that measured it (exploration, never evidence).

### Changed

- `docs/knowledge/research-sources.md` records the SEC insider data sets, the SEC ticker list and
  XBRL frames, Yahoo's behaviour with delisted and reused symbols and its throttling, MSCI's
  country indices and index search, and Tiingo's public ticker list.

## [0.7.2] - 2026-10-17

Session 4c, last part — the core verdict. The locked pre-registration `core-allocation` ran
once more after the fix in 0.7.1 (DECISIONS D83), on the same inputs. No IBKR call.

### Added

- `research/reports/core-allocation.md`: the nine candidates against buying and holding a global
  equity fund after Spanish tax, 2005–2026. Every candidate is inconclusive (sign uncertain, low
  power), as the pre-registered power predicted. The report also gives every portfolio before
  and after tax and year by year, the frontier of median after-tax growth against drawdown
  breach for the owner's choice, robustness, selection across trials and the descriptive
  structure check.

### Changed

- `research/reports/README.md` states how reports are named and lists the reports.

## [0.7.1] - 2026-10-17

Session 4c, second part: the fix between the confirmation run that was not evaluable and its
repeat (DECISIONS D83), and the pre-registration's lock. No IBKR call.

### Added

- `research/preregistrations/core-allocation.lock`: the pre-registration's SHA-256 and the
  commit that merged it, written with `obliskiq research lock`; a test checks every locked
  pre-registration against its lock and the constants hash its prose states.
- Reports count the sales not made because their costs would exceed what they raise, on the
  historical path and on the bootstrap paths, for each portfolio.

### Fixed

- A sale whose costs would exceed what it raises (a minimum commission on a few units) is not
  made: the units stay held and count as nothing at the horizon. The simulator used to stop the
  run, so one resampled path made a whole confirmation run not evaluable (D83).
- A report whose pre-registered frontier the bootstrap never reached says so, instead of saying
  that none was pre-registered.

## [0.7.0] - 2026-10-17

Session 4c, first part — the harness for the core verdict. A pre-registration now tests several
candidates against one reference (one test each, Bonferroni across them), the core's variants
are strategies of the harness, and the report states a frontier of after-tax growth against
drawdown. The pre-registration and its one run follow in their own pull requests. No IBKR call.

### Added

- Research vendors MSCI (end-of-day index levels), the ECB Data Portal and the Bundesbank, and
  two derived series computed from stored ones: an overnight-rate accrual (EONIA, then the euro
  short-term rate) and a constant-maturity zero-coupon bond (DECISIONS D78). Migration 005 widens
  the vendor list. Datasets for MSCI World and Emerging Markets in EUR, EONIA and the euro STR,
  German Federal zero-coupon yields at 4, 5, 16 and 17 years, the euro cash index, 5- and 17-year
  zeros, TIP, IEF, GLD, CRSOX and the Ken French market and bill as public at the month's close.
- `quant/risk_budget.py`: long-only risk budgeting by cyclical coordinate descent on the convex
  problem (the legacy `_risk_parity` fixed), tested against closed forms and an independent
  Newton solve. `quant/trend.py`: momentum over cash and a moving average with hysteresis and a
  scale floor (D80).
- `research/strategies.py`: `banded_weights`, `risk_balance` and `equity_overlay` as one core
  strategy — static weights by role, wide bands, the trend tilt with freed weight to cash,
  tax-aware execution (a costly trend reduction held, losses taken, swap partners on a purchase
  that would defer a loss) (D80).
- `research/structure.py`: a pre-registered pre-tax check of the tilt on the longest history,
  descriptive (D79).
- The study and report: several candidates, the runs before tax, the frontier with its
  resolution to the simplest portfolio the bootstrap cannot tell apart from the best, the
  aspiration as a distance, static weights, a year-by-year table and taxes by year, power before
  data per candidate (D79). `research.workers` shares the bootstrap's paths among processes
  without changing the result (D81).
- A pre-registration's fields `candidates`, `roles`, `core`, `frontier`, `aspiration`,
  `structure`, `power.by_candidate`, each instrument's `expense_ratio` and each strategy's
  `simplicity`; `trial_budget` may be 0.

### Changed

- The simulator gives a strategy a `Book` (values, the investable value after tax owed and the
  lot engine's answers), indexes each series once and keeps prices, month-end closes and trend
  states per panel (D81).
- `obliskiq research power` prints each candidate's standard error, power and detectable effect;
  `research run` records one trial per candidate and prints each candidate's outcome.
- The lot engine's trial copy is built field by field; `LotEngine.basis` and
  `LotEngine.purchase_defers_loss` answer without a trial.
- MASTER_PLAN §5.4: the registered variants hold static weights from a declared covariance;
  Session 13's prompt follows (D80, D82).

### Fixed

- A part sale of a lot whose units all carry a deferred loss could leave a rounding excess of
  replacement units that the lot's own invariant refused; they are capped at the units left
  (D81).
- Target weights are rounded down to a quantum at which they add up exactly, so a Decimal
  residue can no longer sum them above 1 (which the simulator refuses as leverage), and a unit
  count within a residue of a whole number is that number, so a position kept at its weight is
  not sold by one unit.
- A trend reduction's tax is priced on its sales together (`LotEngine.trial_tax`), so a loss on
  one vehicle offsets a gain on the other as the year would.
- The structure check annualises a subperiod over the months it holds; the report prints a grid
  value with the digits it was written with (2.5 %, not 2 %).

## [0.6.0] - 2026-10-17

Session 4b — Research harness. One harness whose output can move capital, under every rule of
`src/`: point-in-time research history, a monthly walk-forward simulator that prices costs and
Spanish tax with the code the live path will use, the statistics and decision rule of DECISIONS
D62–D63, the trial registry and the pre-registration lock, and reports that say sufficient,
insufficient, inconclusive or not evaluable with what they cannot conclude. No IBKR call was
made and no real-data study was run (Sessions 4c and 4d).

### Added

- `tax/` and `jurisdiction/`: the `TaxPolicy` protocol beside `EligibilityPolicy`; `es_irpf`
  (FIFO, conservative homogeneity, the two-month rule with deferral and release, offsets and a
  four-year carry-forward, the 2025–2026 savings scale with its tax years, payment by 30 June of
  the next year, withholding and the foreign-tax credit, switches and swap partners) and the
  `generic` template, selected by `config/jurisdictions/es.yaml`, which carries every value with
  its source; the lot engine with `tax_cost`, `wash_sale_conflict` and `harvestable_losses`;
  the tax ledger; the `TaxLot` domain and `tax_lots` table. `docs/knowledge/tax-es.md` cites the
  article and an official source per rule (not tax advice). A test keeps every tax constant out
  of code.
- `research/data.py`: the research store (`paths.research_database`, migration 004:
  `research_series`, `research_history` with vintages, `research_trials`) and loaders for
  yfinance end-of-day prices, the Ken French and AQR libraries, FRED and ALFRED and the ECB
  reference rates, each series with its vendor, download stamp, content hash, availability kind
  and bias notes; conversion to the base currency; coverage and survivorship reports. Loaders
  are tested on recorded fixtures (`tests/fixtures/research/`).
- `research/simulate.py`: delta execution, the cost model on every leg on the listing venue,
  the lot engine, tax paid at its payment date and at the terminal liquidation, fail-closed on
  a missing price, rate or cost.
- `research/stats.py`, `research/decide.py`: log growth of liquidation value, drawdowns and
  breach probabilities, a seeded block bootstrap, Newey–West, Fama–MacBeth, power and the
  detectable effect, n_eff from a panel, the deflated Sharpe, Bonferroni intervals, the
  break-even cost multiplier; the D62 rule, its labels and power beside it.
- `research/registry.py`, `research/prereg.py`, `research/study.py`, `research/report.py`: the
  trial registry with the trial budget, the lock verified against git, a portfolio comparison
  with a paired bootstrap and robustness, and the Markdown report.
- `obliskiq research data load|coverage`, `research power`, `research run [--explore]` and
  `research lock`.
- `research/preregistrations/TEMPLATE.md` with every field of D63, `research/explore/README.md`
  and `research/reports/`.
- IBKR's fixed commissions for shares, ETFs and ETCs on Xetra, the LSE, Euronext Amsterdam and
  Paris, Borsa Italiana and SIX (`fraction_of_value` schedules); a UCITS order is priced and
  filled in shadow.
- Dependency: `yfinance>=1.7,<2` (DECISIONS D72).

### Changed

- `config validate` builds the tax policy the profile selects, so missing tax data fails at
  load.
- Import contracts: the live decision path never imports research or the research store;
  research never imports execution or the broker.

## [0.5.1] - 2026-10-16

Design review (owner request): evidence before machinery. A red-team of the plan against the
code and the legacy evidence, challenged twice by two other reviewers, recorded as DECISIONS
D60–D71. Process and documents only: no runtime code changes; `pyproject.toml` and `uv.lock`
carry only the project's own version, 0.5.0 → 0.5.1, which the release workflow publishes.

### Changed

- MASTER_PLAN: the objective is the after-tax log growth of liquidation value within the
  owner's drawdown limit, tax simulated as it arises, with an equity reference and a 60/40
  scored under the same rule; research reports a frontier and the owner's tolerance picks the
  variant (§1, D60); every capability runs under a computed activation condition, never a NAV
  threshold (§2, D66); the core's variants, including an equity-dominant one, are compared
  after tax on that frontier (§5.4); the research pipeline has a protocol, registered
  combinations and a decision rule with four mutually exclusive outcomes (§5.9, D62–D64);
  LLM-derived evidence is forward only, with a minimal extractor from Session 10 (§5.10, D70);
  sizing uses a gap-adjusted loss and protective exits handle gaps without market orders
  (§5.11, §5.12, D67); currency gains enter the tax ledger (§5.16, D69); capital follows
  research evidence and live results are monitored through a twin (§5.19, D65); the
  transport's fallback is open again (§7, D68); the session sequence (§10).
- SESSION_PROMPTS: T1, the owner's first run on the deployment machine (§A.5); new Sessions 4b
  (research harness, with the tax engine and European commissions), 4c (core verdict), 4d
  (first selection hypothesis) and 19a (capacity and scale, built before 1.0.0); Session 14
  absorbed into 4c; Sessions 5–13 and 15–21 adjusted (D61–D71).
- ROADMAP: the new sequence, the design-review log, live checks for attached orders, the
  reporting lag and stop-limits through a gap, and T1 as the first live run.
- AGENTS.md: the research harness's rules, and software tests are not evidence of returns.

## [0.5.0] - 2026-10-16

Session 4a — Pre-trade rails. The pre-trade gate replaces Session 4's refuse-everything gate:
no order reaches the broker unless every check passes, and every refusal carries a reason code.
No IBKR call was made (the paper Gateway login does not complete from a cloud container); the
paper order test is built and tested against the fake broker and queued VERIFY-LIVE.

### Added

- `execution/pretrade.py`: the gate, still the only issuer of a clearance. It works from the
  stored intent and checks, in three stages that each report every failure: the intent and the
  write asked for; the paper guard, the mode, the kill switch, the instrument, prohibited
  products, buy-side eligibility (the mandatory close of an option on an ineligible ETP),
  approval and the calendars' act-now view (an option also inside the windows its venues share,
  date-critical inside its mandatory-close window); the broker reads, the quote policy and the
  collar (a protective stop against its spec); and, in one store transaction with the
  clearance, the holdings registry, no short (I2), the cash in the order's currency against
  every open commitment and the floor, no borrowing by the whatif's own cost, and the caps. 42
  reason codes (`Reason`), each tested with its positive control. A clearance carries the
  order's notional and expires with the earliest quote it rests on.
- Class by effect (I3): an order that reduces risk waives exactly the kill switch, the caps,
  the order count, buy-side eligibility, the cash check and approval. An option close refused
  for want of a real-time quote alerts the owner.
- `execution/exposure.py`: the position (the lower of the broker's read and the store's after
  every reported fill) and every sell that could still reach the broker, in any process.
- `execution/exits.py`: the exit coordinator (I5). An exit's other checks pass before its
  protective stop is cut; the stop is restored (modified back, or placed again) when the exit is
  refused, cancelled or expires; a failed restore alerts and is retried.
- `execution/flatten.py` and `obliskiq flatten --confirm`: closing orders for every managed
  position, at the bid, through the coordinator; they pass with the kill switch on.
- `execution/approvals.py` and `obliskiq approvals list|approve|reject`: the approval queue, off
  by default, per sleeve (`execution.approval.sleeves`).
- `execution/paper_test.py` and `obliskiq preflight --paper-order-test`: per configured
  instrument, the contract confirmed by search, rules, algos, a quote, a whatif, then one buy
  that cannot fill, its status, its replies and the cOID in the live orders, and a cancel.
- `execution/account.py` (the gate's broker reads), `execution/instruments.py` (the instrument
  directory and the prohibited categories), `execution/runtime.py` (the order path a command
  opens).
- `jurisdiction/eligibility.py`: the `EligibilityPolicy` protocol and the skeleton policy, which
  `config/jurisdictions/es.yaml` selects until Session 8.
- Migration `003_pretrade.sql`: `approvals` (append-only) and `stop_resizes`.
- `config/defaults/instruments.yaml` (lite's `instruments` keys and `tenor.mandatory_close_trading_days`),
  `config/defaults/eligibility.yaml` (the skeleton allowlist), and `execution.pretrade`,
  `execution.approval`, `execution.flatten` and `ibkr.preflight.paper_order`.
- The fake broker serves what the gate reads (session, accounts, positions, ledger, snapshots,
  rules, algos, contract search); the order rig carries the gate over test instruments.

### Changed

- Policy factories take the configuration as well as the profile (`PolicyRegistry.resolve`).
- The ledger's cash balances carry IBKR's rate to the base currency; a whatif names the currency
  of its amounts.
- An import contract keeps `jurisdiction` away from the broker, the network, the store and
  execution.

## [0.4.0] - 2026-10-15

Session 4 — Order path, kill switch and modes. Everything at and below the broker-write
boundary, locked behind a pre-trade clearance whose gate still refuses every intent (Session 4a
builds its checks), so no order can reach the broker yet. No IBKR call was made: every order
shape is VERIFY-LIVE.

### Added

- Order domain (`domain/orders.py`): intents with uuid7 ids, order terms whose every price
  policy carries a limit (a plain market order cannot be expressed), the pre-trade clearance
  bound to an intent, an action and the terms' digest, broker orders, fills, positions, cash
  balances, holding assignments, mode changes and kill-switch records.
- Migration `002_orders.sql` and `store/orders.py`: intents, an append-only event history,
  clearances spent once, broker orders, fills, account reads with position and cash snapshots,
  the mode audit, the kill-switch flag and the holdings registry; `store/worker.py` runs the
  store on one thread for async callers.
- IBKR order client (`ibkr/orders.py`, `ibkr/order_decode.py`): whatif, an idempotent placement
  (the cOID looked up in a complete live-orders snapshot and the trades before anything is
  sent), the warning loop under a reply policy, modify, cancel, status, live orders, trades,
  conversion fields, the cOID short form, and the reset of suppressed warnings.
- `execution/`: the intent state machine with property tests, lite's order discipline, the
  quote policy per asset class and the collar, the reply policy (`o354` only under the quote
  policy and inside the collar; unknown warnings declined, cancelled and alerted), modes with
  audited upgrades and automatic downgrades, the kill switch (file and database flags, a sweep
  that cancels what adds risk and keeps the sells that reduce it), the fail-closed pre-trade
  gate, the clearance ledger, the write boundary (mode, paper guard, kill switch, clearance) and
  shadow fills priced with the cost model.
- `obliskiq kill on|off|status` (`kill on` works even when the configuration does not load) and
  `obliskiq mode show|set`.
- `config/defaults/execution.yaml`: lite's `discipline` keys the order path reads, and
  `execution` (modes, orders, reply policy, quote policy, collar, kill switch).
- The stateful fake broker and order rig in `tests/support/`, guard tests (only the gate
  constructs or records a clearance; only the order client spells the write endpoints and only
  the write boundary places or modifies; only limit-capped order types are reachable; the
  order path's state reads are the listed ones) and `docs/knowledge/ibkr-orders.md`.
- Schema triggers: a clearance is spent once and never changed or deleted; an intent's identity
  never changes.
- Adversarial tests (`tests/unit/test_breaker_*.py`, a property file) against the clearance
  lock, class by effect, the kill switch, the reply policy, modes and the state machine.

### Changed

- `BrokerSession.request(..., retry=False)` sends a write at most once.
- The transport is readied (an OAuth token renewed) before a request takes its pacing slot, and
  a re-arm stops at the first request the transport cannot sign (DECISIONS D53).
- `/iserver/account/orders`, `/trades` and `/pnl/partitioned` each have a rate group at one
  request per 6 s.
- `run_read_only` takes a login driver; it is tested end to end through the transport it builds.
- The magic-number guard requires every numeric constant in `execution` and `decision` to be a
  listed protocol fact (DECISIONS D50).

## [0.3.2] - 2026-10-12

Sessions read what their work needs, when it needs it (DECISIONS D43); Session 4 is split into
Sessions 4 and 4a (D44). Process and documents only; no runtime code changes.

### Added

- `make prompt S=<id>` prints the common header and one session's prompt. Tests cover the
  extraction (4 versus 4a, 1 versus 10, unknown and missing ids) and check that every line
  range a prompt pins in `legacy/` covers whole sections.

### Changed

- AGENTS.md: "Read first" reads by section; a new "Context budget" (legacy per deliverable,
  frozen ranges for prose and YAML, code by symbol, money-path mechanics read at the source,
  no polling, read once); the paper Gateway login is not attempted from a cloud container; the
  reviewer reads its own diff; the environment's GitHub tools stand in for `gh`.
- `.claude/agents/`: `explorer` cites `path:start-end` ranges and never stands in for
  money-path source; `port-checker` and `reviewer` read `MANIFEST`, `DECISIONS` and
  `MASTER_PLAN` by section.
- SESSION_PROMPTS: Session 4 becomes Session 4 (order path, kill switch and modes, behind a
  fail-closed pre-trade clearance) and Session 4a (pre-trade rails, approval queue, `flatten`,
  paper order test). The pre-trade clearance is specified as five invariants with their tests
  (the gate; no short against every sell that could still reach the broker; class by effect
  with a fixed list of waivers; a kill switch that works when everything else fails; no exit
  leaving a position unprotected); later prompts pin frozen legacy ranges; §B asks for auto
  mode and a fresh session for follow-ups. MASTER_PLAN §10 and the ROADMAP follow the split.

## [0.3.1] - 2026-10-12

Paper runs use the Gateway; OAuth is the live account's (DECISIONS D42).

### Changed

- The `oauth` transport refuses any profile but `live`, as the Gateway refuses `live`; the
  refusal comes before any credential is read, so a paper run never looks for an OAuth key
  file. `obliskiq preflight --read-only` prints `refused: …` (exit 1) for such a combination.
- `Environment` no longer checks that the OAuth key paths exist when it loads; the live OAuth
  transport looks for them and refuses a missing file by name.
- AGENTS.md, MASTER_PLAN §5.12, `.env.example` and SESSION_PROMPTS §A: cloud environments hold
  the paper Gateway login only, never OAuth keys.
- `docs/knowledge/ibkr.md`: the second Gateway attempt from a cloud container, and its likely
  cause (the outbound address changes per connection).

## [0.3.0] - 2026-10-12

Session 3 — IBKR read path. Read-only: no order endpoint is touched.

### Added

- `ibkr/transport/`: one transport interface with two implementations chosen by
  `OBLISK_IBKR_TRANSPORT` — OAuth 1.0a (ported from lite: DH prepend, signed big integer, the
  live-session-token signature check, renegotiation before expiry) and the Client Portal Gateway
  (pinned archive, a per-install `localhost` certificate pinned by the client and the browser,
  a Java-version gate, a headless paper login with Playwright, re-login on session loss; refuses
  profile `live`).
- `ibkr/session.py`: the request pipeline with re-arm on 401 and on the lost `/accounts`
  precondition, re-authentication, no retry on 403, jittered backoff with `Retry-After`, the
  tickle keepalive, competing-session and session-loss detection with an alert hook.
- `ibkr/ratelimit.py`: sliding windows per endpoint group below IBKR's published caps, a global
  window, a concurrency cap, per-group pauses and circuit breakers.
- `ibkr/errors.py`: errors classified by body, each with its retry advice.
- `ibkr/client.py`: portfolio (accounts, positions, ledger with the NAV in base currency only,
  cash per currency), snapshots with the settle loop and declared-unit decoding, history with a
  probe-chunked walk, contract search with the declared-venue tie-break, contract info, strikes,
  futures ladders, bond filters, funds, trading rules, algos, the trading schedule and the
  scanner; `ibkr/identity.py` (conid, venue and currency pinned; ambiguity refused; corrections
  supersede); `ibkr/guard.py` (the account must be listed and, under paper, paper-prefixed).
  The endpoint groups refuse every read until the guard has passed; incomplete position and cash
  reads raise rather than return short; snapshot field ids follow IBKR's field table where
  lite's differed (DECISIONS D37, D41).
- IBKR's trading schedule as a calendar `ScheduleSource` (`ibkr/schedule_source.py`).
- `obliskiq preflight --read-only`; without credentials it prints "no credentials" and exits 2.
- `config/defaults/ibkr.yaml`, `config/defaults/http.yaml`, `paths.ibkr_gateway`,
  `OBLISK_IBKR_GATEWAY_JAVA_HOME`; `docs/knowledge/ibkr.md`; IBKR fixtures with a sidecar note.
- Dependencies: `cryptography`, `playwright` (1.56).

### Changed

- `RetryPolicy.from_settings` and a public `retry_after_seconds` in `util/http.py`.

## [0.2.0] - 2026-10-09

Session 2 — Store, calendars and numerics. No broker access yet.

### Added

- `domain/`: frozen, `extra="forbid"` base models with no NaN or infinity and UTC-only
  datetimes — `Instrument`, `Bar`, `Snapshot`, `ChainRow`, `MacroObservation`, `Venue`,
  `Phase`, `Incident`, `SessionObservation` — and the market-data-quality helper.
- `store/`: SQLite with WAL, foreign keys and a busy timeout; a forward-only migration runner
  that checksums each script, applies it atomically and refuses edited, missing,
  out-of-order or transaction-controlling migrations; `001_core.sql` with the final table
  shapes; repositories whose every read — `COUNT(*)` included — takes a required keyword
  `as_of`, with macro series aligned on release date; row mappers that raise on a stamp they
  cannot parse.
- `calendars/`: UTC interval algebra; library-backed, synthetic and uncovered schedules with
  seasonal closes and lunch overrides; the seven-phase session model with transitions; manual
  incidents that can block and never open, with a loader that fails closed; and the
  `Calendars` facade — `venue_status` (planning view), `tradability` and `tradable_windows`
  (act-now view), `common_windows` for options, `last_tradable_session`, `session_before`,
  `common_sessions_between`, `last_completed_session`, `coverage` — with a schedule
  cross-check that alerts on a disagreement and blocks only date-critical orders.
- Configuration: `config/defaults/venues.yaml` (27 venues, the IBKR exchange-code map,
  synthetic calendars, lunch breaks, seasonal closes, order offsets, incident grace),
  `store.yaml`, `numerics.yaml`, `smile.yaml`, `liquidity.yaml`, `costs.yaml`, and
  `config/manual-incidents.example.yaml`.
- `quant/`: a numpy-only Black–Scholes–Merton engine with admissibility checks, a convergence
  mask, OTM-twin implied-volatility solving and a vega guard; the smile hygiene and
  strike-smile helpers; descriptive statistics; Newey–West, Benjamini–Hochberg, Spearman,
  Brier and moving-block-bootstrap inference with every threshold a required argument; and
  a `Decimal` cost model — commission schedules per venue, asset class and currency
  (including the bond schedules), transaction taxes by issuer country, and the friction
  tiers — that reports `no_schedule` or `tax_unknown` rather than a guess.
- Tests: calendar cases for both daylight-saving changes, the Tokyo and Hong Kong lunch
  breaks, the Hong Kong half day, the US post-Thanksgiving half day, Good Friday and Easter
  Monday, synthetic venues, incidents, source disagreement and option expiry; a golden
  snapshot of 23 venues on nine dates; migration failure branches on file databases;
  point-in-time reads; Hypothesis properties (put-call parity, implied-volatility round trip,
  price monotone in volatility, Newey–West at least the independent standard error, interval
  algebra, store round trips); a guard against hardcoded trading times.
- Import contracts: `domain` is a leaf; `store`, `calendars` and `quant` are mutually
  independent; `calendars` never reaches `ibkr`, `httpx` or `store`.
- `docs/knowledge/calendars.md` and `docs/knowledge/costs.md`.

### Changed

- Dependencies: `numpy` and `exchange-calendars` (pandas only transitively); no scipy — the
  normal CDF is `0.5 * erfc(-x / sqrt(2))`.
- The unread-config-key guard reads keyed mappings (`KEYED_MAPPINGS`) and list records; the
  exact-leaf tests of Session 1 became "every leaf has a source"; `settings show` shortens
  long values.
- Tooling: Hypothesis profiles (`ci` is derandomised), a narrow warning filter for a numpy
  deprecation inside `exchange-calendars`, a per-file `ARG002` ignore for tests, and
  `.gitattributes` fixing LF endings for migrations.

### Fixed

- Behaviour that the legacy code got wrong and the port does not repeat, each recorded in
  `docs/DECISIONS.md` (D24–D32): a clock difference of wall times across a daylight-saving
  change; a manual-incident loader that dropped every manual incident after one typo; a commission
  estimate that never charged the per-share fee on equities; an open-interest gate that
  passed when the figure was missing; an unbounded `COUNT(*)` that saw rows from the future.

## [0.1.0] - 2026-10-09

Session 1 — Foundation. No trading logic yet.

### Added

- The design documents: `docs/MASTER_PLAN.md`, `docs/SESSION_PROMPTS.md` and `AGENTS.md`
  (loaded through `CLAUDE.md`), plus `docs/DECISIONS.md`, `docs/ROADMAP.md` and
  `docs/knowledge/README.md`.
- A read-only snapshot of the three legacy repositories under `legacy/` — ObliskIQ-lite-MCP
  @ `b1ebd49`, Inversor-Inteligente @ `68280e1` (the MASTER_PLAN §6.2 paths) and
  market-pulse-bot @ `900dbf4` — with `legacy/MANIFEST.md` listing every file's source,
  commit, licence, target and action. Legacy agent instructions are renamed so no tool
  loads them.
- The Python 3.14 scaffold: `pyproject.toml` (AGPL-3.0-or-later), `uv.lock`, the `obliskiq`
  package with one documented package per area of the plan, a `Makefile`
  (`setup`, `check`, `test`, `fmt`, `lint`, `types`, `imports`), ruff, `mypy --strict`,
  pytest with a coverage floor, pre-commit hooks and import contracts (the LLM layer cannot
  reach decision, execution or the broker; compute cannot reach the broker, httpx or
  execution; nothing imports `legacy/`).
- Foundation modules ported from ObliskIQ-lite-MCP with their tests: the error hierarchy,
  structured logging (JSON or text, UTC), tolerant numeric coercion, UTC time helpers and
  the HTTP retry loop.
- The environment loader: every credential and deployment location the plan names, with
  every key, token, account id and personal identifier held as a secret that never reaches
  a repr, a log line or an error message.
- The configuration system: expert defaults in `config/defaults/`, the owner's panel in
  `config/owner.yaml`, merged into one validated model that remembers where every value
  came from and fails with plain-language, file-and-key messages; the `es` jurisdiction
  profile and the policy registry that resolves a profile's policies; an append-only,
  hash-chained, lock-protected settings-change audit log.
- The command line: `obliskiq config validate` and `obliskiq settings show`, with
  `--config-dir` and `--env-file`.
- Guard tests: every config key has a consumer, no committed secrets or personal data, no
  magic numbers in decision paths, packaging, the environment template, the CHANGELOG
  section for the current version, the workflows and the legacy snapshot.
- CI (`ci`: `make check` on uv-managed Python 3.14), automatic releases from this file,
  weekly grouped Dependabot updates and the pull-request template.
