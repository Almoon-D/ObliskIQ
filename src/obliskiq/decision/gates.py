"""The gates every option candidate passes, in one place (lite's ``compute/gates.py`` and the
evaluation of its ``compute/screen.py``).

The census counts what clears them, the options sleeve logs what clears them while it runs in
shadow, and the decision layer (Session 17) enters only what clears them: one implementation, so
those callers can never disagree about what "eligible" means, and a gate cannot be added to one of
them and forgotten in another. :meth:`CandidateGates.candidate` is the composition; the entry
gates (a position's size, the payoff at its target, the entry's place in its range, the theta
budget) are applied by :meth:`CandidateGates.entry` once a size and a thesis exist.

What the module is strict about:

* **A futures contract is never a candidate**, nor a leveraged or inverse product: the
  underlying's declared class and resolved description are checked before anything is measured.
* **The minimum entry tenor is not the minimum hold.** ``tenor.entry_dte_min`` sits above
  ``tenor.hold_days_min`` on purpose: entering at the hold figure lands the exit on the expiry.
  A dated catalyst moves the window's lower edge to the catalyst plus its buffer.
* **The budget is a shared pool.** :meth:`CandidateGates.affordability` subtracts the penalty
  balance and the premium already committed to open positions from the sleeve's budget; without
  the second subtraction the budget would not be a cap. Unspent budget stays unspent.
* **Every refusal names itself**, with a reason code (:class:`Gate`) a digest can group on, and
  a gate whose input was absent is **untested**, with why: an untested gate is never a passed
  one, and a waived gate (a rule in configuration) is a passed one.
* **Currencies are compared converted, or not at all.** A premium arrives in the contract's
  currency and the pool is in the base currency; a missing rate leaves the gate untested and
  never assumes parity.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..domain.enums import AssetClass, OptionRight
from ..domain.instrument import leveraged_inverse_marker
from ..domain.market import ChainRow
from ..quant.smile import Smile

ZERO = Decimal(0)


class Gate(StrEnum):
    """The gates, as reason codes a refusal is grouped on."""

    INSTRUMENT = "instrument"
    TENOR = "tenor"
    CATALYST = "catalyst_buffer"
    MANDATORY_CLOSE = "mandatory_close"
    SPREAD = "spread"
    OPEN_INTEREST = "open_interest"
    VOLUME = "session_volume"
    SMILE = "smile"
    CONCURRENCY = "max_concurrent_positions"
    PRICE_CEILING = "price_ceiling"
    POSITION_SIZE = "position_size"
    IV_PERCENTILE = "iv_percentile"
    PAYOFF_RATIO = "payoff_ratio"
    ENTRY_RANGE = "entry_range_catalyst"
    THETA_BUDGET = "theta_budget"


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One gate, and why it closed."""

    gate: Gate
    reason: str
    observed: float | None = None
    threshold: float | None = None


@dataclass(frozen=True, slots=True)
class Untested:
    """A gate that could not be applied, and which input was missing."""

    gate: Gate
    reason: str


@dataclass(frozen=True, slots=True)
class GateResult:
    """The outcome of a set of gates.

    ``untested`` gates were never applied (their input was absent); ``waived`` ones were applied
    and passed by a rule in configuration (the spread on a book deeper than
    ``liquidity.spread_waiver_min_open_interest``).
    """

    failures: tuple[GateFailure, ...] = ()
    untested: tuple[Untested, ...] = ()
    waived: tuple[Gate, ...] = ()

    @property
    def passed(self) -> bool:
        """Nothing failed. Not the same as clearing every gate: see :attr:`fully_tested`."""
        return not self.failures

    @property
    def fully_tested(self) -> bool:
        """Passed, with every gate actually applied."""
        return self.passed and not self.untested

    @property
    def failed_gates(self) -> tuple[Gate, ...]:
        return tuple(failure.gate for failure in self.failures)

    @property
    def untested_gates(self) -> tuple[Gate, ...]:
        return tuple(dict.fromkeys(note.gate for note in self.untested))

    def reason(self) -> str | None:
        if not self.failures:
            return None
        return "; ".join(failure.reason for failure in self.failures)

    def merge(self, other: GateResult) -> GateResult:
        return GateResult(
            failures=self.failures + other.failures,
            untested=self.untested + other.untested,
            waived=self.waived + other.waived,
        )


