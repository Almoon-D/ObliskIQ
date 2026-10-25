"""The core engine: the variant the owner chose, as a target portfolio (MASTER_PLAN §5.4,
DECISIONS D91, D140).

:class:`CoreEngine` takes the vehicles that fill the roles (the vehicle selector's choice), the
account's book and the month-end closes the tilt reads, and answers with a
:class:`~obliskiq.domain.portfolio.TargetPortfolio`: each role's weight and why, each vehicle's
share of the core's investable value, and whether a role left its outer band, so that every vehicle
is traded to its target. It runs :class:`~obliskiq.engines.core.mechanism.CoreStrategy`, the class
the research harness simulated; it estimates no covariance and scales no total risk (D95).

Honest degradation: a role no vehicle fills is held in the cash role, and says so; a target that
needs what is missing (the cash role's vehicle, the closes a tilt reads) is not evaluable and
trades nothing. Nothing is guessed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...config.schema import ObliskConfig
from ...domain.portfolio import (
    RoleStatus,
    RoleTarget,
    TargetPortfolio,
    TargetStatus,
    TrendReading,
    VehicleTarget,
)
from ...errors import TrendUnavailableError
from ...quant.trend import TrendRule, TrendState
from .mechanism import (
    Bands,
    BuyAndHold,
    CoreBook,
    CorePlan,
    CoreStrategy,
    RoleVehicles,
    TaxAware,
)
from .trades import Deltas, plan_deltas
from .variants import CoreVariants, Variant
from .view import CoreView, MonthEnds

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class _Formed:
    """A variant's plan and the weights by role it was formed from."""

    plan: CorePlan
    weights: Mapping[str, Decimal]


