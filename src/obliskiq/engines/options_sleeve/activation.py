"""When the options sleeve trades, and the friction gate it trades within (MASTER_PLAN §5.6;
DECISIONS D66).

The sleeve trades only when its activation condition holds, computed from configuration and the
research record, never from a NAV threshold:

1. **Evidence**: its hypothesis (``option_sleeve.activation.hypothesis``, H8: an option against a
   share with a stop, for a signal the research pipeline found sufficient) is registered and its
   outcome under the decision rule is ``sufficient`` (DECISIONS D62).
2. **Budget**: the sleeve's budget buys the number of median premiums that hypothesis's
   pre-registration states. The number belongs to the pre-registration, so without one this leg
   is untested.
3. **Friction**: a median position's round trip of commission and currency conversion costs at
   most ``option_sleeve.activation.friction_max_share_of_premium`` of its premium.

Until all three hold the sleeve runs in shadow and logs what it would have entered, which is
H8's forward record. A leg that cannot be evaluated is untested, never assumed to hold.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from ...config.schema import ObliskConfig
from ...domain.enums import AssetClass
from ...domain.research import RegisteredHypothesis
from ...quant.costs import FX_VENUE, LEGS_IN_A_ROUND_TRIP, CostBook, OrderTerms
from .penalty import ZERO

#: The research outcome that activates a layer (DECISIONS D62).
SUFFICIENT = "sufficient"
EVIDENCE = "evidence"
BUDGET = "budget"
FRICTION = "friction"


@dataclass(frozen=True, slots=True)
class ActivationLeg:
    """One leg of the condition: ``holds`` is ``None`` while it is untested."""

    name: str
    holds: bool | None
    detail: str


@dataclass(frozen=True, slots=True)
class Activation:
    """The condition's verdict: the sleeve trades only when every leg holds."""

    legs: tuple[ActivationLeg, ...]

    @property
    def active(self) -> bool:
        return all(leg.holds is True for leg in self.legs)

    def reason(self) -> str:
        if self.active:
            return "active: " + "; ".join(leg.detail for leg in self.legs)
        blocking = [
            f"{leg.name} {'untested' if leg.holds is None else 'fails'}: {leg.detail}"
            for leg in self.legs
            if leg.holds is not True
        ]
        return "shadow: " + "; ".join(blocking)


@dataclass(frozen=True, slots=True)
class FrictionReading:
    """A position's round-trip friction: commission and conversion, in the base currency, and
    their share of the premium; ``share`` ``None`` with the reason when it cannot be priced."""

    premium_base: Decimal
    commission_base: Decimal | None
    conversion_base: Decimal | None
    share: Decimal | None
    reason: str


class FrictionGate:
    """Prices a position's round trip with the cost model (the same functions shadow and live
    charge), and holds it to the configured share of the premium."""

    def __init__(self, settings: ObliskConfig, costs: CostBook | None = None) -> None:
        self._costs = costs or CostBook.from_config(settings)
        self._base = settings.account.base_currency
        self._max_share = settings.option_sleeve.activation.friction_max_share_of_premium
        self._fx_schedules = [
            schedule
            for schedule in settings.costs.commissions
            if AssetClass.FX in schedule.asset_classes
        ]

    @property
    def max_share(self) -> Decimal:
        return self._max_share

    def assess(
        self,
        *,
        premium_per_unit: Decimal,
        multiplier: Decimal,
        contracts: int,
        venue: str,
        currency: str,
        rate_to_base: Callable[[str], Decimal | None],
    ) -> FrictionReading:
        """The round trip of ``contracts`` at ``premium_per_unit`` on ``venue`` in ``currency``;
        ``rate_to_base`` gives what one unit of a currency is worth in the base."""
        local = premium_per_unit * multiplier * contracts
        to_base = Decimal(1) if currency == self._base else rate_to_base(currency)
        if to_base is None:
            return FrictionReading(
                ZERO, None, None, None, f"no rate converts {currency} into {self._base}"
            )
        premium_base = local * to_base
        estimate = self._costs.commission(
            OrderTerms(
                venue=venue,
                asset_class=AssetClass.OPTION,
                currency=currency,
                quantity=Decimal(contracts),
                price=premium_per_unit,
            )
        )
        if estimate.amount is None:
            return FrictionReading(
                premium_base,
                None,
                None,
                None,
                f"the commission cannot be priced: {estimate.detail}",
            )
        commission = estimate.amount * LEGS_IN_A_ROUND_TRIP * to_base
        conversion, why = self._conversion(local, currency, rate_to_base)
        if conversion is None:
            return FrictionReading(premium_base, commission, None, None, why)
        share = (commission + conversion) / premium_base
        return FrictionReading(
            premium_base,
            commission,
            conversion,
            share,
            f"a round trip costs {commission + conversion:.2f} {self._base} ({commission:.2f} "
            f"commission, {conversion:.2f} conversion), {share:.1%} of a {premium_base:.2f} "
            f"premium against a ceiling of {self._max_share:.0%}",
        )

    def _conversion(
        self, local: Decimal, currency: str, rate_to_base: Callable[[str], Decimal | None]
    ) -> tuple[Decimal | None, str]:
        """Converting the premium in and the proceeds out, in the base currency: nothing when
        the contract is in the base currency."""
        if currency == self._base:
            return ZERO, "no conversion"
        if not self._fx_schedules:
            return None, "no commission schedule prices a currency conversion"
        schedule = self._fx_schedules[0]
        quoted = schedule.currency
        quoted_rate = Decimal(1) if quoted == self._base else rate_to_base(quoted)
        local_rate = rate_to_base(currency)
        if quoted_rate is None or local_rate is None:
            return None, f"no rate values the conversion in {quoted} to price it"
        amount = local * local_rate / quoted_rate
        estimate = self._costs.commission(
            OrderTerms(
                venue=FX_VENUE,
                asset_class=AssetClass.FX,
                currency=quoted,
                quantity=amount,
                price=Decimal(1),
            )
        )
        if estimate.amount is None:
            return None, f"the conversion cannot be priced: {estimate.detail}"
        return estimate.amount * LEGS_IN_A_ROUND_TRIP * quoted_rate, "converted in and out"

    def holds(self, reading: FrictionReading) -> bool | None:
        return None if reading.share is None else reading.share <= self._max_share


