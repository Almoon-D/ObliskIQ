"""What a trade costs: commission, transaction tax, spread, slippage and impact.

One cost model serves position sizing, shadow mode and backtests, so paper and live charge the
same costs because they call the same functions. Everything is configuration under ``costs``
(``config/defaults/costs.yaml``); this module holds the mechanics.

Money is ``Decimal`` throughout, and rates are decimals (0.001 is 0.1 %).

**An unknown cost is a status, not a number.** A venue, asset class or currency with no
commission schedule is ``NO_SCHEDULE``; an order past the tier a schedule covers is
``BEYOND_TIER``; a transaction tax whose rate could not be stated is ``TAX_UNKNOWN``; an asset
class with no spread model is ``NO_FRICTION_MODEL``. A leg carrying any of them has no total
(``None``): a cost that cannot be priced must not be priced at zero. What the model does carry is
labelled when it rests on something unverified (a schedule awaiting a live reading, a friction
tier that is an assumption, a tax assumed to apply because the issuer list was not given).

Derived from the transaction-cost model of Almoon-D/Inversor-Inteligente
(``modules/analytics/transaction_costs.py`` @68280e1, Apache License 2.0,
https://www.apache.org/licenses/LICENSE-2.0), modified and used here under AGPL-3.0-or-later as
the Apache License permits.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from ..config import ObliskConfig
from ..config.schema_costs import CommissionSchedule, TaxRule
from ..domain.enums import AssetClass

ZERO = Decimal(0)

#: Asset classes that trade like shares, and so take the equity friction tiers.
_EQUITY_LIKE = frozenset({AssetClass.STOCK, AssetClass.ETF, AssetClass.ETC})

#: A round trip pays every cost twice: once to enter, once to leave.
LEGS_IN_A_ROUND_TRIP = 2

type Side = Literal["buy", "sell"]


class CostGap(StrEnum):
    """Why a cost cannot be priced."""

    NO_SCHEDULE = "no_schedule"
    BEYOND_TIER = "beyond_tier"
    TAX_UNKNOWN = "tax_unknown"
    NO_FRICTION_MODEL = "no_friction_model"


@dataclass(frozen=True, slots=True)
class OrderTerms:
    """What is being traded, where, and how much of it.

    ``quantity`` is shares, contracts, or the face amount for a bond. ``price`` is the price per
    share or the premium per unit; it is needed to value a share or option order and to apply a
    per-share commission cap, and is unused for a bond priced on face.
    """

    venue: str
    asset_class: AssetClass
    currency: str
    quantity: Decimal
    price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity <= ZERO:
            raise ValueError(f"an order's quantity must be positive, got {self.quantity}")
        if self.price is not None and (not self.price.is_finite() or self.price <= ZERO):
            raise ValueError(f"an order's price must be positive when given, got {self.price}")

    def value(self, multiplier: Decimal = Decimal(1)) -> Decimal | None:
        """Notional value of the order, or ``None`` when no price was given."""
        return None if self.price is None else self.quantity * self.price * multiplier


@dataclass(frozen=True, slots=True)
class CommissionEstimate:
    amount: Decimal | None
    gap: CostGap | None
    schedule_id: str | None
    unverified: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TaxEstimate:
    """A transaction tax on one leg.

    ``applies`` is ``False`` when no configured rule covers the issuer and side (the tax is then
    not modelled, which is not the same as the tax being zero). ``scope_assumed`` marks an
    estimate that assumed an issuer was on a published list because the caller did not say.
    """

    amount: Decimal
    gap: CostGap | None
    rule_id: str | None
    applies: bool
    unverified: bool
    scope_assumed: bool
    detail: str
    #: What was read when the rate was entered ("official", "secondary" or "none"), and the rule's
    #: own caveat; empty when no rule applied.
    confirmation: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class LegCost:
    """The cost of one leg, with every component and every reason a total could not be given."""

    spread_cost: Decimal | None
    slippage_cost: Decimal | None
    commission: CommissionEstimate
    tax: TaxEstimate
    gaps: tuple[CostGap, ...]
    liquidity_tier: str
    notional: Decimal | None
    #: What the figures rest on without a live reading, one label each.
    unverified: tuple[str, ...]

    @property
    def total(self) -> Decimal | None:
        """The sum of every component, or ``None`` while any of them cannot be priced."""
        commission = self.commission.amount
        if (
            self.gaps
            or self.spread_cost is None
            or self.slippage_cost is None
            or commission is None
        ):
            return None
        return self.spread_cost + self.slippage_cost + commission + self.tax.amount

    @property
    def total_fraction(self) -> Decimal | None:
        """The total as a fraction of the notional (0.002 is 0.2 %)."""
        total = self.total
        if total is None or self.notional is None:
            return None
        return total / self.notional


@dataclass(frozen=True, slots=True)
class FrictionInputs:
    """The friction assumptions, read from ``costs.friction``."""

    liquid_market_cap_usd: Decimal
    liquid_spread_fraction: Decimal
    illiquid_spread_fraction: Decimal
    liquid_slippage_fraction: Decimal
    illiquid_slippage_fraction: Decimal
    impact_coefficient: Decimal
    impact_cap_fraction: Decimal
    liquid_half_spread_fraction: Decimal
    single_name_half_spread_fraction: Decimal
    liquid_underlyings: frozenset[str]
    unverified: bool
    origin: str

    @classmethod
    def from_config(cls, settings: ObliskConfig) -> FrictionInputs:
        friction = settings.costs.friction
        equity = friction.equity
        option = friction.option
        return cls(
            liquid_market_cap_usd=equity.liquid_market_cap_usd,
            liquid_spread_fraction=equity.liquid_spread_fraction,
            illiquid_spread_fraction=equity.illiquid_spread_fraction,
            liquid_slippage_fraction=equity.liquid_slippage_fraction,
            illiquid_slippage_fraction=equity.illiquid_slippage_fraction,
            impact_coefficient=equity.impact_coefficient,
            impact_cap_fraction=equity.impact_cap_fraction,
            liquid_half_spread_fraction=option.liquid_half_spread_fraction,
            single_name_half_spread_fraction=option.single_name_half_spread_fraction,
            liquid_underlyings=frozenset(name.upper() for name in option.liquid_underlyings),
            unverified=friction.source.verify_live,
            origin=friction.source.origin,
        )


def _any_venue(schedule: CommissionSchedule, venue: str) -> bool:
    return schedule.venues is None or venue in schedule.venues


class CostBook:
    """The configured schedules, taxes and friction tiers, and the mechanics that apply them."""

    def __init__(
        self,
        commissions: Iterable[CommissionSchedule],
        taxes: Iterable[TaxRule],
        friction: FrictionInputs,
    ) -> None:
        self._commissions = tuple(commissions)
        self._taxes = tuple(taxes)
        self._friction = friction

    @classmethod
    def from_config(cls, settings: ObliskConfig) -> CostBook:
        costs = settings.costs
        return cls(
            commissions=costs.commissions,
            taxes=costs.taxes,
            friction=FrictionInputs.from_config(settings),
        )

    # ── commission ──

    def commission(self, terms: OrderTerms) -> CommissionEstimate:
        """The commission on an order from the schedule configured for it.

        A schedule is chosen by venue, asset class and currency. None configured: ``NO_SCHEDULE``.
        Two configured for the same key is a configuration error, not a choice to make silently.
        """
        matches = [
            schedule
            for schedule in self._commissions
            if _any_venue(schedule, terms.venue)
            and terms.asset_class in schedule.asset_classes
            and schedule.currency == terms.currency
        ]
        if not matches:
            return CommissionEstimate(
                None,
                CostGap.NO_SCHEDULE,
                None,
                False,
                f"no commission schedule for {terms.asset_class.value} on {terms.venue} "
                f"in {terms.currency}",
            )
        if len(matches) > 1:
            names = ", ".join(sorted(m.id for m in matches))
            raise ValueError(
                f"several commission schedules match {terms.asset_class.value} on {terms.venue} "
                f"in {terms.currency}: {names}"
            )
        schedule = matches[0]
        return self._apply_schedule(schedule, terms)

    @staticmethod
    def _apply_schedule(schedule: CommissionSchedule, terms: OrderTerms) -> CommissionEstimate:
        unverified = schedule.source.verify_live
        if (
            schedule.kind == "fraction_of_face"
            and schedule.up_to_face is not None
            and terms.quantity > schedule.up_to_face
        ):
            return CommissionEstimate(
                None,
                CostGap.BEYOND_TIER,
                schedule.id,
                unverified,
                f"face {terms.quantity} is above the {schedule.up_to_face} this schedule covers",
            )
        amount = terms.quantity * schedule.rate
        if schedule.minimum is not None:
            amount = max(amount, schedule.minimum)
        if schedule.maximum_fraction_of_value is not None:
            value = terms.value()
            if value is None:
                raise ValueError("a per-share schedule with a cap needs the order's price")
            # The cap is the last word: a minimum above it would charge more than the schedule's
            # own ceiling on the value traded.
            amount = min(amount, value * schedule.maximum_fraction_of_value)
        return CommissionEstimate(amount, None, schedule.id, unverified, schedule.scope_note)

    # ── transaction tax ──

    def transaction_tax(
        self,
        *,
        issuer_country: str | None,
        asset_class: AssetClass,
        side: Side,
        notional: Decimal | None,
        on: date,
        taxable_issuer: bool | None = None,
    ) -> TaxEstimate:
        """The transaction tax on one leg, as a conservative upper bound.

        Rules are chosen by the issuer's country, the asset class, the side and the date. When
        several apply the highest rate stands. A rule that exists but has no rate makes the
        estimate ``TAX_UNKNOWN``; an issuer on no rule's country is not modelled (``applies``
        false), which is not the same as untaxed.

        A tax that applies only to issuers on a published list (``needs_issuer_list``) is skipped
        when the caller says the issuer is not on it and assumed to apply when the caller does not
        say, which errs towards the higher cost and is labelled ``scope_assumed``.
        """
        if issuer_country is None:
            return TaxEstimate(ZERO, None, None, False, False, False, "issuer country not given")
        rules = [
            rule
            for rule in self._taxes
            if rule.issuer_country == issuer_country
            and asset_class in rule.asset_classes
            and side in rule.sides
            and (rule.effective_from is None or rule.effective_from <= on)
            and not (rule.needs_issuer_list and taxable_issuer is False)
        ]
        if not rules:
            return TaxEstimate(
                ZERO, None, None, False, False, False, f"no tax rule for {issuer_country}"
            )
        unknown = [rule for rule in rules if rule.rate is None]
        if unknown:
            first = unknown[0]
            return TaxEstimate(
                ZERO,
                CostGap.TAX_UNKNOWN,
                first.id,
                True,
                True,
                first.needs_issuer_list and taxable_issuer is None,
                f"{first.id}: the rate could not be stated ({first.provision})",
                first.confirmation,
                first.note,
            )
        chosen = max(rules, key=lambda rule: rule.rate or ZERO)
        rate = chosen.rate or ZERO
        scope_assumed = chosen.needs_issuer_list and taxable_issuer is None
        amount = ZERO if notional is None else notional * rate
        return TaxEstimate(
            amount,
            None,
            chosen.id,
            True,
            chosen.source.verify_live,
            scope_assumed,
            f"{chosen.id} at {rate}",
            chosen.confirmation,
            chosen.note,
        )

    # ── friction ──

    def is_liquid_share(self, market_cap_usd: Decimal | None) -> bool:
        """A share is liquid when its market capitalisation reaches the configured threshold."""
        return market_cap_usd is not None and market_cap_usd >= self._friction.liquid_market_cap_usd

    def market_impact_fraction(
        self, order_value: Decimal, average_daily_value: Decimal | None
    ) -> Decimal:
        """Square-root market impact: ``coefficient * sqrt(order / average daily value)``, capped.

        Zero when the average daily value is unknown or not positive (the caller then has only
        the flat slippage tier), and for an order with no value.
        """
        if average_daily_value is None or average_daily_value <= 0 or order_value <= 0:
            return ZERO
        participation = order_value / average_daily_value
        impact = self._friction.impact_coefficient * participation.sqrt()
        return min(impact, self._friction.impact_cap_fraction)

    def equity_slippage_fraction(
        self,
        order_value: Decimal,
        average_daily_value: Decimal | None = None,
        *,
        liquid: bool = True,
    ) -> Decimal:
        """Flat slippage by liquidity tier plus square-root impact, as a fraction of notional."""
        tiers = self._friction
        base = tiers.liquid_slippage_fraction if liquid else tiers.illiquid_slippage_fraction
        return base + self.market_impact_fraction(order_value, average_daily_value)

    def is_liquid_option_underlying(self, symbol: str | None) -> bool:
        """Whether an underlying's listed options are in the deep-market tier."""
        return symbol is not None and symbol.upper() in self._friction.liquid_underlyings

    def option_half_spread_fraction(
        self, symbol: str | None = None, *, liquid_underlying: bool | None = None
    ) -> Decimal:
        """Per-leg option half-spread as a fraction of value (a round trip is twice this).

        An explicit ``liquid_underlying`` wins; otherwise the symbol decides; a symbol that is not
        known to be deep is treated as a single name, the conservative tier.
        """
        liquid = (
            liquid_underlying
            if liquid_underlying is not None
            else self.is_liquid_option_underlying(symbol)
        )
        tiers = self._friction
        return (
            tiers.liquid_half_spread_fraction if liquid else tiers.single_name_half_spread_fraction
        )

    def origins(self, cost: LegCost) -> dict[str, str]:
        """Where each unverified figure on a leg came from, keyed by its label.

        Labels without a configured source (``market_cap_unknown``, ``tax_not_modelled``) say what
        is missing instead.
        """
        origins: dict[str, str] = {}
        for label in cost.unverified:
            kind, _, name = label.partition(":")
            if kind == "friction_assumption":
                origins[label] = self._friction.origin
            elif kind == "commission":
                origins[label] = next(c.source.origin for c in self._commissions if c.id == name)
            elif kind in ("tax", "tax_scope_assumed"):
                rule = next(t for t in self._taxes if t.id == name)
                origins[label] = f"{rule.source.origin} ({rule.confirmation}: {rule.note})"
            elif kind == "market_cap_unknown":
                origins[label] = "no market capitalisation was given, so the illiquid tier applies"
            else:
                origins[label] = "no transaction tax is configured for this issuer country"
        return origins

    # ── one leg ──

    def leg_cost(
        self,
        terms: OrderTerms,
        *,
        side: Side,
        on: date,
        issuer_country: str | None = None,
        taxable_issuer: bool | None = None,
        market_cap_usd: Decimal | None = None,
        average_daily_value: Decimal | None = None,
        underlying_symbol: str | None = None,
        liquid_underlying: bool | None = None,
        multiplier: Decimal | None = None,
    ) -> LegCost:
        """The cost of one leg: spread, slippage, commission and transaction tax.

        Equity-like instruments take the equity tiers (an unknown market capitalisation is
        illiquid, the conservative tier); options take the half-spread tier; any other asset class
        has no spread model and is ``NO_FRICTION_MODEL``. ``multiplier`` is the contract
        multiplier for an option's notional and is required for an option: a premium per unit
        without it would be costed as one unit, a hundredth of the money at risk.
        """
        if terms.asset_class is AssetClass.OPTION and (multiplier is None or multiplier <= ZERO):
            raise ValueError("an option leg needs its contract multiplier to be costed")
        value = terms.value(Decimal(1) if multiplier is None else multiplier)
        if value is None:
            raise ValueError("a leg needs the order's price to be costed")
        commission = self.commission(terms)
        tax = self.transaction_tax(
            issuer_country=issuer_country,
            asset_class=terms.asset_class,
            side=side,
            notional=value,
            on=on,
            taxable_issuer=taxable_issuer,
        )
        gaps = [gap for gap in (commission.gap, tax.gap) if gap is not None]
        unverified: list[str] = []
        spread: Decimal | None
        slippage: Decimal | None
        if terms.asset_class in _EQUITY_LIKE:
            liquid = self.is_liquid_share(market_cap_usd)
            tier = "liquid" if liquid else "illiquid"
            if market_cap_usd is None:
                tier = "illiquid_assumed"
                unverified.append("market_cap_unknown")
            tiers = self._friction
            spread = value * (
                tiers.liquid_spread_fraction if liquid else tiers.illiquid_spread_fraction
            )
            slippage = value * self.equity_slippage_fraction(
                value, average_daily_value, liquid=liquid
            )
        elif terms.asset_class is AssetClass.OPTION:
            liquid_option = (
                liquid_underlying
                if liquid_underlying is not None
                else self.is_liquid_option_underlying(underlying_symbol)
            )
            tier = "liquid_option" if liquid_option else "single_name_option"
            # The half-spread is the dominant execution cost of an option and is booked as the
            # spread; there is no separate equity-style slippage term.
            spread = value * self.option_half_spread_fraction(
                underlying_symbol, liquid_underlying=liquid_underlying
            )
            slippage = ZERO
        else:
            tier = "not_modelled"
            spread = slippage = None
            gaps.append(CostGap.NO_FRICTION_MODEL)
        if self._friction.unverified and spread is not None:
            unverified.append("friction_assumption")
        if commission.unverified:
            unverified.append(f"commission:{commission.schedule_id}")
        if tax.unverified:
            unverified.append(f"tax:{tax.rule_id}")
        if tax.scope_assumed:
            unverified.append(f"tax_scope_assumed:{tax.rule_id}")
        if not tax.applies and tax.gap is None:
            unverified.append("tax_not_modelled")
        return LegCost(
            spread_cost=spread,
            slippage_cost=slippage,
            commission=commission,
            tax=tax,
            gaps=tuple(gaps),
            liquidity_tier=tier,
            notional=value,
            unverified=tuple(unverified),
        )


def adjust_position_for_costs(target_value: Decimal, cost: LegCost) -> Decimal | None:
    """The position that remains after the leg's costs come out of the target value.

    ``None`` when the leg could not be priced: a size cannot be adjusted by a cost nobody knows.
    """
    total = cost.total
    if total is None:
        return None
    return max(ZERO, target_value - total)


def breakeven_move(entry_price: Decimal, cost: LegCost) -> tuple[Decimal, Decimal] | None:
    """The price and the move (as a fraction) needed to recover a round trip of this leg's costs.

    Costs are paid on entry and again on exit, so the drag is twice the single-leg fraction.
    ``None`` when the leg could not be priced or has no notional.
    """
    fraction = cost.total_fraction
    if fraction is None:
        return None
    drag = fraction * LEGS_IN_A_ROUND_TRIP
    return entry_price * (1 + drag), drag
