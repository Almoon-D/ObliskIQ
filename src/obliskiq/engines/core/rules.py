"""The core's rules: static role weights held within wide bands, tilted by trend and executed
tax-aware (MASTER_PLAN §5.4, DECISIONS D80). One mechanism, shared by the research harness
(``research/strategies.py`` runs Session 4c's variants with it) and the live engine
(``engines/core/engine.py``), so the tested core and the live core cannot drift apart.

- **Wide bands.** Nothing trades while every role sits inside its outer band around its target,
  the smaller of an absolute width and a fraction of the target; when one leaves it, every role
  is traded back to its target (the inner band is the target itself).
- **The trend tilt.** Each risky role's target is scaled by its trend (``quant.trend``: 12-month
  momentum over cash and a 10-month moving average, hysteresis, a floor above zero); the weight
  freed goes to the cash role.
- **Tax-aware execution** with the tilt: a trend reduction is made only while the tax it would
  cost (its sales together, so a loss on one vehicle offsets a gain on another) is at most a
  pre-registered fraction of the value sold (its assumed benefit), else the role keeps its weight
  and the cash role gives up what it would have received; losses are taken freely; a purchase
  that would defer a loss under the wash-sale rule goes to the role's swap partner.

Each role is held through its **vehicles** (:class:`RoleVehicles`): its primary, its swap partner
with tax-aware execution, and, in the live book, the other vehicles of the role it holds. A
purchase goes to a vehicle already held first, then the primary; a sale comes first from the
vehicle with the smaller gain for its value. A vehicle that may no longer be bought (``closed``)
is still counted and sold, never bought; a role none of whose vehicles may be bought keeps at most
what it holds, and the rest of its target stays in the book's cash. The research harness never
closes a vehicle, so what it tested is the mechanism with every vehicle open.

The rules see the market through a :class:`TrendView` (the trend as of a date) and the account
through a :class:`CoreBook` (units, values, the value to invest and the tax lots' answers). A
book that cannot answer a tax question returns ``None``, and the rules fail closed: a reduction
whose tax is unknown is held, and a vehicle whose cost basis is unknown is sold last.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from ...errors import CoreError
from ...quant.trend import TrendRule, TrendState
from .trades import share

type Weights = Mapping[str, Decimal]

ZERO = Decimal(0)
ONE = Decimal(1)


class TrendView(Protocol):
    """What the tilt may see on a date: each instrument's trend against cash, as of then."""

    @property
    def as_of(self) -> date: ...

    def trend(self, key: str, cash: str, rule: TrendRule) -> TrendState | None: ...