class SleeveActivation:
    """The sleeve's activation condition, from configuration and the research record."""

    def __init__(self, settings: ObliskConfig) -> None:
        self._hypothesis = settings.option_sleeve.activation.hypothesis

    @property
    def hypothesis(self) -> str:
        return self._hypothesis

    def evaluate(
        self,
        *,
        evidence: RegisteredHypothesis | None,
        budget: Decimal | None,
        budget_gap: str | None,
        median_premium: Decimal | None,
        median_gap: str | None,
        friction: FrictionReading | None,
        friction_holds: bool | None,
    ) -> Activation:
        """The three legs. ``median_premium`` is one median would-be entry's premium in the base
        currency; ``friction`` that position's round trip."""
        return Activation(
            (
                self._evidence(evidence),
                self._budget(evidence, budget, budget_gap, median_premium, median_gap),
                self._friction(friction, friction_holds, median_gap),
            )
        )

    def _evidence(self, evidence: RegisteredHypothesis | None) -> ActivationLeg:
        if evidence is None:
            return ActivationLeg(
                EVIDENCE,
                False,
                f"{self._hypothesis} is not in the research registry: it has no pre-registration "
                "and no outcome",
            )
        if evidence.decision != SUFFICIENT:
            outcome = evidence.decision or "no outcome yet"
            return ActivationLeg(
                EVIDENCE,
                False,
                f"{self._hypothesis} is {evidence.status} ({outcome}); the sleeve trades on a "
                "sufficient outcome only",
            )
        return ActivationLeg(
            EVIDENCE, True, f"{self._hypothesis} is sufficient ({evidence.source})"
        )

    def _budget(
        self,
        evidence: RegisteredHypothesis | None,
        budget: Decimal | None,
        budget_gap: str | None,
        median: Decimal | None,
        median_gap: str | None,
    ) -> ActivationLeg:
        needed = None if evidence is None else evidence.min_median_premiums
        if needed is None:
            return ActivationLeg(
                BUDGET,
                None,
                f"{self._hypothesis}'s pre-registration states no number of median premiums the "
                "budget must buy",
            )
        if budget is None:
            return ActivationLeg(BUDGET, None, budget_gap or "the sleeve's budget is unknown")
        if median is None:
            return ActivationLeg(BUDGET, None, median_gap or "no median premium is measured")
        bought = budget / median
        return ActivationLeg(
            BUDGET,
            bought >= needed,
            f"a budget of {budget:.2f} buys {bought:.1f} median premiums of {median:.2f}, "
            f"{needed} needed",
        )

    @staticmethod
    def _friction(
        reading: FrictionReading | None, holds: bool | None, median_gap: str | None
    ) -> ActivationLeg:
        if reading is None:
            return ActivationLeg(FRICTION, None, median_gap or "no median position is priced")
        return ActivationLeg(FRICTION, holds, reading.reason)