def _fail(gate: Gate, reason: str, observed: float | None, threshold: float | None) -> GateResult:
    return GateResult(failures=(GateFailure(gate, reason, observed, threshold),))


def _untested(gate: Gate, reason: str) -> GateResult:
    return GateResult(untested=(Untested(gate, reason),))


def merged(results: Sequence[GateResult]) -> GateResult:
    out = GateResult()
    for result in results:
        out = out.merge(result)
    return out


@dataclass(frozen=True, slots=True)
class Affordability:
    """What the sleeve can still commit, before any contract is priced.

    ``budget`` is the sleeve's gross budget (``capital.budgets.option_premium`` of investable
    capital); ``available`` is what is left after the penalty balance and the premium open
    positions commit, floored at zero. Every figure is in ``base_currency``.
    """

    budget: Decimal
    penalty_balance: Decimal
    committed_premium: Decimal
    available: Decimal
    min_premium_position: Decimal
    base_currency: str

    @property
    def locked_by(self) -> str | None:
        """What closed the sleeve, when nothing is available: a penalty reopens on qualifying
        realised gains only, commitments the moment a position closes."""
        if self.available > ZERO:
            return None
        penalty = self.penalty_balance > ZERO
        committed = self.committed_premium > ZERO
        if penalty and committed:
            return "penalty_and_commitments"
        if penalty:
            return "penalty"
        if committed:
            return "commitments"
        return "no_budget"


@dataclass(frozen=True, slots=True)
class OptionCandidate:
    """One contract of a chain, and what the gates read about it besides its quote.

    Each optional input says why it is missing in the matching ``*_gap``: the gate it feeds is
    then untested with that reason. ``open_positions`` is the sleeve's open option positions;
    ``fx_to_base`` multiplies an amount in the contract's currency into the base currency.
    """

    row: ChainRow
    underlying: UniverseInstrument
    as_of: date
    currency: str
    smile: Smile | None
    smile_gap: str | None = None
    iv_percentile: float | None = None
    iv_percentile_gap: str | None = None
    close_by: date | None = None
    close_by_gap: str | None = None
    affordability: Affordability | None = None
    affordability_gap: str | None = None
    fx_to_base: Decimal | None = None
    fx_gap: str | None = None
    open_positions: int | None = None
    open_positions_gap: str | None = None
    catalyst_date: date | None = None


@dataclass(frozen=True, slots=True)
class OptionEntry:
    """The terms an entry states on top of its candidate: how many contracts, the named target,
    where the underlying sits in its long-run range, and the share of premium the theta budget
    leaves (``None`` with ``theta_gap`` when it could not be computed)."""

    contracts: int
    target: float | None
    entry_range_pctile: float | None
    theta_remaining_share: float | None
    theta_gap: str | None = None


def _money(value: float) -> Decimal:
    """A float quote as the decimal it prints as (no binary noise at the order boundary)."""
    return Decimal(repr(value))