class CoreBook(Protocol):
    """What the rules may know of the account: units and base-currency values, the value they
    may invest, and the tax lots' answers (``None``: the lots cannot answer)."""

    @property
    def units(self) -> Mapping[str, Decimal]: ...

    @property
    def investable(self) -> Decimal: ...

    def value(self, key: str) -> Decimal: ...

    def weight(self, key: str) -> Decimal: ...

    def basis(self, key: str) -> Decimal | None: ...

    def units_for(self, key: str, value: Decimal) -> Decimal: ...

    def sales_tax(self, sales: Sequence[tuple[str, Decimal]]) -> Decimal | None: ...

    def purchase_defers_loss(self, key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class RoleVehicles:
    """The instruments that hold a role: its primary vehicle, its swap partner (with tax-aware
    execution), the other vehicles of the role the book holds, and those that may no longer be
    bought."""

    primary: str
    partner: str | None
    others: tuple[str, ...] = ()
    closed: frozenset[str] = field(default_factory=frozenset)

    @property
    def keys(self) -> tuple[str, ...]:
        first = (self.primary,) if self.partner is None else (self.primary, self.partner)
        return (*first, *self.others)

    @property
    def buyable(self) -> tuple[str, ...]:
        return tuple(key for key in self.keys if key not in self.closed)


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
class CoreDecision:
    """What the rules decided on a date: each role's target weight (after the tilt, a held
    reduction and the cap of a role that cannot be bought), its trend scale and its weight now;
    whether a role sits outside its band (the empty book always does); the roles whose costly
    reduction was held; and each vehicle's target weight (``None`` when nothing trades and they
    were not asked for)."""

    targets: dict[str, Decimal]
    scales: dict[str, Decimal]
    current: dict[str, Decimal]
    outside: bool
    held: tuple[str, ...]
    weights: dict[str, Decimal] | None


@dataclass(frozen=True, slots=True)
class CoreRules:
    """Static role weights held within wide bands, optionally tilted by trend, and executed
    tax-aware (the module docstring). ``bands`` ``None`` buys and holds: once the book holds
    something, nothing is traded back to the weights. ``untilted`` names roles the tilt leaves
    whole because their trend cannot be read (the live engine's honest fallback; research names
    none)."""

    name: str
    weights: Weights
    vehicles: Mapping[str, RoleVehicles]
    bands: Bands | None
    cash_role: str | None
    tilt: TrendRule | None
    tax_aware: TaxAware | None
    untilted: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
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
        if self.tax_aware is not None and not self._adapts():
            raise ValueError(
                f"strategy {self.name}: tax-aware execution belongs to the tilt or the scaling"
            )

    def _adapts(self) -> bool:
        """Whether something moves the targets (the tilt here): tax-aware execution needs it."""
        return self.tilt is not None

    # ── targets ──

    def _no_trend(self, role: str, view: TrendView) -> Exception:
        """The error for a tilted role whose trend cannot be read."""
        months = self.tilt.months_needed if self.tilt is not None else 0
        return CoreError(
            f"{self.name}: {role} lacks the {months} month ends the tilt needs on {view.as_of}"
        )

    def role_targets(
        self, view: TrendView, book: CoreBook | None = None
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        """Each role's target weight and its trend scale (1 without a tilt)."""
        del book  # the tilt reads the market alone
        targets = dict(self.weights)
        scales = dict.fromkeys(targets, ONE)
        if self.tilt is None or self.cash_role is None:
            return targets, scales
        targets.setdefault(self.cash_role, ZERO)
        scales.setdefault(self.cash_role, ONE)
        cash = self.vehicles[self.cash_role].primary
        for role, weight in self.weights.items():
            if role == self.cash_role or weight == ZERO or role in self.untilted:
                continue
            state = view.trend(self.vehicles[role].primary, cash, self.tilt)
            if state is None:
                raise self._no_trend(role, view)
            scale = Decimal(repr(state.scale))
            scales[role] = scale
            targets[role] = weight * scale
            targets[self.cash_role] += weight * (ONE - scale)
        return targets, scales

    def decide(
        self, view: TrendView, book: CoreBook, *, always_weigh: bool = False
    ) -> CoreDecision:
        """The decision on ``view``'s date; ``always_weigh`` names each vehicle's target weight
        even when nothing sits outside its band (the live engine's cash-first buys read them)."""
        targets, scales = self.role_targets(view, book)
        current = {role: self._role_weight(role, book) for role in self.vehicles}
        targets = self._capped(targets, current)
        if not book.units:
            return CoreDecision(
                targets, scales, current, True, (), self._vehicle_weights(targets, book)
            )
        outside = self._outside(targets, current)
        held: tuple[str, ...] = ()
        aware = self.tax_aware
        if outside and aware is not None and aware.benefit_fraction is not None and self.cash_role:
            targets, held = self._hold_costly_reductions(
                targets, scales, current, book, self.cash_role, aware.benefit_fraction
            )
            # A held reduction alone is no reason to trade the other roles.
            outside = self._outside(targets, current)
        weights = self._vehicle_weights(targets, book) if outside or always_weigh else None
        return CoreDecision(targets, scales, current, outside, held, weights)

    def targets(self, view: TrendView, book: CoreBook) -> Weights | None:
        """Each vehicle's target weight of the investable value, or ``None`` to keep what is
        held (every role inside its band)."""
        decision = self.decide(view, book)
        return decision.weights if decision.outside else None

    def _capped(
        self, targets: dict[str, Decimal], current: Mapping[str, Decimal]
    ) -> dict[str, Decimal]:
        """A role none of whose vehicles may be bought keeps at most what it holds."""
        closed = [
            role
            for role, target in targets.items()
            if not self.vehicles[role].buyable and target > current[role]
        ]
        if not closed:
            return targets
        capped = dict(targets)
        for role in closed:
            capped[role] = current[role]
        return capped

    def _outside(self, targets: Mapping[str, Decimal], current: Mapping[str, Decimal]) -> bool:
        """Whether a role sits outside its outer band (never, buying and holding), or a role
        without a target is held."""
        bands = self.bands
        return (
            bands is not None
            and any(
                abs(current[role] - target) > bands.width(target)
                for role, target in targets.items()
            )
        ) or any(current[role] > ZERO for role in set(self.vehicles) - set(targets))

    def _role_weight(self, role: str, book: CoreBook) -> Decimal:
        return sum((book.weight(key) for key in self.vehicles[role].keys), ZERO)

    # ── tax-aware execution ──

    def _sale_order(self, role: str, book: CoreBook) -> list[str]:
        """The role's held vehicles, the one with the smaller gain for its value first; one
        whose cost basis is unknown last."""

        def gain_share(key: str) -> tuple[int, Decimal, str]:
            value = book.value(key)
            basis = book.basis(key)
            if basis is None:
                return (1, ZERO, key)
            return (0, (value - basis) / value, key)

        held = [key for key in self.vehicles[role].keys if book.value(key) > ZERO]
        return sorted(held, key=gain_share)

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
        targets: dict[str, Decimal],
        scales: Mapping[str, Decimal],
        current: Mapping[str, Decimal],
        book: CoreBook,
        cash_role: str,
        benefit_fraction: Decimal,
    ) -> tuple[dict[str, Decimal], tuple[str, ...]]:
        """A trend reduction whose tax exceeds its assumed benefit (or is unknown) is not made:
        the role keeps its weight and the cash role gives up what it would have received."""
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
            if tax is None or tax > benefit_fraction * amount:
                held[role] = current[role]
                kept += current[role] - target
                roles.append(role)
        if kept == ZERO:
            return held, tuple(roles)
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

    def _buy_vehicle(self, role: str, book: CoreBook) -> str | None:
        """Where a purchase for ``role`` goes: a vehicle already held first, then the primary;
        with tax-aware execution, the first that would not defer a loss (else the first).
        ``None`` when none of its vehicles may be bought."""
        vehicles = self.vehicles[role]
        buyable = vehicles.buyable
        held = [key for key in buyable if book.value(key) > ZERO]
        order = held + [key for key in buyable if key not in held]
        if not order:
            return None
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
            first = {
                role: self.vehicles[role].buyable[0]
                for role in targets
                if self.vehicles[role].buyable
            }
            return {
                first[role]: share(w, ONE)
                for role, w in targets.items()
                if w > ZERO and role in first
            }
        weights: dict[str, Decimal] = {}
        for role, target in targets.items():
            vehicles = self.vehicles[role]
            values = {key: book.value(key) for key in vehicles.keys}
            wanted = target * book.investable
            change = wanted - sum(values.values(), ZERO)
            if change > ZERO:
                key = self._buy_vehicle(role, book)
                if key is not None:
                    values[key] += change
            elif change < ZERO:
                for key, value in self._sale_plan(role, -change, book):
                    values[key] -= value
            for key, value in values.items():
                if value > ZERO:
                    weights[key] = share(value, book.investable)
        return weights