class CoreEngine:
    """Forms the core's target portfolio for any registered variant."""

    def __init__(self, settings: ObliskConfig) -> None:
        config = settings.core
        self.variants = CoreVariants(settings)
        self._cash = config.cash_role
        self._bands = Bands(config.bands.absolute, config.bands.relative)
        self._tilt = TrendRule(
            momentum_months=config.tilt.momentum_months,
            average_months=config.tilt.average_months,
            hysteresis=float(config.tilt.hysteresis),
            floor=float(config.tilt.floor),
        )
        self._aware = TaxAware(config.tax_aware.benefit_fraction)

    @property
    def tilt(self) -> TrendRule:
        return self._tilt

    def portfolio(
        self,
        *,
        vehicles: Mapping[str, RoleVehicles],
        book: CoreBook,
        view: CoreView | None,
        as_of: date,
        variant: str | None = None,
    ) -> TargetPortfolio:
        """The target of ``variant`` (default: ``core.variant``) on ``as_of``.

        ``vehicles`` are the roles a vehicle fills, by the keys the book names them; ``view`` is
        the month-end closes of the tilt (``None``: none were read, which a tilting variant cannot
        do without)."""
        chosen = self.variants.get(variant if variant is not None else self.variants.chosen)
        trend_as_of = view.as_of if chosen.tilts and view is not None else None

        def refuse(*reasons: str) -> TargetPortfolio:
            return TargetPortfolio(
                variant=chosen.name,
                as_of=as_of,
                trend_as_of=trend_as_of,
                status=TargetStatus.NOT_EVALUABLE,
                rebalance=False,
                roles=(),
                vehicles=(),
                reasons=reasons,
            )

        unfilled = sorted(
            role for role, w in chosen.weights.items() if w > ZERO and role not in vehicles
        )
        if (unfilled or chosen.tilts) and self._cash not in vehicles:
            return refuse(
                f"the cash role {self._cash} has no vehicle"
                + (f", to hold the weight of {', '.join(unfilled)}" if unfilled else "")
            )
        if chosen.tilts and view is None:
            return refuse(f"{chosen.name} reads a trend and no month-end closes were read")
        weights = {role: w for role, w in chosen.weights.items() if role in vehicles}
        if unfilled:
            weights[self._cash] = weights.get(self._cash, ZERO) + sum(
                (chosen.weights[role] for role in unfilled), ZERO
            )
        try:
            formed = self._form(chosen, weights, vehicles, book, view, as_of)
        except TrendUnavailableError as exc:
            gaps = view.gaps.values() if isinstance(view, MonthEnds) else ()
            return refuse(str(exc), *gaps)
        return self._assemble(chosen, formed, vehicles, unfilled, book, as_of, trend_as_of)

    def deltas(
        self, portfolio: TargetPortfolio, book: CoreBook, *, band: Decimal, whole_units: bool = True
    ) -> Deltas:
        """The units to sell and to buy to bring ``book`` to ``portfolio``'s vehicle weights, by
        the delta execution the simulator uses (``trades.plan_deltas``); a held vehicle the target
        does not name has a weight of zero."""
        weights = {v.key: v.weight for v in portfolio.vehicles}
        prices = {key: book.price(key) for key in set(weights) | set(book.units)}
        return plan_deltas(
            weights, book.units, prices, book.investable, band=band, whole_units=whole_units
        )

    # ── the plan ──

    def _form(
        self,
        chosen: Variant,
        weights: Mapping[str, Decimal],
        vehicles: Mapping[str, RoleVehicles],
        book: CoreBook,
        view: CoreView | None,
        as_of: date,
    ) -> _Formed:
        if chosen.holds:
            held = {vehicles[role].primary: w for role, w in weights.items() if w > ZERO}
            wanted = BuyAndHold(chosen.name, held).targets(MonthEnds(as_of, {}), book)
            plan = CorePlan(
                targets=weights,
                scales=dict.fromkeys(weights, ONE),
                states={},
                held=(),
                weights=held,
                rebalance=wanted is not None,
            )
            return _Formed(plan, weights)
        aware = chosen.tax_aware
        strategy = CoreStrategy(
            name=chosen.name,
            weights=weights,
            vehicles={
                role: RoleVehicles(v.primary, v.partner if aware else None)
                for role, v in vehicles.items()
            },
            bands=self._bands,
            cash_role=self._cash if self._cash in vehicles else None,
            tilt=self._tilt if chosen.tilts else None,
            tax_aware=self._aware if aware else None,
        )
        return _Formed(strategy.plan(view or MonthEnds(as_of, {}), book), weights)

    def _assemble(
        self,
        chosen: Variant,
        formed: _Formed,
        vehicles: Mapping[str, RoleVehicles],
        unfilled: list[str],
        book: CoreBook,
        as_of: date,
        trend_as_of: date | None,
    ) -> TargetPortfolio:
        plan = formed.plan
        owners = {key: role for role, v in vehicles.items() for key in v.keys}
        stray = sorted(
            key for key, units in book.units.items() if units > ZERO and key not in owners
        )
        reasons = [
            f"{key} is held and no role names it: it is sold at the next rebalance" for key in stray
        ]
        reasons += [
            f"{role} has no vehicle: its weight is held in the cash role {self._cash}"
            for role in unfilled
        ]
        roles = [
            self._role_target(chosen, role, plan, vehicles, filled=role not in unfilled)
            for role in dict.fromkeys([*chosen.weights, *plan.targets])
        ]
        order = {role.role: place for place, role in enumerate(roles)}
        targets = tuple(
            VehicleTarget(key=key, role=owners[key], weight=weight)
            for key, weight in sorted(
                plan.weights.items(),
                key=lambda item: (order.get(owners[item[0]], len(order)), item[0]),
            )
            if weight > ZERO
        )
        return TargetPortfolio(
            variant=chosen.name,
            as_of=as_of,
            trend_as_of=trend_as_of,
            status=TargetStatus.READY,
            rebalance=plan.rebalance or bool(stray),
            roles=tuple(roles),
            vehicles=targets,
            reasons=tuple(reasons),
        )

    def _role_target(
        self,
        chosen: Variant,
        role: str,
        plan: CorePlan,
        vehicles: Mapping[str, RoleVehicles],
        *,
        filled: bool,
    ) -> RoleTarget:
        scale = plan.scales.get(role, ONE)
        held = role in plan.held
        state = plan.states.get(role)
        fitted = vehicles.get(role)
        if not filled:
            why = f"no vehicle fills it: its weight is held in the cash role {self._cash}"
        elif held:
            why = (
                f"trend scale {scale}: the reduction is held, its tax exceeds "
                f"{self._aware.benefit_fraction} of the value sold"
            )
        elif state is not None:
            why = f"trend scale {scale}: {_signals(state)}"
        else:
            why = "the variant's static weight"
        return RoleTarget(
            role=role,
            status=RoleStatus.FILLED if filled else RoleStatus.UNFILLED,
            static_weight=chosen.weights.get(role, ZERO),
            scale=scale,
            weight=ZERO if not filled else plan.targets.get(role, ZERO),
            vehicle=None if fitted is None else fitted.primary,
            partner=None if fitted is None else fitted.partner,
            trend=None if state is None else _reading(state),
            reduction_held=held,
            reason=why,
        )


def _signals(state: TrendState) -> str:
    momentum = "positive" if state.momentum_positive else "negative"
    average = "positive" if state.average_positive else "negative"
    return f"momentum {momentum}, moving average {average}"


def _reading(state: TrendState) -> TrendReading:
    return TrendReading(
        momentum_positive=state.momentum_positive,
        average_positive=state.average_positive,
        momentum=state.momentum,
        average_gap=state.average_gap,
    )
