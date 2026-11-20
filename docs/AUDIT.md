# Audit: the silent failures

Session 20's red-team of the build (2026-11-16, at v0.28.0). A silent failure is one that loses
money or protection without an error, an alert or a red test. The ten most probable are ranked
by probability times the euros at stake on a small account; each names the test that fails when
its guard is removed. Three were real and are fixed; seven were guarded, and the guard was proven
by breaking it. Then a mutation pass over the six money-path packages. Nothing here is evidence
of returns (DECISIONS D62).

## Method

- **Read-only first.** The money-path modules were read against the failure modes the prompt
  lists; a suspected gap was confirmed by a failing test before any fix.
- **A guard is proven by removing it** (lite's rule, `docs/knowledge/legacy-findings.md`): the
  guarding line was changed on a copy of the tree and the tests named below were run. Every
  mutant in the table was killed; the tests are the proof, and each can be repeated by hand.
- **Mutation pass.** One operator changed per mutant (a comparison, `and`/`or`, `not`, `is`,
  `in`, a sign, `min`/`max`, a boolean) in `decision/`, `execution/`, `capital/`, `tax/`,
  `jurisdiction/` and `calendars/`, and the tests that cover the mutated line run against it.

## The ten

| # | Failure | Before | Guard, and the test that fails without it |
|---|---|---|---|
| 1 | **New risk on an account nobody reconciled.** The freeze read "the latest run is not green": a machine that had never reconciled, or whose last green run was days old (an outage, a failing heartbeat), cleared buys and conversions | real | `execution/freeze.py` (D179): no run, a run not green, or a green one older than `execution.reconcile.max_age_seconds` freezes new risk at the gate, the executor's staging and both rebalancers. `tests/unit/test_execution_freeze.py`; `test_service_jobs.py::test_a_cycle_with_no_recent_reconciliation_reconciles_before_it_decides`; `test_paper_order_test.py::test_a_machine_whose_reconciliation_has_aged_reconciles_before_it_places` |
| 2 | **A deposit that reads as a loss.** An owner's flow was placed in the return on the day he dated it, not the day the broker credited it: a transfer of 30 % of the book credited two days late read as −30 % on one day and +30 % on another, leaving −9 % in the month and a fall the breakers measure | real | `capital/twr.py: reached_on` (D184): the day the account's cash showed it. `test_capital_units.py::test_a_transfer_credited_after_its_date_is_never_a_loss_and_then_a_gain`; `test_book_breakers.py::test_the_books_flows_are_the_capped_books_or_the_accounts` |
| 3 | **An update that leaves the machine on a version nobody judged.** `update.sh` ran under `set -e`: a unit that failed to start, or a closed terminal during the 15-minute wait, ended it on the new tag with no rollback and no alert | real | one rollback on every failure and signal (D182). `test_deploy_scripts.py::test_a_new_version_that_does_not_start_is_rolled_back`, `::test_units_that_do_not_stop_leave_the_previous_version_running`, `::test_an_interrupted_update_is_rolled_back` |
| 4 | **IBKR's no-market-data warning (`o354`) confirmed on a quote it should not rest on**: real-time-only classes, a stale or undated delayed quote, a limit outside the collar; or any unknown warning confirmed | guarded | `execution/replies.py`, `execution/quotes.py`. Four mutants of the reply policy and one of the freshness check, all killed by `test_execution_replies.py` and `test_execution_quotes.py`; none of the reply policy's 31 mutants survived the pass, and one of the quote policy's does (the collar's ceiling at exact equality) |
| 5 | **A second order after a restart or an unanswered placement** | guarded | the cOID is looked up before every placement (`ibkr/orders.py`) and an unknown outcome waits out the grace period (`execution/executor.py`). Killed by `test_ibkr_orders.py` and `test_execution_executor.py` |
| 6 | **A position left without its stop, or with a stop larger than the position, after a partial fill or beside an exit** | guarded | `execution/protective.py` (coverage is the position less the exits working beside it). Killed by `test_execution_protective.py` and `test_exits.py` |
| 7 | **The kill switch cancelling protective stops, or reading as off when it cannot be read** | guarded | `execution/killswitch.py` (a sell of a security is kept; any read failure is ON). Killed by `test_execution_killswitch.py` |
| 8 | **A conversion in the wrong direction, or a foreign balance driven negative** | guarded | `execution/fx.py`, the gate's `margin_use` checks on a buy and on a conversion, reconciliation's `negative_cash`. Killed by `test_execution_currency.py`, `test_pretrade_commitments.py`, `test_pretrade_reasons.py`, `test_execution_reconcile.py`; every mutant of a leg's direction was killed, and one survives in `fx.py` (the guard of the whatif preview, which only a conversion without an amount could reach) |
| 9 | **The paper guard bypassed**: a write on an account without a paper prefix under profile `paper` | guarded | `ibkr/guard.py`, checked by the client, the gate and the write boundary. Killed by `test_order_path_guards.py`, `test_pretrade_reasons.py`, `test_execution_boundary.py` |
| 10 | **An option left to expire, or traded past its last session**, because the last tradable session was computed from the wrong venue or ignored a holiday | guarded, two tests added | `calendars/service.py: last_tradable_session` and the gate's date checks. The pass found the gate's two boundaries untested (the last session itself, the first day of the mandatory-close window); `test_pretrade_reasons.py::test_an_option_trades_on_its_last_session_and_not_the_day_after` and `::test_the_mandatory_close_window_opens_on_its_first_day` close them. Killed with `test_calendars_service.py`, `test_calendars_breaker.py` |

