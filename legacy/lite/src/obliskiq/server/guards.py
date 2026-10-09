"""The gate that stands between a bad trade and the ledger.

``store.ledger.open_position`` enforces ledger integrity: a thesis exists, an
invalidation trigger exists, the entry price is positive. It cannot enforce the
mandate, because it can see neither the chain nor the sleeve. That leaves a
``log_entry`` tool that merely forwarded to it with the skill as the only thing
between a bad trade and the ledger — the arrangement CLAUDE.md exists to forbid.

So this module composes the checks before the write. It defines **no threshold
of its own**: every decision here comes from ``CandidateGates``, ``PenaltyLedger``,
``TradingCalendars`` and ``RiskEngine``, which are the same objects the census,
the screener and the risk engine use. A rule enforced twice is a rule that will
eventually be enforced two different ways.

What it refuses, and why each one is here rather than left to judgment:

* **A prohibited instrument.** Futures carry undefined loss. Never a candidate,
  in any code path — including this one.
* **An entry outside the tenor window.** Entering at the minimum-hold figure
  lands the exit on expiry and violates the assignment rule.
* **An illiquid contract.** A spread nobody can cross is a position nobody can
  exit at the modelled price.
* **A premium the sleeve cannot afford**, measured against the pool net of what
  open positions already commit, or one so small that costs dominate it.
* **An option entry that does not say how many contracts it is.** The sleeve is
  a shared pool, so an entry whose premium commitment cannot be computed cannot
  be subtracted from it, and an uncounted commitment is over-permissive in
  exactly the direction the pool exists to close.
* **A book already at ``option_sleeve.max_concurrent_positions``.**
* **A mandatory close date that does not match the calendar**, which is how an
  option on a US-listed ETF gets held into an auto-exercise.
* **Averaging down** without a logged re-underwrite carrying a new dated catalyst.
* **A target that does not pay ``option_sleeve.min_payoff_ratio``.** The true
  cost of a loss in this sleeve is the premium times the penalty multiple, so a
  symmetric payoff loses before it is even wrong. The ratio was in config from
  the start and read in one place only — inside the zombie test's derived target
  — so no entry was ever measured against it.
* **A top-decile entry with no dated catalyst**, while
  ``discipline.entry_top_decile_requires_catalyst`` holds. That key was read by
  no code at all, and ``log_entry`` wrote ``entry_range_pctile`` to the ledger
  unchecked: the most expensive pattern in this account was being recorded
  faithfully and gated by nothing.
* **A tenor that decays below ``zombie_test.min_recoverable_pct_of_premium``
  before the thesis can resolve**, while ``tenor.require_theta_budget_check``
  holds. The theta budget existed as review-time reporting, which is the wrong
  end: it is an entry decision, and the flag saying it was required enforced
  nothing.

**Two kinds of missing input, and they are not treated alike.** An input only
the CALLER can state — the strike, the target, where in the range this entry
sits, how long the thesis is expected to take — is required, and its absence is
a refusal, exactly as ``contracts`` already is. An input the STORE may not hold
— NAV, a declared holiday calendar, a collected risk-free rate, a chain to build
a smile from — leaves its gate UNTESTED and reported, because that absence is
not the caller's to fix and refusing on it would make the tool unusable on a
young store. An untested gate is never a passed gate in either case.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ..compute.calendar import CloseDate, TradingCalendars
from ..compute.frames import chain_rows, latest_chain_date, latest_nav
from ..compute.gates import (
    GATE_ENTRY_RANGE,
    GATE_INSTRUMENT,
    GATE_IV_PERCENTILE,
    GATE_PAYOFF_RATIO,
    GATE_POSITION_SIZE,
    GATE_PRICE_CEILING,
    GATE_SPREAD,
    GATE_TENOR,
    GATE_THETA_BUDGET,
    AffordabilityCeiling,
    CandidateGates,
    GateFailure,
    GateResult,
)
from ..compute.penalty import Conversion, PenaltyLedger
from ..compute.risk_engine import CapReport, CircuitBreaker, Holding, RiskEngine, ThetaBudget
from ..compute.screen import OptionScreen
from ..compute.smile import Smile, SmileFilters
from ..config import Config
from ..logging_setup import get_logger
from ..macro.rates import cached_risk_free_rate
from ..store.db import Store
from ..store.ledger import Position, open_positions, open_sleeve_positions
from ..util.timeutil import parse_date

log = get_logger("server.guards")

#: A gate name for the concurrency cap, so a caller can test for it without
#: matching on prose.
GATE_CONCURRENCY = "max_concurrent_positions"

#: A gate name for a mandatory close date that disagrees with the calendar.
GATE_MANDATORY_CLOSE = "mandatory_close"

#: A gate name for an option entry that never said how many contracts it is.
GATE_CONTRACT_COUNT = "contract_count"

#: The portfolio drawdown breaker. An entry is NEW risk, which is exactly what
#: portfolio_circuit_breaker.action freezes; managing open risk is untouched.
GATE_CIRCUIT_BREAKER = "circuit_breaker"

#: The position, sector, country and single-foreign-currency caps under
#: `positions`, measured with THIS entry added to the book.
GATE_CONCENTRATION = "concentration_cap"

#: Cheap implied vol, per screen.cheap_iv_percentile_max. The screener applied
#: it and the write path did not, so an entry the screen would never have
#: surfaced could still be logged.
GATE_IV_PERCENTILE_ENTRY = GATE_IV_PERCENTILE


@dataclass(frozen=True)
class EntryVerdict:
    """Whether this entry may be written, and everything that was checked."""

    allowed: bool
    gates: GateResult
    ceiling: AffordabilityCeiling | None = None
    #: The close date the calendar computes, whenever an expiry was supplied.
    computed_close: CloseDate | None = None
    #: What the position is worth if the thesis takes its full expected time,
    #: computed whenever the inputs were there. Returned whether it passed or
    #: failed: a tenor that only just clears is worth seeing.
    theta_budget: ThetaBudget | None = None
    #: The rolling-month drawdown state this entry was tested against.
    circuit_breaker: CircuitBreaker | None = None
    #: Cap utilisation WITH this entry added to the book, when the inputs to
    #: measure it were there.
    caps: CapReport | None = None
    open_positions: int = 0
    checked: tuple[str, ...] = field(default_factory=tuple)
    #: Gates that could not be applied because their input was absent. Reported,
    #: never treated as passed.
    untested: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str | None:
        return self.gates.reason()


class EntryGuard:
    """Composes the existing gates into one verdict on a proposed entry."""

    def __init__(
        self,
        store: Store,
        config: Config,
        *,
        calendars: TradingCalendars | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._gates = CandidateGates(config)
        self._screen = OptionScreen(store, config)
        self._penalty = PenaltyLedger(store, config)
        self._calendars = calendars or TradingCalendars.from_config(config)
        self._risk = RiskEngine(store, config, calendars=self._calendars, penalty=self._penalty)
        self._max_concurrent = config.require_int("option_sleeve.max_concurrent_positions")
        self._close_trading_days = config.require_int("tenor.mandatory_close_trading_days")
        self._require_theta_budget = config.require_bool("tenor.require_theta_budget_check")
        self._smile_filters = SmileFilters.from_config(config)
        self._multiplier = config.require_int("instruments.default_option_multiplier")
        self._breaker_action = config.require_str("portfolio_circuit_breaker.action")

    def check_entry(
        self,
        *,
        as_of: date,
        instrument: dict[str, object],
        is_option: bool,
        nav: float | None = None,
        expiry: date | None = None,
        catalyst_date: date | None = None,
        premium_per_share: float | None = None,
        contracts: int | None = None,
        spread_pct_of_mid: float | None = None,
        open_interest: int | None = None,
        volume: int | None = None,
        mandatory_close_date: date | None = None,
        exchange: str | None = None,
        strike: float | None = None,
        is_call: bool | None = None,
        spot: float | None = None,
        target: float | None = None,
        entry_range_pctile: float | None = None,
        expected_resolution_days: int | None = None,
        dividend_yield: float | None = None,
        quantity: float | None = None,
        holdings: Sequence[Holding] | None = None,
    ) -> EntryVerdict:
        """Every applicable gate, against one proposed entry.

        ``quantity`` and ``holdings`` are what the concentration caps need: the
        exposure this entry ADDS, and the book it is added to. Absent, that gate
        reports untested rather than passing — the caps are defined over the whole
        book including holdings at brokers with no API, which the store cannot
        see on its own.
        """
        checked: list[str] = []
        untested: list[str] = []

        result = (
            self._gates.option_underlying(instrument)
            if is_option
            else self._gates.equity_purchase(instrument)
        )
        checked.append(GATE_INSTRUMENT)

        # The concurrency cap is an OPTION-sleeve rule: it bounds fragmentation of
        # the premium pool. Counting cash equity holdings against it let a book of
        # shares refuse an option entry in a sleeve those shares are not in.
        result = result.merge(self._concurrency(open_sleeve_positions(self._store)))
        checked.append(GATE_CONCURRENCY)

        # The averaging-down check asks a different question — "is this
        # instrument already held" — so it reads every open row, whatever sleeve.
        held = open_positions(self._store)

        # Buying strength without a catalyst is not an option-only failure: the
        # entries it has cost this account most were equity, bought outright. So
        # this gate runs on every entry, not only on the ones with a strike.
        result = result.merge(
            self._gates.entry_range(
                entry_range_pctile=entry_range_pctile, catalyst_date=catalyst_date
            )
        )
        checked.append(GATE_ENTRY_RANGE)

        # An entry is NEW risk, and that is exactly what the breaker freezes.
        # It was computed by check_position_risk and consulted by nothing, so a
        # book past its drawdown limit could keep opening positions.
        breaker = self._risk.circuit_breaker(as_of=as_of, nav=nav)
        breaker_result, breaker_tested = self._breaker(breaker)
        result = result.merge(breaker_result)
        (checked if breaker_tested else untested).append(GATE_CIRCUIT_BREAKER)

        ceiling: AffordabilityCeiling | None = None
        computed_close: CloseDate | None = None
        theta: ThetaBudget | None = None

        if is_option:
            result, ceiling, computed_close, theta, tested, missing = self._option_gates(
                result,
                as_of=as_of,
                nav=nav,
                expiry=expiry,
                catalyst_date=catalyst_date,
                premium_per_share=premium_per_share,
                contracts=contracts,
                spread_pct_of_mid=spread_pct_of_mid,
                open_interest=open_interest,
                volume=volume,
                mandatory_close_date=mandatory_close_date,
                exchange=exchange,
                currency=currency_of(instrument),
                strike=strike,
                is_call=is_call,
                spot=spot,
                target=target,
                expected_resolution_days=expected_resolution_days,
                dividend_yield=dividend_yield,
                contract_id=instrument.get("contract_id"),
            )
            checked.extend(tested)
            untested.extend(missing)

        result = result.merge(self._not_averaging_down(instrument, held, as_of=as_of))

        # The caps are measured with this entry already in the book: a cap read
        # before the position exists answers a question nobody asked.
        caps, cap_result, cap_tested = self._concentration(
            instrument=instrument,
            is_option=is_option,
            as_of=as_of,
            nav=nav,
            premium_per_share=premium_per_share,
            contracts=contracts,
            quantity=quantity,
            holdings=holdings,
        )
        result = result.merge(cap_result)
        (checked if cap_tested else untested).append(GATE_CONCENTRATION)

        # Cheap implied vol. The screener applies this on every candidate it
        # surfaces; the write path did not, so an entry the screen would have
        # rejected could still be logged by naming its contract directly.
        if is_option:
            contract_id = instrument.get("contract_id")
            percentile = (
                None
                if contract_id is None
                else self._screen.iv_percentile(int(contract_id), as_of=as_of)  # type: ignore[arg-type]
            )
            iv_result = self._gates.iv_percentile(percentile)
            result = result.merge(iv_result)
            (untested if percentile is None else checked).append(GATE_IV_PERCENTILE)

        verdict = EntryVerdict(
            allowed=result.passed,
            gates=result,
            ceiling=ceiling,
            computed_close=computed_close,
            theta_budget=theta,
            circuit_breaker=breaker,
            caps=caps,
            open_positions=len(held),
            checked=tuple(checked),
            untested=tuple(dict.fromkeys((*result.untested, *untested))),
        )
        if not verdict.allowed:
            log.warning(
                "entry rejected before the ledger write",
                extra={
                    "symbol": instrument.get("symbol"),
                    "failed_gates": list(result.failed_gates),
                },
            )
        return verdict

    def convert_to_base(
        self, amount: float, *, currency: str | None, as_of: date
    ) -> tuple[float, Conversion | None] | None:
        """``amount`` in ``account.base_currency``, and how it got there.

        ``None`` when it cannot be converted — no currency, or no rate for it.
        The caller must then refuse rather than book the figure as though it
        were already base: the sleeve is denominated in base currency, and a
        premium in another one added to it unconverted is wrong by the exchange
        rate in whichever direction the rate happens to sit.

        The rate comes from the same converter the affordability gate uses, so
        the amount checked and the amount booked cannot disagree.
        """
        base_currency = self._screen.fx.base_currency
        if not currency:
            return None
        if currency.upper() == base_currency.upper():
            return amount, None
        rate = self._screen.fx.to_base(currency, as_of=as_of)
        if rate is None or rate <= 0:
            return None
        return amount * rate, Conversion(
            currency=currency.upper(), quote_amount=amount, fx_to_base=rate
        )

    @property
    def base_currency(self) -> str:
        return self._screen.fx.base_currency

    # ── internals ──

    def _option_gates(
        self,
        result: GateResult,
        *,
        as_of: date,
        nav: float | None,
        expiry: date | None,
        catalyst_date: date | None,
        premium_per_share: float | None,
        contracts: int | None,
        spread_pct_of_mid: float | None,
        open_interest: int | None,
        volume: int | None,
        mandatory_close_date: date | None,
        exchange: str | None,
        currency: str | None = None,
        strike: float | None = None,
        is_call: bool | None = None,
        spot: float | None = None,
        target: float | None = None,
        expected_resolution_days: int | None = None,
        dividend_yield: float | None = None,
        contract_id: object | None = None,
    ) -> tuple[
        GateResult,
        AffordabilityCeiling | None,
        CloseDate | None,
        ThetaBudget | None,
        list[str],
        list[str],
    ]:
        checked: list[str] = []
        untested: list[str] = []

        if expiry is not None:
            result = result.merge(
                self._gates.tenor(
                    dte=(expiry - as_of).days, catalyst_date=catalyst_date, today=as_of
                )
            )
            checked.append(GATE_TENOR)
        else:
            untested.append(GATE_TENOR)

        result = result.merge(
            self._gates.liquidity(
                spread_pct_of_mid=spread_pct_of_mid,
                open_interest=open_interest,
                volume=volume,
            )
        )
        checked.append(GATE_SPREAD)

        result = result.merge(self._contract_count(contracts))
        checked.append(GATE_CONTRACT_COUNT)

        ceiling = self._ceiling(nav)
        if ceiling is not None and premium_per_share is not None:
            # The premium arrives in the CONTRACT's currency and the ceiling is in
            # base currency. The rate comes from the screen's converter rather
            # than a second one, so the guard and the screen cannot disagree
            # about the same trade.
            fx_to_base = self._screen.fx.to_base(currency, as_of=as_of)
            result = result.merge(
                self._gates.price_ceiling(
                    premium_per_share=premium_per_share,
                    ceiling=ceiling,
                    currency=currency,
                    fx_to_base=fx_to_base,
                )
            )
            checked.append(GATE_PRICE_CEILING)
            if contracts is not None:
                result = result.merge(
                    self._gates.position_size(
                        premium_per_share=premium_per_share,
                        contracts=contracts,
                        ceiling=ceiling,
                        currency=currency,
                        fx_to_base=fx_to_base,
                    )
                )
                checked.append(GATE_POSITION_SIZE)
            else:
                untested.append(GATE_POSITION_SIZE)
        else:
            # No NAV and none in the store, or no premium: neither affordability
            # gate could run. BOTH are reported. GATE_POSITION_SIZE used to fall
            # out of this branch appearing in neither list, which reads as a gate
            # that was not applicable rather than one nobody could apply.
            untested.append(GATE_PRICE_CEILING)
            untested.append(GATE_POSITION_SIZE)

        computed_close: CloseDate | None = None
        if expiry is not None:
            computed_close = self._calendars.close_date(
                expiry, trading_days=self._close_trading_days, exchange=exchange
            )
            result = result.merge(self._close_matches(computed_close, mandatory_close_date))
            checked.append(GATE_MANDATORY_CLOSE)
        else:
            # Without an expiry the calendar has nothing to count back from. The
            # gate vanished from both lists here, which is the one thing the
            # untested-is-never-passed rule exists to prevent: log_entry's own
            # description promises the close date is recomputed, and a caller
            # omitting `expiry` would have been told nothing. log_entry now
            # requires it; other callers get it reported.
            untested.append(GATE_MANDATORY_CLOSE)

        result = result.merge(
            self._gates.payoff_ratio(
                target=target,
                strike=strike,
                premium_per_share=premium_per_share,
                is_call=is_call,
            )
        )
        checked.append(GATE_PAYOFF_RATIO)

        theta: ThetaBudget | None = None
        if self._require_theta_budget:
            theta, theta_result = self._theta_budget(
                as_of=as_of,
                contract_id=contract_id,
                spot=spot,
                strike=strike,
                is_call=is_call,
                expiry=expiry,
                premium_per_share=premium_per_share,
                expected_resolution_days=expected_resolution_days,
                dividend_yield=dividend_yield,
            )
            result = result.merge(theta_result)
            checked.append(GATE_THETA_BUDGET)

        return result, ceiling, computed_close, theta, checked, untested

    def _theta_budget(
        self,
        *,
        as_of: date,
        contract_id: object | None,
        spot: float | None,
        strike: float | None,
        is_call: bool | None,
        expiry: date | None,
        premium_per_share: float | None,
        expected_resolution_days: int | None,
        dividend_yield: float | None,
    ) -> tuple[ThetaBudget | None, GateResult]:
        """What survives to the expected resolution date, with nothing else moving.

        Extrinsic value scales roughly with the square root of time, so a long
        hold burns a large share of it before the thesis has had a chance to
        work. ``tenor.require_theta_budget_check`` has said this must be checked
        at entry since the first config, and until now the only implementation
        ran at REVIEW time — which is the wrong end of the trade. By review the
        decay has already happened; the decision it should have informed was
        made months earlier.

        The caller's inputs are required; the store's are not. Sigma is read off
        the collected smile at the strike, at exactly the hygiene standard the
        zombie test uses, and the rate from the last collector run. Where the
        store holds neither, the gate is UNTESTED — that gap is not the caller's
        to close, and refusing every entry on a young store would make the tool
        unusable for the reason it exists.
        """
        if (
            spot is None
            or strike is None
            or is_call is None
            or expiry is None
            or premium_per_share is None
            or premium_per_share <= 0
            or expected_resolution_days is None
        ):
            return None, GateResult(untested=(GATE_THETA_BUDGET,))

        sigma = self._smile_iv(
            as_of=as_of, contract_id=contract_id, spot=spot, strike=strike, expiry=expiry
        )
        cached = cached_risk_free_rate(self._store, self._config)
        if sigma is None or cached is None:
            return None, GateResult(untested=(GATE_THETA_BUDGET,))

        budget = self._risk.theta_budget(
            spot=spot,
            strike=strike,
            is_call=is_call,
            sigma=sigma,
            rate=cached.rate,
            premium_paid=premium_per_share,
            entry_dte=(expiry - as_of).days,
            expected_resolution_days=expected_resolution_days,
            # The ZERO-DIVIDEND case, not a measured zero, and it is the LAST
            # resort rather than the default it used to be: the caller resolves
            # caller -> collected (dated, as_of-bounded) -> declared before ever
            # reaching this, and reports which of the four it used. `None` here
            # therefore means nothing is known from any of them.
            #
            # Zero rather than refusing to test, deliberately: most of this
            # universe genuinely pays nothing, and marking the theta budget
            # untested on every name without a yield would disable a gate
            # tenor.require_theta_budget_check says must run at entry. The
            # direction of the error is the reason it is tolerable here — pricing
            # a payer at q=0 leaves MORE extrinsic value in the model than really
            # survives, so this gate fails safe: it is harder to pass, never
            # easier.
            dividend_yield=dividend_yield if dividend_yield is not None else 0.0,
        )
        if budget.passes:
            return budget, GateResult()
        return budget, GateResult(
            failures=(
                GateFailure(
                    GATE_THETA_BUDGET,
                    f"if the thesis takes its full {expected_resolution_days} days, this "
                    f"position is worth {budget.remaining_pct_of_premium:.0%} of the premium "
                    f"paid, below the {budget.floor_pct:.0%} floor in "
                    "zombie_test.min_recoverable_pct_of_premium. The tenor is wrong: fix it "
                    "or drop the idea. Never enter hoping it resolves early "
                    "(tenor.require_theta_budget_check).",
                    observed=budget.remaining_pct_of_premium,
                    threshold=budget.floor_pct,
                ),
            )
        )

    def _smile_iv(
        self,
        *,
        as_of: date,
        contract_id: object | None,
        spot: float,
        strike: float,
        expiry: date | None = None,
    ) -> float | None:
        """The strike's own implied vol from the collected chain, or ``None``.

        Same path and same hygiene filters as check_position_risk: the entry
        gate and the exit gate must not disagree about what this strike's
        volatility is. That included ``expiry``, and did not use to: this built a
        smile pooled across every expiry on the chain while the zombie test built
        one from a single expiry, so the same strike carried two different
        volatilities depending on which gate asked. Different tenors are
        different volatilities; pooling them is not a smile.
        """
        if contract_id is None:
            return None
        underlying_id = int(contract_id)  # type: ignore[arg-type]
        session = latest_chain_date(self._store, underlying_id, until=as_of)
        if session is None:
            return None
        rows = chain_rows(
            self._store, underlying_id=underlying_id, snapshot_date=session, expiry=expiry
        )
        smile = Smile.from_rows(
            rows, spot=spot, underlying_id=underlying_id, expiry=expiry, filters=self._smile_filters
        )
        if smile is None:
            return None
        quote = smile.iv_at(strike)
        return None if quote is None else quote.iv

    def _ceiling(self, nav: float | None) -> AffordabilityCeiling | None:
        resolved = nav if nav is not None else latest_nav(self._store)
        if resolved is None or resolved <= 0:
            return None
        return self._screen.affordability(resolved)

    def _contract_count(self, contracts: int | None) -> GateResult:
        """An option entry must say how many contracts it is.

        Not a nicety. The sleeve budget is a shared pool, and what an entry
        commits to it is ``premium x multiplier x contracts``. Left unstated,
        the commitment is either uncounted — freeing budget that is actually
        spent, which is the defect the pool exists to close — or guessed, and a
        guessed commitment is worse than a refused entry.
        """
        if contracts is not None and contracts > 0:
            return GateResult()
        observed = None if contracts is None else float(contracts)
        return GateResult(
            failures=(
                GateFailure(
                    GATE_CONTRACT_COUNT,
                    f"an option entry must state a positive contract count, got "
                    f"{contracts!r}. The sleeve is a shared pool: without a size, the "
                    "premium this position commits cannot be subtracted from it.",
                    observed=observed,
                    threshold=1.0,
                ),
            )
        )

    def _breaker(self, breaker: CircuitBreaker) -> tuple[GateResult, bool]:
        """Refuse a NEW position while the drawdown breaker is tripped.

        Returns the result and whether the gate could be applied at all. The
        breaker reports ``n/a`` when ``nav_history`` holds too few observations
        in the window to measure a drawdown, and that is untested — not clear.

        ``portfolio_circuit_breaker.action`` freezes new buys and requires a
        book audit. It does not freeze the management of open risk, so nothing
        here touches log_exit, log_reunderwrite or check_position_risk.
        """
        if breaker.status != "ok":
            return GateResult(untested=(GATE_CIRCUIT_BREAKER,)), False
        if not breaker.tripped:
            return GateResult(), True
        drawdown = "unknown" if breaker.drawdown is None else f"{breaker.drawdown:.1%}"
        return (
            GateResult(
                failures=(
                    GateFailure(
                        GATE_CIRCUIT_BREAKER,
                        f"NAV is {drawdown} below its {breaker.window_days}-day peak, beyond "
                        f"portfolio_circuit_breaker.monthly_drawdown_pct "
                        f"({breaker.threshold:.0%}). The action is {breaker.action!r}: run the "
                        "book audit before adding risk. Existing positions keep their own "
                        "exit rules — this freezes new risk, not the management of open risk.",
                        observed=breaker.drawdown,
                        threshold=-breaker.threshold,
                    ),
                )
            ),
            True,
        )

    def _proposed_exposure(
        self,
        *,
        is_option: bool,
        premium_per_share: float | None,
        contracts: int | None,
        quantity: float | None,
    ) -> float | None:
        """What this entry adds to the book, in the CONTRACT's currency.

        ``None`` when the caller did not say how big it is: an option without a
        contract count, or an equity purchase without a quantity. The caps then
        report untested rather than measuring the book as though the new
        position were free.
        """
        if premium_per_share is None or premium_per_share <= 0:
            return None
        if is_option:
            if contracts is None or contracts <= 0:
                return None
            return premium_per_share * self._multiplier * contracts
        if quantity is None or quantity <= 0:
            return None
        return premium_per_share * quantity

    def _concentration(
        self,
        *,
        instrument: dict[str, object],
        is_option: bool,
        as_of: date,
        nav: float | None,
        premium_per_share: float | None,
        contracts: int | None,
        quantity: float | None,
        holdings: Sequence[Holding] | None,
    ) -> tuple[CapReport | None, GateResult, bool]:
        """The `positions` caps, measured with this entry added to the book.

        ``RiskEngine.cap_utilisation`` computed these and only ever REPORTED
        them, from check_position_risk — which runs on positions that already
        exist. The caps govern what is bought, so the place they have to bind is
        here, before the write.

        Untested rather than failed whenever the inputs are not all present: the
        caps are defined over the whole book, external holdings included, and
        that book comes from the caller. Measuring a country cap against the
        fraction of it the store happens to know is worse than saying nothing.
        """
        resolved_nav = nav if nav is not None else latest_nav(self._store)
        exposure = self._proposed_exposure(
            is_option=is_option,
            premium_per_share=premium_per_share,
            contracts=contracts,
            quantity=quantity,
        )
        currency = currency_of(instrument)
        fx_to_base = self._screen.fx.to_base(currency, as_of=as_of)
        base_exposure = None if exposure is None or fx_to_base is None else exposure * fx_to_base

        if resolved_nav is None or resolved_nav <= 0 or holdings is None or base_exposure is None:
            return None, GateResult(untested=(GATE_CONCENTRATION,)), False

        symbol = str(instrument.get("symbol") or f"contract:{instrument.get('contract_id')}")
        proposed = Holding(
            symbol=symbol,
            market_value_base=base_exposure,
            sector=_text_of(instrument, "sector"),
            country=_text_of(instrument, "market"),
            currency=currency,
            contract_id=(
                None if instrument.get("contract_id") is None else int(instrument["contract_id"])  # type: ignore[arg-type]
            ),
            cost_basis_base=base_exposure,
            nav_at_purchase=resolved_nav,
        )
        report = self._risk.cap_utilisation([*holdings, proposed], nav=resolved_nav, as_of=as_of)
        if not report.reliable:
            # Stale external holdings make every figure in it unreliable, and an
            # unreliable cap is not a passed cap.
            return report, GateResult(untested=(GATE_CONCENTRATION,)), False

        failures = tuple(
            GateFailure(
                GATE_CONCENTRATION,
                f"{entry.dimension} {entry.key!r} would be {entry.pct_of_nav:.0%} of NAV with "
                f"this entry, against a cap of {entry.cap:.0%} (positions). The cap governs "
                "what is bought.",
                observed=entry.pct_of_nav,
                threshold=entry.cap,
            )
            for entry in report.breaches
            if not entry.exceeds_by_compounding
        )
        return report, GateResult(failures=failures), True

    def _concurrency(self, held: list[Position]) -> GateResult:
        if len(held) < self._max_concurrent:
            return GateResult()
        return GateResult(
            failures=(
                GateFailure(
                    GATE_CONCURRENCY,
                    f"{len(held)} position(s) are already open against a cap of "
                    f"{self._max_concurrent}. Splitting the sleeve does not reduce the "
                    "penalty, it raises the probability of paying it.",
                    observed=float(len(held)),
                    threshold=float(self._max_concurrent),
                ),
            )
        )

    def _close_matches(self, computed: CloseDate, declared: date | None) -> GateResult:
        """The close date is recomputed here rather than trusted.

        A date supplied by the caller and never checked is how an option on a
        US-listed ETF gets held into an OCC auto-exercise: the whole point of
        setting it at entry is that the exit is controlled, not inherited.
        """
        if declared is None:
            return GateResult(
                failures=(
                    GateFailure(
                        GATE_MANDATORY_CLOSE,
                        "no mandatory_close_date was supplied. The calendar computes "
                        f"{computed.close_date.isoformat()} ({computed.provenance}); set it "
                        "at entry so the exit is controlled rather than inherited.",
                    ),
                )
            )
        if declared != computed.close_date:
            return GateResult(
                failures=(
                    GateFailure(
                        GATE_MANDATORY_CLOSE,
                        f"mandatory_close_date {declared.isoformat()} disagrees with the "
                        f"calendar, which computes {computed.close_date.isoformat()} "
                        f"({computed.provenance}).",
                    ),
                )
            )
        if not computed.holidays_declared or not computed.years_covered:
            # Two ways for the same thing to be true: no holiday list at all, or
            # a list that declares nothing in the years this date spans. Both
            # mean the date was computed on weekends alone, so both leave the
            # gate untested rather than passed.
            return GateResult(untested=(GATE_MANDATORY_CLOSE,))
        return GateResult()

    def _not_averaging_down(
        self, instrument: dict[str, object], held: list[Position], *, as_of: date
    ) -> GateResult:
        """An entry on an instrument already held is an add, and adds are gated."""
        contract_id = instrument.get("contract_id")
        if contract_id is None:
            return GateResult()
        existing = [p for p in held if p.contract_id == int(contract_id)]
        if not existing:
            return GateResult()

        failures: list[GateFailure] = []
        for position in existing:
            verdict = self._risk.averaging_down(position, as_of=as_of)
            if not verdict.allowed:
                failures.append(
                    GateFailure(
                        GATE_INSTRUMENT,
                        f"position {position.position_id} on this instrument is already "
                        f"open: {verdict.reason}",
                    )
                )
        return GateResult(failures=tuple(failures))


def parse_optional_date(raw: str | None, *, label: str) -> date | None:
    """A date argument that may be absent, but must be a date when present."""
    if raw is None or not str(raw).strip():
        return None
    parsed = parse_date(str(raw))
    if parsed is None:
        raise ValueError(f"{label} {raw!r} is not a date in YYYY-MM-DD form")
    return parsed


def _text_of(instrument: dict[str, object], key: str) -> str | None:
    """A stripped string field of the universe row, or ``None`` when it is blank."""
    value = instrument.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def currency_of(instrument: dict[str, object]) -> str | None:
    """The instrument's own currency, or ``None`` when it does not declare one.

    ``None`` matters: it makes the affordability gates report UNTESTED rather
    than compare a premium of unknown denomination against a base-currency
    budget. An instrument row with no currency is a universe gap to fix, not a
    reason to assume the account's own.
    """
    value = instrument.get("currency")
    if value is None:
        return None
    text = str(value).strip()
    return text or None
