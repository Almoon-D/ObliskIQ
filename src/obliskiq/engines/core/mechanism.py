"""The core's one mechanism (MASTER_PLAN §5.4, DECISIONS D80): static role weights held within wide
bands, optionally tilted by trend and executed tax-aware.

The research harness simulated this class and the account runs it, so the core that was tested and
the core that trades cannot drift apart (DECISIONS D61). Both give it a *book* and a *view*:
what it may know of the account and of the market on its date.

- **Static weights by role.** Risk-balanced, banded or an equity overlay (``weights.py`` and
  ``variants.py``); a role without a weight is not held.
- **Wide bands.** Nothing is sold while every role sits inside its outer band around its target,
  the smaller of an absolute width and a fraction of the target. When one leaves it, or a role
  without a target is held, every role is traded back to its target.
- **The trend tilt.** Each risky role's target is scaled by its trend (``quant.trend``): 12-month
  momentum over cash and a 10-month moving average, hysteresis, a floor above zero. The weight
  freed goes to the cash role.
- **Tax-aware execution.** A trend reduction is made only while the tax it would cost (its sales
  together, so a loss on one vehicle offsets a gain on another) is at most a pre-registered
  fraction of the value sold, else the role keeps its weight. Losses are taken freely. A purchase
  that would defer a loss under the wash-sale rule goes to the role's swap partner. A sale comes
  first from the vehicle with the smaller gain for its value.

A role is held through its primary vehicle and, with tax-aware execution, its swap partner.
:meth:`CoreStrategy.plan` answers what the account should hold and whether it should trade to get
there; :meth:`CoreStrategy.targets` is the simulator's reading of it: the weights to trade to, or
``None`` to keep what is held.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from ...errors import TrendUnavailableError
from ...quant.trend import TrendRule, TrendState
from ...util.decimals import share
from .view import CoreView

type Weights = Mapping[str, Decimal]

ZERO = Decimal(0)
ONE = Decimal(1)


class CoreBook(Protocol):
    """What a core strategy may know of the account: units, base-currency values, the value it may
    invest (after the tax now owed), and the lot engine's answers about a trade."""

    @property
    def units(self) -> Mapping[str, Decimal]: ...

    @property
    def investable(self) -> Decimal: ...

    def price(self, key: str) -> Decimal:
        """The base-currency price of one unit of ``key``."""
        ...

    def value(self, key: str) -> Decimal: ...

    def weight(self, key: str) -> Decimal:
        """``key``'s share of the investable value."""
        ...

    def basis(self, key: str) -> Decimal:
        """What a sale of every unit counts against: the lots' cost and the losses they carry."""
        ...

    def units_for(self, key: str, value: Decimal) -> Decimal:
        """The whole units of ``key`` that ``value`` buys or sells, rounded down."""
        ...

    def sales_tax(self, sales: Sequence[tuple[str, Decimal]]) -> Decimal:
        """The tax that selling each ``(key, units)`` now, together, would add to the year
        (negative: saved)."""
        ...

    def purchase_defers_loss(self, key: str) -> bool:
        """Whether buying ``key`` now would defer a loss under the wash-sale rule."""
        ...


@dataclass(frozen=True, slots=True)
class BuyAndHold:
    """Its weights on the first date, then nothing: its tax waits for the horizon, and in the
    account what is held is never sold for it (cash that arrives is bought into its weights)."""

    name: str
    weights: Weights

    def targets(self, view: CoreView, book: CoreBook) -> Weights | None:
        del view
        return None if book.units else self.weights


@dataclass(frozen=True, slots=True)
class RoleVehicles:
    """The instruments that hold a role: its primary vehicle and, if any, its swap partner."""

    primary: str
    partner: str | None

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.primary,) if self.partner is None else (self.primary, self.partner)


@dataclass(frozen=True, slots=True)
class Bands:
    """A role's outer band: the smaller of ``absolute`` weight and ``relative`` of its target."""

    absolute: Decimal
    relative: Decimal

    def width(self, target: Decimal) -> Decimal:
        return min(self.absolute, self.relative * target)


@dataclass(frozen=True, slots=True)
class TaxAware:
    """A reduction is made only while its tax is at most ``benefit_fraction`` of the value sold
    (``None``: reductions are always made); purchases go to a swap partner when the primary
    would defer a loss."""

    benefit_fraction: Decimal | None


@dataclass(frozen=True, slots=True)
class CorePlan:
    """What the strategy would hold on a date and whether it should trade to get there.

    ``targets`` are the role weights after the tilt (and after a held reduction), ``scales`` each
    role's trend scale (1 untilted), ``states`` the trend of each tilted role, ``held`` the roles
    whose reduction was kept for its tax, and ``weights`` the vehicle weights of the investable
    value. ``rebalance`` is whether a role left its outer band (or none is held yet): then every
    role is traded to ``weights``, sells included. Otherwise ``weights`` are where new cash goes.
    """

    targets: Weights
    scales: Weights
    states: Mapping[str, TrendState]
    held: tuple[str, ...]
    weights: Weights
    rebalance: bool