class CandidateGates:
    """Every gate, bound to configuration once."""

    def __init__(self, settings: ObliskConfig) -> None:
        liquidity = settings.liquidity
        self._max_spread = liquidity.max_spread_pct
        self._spread_waiver_oi = liquidity.spread_waiver_min_open_interest
        self._min_open_interest = liquidity.min_open_interest
        self._require_volume = liquidity.require_session_volume
        tenor = settings.tenor
        self._entry_dte_min = tenor.entry_dte_min
        self._entry_dte_max = tenor.entry_dte_max
        self._catalyst_buffer_days = tenor.catalyst_buffer_days
        self._hold_days_min = tenor.hold_days_min
        self._require_theta_budget = tenor.require_theta_budget_check
        sleeve = settings.option_sleeve
        self._max_concurrent = sleeve.max_concurrent_positions
        self._min_premium = sleeve.min_premium_base_currency
        self._min_payoff_ratio = sleeve.min_payoff_ratio
        self._min_recoverable = settings.zombie_test.min_recoverable_pct_of_premium
        discipline = settings.discipline
        self._top_decile_needs_catalyst = discipline.entry_top_decile_requires_catalyst
        self._top_decile_pctile = discipline.entry_top_decile_pctile
        self._cheap_iv_max = settings.screen.cheap_iv_percentile_max
        self._base_currency = settings.account.base_currency
        instruments = settings.instruments
        self._prohibited = frozenset(instruments.prohibited)
        self._markers = tuple(instruments.leveraged_inverse_markers)

    # ── the composition ──

    def candidate(self, candidate: OptionCandidate) -> GateResult:
        """Every candidate gate against one contract: the single gate path for option candidates
        (the census, the sleeve's shadow log and the decision layer all call it)."""
        row = candidate.row
        as_of = candidate.as_of
        results = [
            self.option_underlying(candidate.underlying),
            self.tenor(
                dte=(row.expiry - as_of).days, catalyst_date=candidate.catalyst_date, as_of=as_of
            ),
            self.mandatory_close(
                close_by=candidate.close_by, as_of=as_of, gap=candidate.close_by_gap
            ),
            self.liquidity(
                spread_pct_of_mid=row.spread_pct_of_mid,
                open_interest=row.open_interest,
                volume=row.volume,
            ),
            self.smile(candidate.smile, strike=row.strike, gap=candidate.smile_gap),
            self.concurrency(
                open_positions=candidate.open_positions, gap=candidate.open_positions_gap
            ),
            self.price_ceiling(
                premium_per_unit=row.mid,
                multiplier=row.multiplier,
                currency=candidate.currency,
                fx_to_base=candidate.fx_to_base,
                fx_gap=candidate.fx_gap,
                ceiling=candidate.affordability,
                ceiling_gap=candidate.affordability_gap,
            ),
            self.iv_percentile(candidate.iv_percentile, gap=candidate.iv_percentile_gap),
        ]
        return merged(results)

    def entry(self, candidate: OptionCandidate, terms: OptionEntry) -> GateResult:
        """The candidate gates and the entry's own: its size against the pool and the floor, the
        payoff at its named target, a top-decile entry's catalyst, the theta budget."""
        row = candidate.row
        results = [
            self.candidate(candidate),
            self.position_size(
                premium_per_unit=row.mid,
                multiplier=row.multiplier,
                contracts=terms.contracts,
                currency=candidate.currency,
                fx_to_base=candidate.fx_to_base,
                fx_gap=candidate.fx_gap,
                ceiling=candidate.affordability,
                ceiling_gap=candidate.affordability_gap,
            ),
            self.payoff_ratio(
                target=terms.target,
                strike=row.strike,
                premium_per_unit=row.mid,
                is_call=row.right is OptionRight.CALL,
            ),
            self.entry_range(
                entry_range_pctile=terms.entry_range_pctile, catalyst_date=candidate.catalyst_date
            ),
            self.theta_budget(terms.theta_remaining_share, gap=terms.theta_gap),
        ]
        return merged(results)

    # ── the instrument ──

    def option_underlying(self, declared: UniverseInstrument) -> GateResult:
        """May a listed option on this instrument be bought? PRIIPs is not consulted: it restricts
        the cash instrument, not the option, which is why the sleeve exists; the jurisdiction's
        eligibility policy judges the option at the order (pre-trade)."""
        symbol = declared.symbol
        if declared.asset_class is AssetClass.FUTURE and "futures_options" in self._prohibited:
            return _fail(
                Gate.INSTRUMENT,
                f"{symbol} is a futures contract: an option on it is a futures option, which the "
                "mandate prohibits (its curve stays a data read for the wrapper choice)",
                None,
                None,
            )
        if declared.description is None:
            return _fail(
                Gate.INSTRUMENT,
                f"{symbol} has no resolved description, so it cannot be told apart from a "
                "leveraged or inverse product; an instrument nobody can describe is never bought",
                None,
                None,
            )
        if self._prohibited & {"leveraged_etps", "inverse_etps"}:
            marker = leveraged_inverse_marker(declared.description, self._markers)
            if marker is not None:
                return _fail(
                    Gate.INSTRUMENT,
                    f"{symbol} reads as a leveraged or inverse product (marker {marker!r} in "
                    f"{declared.description!r})",
                    None,
                    None,
                )
        return GateResult()

    # ── tenor ──

    def tenor(self, *, dte: int, catalyst_date: date | None, as_of: date) -> GateResult:
        """Is the expiry inside the entry window, and does it clear a dated catalyst?"""
        results: list[GateResult] = []
        if dte < self._entry_dte_min:
            results.append(
                _fail(
                    Gate.TENOR,
                    f"{dte} DTE is below the minimum entry tenor of {self._entry_dte_min}; it sits "
                    f"above the {self._hold_days_min}-day minimum hold on purpose: entering at the "
                    "hold figure lands the exit on the expiry and violates the assignment rule",
                    float(dte),
                    float(self._entry_dte_min),
                )
            )
        elif dte > self._entry_dte_max:
            results.append(
                _fail(
                    Gate.TENOR,
                    f"{dte} DTE is beyond the maximum entry tenor of {self._entry_dte_max}",
                    float(dte),
                    float(self._entry_dte_max),
                )
            )
        if catalyst_date is not None:
            required = catalyst_date + timedelta(days=self._catalyst_buffer_days)
            expiry = as_of + timedelta(days=dte)
            if expiry < required:
                results.append(
                    _fail(
                        Gate.CATALYST,
                        f"the expiry {expiry} is before the catalyst on {catalyst_date} plus the "
                        f"{self._catalyst_buffer_days}-day buffer: catalysts slip, and post-event "
                        "repricing takes days",
                        float((expiry - as_of).days),
                        float((required - as_of).days),
                    )
                )
        return merged(results)

    def mandatory_close(
        self, *, close_by: date | None, as_of: date, gap: str | None = None
    ) -> GateResult:
        """An option is never opened on or after the session it must be sold by."""
        if close_by is None:
            return _untested(
                Gate.MANDATORY_CLOSE, gap or "the mandatory-close date could not be computed"
            )
        if as_of >= close_by:
            return _fail(
                Gate.MANDATORY_CLOSE,
                f"the option must be sold by {close_by}: it is not opened inside its "
                "mandatory-close window",
                float((close_by - as_of).days),
                0.0,
            )
        return GateResult()

    # ── liquidity ──

    def liquidity(
        self, *, spread_pct_of_mid: float | None, open_interest: int | None, volume: int | None
    ) -> GateResult:
        """Open interest, spread and session volume.

        A book deeper than ``liquidity.spread_waiver_min_open_interest`` waives the spread gate:
        with that much size resting, the quoted spread is not what a limit at mid pays. A negative
        spread is a crossed book (ask below bid), a broken quote and never a tight one, and no
        depth waives it.
        """
        results: list[GateResult] = []
        if open_interest is None:
            results.append(_untested(Gate.OPEN_INTEREST, "the route reported no open interest"))
        elif open_interest < self._min_open_interest:
            results.append(
                _fail(
                    Gate.OPEN_INTEREST,
                    f"open interest {open_interest} is below the floor of "
                    f"{self._min_open_interest}",
                    float(open_interest),
                    float(self._min_open_interest),
                )
            )
        if spread_pct_of_mid is not None and spread_pct_of_mid < 0:
            results.append(
                _fail(
                    Gate.SPREAD,
                    f"the spread is {spread_pct_of_mid:.1%} of mid: the book is crossed (ask below "
                    "bid), a broken quote that depth does not waive",
                    spread_pct_of_mid,
                    0.0,
                )
            )
        elif open_interest is not None and open_interest >= self._spread_waiver_oi:
            results.append(GateResult(waived=(Gate.SPREAD,)))
        elif spread_pct_of_mid is None:
            results.append(
                _untested(Gate.SPREAD, "a side of the book is missing, so no spread is measured")
            )
        elif spread_pct_of_mid > self._max_spread:
            results.append(
                _fail(
                    Gate.SPREAD,
                    f"the spread is {spread_pct_of_mid:.1%} of mid against a ceiling of "
                    f"{self._max_spread:.1%}",
                    spread_pct_of_mid,
                    self._max_spread,
                )
            )
        if self._require_volume:
            if volume is None:
                results.append(_untested(Gate.VOLUME, "the route reported no session volume"))
            elif volume <= 0:
                results.append(
                    _fail(Gate.VOLUME, "no contracts traded this session", float(volume), 1.0)
                )
        return merged(results)

    # ── the smile ──

    def smile(self, smile: Smile | None, *, strike: float, gap: str | None = None) -> GateResult:
        """The contract's expiry has a smile that survived hygiene and spans its strike: the
        zombie test reads N(d2) and the greeks off it, and a strike beyond the surviving wings
        would read a clamped volatility (an extrapolation, not an observation)."""
        if smile is None:
            return _fail(
                Gate.SMILE,
                gap
                or "no smile survived hygiene at this expiry, so the zombie test could never read "
                "this position's N(d2) or greeks",
                None,
                None,
            )
        strikes = smile.strikes
        if not strikes[0] <= strike <= strikes[-1]:
            return _fail(
                Gate.SMILE,
                f"the strike {strike:g} lies outside the surviving smile ({strikes[0]:g} to "
                f"{strikes[-1]:g}): its volatility would be clamped to the wing",
                strike,
                None,
            )
        return GateResult()

    # ── the pool ──

    def affordability(
        self, *, budget: Decimal, penalty_balance: Decimal, committed_premium: Decimal
    ) -> Affordability:
        """The pool: the sleeve's budget less the penalty balance and the committed premium.

        ``committed_premium`` is required rather than defaulted: forgetting it would size every
        entry against the whole sleeve, the defect it exists to close.
        """
        committed = max(ZERO, committed_premium)
        penalty = max(ZERO, penalty_balance)
        return Affordability(
            budget=budget,
            penalty_balance=penalty,
            committed_premium=committed,
            available=max(ZERO, budget - penalty - committed),
            min_premium_position=self._min_premium,
            base_currency=self._base_currency,
        )

    def concurrency(self, *, open_positions: int | None, gap: str | None = None) -> GateResult:
        """The sleeve's only anti-fragmentation rule: at most ``max_concurrent_positions``."""
        if open_positions is None:
            return _untested(
                Gate.CONCURRENCY, gap or "the sleeve's open positions could not be counted"
            )
        if open_positions >= self._max_concurrent:
            return _fail(
                Gate.CONCURRENCY,
                f"{open_positions} option positions are open, the most "
                "option_sleeve.max_concurrent_positions allows: splitting does not reduce the "
                "penalty, it raises the probability of paying it",
                float(open_positions),
                float(self._max_concurrent),
            )
        return GateResult()

    def cost_in_base(
        self,
        amount: Decimal,
        *,
        currency: str,
        fx_to_base: Decimal | None,
    ) -> Decimal | None:
        """``amount`` in the contract's ``currency`` stated in the base currency; ``None`` when no
        rate converts it (never parity: an assumed rate is invisible and admits the one thing the
        affordability gates exist to refuse)."""
        if currency == self._base_currency:
            return amount
        if fx_to_base is None or fx_to_base <= ZERO:
            return None
        return amount * fx_to_base

    def _premium(
        self,
        gate: Gate,
        *,
        premium_per_unit: float | None,
        multiplier: float | None,
        contracts: int,
        currency: str,
        fx_to_base: Decimal | None,
        fx_gap: str | None,
        ceiling: Affordability | None,
        ceiling_gap: str | None,
    ) -> tuple[Decimal | None, GateResult]:
        if ceiling is None:
            return None, _untested(gate, ceiling_gap or "the sleeve's budget is unknown")
        if premium_per_unit is None or premium_per_unit <= 0:
            return None, _untested(gate, "the contract has no two-sided quote to price it at")
        if multiplier is None:
            return None, _untested(
                gate,
                "IBKR's contract details stated no multiplier, so one contract's cost is unknown",
            )
        local = _money(premium_per_unit) * _money(multiplier) * contracts
        cost = self.cost_in_base(local, currency=currency, fx_to_base=fx_to_base)
        if cost is None:
            return None, _untested(
                gate, fx_gap or f"no rate converts {currency} into {self._base_currency}"
            )
        return cost, GateResult()

    def price_ceiling(
        self,
        *,
        premium_per_unit: float | None,
        multiplier: float | None,
        currency: str,
        fx_to_base: Decimal | None,
        ceiling: Affordability | None,
        fx_gap: str | None = None,
        ceiling_gap: str | None = None,
    ) -> GateResult:
        """Does one contract fit the available pool? A ceiling only: the premium floor is about a
        position, not a contract (a cheap contract is bought in size), so it lives in
        :meth:`position_size`."""
        cost, result = self._premium(
            Gate.PRICE_CEILING,
            premium_per_unit=premium_per_unit,
            multiplier=multiplier,
            contracts=1,
            currency=currency,
            fx_to_base=fx_to_base,
            fx_gap=fx_gap,
            ceiling=ceiling,
            ceiling_gap=ceiling_gap,
        )
        if cost is None or ceiling is None:
            return result
        if cost > ceiling.available:
            return _fail(
                Gate.PRICE_CEILING,
                f"one contract costs {cost:,.2f} {self._base_currency} against an available "
                f"pool of {ceiling.available:,.2f}"
                + (f" (locked by {ceiling.locked_by})" if ceiling.locked_by else ""),
                float(cost),
                float(ceiling.available),
            )
        return GateResult()

    def position_size(
        self,
        *,
        premium_per_unit: float | None,
        multiplier: float | None,
        contracts: int,
        currency: str,
        fx_to_base: Decimal | None,
        ceiling: Affordability | None,
        fx_gap: str | None = None,
        ceiling_gap: str | None = None,
    ) -> GateResult:
        """A position of ``contracts`` between the absolute floor and the available pool. The
        floor (``option_sleeve.min_premium_base_currency``) refuses a position whose costs
        dominate it, and nothing else."""
        if contracts <= 0:
            return _fail(
                Gate.POSITION_SIZE,
                f"an option entry states a positive contract count, got {contracts}: the pool "
                "cannot subtract a commitment it cannot compute",
                float(contracts),
                1.0,
            )
        cost, result = self._premium(
            Gate.POSITION_SIZE,
            premium_per_unit=premium_per_unit,
            multiplier=multiplier,
            contracts=contracts,
            currency=currency,
            fx_to_base=fx_to_base,
            fx_gap=fx_gap,
            ceiling=ceiling,
            ceiling_gap=ceiling_gap,
        )
        if cost is None or ceiling is None:
            return result
        if cost > ceiling.available:
            return _fail(
                Gate.POSITION_SIZE,
                f"{contracts} contract(s) cost {cost:,.2f} {self._base_currency} against an "
                f"available pool of {ceiling.available:,.2f}",
                float(cost),
                float(ceiling.available),
            )
        if cost < ceiling.min_premium_position:
            return _fail(
                Gate.POSITION_SIZE,
                f"{contracts} contract(s) cost {cost:,.2f} {self._base_currency}, below the "
                f"position floor of {ceiling.min_premium_position:,.2f}: a round trip's commission "
                "alone would cost more than the widest spread the liquidity gate accepts",
                float(cost),
                float(ceiling.min_premium_position),
            )
        return GateResult()

    @staticmethod
    def contracts_for_budget(*, unit_cost_base: Decimal, ceiling: Affordability) -> int:
        """How many contracts the available pool buys at ``unit_cost_base`` each (never fewer
        than zero; leftovers stay unspent)."""
        if unit_cost_base <= ZERO:
            return 0
        return int(ceiling.available // unit_cost_base)

    # ── the screen ──

    def iv_percentile(self, percentile: float | None, *, gap: str | None = None) -> GateResult:
        """Implied vol in the cheap quartile of its own range (``screen.cheap_iv_percentile_max``,
        the bound the ranked views read too)."""
        if percentile is None:
            return _untested(
                Gate.IV_PERCENTILE,
                gap or "no implied-vol percentile is measured for the underlying",
            )
        if percentile > self._cheap_iv_max:
            return _fail(
                Gate.IV_PERCENTILE,
                f"the IV percentile {percentile:.0%} is above the cheap-options ceiling of "
                f"{self._cheap_iv_max:.0%}",
                percentile,
                self._cheap_iv_max,
            )
        return GateResult()

    # ── entry discipline ──

    def payoff_ratio(
        self,
        *,
        target: float | None,
        strike: float,
        premium_per_unit: float | None,
        is_call: bool,
    ) -> GateResult:
        """Does the named target pay ``option_sleeve.min_payoff_ratio`` times the premium? Measured
        on intrinsic value at the target, the level the zombie test derives when none is named
        (strike plus the ratio times the premium), so the two cannot disagree."""
        if target is None:
            return _fail(
                Gate.PAYOFF_RATIO,
                "an option entry names a realistic target price; only the caller can state it",
                None,
                self._min_payoff_ratio,
            )
        if premium_per_unit is None or premium_per_unit <= 0:
            return _untested(
                Gate.PAYOFF_RATIO, "the contract has no premium to measure a payoff on"
            )
        intrinsic = (target - strike) if is_call else (strike - target)
        ratio = intrinsic / premium_per_unit
        if ratio >= self._min_payoff_ratio:
            return GateResult()
        return _fail(
            Gate.PAYOFF_RATIO,
            f"the target {target:g} pays {ratio:.2f}x the premium at expiry, below the "
            f"{self._min_payoff_ratio:g}x option_sleeve.min_payoff_ratio asks: a loss costs the "
            "premium times the penalty multiple, so a symmetric payoff loses before it is wrong",
            ratio,
            self._min_payoff_ratio,
        )

    def entry_range(
        self, *, entry_range_pctile: float | None, catalyst_date: date | None
    ) -> GateResult:
        """A top-decile entry needs a dated catalyst while
        ``discipline.entry_top_decile_requires_catalyst`` holds: buying strength is not a
        reason."""
        if not self._top_decile_needs_catalyst:
            return GateResult()
        if entry_range_pctile is None:
            return _untested(
                Gate.ENTRY_RANGE, "where the entry sits in its long-run range was not measured"
            )
        if entry_range_pctile < self._top_decile_pctile or catalyst_date is not None:
            return GateResult()
        return _fail(
            Gate.ENTRY_RANGE,
            f"the entry sits at {entry_range_pctile:.0%} of its long-run range, at or above the "
            f"{self._top_decile_pctile:.0%} of discipline.entry_top_decile_pctile, with no dated "
            'catalyst: "it is going up" is not one',
            entry_range_pctile,
            self._top_decile_pctile,
        )

    def theta_budget(self, remaining_share: float | None, *, gap: str | None = None) -> GateResult:
        """What survives to the thesis's expected resolution with nothing else moving, as a share
        of the premium, at least ``zombie_test.min_recoverable_pct_of_premium`` while
        ``tenor.require_theta_budget_check`` holds: a tenor that decays below it is wrong."""
        if not self._require_theta_budget:
            return GateResult()
        if remaining_share is None:
            return _untested(Gate.THETA_BUDGET, gap or "the theta budget could not be computed")
        if remaining_share >= self._min_recoverable:
            return GateResult()
        return _fail(
            Gate.THETA_BUDGET,
            f"if the thesis takes its full expected time the position keeps "
            f"{remaining_share:.0%} of its premium, below the {self._min_recoverable:.0%} floor: "
            "the tenor is wrong; fix it or drop the idea, never enter hoping it resolves early",
            remaining_share,
            self._min_recoverable,
        )
