# Roadmap

What is done and what is open. The design is `docs/MASTER_PLAN.md`; each session's task is
its section of `docs/SESSION_PROMPTS.md`; why things are as they are is `docs/DECISIONS.md`.

## Sessions

| # | Session | Outcome | Status | Release |
|---|---|---|---|---|
| 1 | Foundation | scaffold, docs, owner settings and defaults, jurisdiction registry, CI, releases, legacy snapshot | done | v0.1.0 |
| 2 | Store, calendars and numerics | store and migrations, calendar and phase layer, BSM, IV, smile, stats, inference, costs | done | v0.2.0 |
| 3 | IBKR read path | OAuth and Gateway transports, session lifecycle, client, rate limits, quote status, contract identity, trading schedules, preflight | done (live login blocked in the cloud) | v0.3.0, v0.3.1 |
| 4 | Order path, kill switch and modes | intents, order client, reply and quote policy, clearance lock, state machine, paper guard, modes, kill switch | done (paper orders not yet placed live) | v0.4.0 |
| 4a | Pre-trade rails | pre-trade checks with reason codes, eligibility skeleton, approval queue, flatten, paper order test | done (paper order test not yet run live) | v0.5.0 |
| T1 | Deployment machine (owner) | unattended login on two days, read-only preflight, paper order test, a Java 21 and 25 side test, on Ubuntu 26.04 (SESSION_PROMPTS §A.5) | logins passed (owner, 2026-10-20 and 2026-10-21): both days' unattended login and read-only preflight passed under WSL2 Ubuntu with Java 11, and Java 21 and 25 passed once (21 Oct); the paper order test has placed no order (outside the windows on Xetra and the NYSE; on ASX inside its window the first live-orders read lacked `snapshot: true`, pre-read since v0.20.2); open: the order test, part A of the owner's list in docs/RUNBOOK.md | v0.12.1 |
| 4b | Research harness | point-in-time research data, simulator with costs and the `es` tax engine, statistics, decision rule, trial registry, European commissions | done (no real-data study: 4c, 4d) | v0.6.0 |
| 4c | Core verdict | pre-registration, then one after-tax run of the core's variants against the references; a frontier from which the owner picks the variant | done (every candidate inconclusive; the owner's point resolves to A, D91) | v0.7.0, v0.7.1, v0.7.2 |
| 4d | First selection hypothesis | data audit, choice among H1, H2, H4, pre-registration, one confirmation run, H5 beside it | done (the audit chose H1; H1 and H5 insufficient) | v0.7.3, v0.8.0, v0.8.1 |
| 4e | Total-risk scaling | data checks, covariance estimators, a total-risk strategy, a bootstrap that respects lookbacks and a long-history check, pre-registration, one confirmation run against static A on the objective (D89, D90, D92) | done (every candidate inconclusive and outside the owner's point; A stays, D95) | v0.8.6, v0.9.0, v0.9.1, v0.9.2 |
| 5 | Execution mechanics | currency manager, pricing, resting stops with gap handling and attached orders, executor, reconciliation | done (live steps a command list for the owner; attached orders not adopted, D99) | v0.10.0 |
| 6 | Walking skeleton | service, cycles, skeleton targets → intents → paper → reconcile → digest, on the T1 machine | done (the live paper cycle a command list for the owner, docs/RUNBOOK.md) | v0.11.0 |
| 7 | Capital, cash flows and tax | managed capital, cash-flow ledger, reservation planner, owner questions, lots wired to fills, currency lots, TWR | done (IBKR's cash-activity shapes not observed: no paper login from the cloud) | v0.12.0 |
| 8 | Universe and market-data collectors | global universe with hedged classes and look-through, stock candidates, venue profiles, eligibility policy, collectors (option surfaces stored forward) | done (collectors built against fixtures: no IBKR call from the cloud; holdings and research history loaded live) | v0.13.0 |
| 9 | Broker capabilities and vehicle selector | capabilities measured, core candidates, swap partners, vehicle selector | done (capabilities from the owner's paper run and lite; the probe on paper is a command for the owner) | v0.14.0 |
| 10 | Macro, positioning, insiders and events | the sources a registered hypothesis, component or safety rule consumes; incident feeds; catalyst calendar; a minimal EventExtractor (H7's clock) | done (every built source but GDELT collected live from the cloud; the extractor waits on its key; H7 pre-registered, #46) | v0.15.0 |
| 11 | Measurement store and views | measurement store, equity grid, macro board, rankings, correlations, IV solve, gap distributions | done (built and accepted on an offline fixture store; the census reports the gates untested until Session 12) | v0.16.0 |
| 12 | Engines and gates | option surface, options sleeve (behind its activation condition), fx, commodity, risk engine, gates | done (built and accepted on an offline fixture store; the sleeve in shadow, H8 unregistered; the order path's contango check still fails closed, D137) | v0.17.0 |
| 13 | Core engine | the variants 4c registered (4e found no total-risk specification sufficient, D95), risk budgeting, tax-aware trend tilt, rebalancing | done (parity with the simulator on a fixture history; the owner's variant is D50 since v0.19.0, D147; the live cycle a command list for the owner) | v0.18.0 |
| 22 | Order path on paper | the live-orders pre-read; the owner's command list from the order test to `serve` | done (on fixtures; the order test, the execution test and the reserve's paper cycle are the owner's list in docs/RUNBOOK.md) | v0.20.2 |
| 27 | Thesis book | the owner's idea intake, the thesis lifecycle and gate, conviction sizes, exits, forward grading; its pre-registration first | done (on fixtures, in shadow; H13 locked, #63; no IBKR call) | v0.21.0 |
| 28 | LLM analyst | the fact sheet, theses the engine proposes on its own, a second opinion on the owner's ideas | after 27 (an LLM key, or fixtures) | |
| 29 | Options for theses | a thesis held as a long call or put through the existing sleeve, inside the book's capital and the option budget | after 27 | |
| 23 | Hunter: data and power | vendors of survivorship-free stock history, universe floors and size tiers, power before data; the owner buys what it recommends | done (no test reaches a probability of a sufficient outcome of 0.5: do not buy, D162) | v0.22.0 |
| 24 | Hunter: verdict | loaders, the stock book in the simulator, the pre-registration, one confirmation run | after the owner buys a history, which Session 23 advises against (D162) | |
| 25 | Book breakers | rolling-month and peak-to-trough breakers on managed NAV, the freeze read by the rebalancers | after 22 (beside 24) | |
| 26 | Hunter engine | the mechanism 24 tested, on the live book | after 22, 24, 25, the reserve's paper cycle and a sufficient or inconclusive-positive outcome | |
| 18 | Scoreboard, capital and promotion | TWR attribution, TCA, twins and monitoring, the automatic allocation rule, promotion | after 25, 27 and 24's verdict | |
| 19 | Cockpit, settings and deployment | MCP cockpit, Telegram, settings flow, dashboard, Docker, machine, backups, updates | after 18 | |
| 20 | Red-team and cleanup | failure-mode audit, mutation tests, `legacy/` deleted, docs truth pass, 1.0.0 | after 19 | |
| 15 | Event sleeve (the Asymmetry Hunter) | components, measured independence, lifecycle, triggers | when a batch leaves one of its components sufficient or inconclusive-positive | |
| 16 | LLM layer | provider and model registry, evaluation set, budget, structured outputs, six roles | when H7 or another LLM-derived signal is sufficient | |
| 17 | Decision layer, the rest | expression selection, calibrated sizing, expected shortfall, stress | when a funded layer needs it | |
| 19a | Capacity and scale | bonds, baskets, mutual funds, capacity limits, tax at scale | when managed capital reaches an activation condition it serves | |
| 21 | Research: later batches | H6, H9, the event sleeve's components, the §6.4 re-test group | any time, with power of at least 0.5 | |

The order is DECISIONS D150's. There is no Session 14: the core's backtest and verdict are
Session 4c (D61). Entries before Session 13 are in `docs/archive/ROADMAP_LOG.md`.

## Where the evidence stands

| Question | Answer so far | Source |
|---|---|---|
| Has the bot filled an order? | No. Three live contacts, three faults the fixtures did not show | Session 22 |
| Does any signal beat its benchmark after costs and tax? | None tested does: country momentum and its stop insufficient, no total-risk scaling sufficient | `research/pattern_registry.yaml` |
| What does the reserve earn? | D50: 6.49 % a year median after tax in Session 4c, not distinguishable from equities held (7.13 %) | `research/reports/core-allocation.md` |
| Do the analyst's or the owner's ideas beat the market? | Unknown: none has been graded. The thesis book grades them forward under H13's locked rule; no idea has been handed in | `research/preregistrations/thesis-book.md` |
| What would the Hunter earn? | Unknown: no stock-level test has run; the literature supports less than θ net of decay, costs and tax for every candidate | `research/reports/hunter-feasibility.md` |
| Can the Hunter's test decide? | Not as registered: the literature supports −0.8 % to +0.8 % a year net for H12 against θ = 2 %, so a sufficient outcome has probability at most 0.009, and no later test reaches 0.05 | `research/reports/hunter-feasibility.md`, D162 |

## Session log

### Session 23 — Hunter: data and power (2026-11-13, v0.22.0)

- **Verdict** (`research/reports/hunter-feasibility.md`, D162): do not buy. H12 on the whole
  universe has a probability of a sufficient outcome of at most 0.009 (the literature supports
  −0.8 % to +0.8 % a year net of decay, costs and `es` tax, against θ = 2 %); every later test
  stays at or below 0.04. The cheapest adequate history if bought anyway: Sharadar's Core US
  Equities Bundle, US$499 a year (prices from 1997 with delisted names, point-in-time
  fundamentals from 1998, a cloud-readable API); none sells a delisting return to one person.
- **Measured:** 4,832 listed US common stocks; 3,100 to 3,700 pass the account's floors at
  €50,000 and €250,000, none in 40 or 80 names at €10,000 (the US$1 commission minimum). XBRL
  covers 95–99 % of today's listed stocks by tier; 8-K item 2.02 precedes 45–64 % of periodic
  reports; 8 to 45 spin-off registrations a year.
- **Landed:** `obliskiq research power` reports the probability of a sufficient outcome for a
  test that states `supported_effect`, and names each below `min_sufficient_probability`.
- **Not built:** loaders, any return or correlation on candidate data, registry changes.
- **Live findings.** None: no IBKR call. **Prompts edited:** none.

### Session 27 — Thesis book (2026-11-13, v0.21.0)

- **Pre-registered first** (#63, locked): H13 reads the book's after-tax log growth minus a
  matched twin's in the equity reference at ten six-monthly looks; a step of 0.10 on the live
  record (alpha 0.05 over all looks, theta 2 %), a return to shadow (alpha 0.10), the same per
  source and conviction. At an edge of 1.5 tracking errors a step within two years has
  probability 0.33, not the "about two years" §5.5 states.
- **Landed** (D159, D161): `obliskiq idea add|list|show|withdraw`; one intake for any source; the gate
  with reason codes; slots, conviction sizes, the thesis's exits and its stop at the
  invalidation; grades per source and conviction; the digest's thesis section; a cycle's
  `thesis` step. A stock candidate is resolved, pinned and described when its thesis needs it.
- **Changed for it:** the reserve's budget in shadow and paper is 85 % (the book's tenth is cash
  while idle, against §5.5's "stays in the reserve", D159).
- **Found** (D160): no sell could be simulated in shadow; the gate now counts a simulated sell
  against the shadow book's fills. **Prompts edited:** none.
- **Not built:** the book's twin and its looks (Session 18).
- **Live findings.** None: no IBKR call.

### Session 22 — Order path on paper (2026-11-13, v0.20.2)

- **Verified first:** main did not handle it. A session's first live-orders read without
  `snapshot: true` made the gate refuse `account_unreadable`, `find` raise
  `LookupIncompleteError` and `reconcile` report an incomplete run.
- **Landed** (D158): the order client reads the live orders once per brokerage session before
  the read that counts, through the `live_orders` rate-limit group; the gate, the cOID lookup,
  `reconcile` and the kill switch's sweep read through it. A failed pre-read or a second
  incomplete read places nothing. The order test's report prints both reads' `snapshot` flag.
- **Runbook:** one ordered list, "From the order test to the reserve on paper": the order test,
  the execution test, the trend brake's history (13 month ends of SPPW or XDWD and XEON from
  `collect backfill`; the research store), the reserve's paper cycle, `serve`, ten shadow days.
- **Found:** no command printed an intent's history, where IBKR's text of a declined warning is
  kept; `intents list --history` prints it (read-only). **Prompts edited:** none.
- **Live findings.** None: no IBKR call. The `snapshot` row stays open until the owner's run.

### Owner questions: instruments, bearish views and options (2026-11-10, v0.20.1)

- **Plan:** the thesis book takes any eligible instrument (stocks on any venue, funds, ETCs on
  commodities and metals); a bearish view is a defensive instrument or a long put; the position
  count grows with capital by configuration (MASTER_PLAN §5.5).
- **Decision** (D157): a thesis may be a long call or put, paid from the book's capital inside the
  option budget and graded beside its underlying. H8's activation could never have been met.
- **Prompts:** 27 resolves an instrument outside the universe file and admits every eligible asset
  class; 29 (options for theses) written, after 27.
- **Owner:** the price history of Session 23 serves the systematic stock test only; the thesis
  book and its options need none.
- **Live findings.** None: no IBKR call.

### Owner ruling: ideas and the thesis book (2026-11-10, v0.20.0)

- **Ruling** (D156): the engine proposes and trades written ideas on its own; the owner hands
  one in when he has it, as any other or weighted by his conviction. All are graded forward.
- **Design:** the thesis book in MASTER_PLAN §5.5 (at most eight positions, one gate, the
  thesis's own exits), its capital in §5.19 (an exploration share, then the live record).
- **Path:** Sessions 27 (thesis book) and 28 (LLM analyst) written; they follow 22, beside 23
  and 24. Session 18 carries the thesis book's capital rule. H13 added as an idea.
- **Owner:** an LLM key before Session 28; the exploration share when the live account nears.
- **Live findings.** None: no IBKR call.

### Challenge review (2026-11-10, v0.20.0)

- **Review** of v0.19.1, read-only, then its fixes by the owner's instruction (D154, D155).
- **Research:** the run rule is the probability of a sufficient outcome at the literature's net
  effect; the Hunter's first test is the composite H12 (the owner's thesis) against its own
  universe, tiers and single hypotheses after it in a fixed sequence. H12 added as an idea.
- **Capital:** steps beyond 50 % count live-account months only; the ceiling is investable capital.
- **Prompts edited:** 22 (the owner pastes `o10164`'s text and the snapshot flags; the conversion's
  policy is decided from it), 23 (the sequence, the probability of sufficient, where the data can
  be read), 24 (the measure, no development sample for a published specification, the run's
  place), 25 (thresholds against D147), 26 (a filled conversion first; dividends, splits, mergers
  and delistings reconcile without the owner).
- **Not changed:** the reserve (D50), the 50 % first share and the aspiration, the owner's rulings.
- **Live findings.** None: no IBKR call.

### Owner ruling: every size and instrument, 50 % or more (2026-11-10, v0.19.1)

- **Ruling** (D153): the Hunter's universe is stocks of any size, each hypothesis tested on the
  whole universe and by size tier; long options through H8 and the sleeve; the aspiration is
  50 % a year or more.
- **Prompts:** Sessions 23, 24 and 26 read D153; 23 counts and powers each size tier and prices
  option-surface history for H8; 24 registers the tiers as tests.
- **Live findings.** None: no IBKR call.

### Owner rulings and the build path (2026-11-09, v0.19.0)

- **Rulings** (D147–D152): the drawdown point is withdrawn; the reserve runs D50 and holds
  everything the Hunter does not; the Hunter is a systematic small- and micro-cap stock book,
  funded to 50 % by a sufficient test and to 100 % by a live rule, with no owner command; no
  cycle rebuilds the book.
- **Path:** Sessions 22 to 26 written, then 18, 19, 20; Sessions 15, 16, 17's rest, 19a and 21
  wait on a condition. Session 15's started work is set aside.
- **Config:** `config/owner.yaml` sets `core.variant: D50`; the reserve already takes the
  inactive sleeves' shares (95 %, cash 5 %).
- **Registry:** H3, H10 and H11 added as ideas.
- **Economy:** AGENTS.md's record limits and effort rule; the older log moved to
  `docs/archive/ROADMAP_LOG.md`; the coverage floor fixed.
- **Live findings.** None: no IBKR call.
### Session 13 — Core engine (2026-10-25, v0.18.0)

**Landed.**
- `engines/core/` (D140): the mechanism Session 4c tested, now shared by research and the live
  book.
  - `rules.py`: role targets, the trend scale, the outer band, the losses-first sale order and
    held costly reductions.
  - `weights.py`: the environments' risk balance and the equity overlay.
  - `trades.py`: whole-unit delta trades, sells first, and buys scaled by one factor.
  - `variants.py`: the ten registered variants built from configuration.
  - `history.py`: month-end closes from the market store's bars, converted to the base
    currency.
  - `engine.py`: `plan_portfolio`, which returns a `TargetPortfolio` (`domain/core.py`).
  Research's `CoreStrategy` and the simulator run on the same code.
- `core.yaml` (D141): every variant with its constants, held to the pre-registration by a test.
  `core.variant: A` (D91, D95).
- `decision/core_vehicles.py` (D143): each role's vehicles through the selector. Swap partners
  for tax-aware variants. Held vehicles are kept, and closed when no longer proposable; a held
  vehicle no role names is sold; a refused identity is never bought.
- `decision/rebalance_core.py` replaces `rebalance_basic.py` (D142, D145). It has four triggers:
  - start;
  - band: every role back to its target, the only sales;
  - cash: cash beyond the targets buys the roles below theirs;
  - hold.
  The live guards cover:
  - an unconfirmed identity, the venue, intents already working;
  - reservations (no sale while one is short), the two-month rule;
  - the minimum trade and the commission;
  - spendable cash.
  Buys are scaled by one factor. A band's buys beyond the cash wait for the proceeds. There are
  no stops.
- `orchestration/correlations.py`: tracking and partner correlations read from the research
  store once a day. Without a store they are unmeasured, with the remedy.
- `capital/core_value.py` (D146): the core's `StrategyValue`. The planner measures "above its
  target" against the core's last target portfolio and exits a vehicle the core holds at zero.
- `quant/trend.py`: `trend_measures` and `settled`. `BaseRates.per_base_on` for a past day.
  `TaxHooks.basis`.
- The digest and the plan show the variant, the trigger, the trend date and each role's status
  and reason.
- Removed: `core.skeleton_targets`, `decision/rebalance_basic.py` and `trading.inner_band`.
- skfolio as a test-only dependency, with an import contract that keeps skfolio, scipy,
  scikit-learn and cvxpy out of the package.

**Tests.**
- `tests/integration/test_core_parity.py`: research's registered strategy through the simulator
  against the live engine on the store's closes. Five histories: A twice (one with a rally) and B
  three times. Equal weights and trades at every rebalance date. Each case asserts what it
  exercises: a band breach with sales, and for B tilt scales, partner buys and held reductions.
- `tests/integration/test_core_acceptance.py`: every registered variant produces a target
  portfolio through the live rebalancer on the shipped universe, for 1,000 and 1,000,000 euros.
  A holds its static weights on the filled roles, and gold's and commodities' 16 % stays in cash.
- `tests/unit/test_quant_risk_budget.py`: closed forms, a Newton solve, skfolio on the core's
  budgets and on random ones, and the legacy optimiser's still-valid cases: sample data, one
  asset, ten assets, near-zero variance, non-negative weights.
- Failure branches:
  - `test_rebalance_core.py`: unvalued holdings, the kill switch, frozen reconciliation, an
    unconfirmed or unquoted vehicle, a short reservation, a deferred loss, unknown tax, the
    minimum trade, the commission, the cash floor and unaffordable buys;
  - `test_engines_core.py` and `test_engines_core_history.py`: unfilled and held-only roles,
    short history, a missing rate, a gap;
  - `test_core_config.py`: a non-convergent solve, an incoherent variant;
  - `test_core_vehicles.py`, `test_core_correlations.py`.
- `make check` green: 5,008 tests, coverage 99.70 %.

**Deferred.**
- No IBKR call from the session. The core's live paper cycle is a command list for the owner
  (docs/RUNBOOK.md, which replaces Session 6's).
- Gold and broad commodities stay in cash until a venue of theirs is verified (Follow-ups).

**Live findings.** None: no live call.

**Dependabot.** filelock 4.2.1 (#49) and virtualenv 21.14.13 (#50) were merged. The major
cryptography 50 (#9, #5) stays in Follow-ups (D96).

**SESSION_PROMPTS edits.** None: later prompts name the core's `TargetPortfolio` and
`StrategyValue` as built.

## Live verification queue

Checks built against fixtures that still need a live reading. Each names the session that
settles it.

| Item | Added | Settled by |
|---|---|---|
| IBKR exchange codes → venues (38 codes in `ibkr_exchanges`; Sessions 8 and 9 pinned 57 universe conids with their listing exchanges through the owner's IBKR connector [MCP ✓ 2026-10-22]; the ten codes Session 8 added and Session 9's `IBIS2` are unverified on CP) | 2 | the collectors' live round |
| Venue phase windows: pre-market, auctions and post-market per venue (pulse values, unmeasured) | 2 | T1 or the first command list after it |
| SGX mid-day break hours (the XSES lunch override; the library skips the break) | 2 | T1 or the first command list after it |
| B3 regular close follows New York daylight saving time (the BVMF seasonal close) | 2 | T1 or the first command list after it |
| XEUR session hours versus single-stock option hours (options use `common_windows`) | 2 | T1 or the first command list after it |
| IBKR's trading-schedule format as a `ScheduleSource` (regular hours only are compared) | 2 | T1 or the first command list after it |
| Order window 30 min after the open and 15 before the close | 2 | 18 (TCA) |
| Holiday lists for XSHE, XNSE, CHIA, XSPX (blocked until sourced; XNSE holds data only) | 2 | 19a |
| Precomputed calendars end 2026-12-31 for XSHG, XBOM, XSES (`Calendars.coverage()`); the universe holds data only there (510300 on XSHG), collected in New York's job past that date (D96, D116) | 2 | 19a (a tradable candidate there) |
| IBKR market-data flag spellings: `FROZEN_DELAYED` → `frozen`, an unlisted or negated flag → NULL with the raw flag kept (CP's 6509 letter codes are decoded by `ibkr/decode.py`; MCP spellings unchanged) | 2 | T1 or the first command list after it |
| US stock, US option and Eurex option commission schedules | 2 | T1 (the paper order test), 18 (TCA) |
| US Treasury commission and the EUR-bond schedule above EUR 8,500 of face | 2 | 19a (bond rule) |
| Transaction taxes: FR's rate against CGI art. 235 ter ZD (Légifrance refused the container, 2026-10-22); IT's regulated-market rate and paying side in Legge 228/2012 art. 1 c. 491 and 494 (Legge 199/2025's 0.4 % standard rate read on Normattiva, 2026-10-22); GB, HK against a trade confirmation (ES's 0.2 % read in Ley 5/2020's BOE text, 2026-10-21). The core's ETFs and ETCs pay none of them | 2 | 15 (the first French or Italian share a thesis orders) |
| Friction tiers (spread, slippage, impact, option half-spread) are legacy assumptions | 2 | 18 (TCA) |
| Snapshot availability and delay: a US stock (Cboe One/IEX), a European UCITS ETF, a US option (OPRA), a Eurex option (Eurex Core), each with the owner logged in live elsewhere and not | 3 | T1 or the first command list after it |
| One market-scanner run (`/iserver/scanner/params`, `/run`) | 3 | T1 or the first command list after it |
| Snapshot field ids from IBKR's table: 7633 strike IV, 7638 open interest, 7087 historical vol, 7310 theta, 7311 vega | 3 | T1 or the first command list after it |
| `/iserver/secdef/search` without `secType` still returns sections | 3 | T1 or the first command list after it |
| `/contract/trading-schedule` shape, holidays omitted, and agreement with the calendars (closes the Session 2 phase-window, SGX, B3 and XEUR rows) | 3 | T1 or the first command list after it |
| `/iserver/auth/ssodh/init` with `publish`/`compete` as query parameters through the Gateway (lite used them over OAuth) | 3 | T1 or the first command list after it |
| `ssoExpires` and the daily reset as seen by the keepalive, re-logged in unattended (D105) | 3 | the first week of `serve` on the machine |
| The daily bar's `t` stamp per venue (the history window is filtered on the UTC date of the bar start; a venue stamping before UTC midnight would shift by a day) | 3 | 8 |
| Fixture shapes in `tests/fixtures/ibkr/` (from IBKR's reference and lite's readings, none captured through ObliskIQ) | 3 | T1 or the first command list after it |
| Order shapes: place, reply, modify, cancel, live orders (the `snapshot` flag on a session's first call), status, trades, whatif (docs/knowledge/ibkr-orders.md) | 4 | T1 (the paper order test) |
| The cOID: maximum length, `order_ref` in live orders and trades, the duplicate-cOID refusal and its window | 4 | T1 (the paper order test) |
| Warning ids on a stop-limit placement and modify (`o10331`; the reference's modify example shows `o0`) | 4 | the execution test (Session 5's command list) |
| Routing by `conidex` for a non-SMART venue | 4 | T1 (the paper order test) |
| A conversion as ObliskIQ sends it: `LMT` with `isCcyConv` and `fxQty` (the amount from) and no `quantity`; both sides; its trades' size and price; the `399` odd-lot notice as a reply or a notice (D98) | 4, 5 | the execution test |
| What a cancel of an order that has just filled answers | 4 | T1 (the paper order test) |
| `Inactive` after acknowledgement (an order the destination rejected); read as working | 4 | T1 (the paper order test), the execution test |
| Whether a `/tickle` between a warning and its answer invalidates the warning | 4 | T1 (the paper order test) |
| `obliskiq preflight --paper-order-test` end to end: identity, rules, algos, quote, whatif, place, replies, the cOID listed, status, cancel; record the shapes in docs/knowledge/ibkr-orders.md | 4a | T1 |
| Whether a whatif registers its cOID (the gate previews with its own) | 4a | T1 (the paper order test) |
| Whatif margin fields on a paper margin account, and the currency in its amount strings | 4a | T1 (the paper order test) |
| How long the positions read lags a fill; whether the trades lag the live orders' `filledQuantity`; then `reconcile.grace_seconds` from it (a judgement of 120 s until then) | 4a | the execution test (item d) |
| The live orders show a modify or a cancel at once (the exit coordinator clears the exit on the next read) | 4a | the execution test (`--finish`) |
| Positions closed during the day listed at zero | 4a | the execution test (`--finish`) |
| IBKR's attached orders: a whatif of an entry with a child stop-limit linked by `parentId`, then whether IBKR lists, reports and cancels the child (not adopted, D99) | review | a later session, before attached orders are designed |
| A triggered stop-limit that does not fill through a gap on paper, and its repricing | review | the first gap on paper (Session 6 on) |
| A fresh unattended Gateway login after IBKR's daily reset on the machine: settled, day 1 (2026-10-20) and day 2 (2026-10-21) passed under WSL2 Ubuntu with Java 11 [CP ✓ 2026-10-21]; the in-process re-login is Session 6's | review | settled (T1) |
| The live orders' `snapshot: true` on a session's first read: absent on the first read after a login (the ASX order test, 2026-10-21; the execution test's first `reconcile run`); the order client pre-reads once per brokerage session (D158): whether the read after it carries `snapshot: true`, and whether a re-armed session's first read lacks it too | review, 22 | T1's order test (docs/RUNBOOK.md, part A: the report's `live orders` line) |
| The stop-limit's `orderType`: ObliskIQ sends `STOP_LIMIT`, IBKR's order-types page writes `STP LMT` | 5 | the execution test |
| A GTC stop listed in a later session's live orders, and its status readable by id (D99) | 5 | the execution test (`--finish` the next day) |
| The spot-FX commission (0.20 bp, at least USD 2.00, charged in the base currency) against a conversion's trade | 5 | the execution test |
| The cOID kept unique for 24 hours (the place-order reference) | 5 | T1 (the paper order test) |
| The Adaptive algo's parameters and its limit cap, before `execution.pricing.algos` enables it | 5 | a paper order with the algo (Session 6 on) |
| The core's vehicles' conids through `/iserver/secdef/search` and `/iserver/contract/{conid}/info` (every vehicle a core role may hold is confirmed at each start; Session 9 pinned them on MCP) | 6, 13 | the core's live paper cycle (`run-cycle --dry-run`) |
| A whole cycle on paper: variant A's start (a conversion first if a chosen fund trades in another currency, and IBKR's text of `o10164` on it), the buys, their fills, the holdings registered to the core, reconciliation green, the digest with the core's variant, trigger and roles | 6, 13 | the core's live paper cycle |
| Telegram's `sendMessage` in HTML mode from the machine, and a digest split under the cap | 6 | the core's live paper cycle (with `TELEGRAM_*` set) |
| The shadow fill at the touch (D103) against paper fills of the same orders | 6 | 18 (TCA) |
| The health ping (`OBLISK_HEALTH_PING_URL`) from the machine | 6 | the first week of `serve` |
| The Gateway login on Java 21 and on Java 25 (the side test): passed once each, 2026-10-21 [CP ✓ 2026-10-21]; a second day lets them into `ibkr.gateway.java_major_versions` (D96) | D96 | a second day of the side runs (the order test's rerun day) |
| Playwright 1.64 logs in on Ubuntu 26.04 with the pinned-key flag: settled, six first-attempt logins on 2026-10-21 under WSL2 Ubuntu 26.04.1 [CP ✓ 2026-10-21] | D96 | settled (T1) |
| A manual Gateway login on the machine through an SSH-forwarded browser: not needed, the unattended login was accepted with a session | D96 | closed (T1) |
| European commissions (`europe_eur`, `europe_usd`, `lse_gbp`, `six_gbp`, `six_chf`, SmartRouting since D97) against an executed UCITS order | 4b | T1 (the paper order test), 6, 18 (TCA) |
| Tax-free fund switches at IBKR (`fund_switch_deferral_at_broker: false` until verified) | 4b | 19a (mutual funds) |
| The savings scale for tax year 2027 (the table is verified through 2026) | 4b | the monthly tax-source check (§5.16), 2027 |
| `yfinance_history` against Yahoo from the deployment machine (the call is network code outside the coverage floor; its frame is pinned by a fixture). From the cloud container it worked on 2026-10-17 (4c loaded TIP, IEF, GLD and CRSOX) | 4b | T1 |
| How IBKR reports dividends, interest, fees, withholding and conversions in the ledger and the account's activity; the classifier's fixtures from them (Session 7's item 10) | 7 | the first session with a working paper login |
| Settlement lags read in secondary sources only (`verify_live` in `venues.yaml`: BVMF, XSGO, XSWX, XSHG, XSHE, XKRX, XTAI, XSPX, XEUR) | 7 | 8 (the venues the universe admits) |
| The DGT's rulings on currency balances (V2422-20, V0463-21, V1613-25, V0152-26) in their official text | 7 | the monthly tax-source check (§5.16) |
| The ledger's per-currency `exchangerate` as the trade date's rate for tax (recorded by the NAV step and the heartbeat) against the rate IBKR reports on a foreign fill | 7 | the core's live paper cycle (a fund in another currency) |
| The core's vehicles' tax facts (ISIN, issuer, tracked index) against `/iserver/contract/{conid}/info` | 7, 13 | the core's live paper cycle |
| The collectors' live round (`obliskiq collect daily`, read-only): the underlying snapshot field set on every venue and its availability against the plan (`market_data.feeds`, their prices and their sharing with the paper session); the daily bar's `t` stamp per venue (bars are dated in the venue's zone); the chain walk (`secdef/search` OPT months and venues against the pinned conid, `secdef/strikes`, `secdef/info` per strike with `maturityDate`, `multiplier`, `local_symbol`; the declared venues FTA, MEFFRV, MONEP, EUREX); `/trsrv/futures` for the 16 roots on their native exchanges; one scanner run (`STK.US.MAJOR`) | 8 | the first command list after T1's order test |
| Pins for TOPIX, BARN, LISN and JDEP (`obliskiq universe pin`), and resolved descriptions for the 47 pinned instruments without one (an instrument without one is never ordered) | 8 | the collectors' live round |
| Venue profiles: trading permissions, listing currencies and lot conventions (`venues.yaml`, `verify_live: true`); Stocks: United States, Australia and Germany were held on the paper account by accepted previews [CP ✓ 2026-10-21] (`docs/knowledge/ibkr-capabilities.md`) | 8 | the capability probe (below), then each venue's first order |
| A stock candidate's holdings ticker and exchange name to IBKR's symbol and code, per venue (`collectors.candidates.exchanges`; resolution on demand) | 8 | the first candidate a thesis pins |
| Whether `/iserver/contract/{conid}/info` or `/trsrv/secdef` tells a share from an ETF and carries the ISIN: until read, a share outside the universe file needs a holdings-file equity line and an owner-given ISIN (D159) | 27 | the first candidate a thesis pins |
| The thesis book on the paper account: an entry with its stop resting at the invalidation, a funded entry in dollars, an exit beside its stop (`decision/rebalance_thesis.py`, fixtures only) | 27 | the first owner idea after the reserve's paper cycle |
| The capability probe on the paper account (`obliskiq universe capabilities --probe`, whatif only): search, quote, contract rules, algos and a preview per check, on every venue `capabilities.yaml` lists, US and Eurex options included; then the readings recorded in `capabilities.yaml` and `docs/knowledge/ibkr-capabilities.md` by a reviewed change | 9 | the first command list after T1's order test |
| Order types and algos per asset class, by name (T1 counted 12 types and 10 algos for SPY, 9 types for BHP, without names) | 9 | the capability probe |
| The trading-permission field 7768's values on a snapshot (IBKR's field table) | 9 | the capability probe |
| Xetra's `IBIS2` segment on CP, and the core candidates' conids, currencies and classes through `/iserver/contract/{conid}/info` (read through IBKR's connector) | 9 | the capability probe |
| UIQK's currency and share class (IBKR describes conid 128562052 as "H. CHF"; its ISIN is the USD A-acc class) | 9 | the capability probe |
| The PRIIPs KIDs of TIP1D, UIQK and Xetra-Gold, and Xetra-Gold's custody fee at IBKR (ubs.com refused the container; ineligible until read) | 9 | the next universe review |
| The DGT's reading of a bullion-backed ETC's result (a gain, or capital income under LIRPF art. 25.2) in an official text | 9 | the monthly tax-source check (§5.16) |
| GDELT's collection end to end from the deployment machine (the cloud session's shared egress met HTTP 429 on nearly every request, 2026-10-23; the fixtures are that day's live answers) | 10 | the first week of `serve` |
| An NYSE-listed halt in Nasdaq Trader's feed and its `Market` code (only NASDAQ and AMEX were listed on 2026-10-23); until seen, XNYS's mapping can add blocks and never lift one | 10 | the first NYSE halt while `serve` runs |
| A halt with its resumption as the feed shows it (`ResumptionDate`, `ResumptionTradeTime` formats; none was listed on 2026-10-23) | 10 | the first resumed halt while `serve` runs |
| The EventExtractor's first call: Gemini's OpenAI-compatible reply, its `usage`, and the `json_schema` response format accepted (no key in the session) | 10 | the owner setting `GEMINI_API_KEY` |
| The service's source jobs, the halt refresh and the extractor on the machine (built and tested over the fake broker and a mock web) | 10 | the first week of `serve` |
| `yfinance`'s earnings dates from the deployment machine (worked from the cloud, 2026-10-23) | 10 | the first week of `serve` |
| The option surface, the sleeve's candidates and the census on live chains: each tenor's nearest expiry, the smile's wings at deltas 0.10 and 0.25, the liquidity share, and how many contracts clear every gate; the FX decomposition on the board's live `fx_vs_base` and CPI; the commodity leaves on live curves (`VERIFY-LIVE(the option surface on live chains)`, `VERIFY-LIVE(futures curve prices)`) | 12 | the first command list after the collectors' live round |
| `obliskiq compute all` on a store the collectors filled live: each family's reach and its dominant blocker against the fixture store's, the session solve on a route that prices chains without vols, the coverage floor, and the run's time on the machine | 11 | the first command list after the collectors' live round |

## Follow-ups

- **Owner question (Session 23):** buy Sharadar's Core US Equities Bundle, full history, at
  US$499 a year, for the Hunter's test: yes or no? The recommendation is no: no test in the
  fixed sequence reaches a probability of a sufficient outcome of 0.5
  (`research/reports/hunter-feasibility.md`, D162). Session 24 waits for the answer.
- The cross-section harness Bonferroni-adjusts over every test of a confirmation run; a fixed
  sequence (D154) needs none. A pre-registration of the Hunter's sequence would need the
  adjustment named per test (Session 24, if it runs).
- The service runs no compute step yet: `obliskiq compute all` by hand. A scheduled job after
  each region's collection lands with the first engine that reads the measurement store on its
  own (Session 17; the core reads the collected bars and the research store directly, D144,
  D143). With it, the order path's eligibility reads the commodity engine's contango check
  through the store worker, in place of the fail-closed `UnavailableContangoCheck` (D137). That
  is needed before a futures-tracking ETC joins the universe.
- Session 17 hands the options sleeve its investable capital (`CapitalReader`) so the pool's
  gates are tested, and turns an active sleeve's entries into intents. Its penalty ledger is
  written by the fills and the zombie reviews it schedules.
- Not ported from lite (D135): the commodity-proxy beta and R² regression with its `min_r2`
  flag, and the agricultural linkage (fertiliser names on natural gas), both for Session 15's
  commodity components; the screen's cheap and coiled buckets, for Session 15; futures options
  (`instruments.allow_futures_options`), only with a never-exercise rule and a wider close
  buffer.
- Lite's 13-week positioning change (`commodities.cot`) is not carried (D134); add it to
  `data/positioning/readings.py` if a consumer asks for it.
- `GEMINI_API_KEY` in a developer's environment makes `test_cli_llm.py` and `test_env.py` fail
  locally (they assume the key absent); CI is unaffected. Clear the key inside those tests.
- The core holds gold's and broad commodities' share (16 % of A) in cash. Their candidates list
  on venues whose capabilities are unverified (the London and Milan lines, D121), so neither role
  has a vehicle the selector may propose. They fill when Session 9's probe on paper verifies a venue.
  The digest names both roles as unfilled (D143).
- On the deployment machine, `obliskiq research data load-universe` fills the research store. The
  service reads the selector's tracking correlations and the swap partners' correlations from it,
  once a day (D143). Without it every reading is unmeasured: the selector ranks without tracking,
  and a tilting variant has no partner. A needs neither. Session 19's deployment runs the load
  with the machine's setup.
- CFTC release exceptions: the 2025 government shutdown delayed the October–November reports,
  which the fixed lag of three days dates early; an exceptions list from the CFTC's notices before
  a backtest reads those weeks (D126).
- A 6-K without an exhibit carries its release after the form's cover page (Santander's); the
  cover's boilerplate opens the text the extractor reads. Skip the cover in
  `data/events/rss.py: filing_text` when Session 16 measures extraction quality.
- Session 15: the event components read `data/events/readings.py` (velocity, pulse),
  `data/positioning/readings.py` and `data/insiders/clusters.py`; GDELT's asset map from the old
  ObliskIQ's `gdelt_regions.json` is component logic and lands there.
- Session 16: the LLM layer builds on the extractor's stored series without breaking its segment
  (model `gemini-3.1-flash-lite`, prompt hash `5bed4ad9…ad77`); a change of either is a new segment
  and, for H7, a new pre-registration.
- Source jobs run inline in the service loop, as the market collections do (D129): a throttled
  GDELT run (two queries, three attempts each, spaced and backed off) can hold the heartbeat for
  about seven minutes at 01:00 UTC, inside Tokyo's session. Run source jobs beside the loop before
  an order on XTKS depends on the heartbeat's pace.
- A declared source is built only when a consumer appears (D66): Eurostat's flash HICP dates,
  the BoE, the SNB and the rest of the regional list in `config/sources.yaml`.

- Dependency currency (D96), each with its review date:
  - by 2026-11-30: Python 3.15. Widen `requires-python` to `<3.16`, try 3.15 in CI, then move
    `.python-version`, CI, mypy's and ruff's targets, `tests/unit/test_packaging.py` and
    MASTER_PLAN §7. The machine verifies it with one `preflight --read-only`.
  - next Dependabot round: one uv version for CI and the machine, in `[tool.uv]
    required-version`, read by `setup-uv` in place of `ci.yml`'s `version: "0.12.23"` (whether
    setup-uv reads that key is to be confirmed in that pull request's CI log).
  - before the live account's OAuth runs: cryptography 50 (Almoon-D/ObliskIQ#9, red;
    Almoon-D/ObliskIQ#5 is the same update). Parse the DH parameter file without
    `load_pem_parameters`, whose finite-field DH is deprecated (`oauth.py`); the exchange is
    already `pow`.
  - Every venue of a tradable candidate or option underlying is covered through
    `calendars.horizon_years` (a test); XSHG holds data only (510300), and past 2026-12-31 its
    collection folds into New York's job. A tradable candidate on XSHG, XBOM or XSES needs a
    calendar first.
  - The Gateway's Java list, and 2027-07-01 at the latest: Java 21 and 25 each passed one day
    (2026-10-21); a second day of side runs adds them, newest first (D96). Temurin builds Java 11 to
    at least October 2027.
- **Owner:** H2 (insider-purchase clusters) and H4 (the stock-level composite) are not evaluable
  with free prices (DECISIONS D84). A survivorship-free US price history with delisting returns
  would make them evaluable: CRSP through WRDS, or Norgate US Platinum at US$630 a year
  (2026-10-17). Session 23 found that no test in the Hunter's sequence would reach a
  probability of a sufficient outcome of 0.5 and advises against buying (D162); Session 24
  waits for the owner's answer. The Hunter has no tested signal (D87).
- **Owner:** the reserve runs D50 (D147). Open: an LLM provider key, or the minimal extractor
  waits (D70); whether a hedge inside a UCITS share class fits the mandate (D69); a deep
  emergency limit for protective exits (off by default, D67); buying option-surface history for
  H8 (D71).
- **Owner:** the paused Session 15 chat is not resumed (D150). Three branches belong to no open
  pull request and can be deleted once checked: `s13-core-engine` (merged as #52),
  `claude/blissful-lamport-8in96f` and `claude/session-4d-execution-ahadas`. The repository's
  description on GitHub still names the earlier design (reflexivity, risk parity, an AI that
  decides); it should say what README.md says.
- The session that records the owner's paste from docs/RUNBOOK.md ("From the order test to the
  reserve on paper") decides `o10164` on a conversion from IBKR's text (D101), in a pull request
  of its own: until a conversion fills on paper no order in a foreign currency can be funded,
  and Session 26 waits for it.
- The reconciliation freeze passes while no run exists, and a green run never ages
  (`execution/pretrade.py: _guards`, `execution/executor.py: _stage`,
  `decision/rebalance_core.py: survey`). Decide a maximum age, with failure-branch tests, before
  live-small (Session 20 at the latest).
- Session 20: `grid/`, `data/events`, `data/insiders`, `data/positioning` and `llm/` feed the
  analyst's fact sheet from Session 28 (D156). What still serves no order, verdict or fact line
  then (`engines/commodity`, `engines/fx`, parts of `engines/options_sleeve`) is kept or deleted
  by whether a layer reads it.
- **Owner:** an LLM provider key and its monthly budget before Session 28 (the analyst runs on
  fixtures without it and proposes nothing live); the thesis book's exploration share before the
  live account (a tenth of investable capital unless set).
- Session 26: `capital.satellite_ceiling`'s validation reserves a fixed core budget beside the
  ceiling, so the owner's 50 % (D149) cannot be written until the budget reader lets the reserve
  shrink. Until then the defaults stand: the Hunter is inactive and its share stays in the reserve.
- Maintenance session (SESSION_PROMPTS §F.2): Almoon-D/ObliskIQ#54 and Almoon-D/ObliskIQ#58
  (Dependabot, minor and patch).
- Research harness: the moving-block bootstrap draws a sample's first and last months less often
  than the others, so on a short sample its interval can sit beside the mean (Session 4e's
  forecast check on 46 development months). A circular block bootstrap would not; a change applies
  only to pre-registrations written after it.
- Re-estimated risk-balance weights, a multi-year blend and return timing are untested (D95).
  One is tested only with power of at least 0.5 on a longer history (D150); the 2004–2026 sample
  is not read again.
- Research harness: the simulator treats conversions as free (D75); `costs.yaml`'s `spot_fx` row
  prices them now (D97). A change applies only to pre-registrations written after it.
- Tax: apply the personal and family minimum to the savings base if the owner's general base
  leaves part of it unused (the engine errs high without it). No session holds it yet.
- Tax on the live path (D110): dividends, interest and withholding enter the tax ledger from the
  cash-flow ledger's classified adjustments; opening lots for holdings and currency the account
  held before the bot's first fill (entered by hand with their cost and date), so the hooks
  answer for them; a session before the live account (Session 18 at the latest).
- Session 15: `StrategyValue` for the satellite (probes, pending catalysts, exits it makes
  anyway); Session 17 for the decision layer (D108). The core's is D146.
- Session 19: Telegram and MCP answers to owner questions and the cash commands (D109).
- Session 19: the deployment's unit or container replaces docs/RUNBOOK.md's example systemd unit.
- When IBKR's pacing binds (the first weeks of `serve` show it): the gate reads the live orders and
  the trades fresh for every clearance, and IBKR paces each at one request per 6 s here, so an
  exit beside a stop (check, cut, clear) takes about 18 s and a cycle of N orders at least 6·N s;
  the heartbeat now runs the executor's cycle, reconciliation included, every 5 minutes (D102).
  Share one read per cycle under the store lock, keeping I2's rule that a sell cleared after the
  read still counts.
- The kill switch classes a sell by the most cautious of its intent's spent clearances (D48); once
  reconciliation knows the terms the broker accepted, it can class by those instead.
- Session 18 (I5): the boundary writes an intent only in the mode it was made for, so an
  automatic `live → live-small` downgrade would freeze every modify of an intent made under
  `live`, stop resizes and restores included. When the live levels can write, let a downgrade
  between two writing modes keep protective modifies of existing intents working.
- First session with captured paper rows (the core's live paper cycle): the sweep cancels a live-order
  row it cannot read (D48), which could strip a protective stop whose row fails validation for
  another field; read the raw `side` and `secType` of an unreadable row and keep a readable
  non-`CASH` sell.
- Session 17: a protective intent lives 365 days (`execution.protective.intent_ttl_days`);
  its GTC stop outlives it, but the boundary refuses the stop's modifies after it expires. Renew
  the intent (a new one adopting the order) before its end (D99).
- Session 18: calibrate the shadow fill at the touch (D103) against paper and live fills; it
  simulates no queue position, size beyond the displayed quote or impact. Shadow converts nothing,
  so a buy in a currency the account holds no cash in waits in shadow; simulate the conversion if
  L1's shadow days need it.
- A later session: IBKR's attached orders (D99). A whatif of an entry with a child stop-limit
  first (read-only), then the package's clearance (I1, I5), then the paper probe.
- Session 12: `tenor.mandatory_close_trading_days` lives in `config/defaults/instruments.yaml`;
  the rest of lite's `tenor` block joins it. `allow_futures_options` stays false until the
  never-exercise rule exists.
- Session 18: calibrate the gate's caps (`execution.pretrade.caps`), the cash floor, the protective
  spec's limit offset and gap steps, the pricing's start and reprices, and the currency manager's
  buffer, minimum and limit fraction from the transaction-cost analysis.
- **Owner:** paper runs use the Gateway with the automated login (owner, 2026-10-12; OAuth is
  the live account's, D42). Its login did not complete from a cloud container (most likely the
  outbound address changing per connection), so live runs happen on the deployment machine
  (SESSION_PROMPTS §A.5, T1 and the core's live paper cycle in docs/RUNBOOK.md); sessions build
  against fixtures. If the login fails there too, the owner chooses between a TWS API with IBC spike
  and a daily manual login (D68).
- Session 19: the deployment image's base is the OS D96 chose (Ubuntu 26.04), with the Java
  versions `ibkr.gateway.java_major_versions` lists after T1 and
  `playwright install --with-deps chromium` for the locked Playwright.
- Session 10: the `macro` CHECK `release_date >= reference_date` rejects a flash estimate
  stamped with a period-end reference date, and the whole batch rolls back; relax or split it
  with the first live macro route.
- Session 11: lite's range-based volatility estimators (Parkinson, Garman-Klass and the restated
  open share, `collect/vol.py`) and their tests, with the indicators that read them; Session 8
  ported the close-to-close estimator and the sigma ladder.
- Session 19a: holiday lists for the synthetic venues (XSHE, XNSE, CHIA, XSPX). XNSE holds data
  only (NIFTYBEES): its collection runs on the synthetic weekday calendar, and a quote on an
  unlisted holiday carries the route's own status.
- Session 10: feeds enter through `Calendars.accept_incidents(origin, incidents)`, which
  replaces one origin wholesale; a single-stock halt uses instrument scope.
- Session 12: lite's `mandatory_close_trading_days` (10) is `tenor.mandatory_close_trading_days`
  (Session 4a). Lite's own test expects 2026-06-05 for a 2026-06-19 holiday expiry at N = 10; the
  new anchor gives 2026-06-04, one session more conservative.
- Session 12: lite counted "N days back from expiry", excluding the expiry; the anchor is
  now the last tradable session itself, so N sessions including it is `session_before(..., sessions=N-1)`.
- Dependabot keeps `exchange-calendars` current; its holiday lists lapse every year, and a
  library upgrade that changes a session fails the golden snapshot for review.
- Research history (D118): during a session Yahoo's last row is that session at its price so far,
  stored as observed on its day; drop a row whose session has not closed when it is loaded
  (`research/data.py`), so a read as of that day never sees a close that was not one.
- Session 12: the options the chain collector records join the order path's directory
  (`UniverseDirectory.from_settings(options=…)`, each with its multiplier) when the options sleeve
  orders one; Session 8 stores them.
- Session 18: the thesis book's twin, its looks and its live share by H13
  (`research/preregistrations/thesis-book.md`); how its idle capital is held on the live account
  (cash in shadow and paper, D159); a thesis left in another mode's record after a mode change
  (held, never managed, today).
- A pinned stock candidate's venue lot (Tokyo's board lot of 100) is not applied to a thesis's
  quantity: IBKR's contract rules refuse an odd lot at the order. A venue first used by a thesis
  has its calendar loaded at the service's next start.
- Session 26: one conid belongs to one sleeve. An idea on an instrument the reserve holds is
  refused; the reverse (a core role whose vehicle a thesis already holds) is refused on paper by
  the gate's holdings check and is not told apart in shadow, where holdings are read from fills.
- The thesis book's caps count a position at its approved size, not its market value, and a
  fund's sector and country are unstated, so no cap counts it (Session 17's look-through, D69).
- Session 11: positions held outside the universe (lite's portfolio union) are not collected. The
  vehicle selector ranks universe instruments only (D120), so a held instrument joins the universe
  by a reviewed entry; Session 11's views decide which ones to propose.
- Session 15: lite's ADR map (legacy/old global_vehicles.py `ADR_MAP`): a company's US depositary
  line as another listing of a stock candidate, chosen when a thesis first orders it (D121).
- Session 18: the cost book prices an ETF of unknown market capitalisation at the illiquid friction
  tier (2.12 % a round trip, an assumption), so the selector separates European ETFs mostly by fee;
  a friction tier for ETFs by average daily traded value, from TCA (D120).
- The selector does not score the withholding a fund suffers by its domicile (MASTER_PLAN §5.8):
  every equity candidate is Irish and a fund's closes are net of it (D120). It joins when a
  candidate of another domicile competes for an equity role.
- Swap partners exist today for euro_govt_long (DBXG ↔ IBCL, 0.996) and, since D123, world_equity
  (SPPW ↔ XDWD, 1.000): the other roles' partners list on venues whose capabilities are
  unverified (VHVE on XMIL, EMIM and TPSA on XAMS, MTB on XPAR, VDTA on XMIL, the gold ETCs and
  commodity ETFs on XLON and XMIL) and appear when the probe verifies them. MSCI EM and FTSE
  Emerging read 0.93 to 0.94, below `min_correlation`; EMIM (MSCI EM IMI) reads 0.993 against
  XMME.
- The cash role's second and third candidates are unmeasured: Yahoo has no `CSH1.PA`, and CSH2's
  history starts on 2025-03-17 (36 monthly returns from 2028-03).
- **Owner:** rule on currency-hedged share classes (`vehicles.hedged_share_classes`, MASTER_PLAN
  §11).
- Session 11: add a column-targeted writer for the solved implied volatility (snapshot upserts
  replace the whole row); add a date-leading index on `bars` if a cross-sectional read needs it.
- A migration is forward-only: a later session that changes a table adds `002_*.sql`; it never
  edits `001_core.sql`.
- **Owner:** confirm the repository settings listed in the Session 1 log entry.
- **Owner:** after the merge, archive `Almoon-D/Inversor-Inteligente` (SESSION_PROMPTS §A.4).
- **Owner:** the copyright notice and package author name the owner (`pyproject.toml`,
  `src/obliskiq/__init__.py`), as in ObliskIQ-lite-MCP; confirm or ask for a change.
- GitHub's dependency graph also reads the manifests inside `legacy/`
  (`legacy/lite/pyproject.toml`, `legacy/lite/requirements.txt`,
  `legacy/pulse/pyproject.toml`), so Dependabot alerts may name them until Session 20
  deletes `legacy/`; they are never installed.
- Session 19: a release created with `GITHUB_TOKEN` does not trigger `on: release`
  workflows, so the image build must be a job in `release.yml` (as its prompt says).
- Release workflow: when a pending run is replaced by a newer push to `main`, the replaced
  version is never released (the newer commit carries its own version). Recovery: re-run the
  cancelled run from the Actions tab; it tags its own commit.