Item 1 changed three owner commands: the paper order test and the execution test reconcile
before they place (a `reconciliation` step in their reports), and a decision cycle that finds
no run in force reconciles before it plans. A dry run, which writes nothing, plans as that
cycle would.

## The other candidates

Read and found guarded, with what the mutation pass says of the module that guards each.

| Candidate | Guard |
|---|---|
| A delayed quote treated as fresh; an option on a delayed quote or underlying | `execution/quotes.py` (one surviving mutant, the collar's ceiling at exact equality) |
| A competing session, or a lost Gateway login, leaving the bot silently disconnected | the keepalive's `tickle`, re-login and alerts (`ibkr/session.py`), a config check that a failed re-login alerts within one heartbeat, the heartbeat's `session` line; outside the six packages |
| An eligibility bypass through a new universe row or stock candidate | the gate asks the jurisdiction's policy for every buy and refuses an instrument nobody describes (`execution/pretrade.py`, `jurisdiction/`) |
| A point-in-time leak | every store read is bounded by `as_of` (`tests/unit/test_store_guards.py`, `test_store_point_in_time.py`); the research harness's own tests perturb data after `as_of` |
| A research run on an unlocked or edited pre-registration | the lock holds the file's SHA-256 and its merge commit (`research/prereg.py`, `research/registry.py`) |
| LLM injection altering a hypothesis or an owner command | `llm/` cannot import `decision/`, `execution/` or `ibkr/` (import contract); commands are parsed by a deterministic grammar and confirmed |
| The release workflow tagging twice or publishing without notes | `release.yml` leaves an existing release alone and fails on a missing or empty changelog section (`tests/unit/test_changelog.py`, `test_workflows.py`) |
| A market-wide halt ignored, or one stock's halt blocking a venue | `calendars/incidents.py`, `calendars/halt_feeds.py` (in the pass) |
| Integer rounding breaching the cash floor | the gate's cash check counts the whatif's own cost, every open commitment and the floor |

## The mutation pass

Run on v0.28.0 with the suite's own coverage telling which tests reach each line. A mutant is
killed when a covering test fails. The first forty (later twenty) most focused covering tests
were run for each; a survivor with more covering tests than that is **unconfirmed**: it may be
killed by a test that was not run.

| Package | Mutants | Killed | Survived | Unconfirmed | No covering test |
|---|---:|---:|---:|---:|---:|
| `execution/` | 1,547 | 1,150 | 203 | 172 | 22 |
| `decision/` | 821 | 649 | 91 | 73 | 8 |
| `capital/` | 416 | 340 | 49 | 26 | 1 |
| `tax/` | 208 | 152 | 24 | 31 | 0 |
| `jurisdiction/` | 147 | 122 | 12 | 11 | 1 |
| `calendars/` | 253 | 216 | 12 | 24 | 1 |
| all | 3,392 | 2,629 | 391 | 337 | 33 |

Two more ended in a tool error. No mutant survived in the reply policy or the intent state
machine.

**Closed in this session** (32 survivors, each re-run against its new test and killed; one
more is equivalent): the option's last session and the first day of the mandatory-close window
(`execution/pretrade.py`); the retry of a partly filled order, which a sign flip would have
re-bought in full plus the fills (`execution/executor.py`); what open orders and working
conversions still commit, and the room a conversion has under the currency cap
(`execution/currency.py`); a flatten line that failed reading as closing
(`execution/flatten.py`); the two-month window's first and last day, the dates a conflict
names, the basis that carries a deferred loss and the tax a trial adds (`tax/lots.py`); which
vehicles are homogeneous, when a fund switch defers and what may be a swap partner
(`jurisdiction/tax_es_irpf.py`, `tax_generic.py`).

**Left open: 358 survivors and the 337 unconfirmed.** They are not closed and not all read, so
none is claimed equivalent. By where they are: `decision/thesis_options.py` 42,
`execution/live_test.py` 40, `capital/planner.py` 38, `execution/executor.py` 20,
`execution/protective.py` 22, `execution/capability_probe.py` 19, `execution/pretrade.py` 15,
`decision/rebalance_thesis.py` 16, `execution/paper_test.py` 14, `execution/reconcile.py` 14;
the unconfirmed are mostly `execution/pretrade.py` (86) and `decision/vehicle_selector.py`
(47), whose lines many tests cover. Of those read, most change a refusal's wording, a report
line or a comparison at exact equality; the ones that matter most are in the reservation
planner (which protected position is cut, the pacing of sales, what a plan counts as covered),
where sign flips and `and`/`or` swaps survive. Session 20a closes or justifies every one,
starting there.

## Observations left as they are

- The currency manager sizes a conversion against the base cash less the conversions already
  working out of it, not less the open buys in the base currency. The gate then counts every
  commitment and refuses a conversion the cash cannot cover, so the trade waits with an alert
  instead of converting less. It fails closed.
- `engines/risk` has no reader until a stock book exists (D181).
- A credit that arrives after the day's last NAV reading and is matched before midnight UTC is
  still counted in that reading (D184).
