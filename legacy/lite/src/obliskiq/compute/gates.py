"""The gates every candidate passes, in one place.

The census counts what clears them; the screener returns what clears them; the
risk engine checks an existing position against them. One implementation, so
those three can never disagree about what "eligible" means — and so that adding
a gate cannot be forgotten in one of the three.

Four things this module is deliberately strict about.

**A futures contract is never a candidate.** Undefined loss, and it violates the
no-leverage mandate. :func:`reject_prohibited` is the only door, and it closes on
``FUT`` before any other test runs. Reading the futures CURVE is a separate
operation with no candidate in it at all — see
:mod:`obliskiq.compute.commodity_engine` and ``instruments.futures_data_use``.

**PRIIPs restricts the CASH instrument, not the option.** An EU retail account
cannot buy SPY for want of a KID and may still trade listed options on it, which
is precisely why the option sleeve exists. The equity gate and the option gate
are therefore different functions, not one function with a flag.

**The minimum entry tenor is not the minimum hold.** ``tenor.entry_dte_min`` is
set above ``tenor.hold_days_min`` plus the close buffer on purpose. Entering at
the minimum-hold figure lands the exit on expiry and violates the assignment
rule, so a 70-day entry is rejected even though the intended hold is 70 days.

**The sleeve budget is a shared pool.** :meth:`CandidateGates.affordability`
subtracts both the penalty balance and the premium already committed to open
option positions. Without the second subtraction ``option_sleeve.budget_pct`` is
not the cap it is specified as, because ``max_concurrent_positions`` entries
could each be sized against the whole sleeve. The caller supplies both figures
rather than this module reading them, so the gates stay a pure function of
config and their inputs.

**Every rejection names itself.** A gate returns the failures, not a boolean, so
the caller can print "rejected: open interest 120 against a floor of 500" rather
than "rejected".

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..config import Config
from ..instruments import ASSET_CLASS_FUTURE, LeveragedInverseFilter, is_futures_contract
from ..util.timeutil import utc_today

#: Gate names, so a caller can test for one without matching on prose.
GATE_INSTRUMENT = "instrument"
GATE_PRIIPS = "priips"
GATE_TENOR = "tenor"
GATE_CATALYST = "catalyst_buffer"
GATE_SPREAD = "spread"
GATE_OPEN_INTEREST = "open_interest"
GATE_VOLUME = "session_volume"
GATE_PRICE_CEILING = "price_ceiling"
GATE_POSITION_SIZE = "position_size"
GATE_IV_PERCENTILE = "iv_percentile"
GATE_PAYOFF_RATIO = "payoff_ratio"
GATE_ENTRY_RANGE = "entry_range_catalyst"
GATE_THETA_BUDGET = "theta_budget"

#: The at-the-money Black-Scholes coefficient, ``1 / sqrt(2*pi)``: an ATM option
#: is worth about ``0.3989 * S * sigma * sqrt(tau)``. A mathematical constant,
#: not a tunable — it is the same approximation the Brenner-Subrahmanyam seed in
#: quant/bsm.py inverts, used here only to filter a universe by affordability
#: before any real premium is fetched.
ATM_PRICE_COEFFICIENT = 1.0 / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class GateFailure:
    """One gate, and why it closed."""

    gate: str
    reason: str
    observed: float | None = None
    threshold: float | None = None

    def __str__(self) -> str:  # pragma: no cover - presentation
        return self.reason


@dataclass(frozen=True)
class GateResult:
    """The outcome of a set of gates."""

    failures: tuple[GateFailure, ...] = ()
    #: Gates skipped because their input was ABSENT. A skipped gate is not a
    #: passed gate, and a caller reporting "clears every gate" must say how many
    #: went untested. Deliberately distinct from :attr:`waived` — a gate waived by
    #: rule was applied and passed; a gate with no input was never applied at all.
    untested: tuple[str, ...] = ()
    #: Gates waived by an explicit rule in config, such as the spread gate on a
    #: book deeper than liquidity.spread_waiver_min_open_interest. These PASSED.
    waived: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(f.gate for f in self.failures)

    def reason(self) -> str | None:
        if not self.failures:
            return None
        return "; ".join(f.reason for f in self.failures)

    @property
    def fully_tested(self) -> bool:
        """Passed, with every gate actually applied. No gate went unmeasured."""
        return self.passed and not self.untested

    def merge(self, other: GateResult) -> GateResult:
        return GateResult(
            failures=self.failures + other.failures,
            untested=self.untested + other.untested,
            waived=self.waived + other.waived,
        )


@dataclass(frozen=True)
class AffordabilityCeiling:
    """What the sleeve can reach, before any screening happens.

    The most repeated failure mode in this account is discovering the budget
    last: three sessions produced full global analyses and then found the budget
    could not reach any top candidate. The ceiling is computed first and the
    universe is filtered by it.
    """

    available_budget: float
    max_premium_per_contract: float
    #: The absolute floor on a POSITION's premium, in base currency, from
    #: ``option_sleeve.min_premium_base_currency``. Deliberately not a share of
    #: the budget: a percentage measured against AVAILABLE budget shrinks as the
    #: pool drains, permitting ever-smaller positions, which is the opposite of
    #: what a floor is for.
    min_premium_position: float
    contract_multiplier: int
    #: Premium already tied up by open option positions, shown so the ceiling
    #: explains itself rather than only stating its result.
    committed_premium: float = 0.0
    #: The currency every figure above is denominated in: ``account.base_currency``.
    #:
    #: A premium arrives in the CONTRACT's currency, so the affordability gates
    #: convert before comparing. This field names the unit they convert INTO, and
    #: it is what makes the comparison auditable from the payload alone.
    base_currency: str = ""

    def max_underlying_price(self, *, sigma: float, tau_years: float) -> float | None:
        """Roughly the priciest underlying a single ATM contract still fits.

        From the at-the-money approximation ``C ~= 0.3989 * S * sigma *
        sqrt(tau)``, inverted. An estimate for filtering a universe, never a
        quote: the real premium comes from the chain, and a candidate that
        survives this still has to clear the actual price.
        """
        if sigma <= 0 or tau_years <= 0 or self.contract_multiplier <= 0:
            return None
        per_share = self.max_premium_per_contract / self.contract_multiplier
        denominator = ATM_PRICE_COEFFICIENT * sigma * math.sqrt(tau_years)
        if denominator <= 0:
            return None
        return per_share / denominator


class CandidateGates:
    """Every gate, bound to config once."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._leverage = LeveragedInverseFilter(config)

        liquidity = config.section("liquidity")
        self._max_spread_pct = liquidity.require_float("max_spread_pct")
        self._spread_waiver_oi = liquidity.require_int("spread_waiver_min_open_interest")
        self._min_open_interest = liquidity.require_int("min_open_interest")
        self._require_volume = liquidity.require_bool("require_session_volume")

        tenor = config.section("tenor")
        self._entry_dte_min = tenor.require_int("entry_dte_min")
        self._entry_dte_max = tenor.require_int("entry_dte_max")
        self._catalyst_buffer_days = tenor.require_int("catalyst_buffer_days")
        self._hold_days_min = tenor.require_int("hold_days_min")

        sleeve = config.section("option_sleeve")
        self._budget_pct = sleeve.require_float("budget_pct")
        self._min_premium_position = sleeve.require_float("min_premium_base_currency")
        self._min_payoff_ratio = sleeve.require_float("min_payoff_ratio")

        discipline = config.section("discipline")
        self._top_decile_needs_catalyst = discipline.require_bool(
            "entry_top_decile_requires_catalyst"
        )
        self._top_decile_pctile = discipline.require_float("entry_top_decile_pctile")

        self._base_currency = config.require_str("account.base_currency")
        self._multiplier = config.require_int("instruments.default_option_multiplier")
        self._cheap_iv_max = config.require_float("screen.cheap_iv_percentile_max")
        self._allow_futures_options = config.require_bool("instruments.allow_futures_options")

    # ── instrument ──

    def option_underlying(self, row: dict[str, Any]) -> GateResult:
        """May a listed OPTION on this instrument be traded?

        PRIIPs is not consulted: it restricts the cash instrument, not the
        option, and gating collection or screening on it would remove the very
        route the sleeve exists to use.
        """
        failures = list(_prohibited_failures(row, self._leverage, self._allow_futures_options))
        return GateResult(failures=tuple(failures))

    def equity_purchase(self, row: dict[str, Any]) -> GateResult:
        """May this instrument be BOUGHT as cash?

        Here PRIIPs binds: a US-domiciled ETF with no KID cannot be purchased by
        an EU retail client, and physical assignment of its shares is not
        permitted either.
        """
        failures = list(_prohibited_failures(row, self._leverage, self._allow_futures_options))
        if _truthy(row.get("priips_restricted")):
            failures.append(
                GateFailure(
                    GATE_PRIIPS,
                    f"{row.get('symbol')} has no PRIIPs KID, so the cash instrument cannot be "
                    "purchased from an EU retail account. Options on it remain permitted.",
                )
            )
        if not _truthy(row.get("tradable", 1)):
            failures.append(
                GateFailure(
                    GATE_INSTRUMENT,
                    f"{row.get('symbol')} is not tradable: "
                    f"{row.get('untradeable_reason') or 'no reason recorded'}",
                )
            )
        return GateResult(failures=tuple(failures))

    # ── tenor ──

    def tenor(
        self, *, dte: int, catalyst_date: date | None = None, today: date | None = None
    ) -> GateResult:
        """Is this expiry inside the entry window, and does it clear the catalyst?"""
        failures: list[GateFailure] = []
        if dte < self._entry_dte_min:
            failures.append(
                GateFailure(
                    GATE_TENOR,
                    f"{dte} DTE is below the minimum entry tenor of {self._entry_dte_min}. "
                    f"The minimum entry tenor is deliberately above the {self._hold_days_min}"
                    "-day minimum hold: entering at the hold figure lands the exit on "
                    "expiry and violates the assignment rule.",
                    observed=float(dte),
                    threshold=float(self._entry_dte_min),
                )
            )
        elif dte > self._entry_dte_max:
            failures.append(
                GateFailure(
                    GATE_TENOR,
                    f"{dte} DTE is beyond the maximum entry tenor of {self._entry_dte_max}",
                    observed=float(dte),
                    threshold=float(self._entry_dte_max),
                )
            )

        if catalyst_date is not None:
            session = today or utc_today()
            required = catalyst_date + timedelta(days=self._catalyst_buffer_days)
            expiry = session + timedelta(days=dte)
            if expiry < required:
                failures.append(
                    GateFailure(
                        GATE_CATALYST,
                        f"expiry {expiry.isoformat()} is before the catalyst on "
                        f"{catalyst_date.isoformat()} plus the {self._catalyst_buffer_days}-day "
                        "buffer. Catalysts slip and post-event repricing takes days.",
                        observed=float((expiry - session).days),
                        threshold=float((required - session).days),
                    )
                )
        return GateResult(failures=tuple(failures))

    # ── liquidity ──

    def liquidity(
        self,
        *,
        spread_pct_of_mid: float | None,
        open_interest: int | None,
        volume: int | None,
    ) -> GateResult:
        """Open interest, spread and session volume.

        A book deeper than ``liquidity.spread_waiver_min_open_interest`` waives
        the spread gate: with that much size resting, the quoted spread is not
        what a limit at mid actually pays. A NEGATIVE spread is a different
        thing from a wide one and is never waived by depth: it means the book is
        CROSSED (ask below bid), which is a broken quote rather than a tight
        market, and no amount of resting size makes a crossed book tradeable.
        """
        failures: list[GateFailure] = []
        untested: list[str] = []
        waived: list[str] = []

        if open_interest is None:
            untested.append(GATE_OPEN_INTEREST)
        elif open_interest < self._min_open_interest:
            failures.append(
                GateFailure(
                    GATE_OPEN_INTEREST,
                    f"open interest {open_interest} is below the floor of "
                    f"{self._min_open_interest}",
                    observed=float(open_interest),
                    threshold=float(self._min_open_interest),
                )
            )

        if spread_pct_of_mid is not None and spread_pct_of_mid < 0:
            # A crossed book, ahead of and unconditioned by the depth waiver
            # below: it fails the gate outright rather than reading as
            # untested or, worse, as a spread tighter than any real quote.
            failures.append(
                GateFailure(
                    GATE_SPREAD,
                    f"spread is {spread_pct_of_mid:.1%} of mid, which is negative: "
                    "the book is CROSSED (ask below bid). That is a broken quote, "
                    "not a tight one, and it is not waivable by depth.",
                    observed=spread_pct_of_mid,
                    threshold=0.0,
                )
            )
        else:
            waived_by_depth = open_interest is not None and open_interest >= self._spread_waiver_oi
            if waived_by_depth:
                # Waived by an explicit rule: with that much size resting, the
                # quoted spread is not what a limit at mid actually pays. PASS.
                waived.append(GATE_SPREAD)
            elif spread_pct_of_mid is None:
                # Unmeasurable, because a side of the book is missing. The gate
                # never ran, and a caller reporting "clears every gate" has to
                # say so.
                untested.append(GATE_SPREAD)
            elif spread_pct_of_mid > self._max_spread_pct:
                failures.append(
                    GateFailure(
                        GATE_SPREAD,
                        f"spread is {spread_pct_of_mid:.1%} of mid against a ceiling of "
                        f"{self._max_spread_pct:.1%}",
                        observed=spread_pct_of_mid,
                        threshold=self._max_spread_pct,
                    )
                )

        if self._require_volume:
            if volume is None:
                untested.append(GATE_VOLUME)
            elif volume <= 0:
                failures.append(
                    GateFailure(
                        GATE_VOLUME,
                        "no contracts traded this session",
                        observed=float(volume),
                        threshold=1.0,
                    )
                )
        return GateResult(failures=tuple(failures), untested=tuple(untested), waived=tuple(waived))

    # ── budget ──

    def affordability(
        self, *, nav: float, penalty_balance: float, committed_premium: float
    ) -> AffordabilityCeiling:
        """The sleeve ceiling, and the floor below which costs dominate a position.

        ``committed_premium`` is required rather than defaulted. A caller that
        forgot it would silently size every entry against the whole sleeve —
        which is precisely the defect this argument exists to close — so
        forgetting it is a ``TypeError``, not a wrong number.
        """
        committed = max(0.0, committed_premium)
        available = max(0.0, self._budget_pct * nav - max(0.0, penalty_balance) - committed)
        return AffordabilityCeiling(
            available_budget=available,
            max_premium_per_contract=available,
            min_premium_position=self._min_premium_position,
            contract_multiplier=self._multiplier,
            committed_premium=committed,
            base_currency=self._base_currency,
        )

    def price_ceiling(
        self,
        *,
        premium_per_share: float | None,
        ceiling: AffordabilityCeiling,
        currency: str | None = None,
        fx_to_base: float | None = None,
    ) -> GateResult:
        """Does ONE contract at this premium fit the available budget?

        A ceiling, and only a ceiling. The premium floor is a rule about the
        POSITION rather than the contract — a cheap contract is not ineligible,
        it just has to be bought in size — so it lives in :meth:`position_size`
        and is applied when a quantity exists to apply it to. Counting it here
        would report an affordable universe as empty.

        **The comparison is currency-aware, and has to be.** ``ceiling`` is in
        ``account.base_currency`` because it derives from NAV; ``premium_per_share``
        arrives in the CONTRACT's currency. Measured live before the conversion
        existed: a contract priced just under the sleeve in its own currency was
        refused, because in base currency the same premium was well over it. The
        error runs in both directions and its size is the exchange rate. See
        :meth:`_cost_in_base` for what an absent rate does, and why it does that.
        """
        if premium_per_share is None:
            return GateResult(untested=(GATE_PRICE_CEILING,))
        cost = self._cost_in_base(
            premium_per_share * ceiling.contract_multiplier,
            ceiling=ceiling,
            currency=currency,
            fx_to_base=fx_to_base,
        )
        if cost is None:
            return GateResult(untested=(GATE_PRICE_CEILING,))
        if cost > ceiling.max_premium_per_contract:
            return GateResult(
                failures=(
                    GateFailure(
                        GATE_PRICE_CEILING,
                        f"one contract costs {cost:,.2f} {ceiling.base_currency} against an "
                        f"available budget of {ceiling.max_premium_per_contract:,.2f}",
                        observed=cost,
                        threshold=ceiling.max_premium_per_contract,
                    ),
                )
            )
        return GateResult()

    def _cost_in_base(
        self,
        cost: float,
        *,
        ceiling: AffordabilityCeiling,
        currency: str | None,
        fx_to_base: float | None,
    ) -> float | None:
        """``cost``, stated in ``ceiling.base_currency``. ``None`` = do not compare.

        ``fx_to_base`` is a MULTIPLIER: base = contract-currency amount x rate.
        Callers get it from :class:`~obliskiq.compute.fx_engine.BaseCurrencyRates`,
        which reads the store and inverts the ECB convention once, so the
        direction is decided in one place rather than at each call site.

        An absent rate returns ``None`` and the gate reports UNTESTED. It
        deliberately does not fall back to parity: a missing EUR/USD would make a
        USD premium look 15% cheaper than it is and could admit a contract the
        sleeve cannot afford. An untested gate is visible and blocks nothing
        silently; an assumed rate is invisible and admits the one thing this gate
        exists to refuse.

        **An absent CURRENCY does the same.** It used to be read as "already in
        base", which is the assumption in the permissive direction wearing a
        different hat: a universe row missing its currency would have had its
        premium compared unconverted against a base-currency budget, and nothing
        would have said so. ``universe.currency`` is NOT NULL, so a row without
        one is a gap in whatever wrote it — reportable, not assumable.
        """
        if not ceiling.base_currency:
            # No base currency declared at all: there is nothing to convert INTO,
            # so the comparison is single-currency by construction.
            return cost
        if not currency:
            return None
        if currency.upper() == ceiling.base_currency.upper():
            return cost
        if fx_to_base is None or fx_to_base <= 0:
            return None
        return cost * fx_to_base

    def position_size(
        self,
        *,
        premium_per_share: float,
        contracts: int,
        ceiling: AffordabilityCeiling,
        currency: str | None = None,
        fx_to_base: float | None = None,
    ) -> GateResult:
        """Does a position of ``contracts`` sit between the floor and the ceiling?

        The floor is ``option_sleeve.min_premium_base_currency``: an absolute
        amount, below which round-trip commission alone costs more than the
        widest spread ``liquidity.max_spread_pct`` would ever accept. It refuses
        a position whose costs dominate it, and nothing else — a sleeve holding
        one position at a fiftieth of budget and another at four fifths is
        allowed by design. What bounds fragmentation is
        ``option_sleeve.max_concurrent_positions``, which carries that rule
        alone.

        Both comparisons are made in ``ceiling.base_currency``:
        ``min_premium_base_currency`` names its unit, and comparing a USD cost
        against it was the same defect :meth:`price_ceiling` carried.
        """
        cost = self._cost_in_base(
            premium_per_share * ceiling.contract_multiplier * max(contracts, 0),
            ceiling=ceiling,
            currency=currency,
            fx_to_base=fx_to_base,
        )
        if cost is None:
            return GateResult(untested=(GATE_POSITION_SIZE,))
        if cost > ceiling.max_premium_per_contract:
            return GateResult(
                failures=(
                    GateFailure(
                        GATE_POSITION_SIZE,
                        f"{contracts} contract(s) cost {cost:,.2f} against an available "
                        f"budget of {ceiling.max_premium_per_contract:,.2f}",
                        observed=cost,
                        threshold=ceiling.max_premium_per_contract,
                    ),
                )
            )
        if cost < ceiling.min_premium_position:
            return GateResult(
                failures=(
                    GateFailure(
                        GATE_POSITION_SIZE,
                        f"{contracts} contract(s) cost {cost:,.2f}, below the minimum "
                        f"position premium of {ceiling.min_premium_position:,.2f}. Below "
                        "that, round-trip commission alone costs more than the widest "
                        "spread the liquidity gate would accept: the costs dominate the "
                        "position.",
                        observed=cost,
                        threshold=ceiling.min_premium_position,
                    ),
                )
            )
        return GateResult()

    def contracts_for_budget(
        self, *, premium_per_share: float, ceiling: AffordabilityCeiling
    ) -> int:
        """How many contracts the available budget buys, floored at zero."""
        unit = premium_per_share * ceiling.contract_multiplier
        if unit <= 0:
            return 0
        return int(ceiling.max_premium_per_contract // unit)

    # ── implied volatility ──

    def iv_percentile(self, percentile: float | None) -> GateResult:
        """Is implied vol in the bottom quartile of its 52-week range?"""
        if percentile is None:
            return GateResult(untested=(GATE_IV_PERCENTILE,))
        if percentile > self._cheap_iv_max:
            return GateResult(
                failures=(
                    GateFailure(
                        GATE_IV_PERCENTILE,
                        f"IV percentile {percentile:.0%} is above the cheap-options ceiling "
                        f"of {self._cheap_iv_max:.0%}",
                        observed=percentile,
                        threshold=self._cheap_iv_max,
                    ),
                )
            )
        return GateResult()

    # ── entry discipline ──

    def payoff_ratio(
        self,
        *,
        target: float | None,
        strike: float | None,
        premium_per_share: float | None,
        is_call: bool | None,
    ) -> GateResult:
        """Does the named target actually pay ``option_sleeve.min_payoff_ratio``?

        SKILL.md has always required asymmetric payoff "at a realistic, named
        target price", and the ratio existed in config — but it was read in one
        place only, inside the zombie test's derived target, where it decides
        what N(d2) is measured AT. Nothing ever tested an entry against it. A
        position could be logged with a target that pays 1.2x and no gate
        objected, which is precisely the arrangement CLAUDE.md forbids: the
        skill standing alone between a bad trade and the ledger.

        Measured on intrinsic value at the target, matching
        ``RiskEngine.target_level``, which derives its level as
        ``strike + ratio * premium``. The two must agree — one deriving a target
        the other would reject is worse than neither existing.
        """
        if target is None or strike is None or premium_per_share is None or is_call is None:
            return GateResult(untested=(GATE_PAYOFF_RATIO,))
        if premium_per_share <= 0:
            return GateResult(untested=(GATE_PAYOFF_RATIO,))
        intrinsic = (target - strike) if is_call else (strike - target)
        ratio = intrinsic / premium_per_share
        if ratio >= self._min_payoff_ratio:
            return GateResult()
        return GateResult(
            failures=(
                GateFailure(
                    GATE_PAYOFF_RATIO,
                    f"the target {target:g} pays {ratio:.2f}x the premium at expiry, below "
                    f"the {self._min_payoff_ratio:g}x in option_sleeve.min_payoff_ratio. The "
                    "true cost of a loss here is the premium times the penalty multiple, so "
                    "a symmetric payoff is a losing proposition before it is a wrong one. "
                    "Name a target that clears the ratio, or drop the idea — do not "
                    "restate the target to make the gate pass.",
                    observed=ratio,
                    threshold=self._min_payoff_ratio,
                ),
            )
        )

    def entry_range(
        self, *, entry_range_pctile: float | None, catalyst_date: date | None
    ) -> GateResult:
        """A top-decile entry needs a reason beyond momentum.

        ``discipline.entry_top_decile_requires_catalyst`` has been in config
        since the beginning and was read by no code at all; ``log_entry``
        accepted ``entry_range_pctile`` and wrote it to the ledger unchecked. So
        the single most expensive pattern in this account — buying strength
        without a catalyst — was recorded faithfully and gated by nothing.

        The positions that pattern produced are in the ledger. What belongs
        here is the gate, not their names: a rule that reads as a lesson about
        two tickers stops applying the moment the book holds different ones.
        """
        if not self._top_decile_needs_catalyst:
            return GateResult()
        if entry_range_pctile is None:
            return GateResult(untested=(GATE_ENTRY_RANGE,))
        if entry_range_pctile < self._top_decile_pctile:
            return GateResult()
        if catalyst_date is not None:
            return GateResult()
        return GateResult(
            failures=(
                GateFailure(
                    GATE_ENTRY_RANGE,
                    f"this entry sits at {entry_range_pctile:.0%} of its long-run range, at "
                    f"or above the {self._top_decile_pctile:.0%} in "
                    "discipline.entry_top_decile_pctile, and no catalyst_date was supplied. "
                    "While discipline.entry_top_decile_requires_catalyst holds, a top-decile "
                    'entry requires an explicit reason beyond momentum. "It is going up" is '
                    "not one.",
                    observed=entry_range_pctile,
                    threshold=self._top_decile_pctile,
                ),
            )
        )


def _prohibited_failures(
    row: dict[str, Any], leverage: LeveragedInverseFilter, allow_futures_options: bool
) -> list[GateFailure]:
    """The instrument-level bans, applied before anything else is measured."""
    failures: list[GateFailure] = []
    asset_class = str(row.get("asset_class") or "").upper()
    symbol = row.get("symbol")

    if is_futures_contract(asset_class):
        if not allow_futures_options:
            failures.append(
                GateFailure(
                    GATE_INSTRUMENT,
                    f"{symbol} is a futures contract ({ASSET_CLASS_FUTURE}), a prohibited "
                    "instrument: undefined loss, and it violates the no-leverage mandate. "
                    "Its curve remains a data call for wrapper selection.",
                )
            )
        else:
            # Even with futures options enabled the CONTRACT stays prohibited.
            # Only the option on it would become available, and that is a
            # different instrument with its own wider close buffer.
            failures.append(
                GateFailure(
                    GATE_INSTRUMENT,
                    f"{symbol} is a futures contract. Enabling allow_futures_options "
                    "permits the OPTION, never the contract itself.",
                )
            )

    rejection = leverage.check_declared(
        str(symbol),
        leveraged=_truthy(row.get("leveraged")),
        inverse=_truthy(row.get("inverse")),
    ) or leverage.check_description(str(symbol), row.get("coverage_note"))
    if rejection is not None:
        failures.append(GateFailure(GATE_INSTRUMENT, f"{symbol}: {rejection.reason}"))
    return failures


def _truthy(value: Any) -> bool:
    """SQLite stores booleans as 0/1 and YAML as True/False. Both mean the same."""
    return bool(value)