@dataclass(frozen=True, slots=True)
class CoreStrategy:
    """Static role weights held within wide bands, optionally tilted by trend and executed
    tax-aware (the module docstring)."""

    name: str
    weights: Weights
    vehicles: Mapping[str, RoleVehicles]
    bands: Bands
    cash_role: str | None
    tilt: TrendRule | None
    tax_aware: TaxAware | None

    def __post_init__(self) -> None:
        self._check()

    def _check(self) -> None:
        """Refuse a strategy that cannot be held; subclasses state their own rules."""
        self._check_common()
        if self.tax_aware is not None and self.tilt is None:
            raise ValueError(f"strategy {self.name}: tax-aware execution belongs to the tilt")

    def _check_common(self) -> None:
        unknown = sorted(set(self.weights) - set(self.vehicles))
        if unknown:
            raise ValueError(f"strategy {self.name} weights roles without vehicles: {unknown}")
        if any(w < ZERO for w in self.weights.values()) or sum(self.weights.values()) > ONE:
            raise ValueError(
                f"strategy {self.name} weights must be at least 0 and sum to 1 at most"
            )
        if (self.tilt is not None or self.tax_aware is not None) and self.cash_role is None:
            raise ValueError(f"strategy {self.name} tilts or trades tax-aware without a cash role")
        if self.cash_role is not None and self.cash_role not in self.vehicles:
            raise ValueError(f"strategy {self.name} names a cash role without vehicles")

    # ── targets ──

    def _tilted(
        self, view: CoreView
    ) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, TrendState]]:
        """Each role's target weight, its scale and the trend behind it."""
        targets = dict(self.weights)
        scales = dict.fromkeys(targets, ONE)
        states: dict[str, TrendState] = {}
        if self.tilt is None or self.cash_role is None:
            return targets, scales, states
        targets.setdefault(self.cash_role, ZERO)
        scales.setdefault(self.cash_role, ONE)
        cash = self.vehicles[self.cash_role].primary
        for role, weight in self.weights.items():
            if role == self.cash_role or weight == ZERO:
                continue
            state = view.trend(self.vehicles[role].primary, cash, self.tilt)
            if state is None:
                raise TrendUnavailableError(
                    f"{self.name}: {role} lacks the {self.tilt.months_needed} month ends "
                    f"the tilt needs on {view.as_of}"
                )
            scale = Decimal(repr(state.scale))
            states[role] = state
            scales[role] = scale
            targets[role] = weight * scale
            targets[self.cash_role] += weight * (ONE - scale)
        return targets, scales, states

    def role_targets(
        self, view: CoreView, book: CoreBook | None = None
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        """Each role's target weight and its scale (1 without a tilt): the trend's scale."""
        del book  # the tilt reads the market alone
        targets, scales, _ = self._tilted(view)
        return targets, scales

    def _states(
        self, view: CoreView, book: CoreBook
    ) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, TrendState]]:
        """What :meth:`plan` reads of the market: role targets, scales and trend states."""
        del book
        return self._tilted(view)

    def _decide(self, view: CoreView, book: CoreBook) -> tuple[CorePlan, Mapping[str, Decimal]]:
        """The plan without its vehicle weights, and the role targets those weights come from."""
        targets, scales, states = self._states(view, book)
        current = {role: self._role_weight(role, book) for role in self.vehicles}
        rebalance = not book.units or self._outside(targets, current)
        held: tuple[str, ...] = ()
        aimed = targets
        aware = self.tax_aware
        if (
            rebalance
            and book.units
            and aware is not None
            and aware.benefit_fraction is not None
            and self.cash_role is not None
        ):
            aimed, held = self._hold_costly_reductions(
                targets, scales, current, book, self.cash_role, aware.benefit_fraction
            )
            # A held reduction alone is no reason to trade the other roles.
            rebalance = self._outside(aimed, current)
        plan = CorePlan(
            targets=aimed, scales=scales, states=states, held=held, weights={}, rebalance=rebalance
        )
        return plan, aimed

    def plan(self, view: CoreView, book: CoreBook) -> CorePlan:
        """What the account should hold, and whether to trade to it (see :class:`CorePlan`)."""
        plan, aimed = self._decide(view, book)
        return replace(plan, weights=self._vehicle_weights(aimed, book))

    def targets(self, view: CoreView, book: CoreBook) -> Weights | None:
        """The vehicle weights to trade to, or ``None`` to keep what is held (the vehicle weights
        are worked out only when the account is to trade)."""
        plan, aimed = self._decide(view, book)
        return self._vehicle_weights(aimed, book) if plan.rebalance else None

    def _outside(self, targets: Mapping[str, Decimal], current: Mapping[str, Decimal]) -> bool:
        """Whether a role sits outside its outer band, or a role without a target is held."""
        return any(
            abs(current[role] - target) > self.bands.width(target)
            for role, target in targets.items()
        ) or any(current[role] > ZERO for role in set(self.vehicles) - set(targets))

    def _role_weight(self, role: str, book: CoreBook) -> Decimal:
        return sum((book.weight(key) for key in self.vehicles[role].keys), ZERO)

    # ── tax-aware execution ──

    def _sale_order(self, role: str, book: CoreBook) -> list[str]:
        """The role's held vehicles, the one with the smaller gain for its value first."""

        def gain_share(key: str) -> Decimal:
            value = book.value(key)
            return (value - book.basis(key)) / value

        held = [key for key in self.vehicles[role].keys if book.value(key) > ZERO]
        return sorted(held, key=lambda key: (gain_share(key), key))

    def _sale_plan(self, role: str, amount: Decimal, book: CoreBook) -> list[tuple[str, Decimal]]:
        """The value to sell from each vehicle to take ``amount`` out of ``role``."""
        plan: list[tuple[str, Decimal]] = []
        left = amount
        for key in self._sale_order(role, book):
            if left <= ZERO:
                break
            take = min(left, book.value(key))
            plan.append((key, take))
            left -= take
        return plan

    def _hold_costly_reductions(
        self,
        targets: Mapping[str, Decimal],
        scales: Mapping[str, Decimal],
        current: Mapping[str, Decimal],
        book: CoreBook,
        cash_role: str,
        benefit_fraction: Decimal,
    ) -> tuple[dict[str, Decimal], tuple[str, ...]]:
        """A trend reduction whose tax exceeds its assumed benefit is not made: the role keeps
        its weight and the cash role gives up what it would have received. Returns the targets
        and the roles whose reduction was held."""
        held = dict(targets)
        kept = ZERO
        roles: list[str] = []
        for role, target in targets.items():
            if role == cash_role or scales[role] >= ONE or current[role] <= target:
                continue
            amount = (current[role] - target) * book.investable
            tax = book.sales_tax(
                [
                    (key, book.units_for(key, value))
                    for key, value in self._sale_plan(role, amount, book)
                ]
            )
            if tax > benefit_fraction * amount:
                held[role] = current[role]
                kept += current[role] - target
                roles.append(role)
        if kept == ZERO:
            return held, ()
        held[cash_role] -= min(kept, held[cash_role])
        excess = sum(held.values(), ZERO) - ONE
        if excess <= ZERO:
            return held, tuple(roles)
        # The cash role could not give it all: the roles still trading give up the rest, and
        # if they cannot (tax owed leaves less to invest than is held), every role scales down.
        free = [
            role
            for role in held
            if role != cash_role and held[role] != current[role] and held[role] > ZERO
        ]
        pool = sum((held[role] for role in free), ZERO)
        if pool >= excess:
            for role in free:
                held[role] -= excess * held[role] / pool
            return held, tuple(roles)
        for role in free:
            held[role] = ZERO
        total = sum(held.values(), ZERO)
        return {role: weight / total for role, weight in held.items()}, tuple(roles)

    def _buy_vehicle(self, role: str, book: CoreBook) -> str:
        """Where a purchase for ``role`` goes: a vehicle already held first, then the primary;
        with tax-aware execution, the first that would not defer a loss (else the first)."""
        vehicles = self.vehicles[role]
        held = [key for key in vehicles.keys if book.value(key) > ZERO]
        order = held + [key for key in vehicles.keys if key not in held]
        if self.tax_aware is None or vehicles.partner is None:
            return order[0]
        for key in order:
            if not book.purchase_defers_loss(key):
                return key
        return order[0]

    def _vehicle_weights(
        self, targets: Mapping[str, Decimal], book: CoreBook
    ) -> dict[str, Decimal]:
        """Each vehicle's weight, rounded down to the weight quantum: a residue of the arithmetic
        above can never make them sum above 1."""
        if book.investable <= ZERO:
            return {
                self.vehicles[role].primary: share(w, ONE)
                for role, w in targets.items()
                if w > ZERO
            }
        weights: dict[str, Decimal] = {}
        for role, target in targets.items():
            vehicles = self.vehicles[role]
            values = {key: book.value(key) for key in vehicles.keys}
            wanted = target * book.investable
            change = wanted - sum(values.values(), ZERO)
            if change > ZERO:
                key = self._buy_vehicle(role, book)
                values[key] += change
            elif change < ZERO:
                for key, value in self._sale_plan(role, -change, book):
                    values[key] -= value
            for key, value in values.items():
                if value > ZERO:
                    weights[key] = share(value, book.investable)
        return weights
