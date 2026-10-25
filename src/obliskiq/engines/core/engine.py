"""The core engine: the owner's variant on the live book, as a target portfolio (MASTER_PLAN §5.4).

The decision layer hands the engine each role's **vehicles** (:class:`RoleChoice`: the primary the
vehicle selector ranks first, its swap partner when the variant trades tax-aware, the other
vehicles of the role the book holds, and those that may not be bought now), the **book** (units,
one unit's price in the base currency, the core's budget and the cash it may spend) and the tax
lots' answers; the engine runs the variant's rules (``engines.core.rules``, the code Session 4c
tested) on them and sizes the trades as the simulator does (``engines.core.trades``):

* **Start**: the book holds nothing; every role is bought to its target.
* **Band**: a role left its outer band; every role goes back to its target, sells first, the buys
  scaled by one factor to the cash and the sales' proceeds after their costs. This is the only
  time the core sells (sells beyond the outer band only).
* **Cash**: inside the bands, the book holds cash beyond what its targets leave in cash (a
  deposit, income, a sale's proceeds) of at least the minimum trade: the roles below their targets
  are bought with it, nothing is sold (cash flows first).
* **Hold**: nothing trades.

A role none of whose vehicles may be bought keeps at most what it holds, and the rest of its
target stays in the book's cash: an **unfilled** role (nothing proposable, nothing held) is held
at zero and untilted, its weight left in cash, never spread over the other roles. A tilted role
whose trend cannot be read on any of its vehicles is left whole (the static variant is the
fallback, DECISIONS D89) and the portfolio says why; so is every role when the cash role has no
history to compare momentum with.

The engine is pure: the decision layer reads the store, the quotes and the lots, and turns the
portfolio's trades into intents under the live guards (``decision/rebalance_core.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Protocol

from ...domain.core import (
    PlannedTrade,
    RoleStatus,
    RoleTarget,
    TargetPortfolio,
    Trigger,
    VehicleTarget,
    WithheldTrade,
)
from ...domain.orders import OrderSide
from ...errors import TaxError
from .history import HistoryView
from .rules import CoreDecision, RoleVehicles
from .trades import Leg, affordable, delta_trades, whole
from .variants import Variant

ZERO = Decimal(0)
ONE = Decimal(1)
#: The key an unfilled role holds its place with: never held, never bought.
UNFILLED: Final = "unfilled:"
#: The role a held vehicle no role names is reported under: it has no target, so it is sold.
UNLISTED: Final = "unlisted:"
#: The cost of a buy the cost model cannot price: never affordable.
UNPRICEABLE: Final = Decimal("Infinity")


@dataclass(frozen=True, slots=True)
class RoleChoice:
    """A role's vehicles by conid, as the decision layer chose them, and why. ``primary`` is
    ``None`` when no vehicle of the role may be bought; ``held`` lists the role's other held
    vehicles, largest first."""

    role: str
    primary: int | None
    partner: int | None
    held: tuple[int, ...]
    closed: frozenset[int]
    reason: str


@dataclass(frozen=True, slots=True)
class BookState:
    """The core's book: units and one unit's base-currency price by conid (every held vehicle and
    every vehicle that may be bought), the core's budget and what the book may spend now."""

    units: Mapping[int, Decimal]
    prices: Mapping[int, Decimal]
    budget: Decimal
    cash: Decimal
    on: date


class TaxAnswers(Protocol):
    """The live tax lots' answers (``tax.book.TaxHooks``)."""

    def complete(self, conid: int, units: Decimal) -> bool: ...

    def basis(self, conid: int) -> Decimal: ...

    def trial_tax(self, sales: Sequence[tuple[int, Decimal, Decimal]], *, on: date) -> Decimal: ...

    def purchase_defers_loss(self, conid: int, on: date) -> bool: ...


@dataclass(frozen=True, slots=True)
class Sizing:
    """How trades are sized: the no-trade band, the least free cash worth deploying, and the cost
    of a leg in the base currency (``buy_cost``: notional and every cost; ``sell_cost``: every
    cost of a sale), ``None`` when the cost model cannot price it."""

    no_trade_band: Decimal
    minimum: Decimal
    buy_cost: Callable[[int, Decimal], Decimal | None]
    sell_cost: Callable[[int, Decimal], Decimal | None]


class LiveBook:
    """The book as the core's rules see it (``engines.core.rules.CoreBook``), keyed by conid
    text. A tax question the lots cannot answer for a position is ``None``: the rules then hold
    a reduction and sell that vehicle last."""

    def __init__(self, state: BookState, tax: TaxAnswers | None) -> None:
        self._state = state
        self._tax = tax
        self._units = {str(conid): q for conid, q in state.units.items() if q > ZERO}

    @property
    def units(self) -> Mapping[str, Decimal]:
        return self._units

    @property
    def investable(self) -> Decimal:
        return self._state.budget

    def price(self, key: str) -> Decimal | None:
        return None if key.startswith(UNFILLED) else self._state.prices.get(int(key))

    def value(self, key: str) -> Decimal:
        quantity = self._units.get(key, ZERO)
        price = self.price(key)
        return quantity * price if quantity > ZERO and price is not None else ZERO

    def weight(self, key: str) -> Decimal:
        return self.value(key) / self.investable if self.investable > ZERO else ZERO

    def _answered(self, key: str) -> TaxAnswers | None:
        tax = self._tax
        units = self._units.get(key, ZERO)
        if tax is None or (units > ZERO and not tax.complete(int(key), units)):
            return None
        return tax

    def basis(self, key: str) -> Decimal | None:
        tax = self._answered(key)
        return None if tax is None else tax.basis(int(key))

    def units_for(self, key: str, value: Decimal) -> Decimal:
        price = self.price(key)
        return ZERO if price is None or price <= ZERO else whole(value / price)

    def sales_tax(self, sales: Sequence[tuple[str, Decimal]]) -> Decimal | None:
        tax = self._tax
        if tax is None:
            return None
        trades: list[tuple[int, Decimal, Decimal]] = []
        for key, units in sales:
            if units <= ZERO:
                continue
            price = self.price(key)
            if self._answered(key) is None or price is None:
                return None
            trades.append((int(key), units, price))
        try:
            return tax.trial_tax(trades, on=self._state.on)
        except TaxError:  # a lot the engine cannot account for: the tax is unknown
            return None

    def purchase_defers_loss(self, key: str) -> bool:
        if self._tax is None or key.startswith(UNFILLED):
            return False
        return self._tax.purchase_defers_loss(int(key), self._state.on)


# ── the vehicles and the trend ──


@dataclass(frozen=True, slots=True)
class _Roles:
    vehicles: dict[str, RoleVehicles]
    status: dict[str, RoleStatus]
    reasons: dict[str, list[str]]


def _role_vehicles(choices: Mapping[str, RoleChoice], held: Mapping[str, Decimal]) -> _Roles:
    vehicles: dict[str, RoleVehicles] = {}
    status: dict[str, RoleStatus] = {}
    reasons: dict[str, list[str]] = {}
    for role, choice in choices.items():
        reasons[role] = [choice.reason]
        keys = [choice.primary, choice.partner, *choice.held]
        named = [str(conid) for conid in dict.fromkeys(keys) if conid is not None]
        if not named:
            placeholder = f"{UNFILLED}{role}"
            vehicles[role] = RoleVehicles(placeholder, None, closed=frozenset({placeholder}))
            status[role] = RoleStatus.UNFILLED
            continue
        partner = None if choice.partner is None else str(choice.partner)
        primary = named[0] if choice.primary is None else str(choice.primary)
        others = tuple(key for key in named if key not in {primary, partner})
        closed = frozenset(str(conid) for conid in choice.closed) & set(named)
        role_vehicles = RoleVehicles(primary, partner, others, closed)
        vehicles[role] = role_vehicles
        if role_vehicles.buyable:
            status[role] = RoleStatus.FILLED
        elif any(key in held for key in named):
            status[role] = RoleStatus.HELD_ONLY
        else:
            status[role] = RoleStatus.UNFILLED
    return _Roles(vehicles, status, reasons)


def _trend(
    variant: Variant, roles: _Roles, history: HistoryView
) -> tuple[HistoryView, frozenset[str]]:
    """The view the tilt reads (each role's primary aliased to its vehicle with the history) and
    the roles the tilt leaves whole, each with its reason."""
    if not variant.tilts:
        return history, frozenset()
    rule = variant.tilt
    needed = rule.months_needed
    cash_vehicles = roles.vehicles[variant.cash_role]
    cash_keys = [key for key in cash_vehicles.keys if not key.startswith(UNFILLED)]
    cash_key = max(cash_keys, key=lambda key: len(history.closes(key).closes), default=None)
    tilted = [
        role
        for role, weight in variant.weights.items()
        if weight > ZERO and role != variant.cash_role
    ]
    if cash_key is None or len(history.closes(cash_key).closes) < needed:
        why = (
            "the cash role has no vehicle"
            if cash_key is None
            else history.closes(cash_key).note or f"{cash_key} has too few month ends"
        )
        for role in tilted:
            roles.reasons[role].append(
                f"untilted: momentum has no cash history to be measured against ({why})"
            )
        return history, frozenset(tilted)
    aliases = {cash_vehicles.primary: cash_key}
    untilted: set[str] = set()
    for role in tilted:
        vehicles = roles.vehicles[role]
        if roles.status[role] is RoleStatus.UNFILLED:
            untilted.add(role)
            continue
        found = next(
            (key for key in vehicles.keys if history.months(key, cash_key) >= needed), None
        )
        if found is None:
            best = max(history.months(key, cash_key) for key in vehicles.keys)
            notes = [history.closes(key).note for key in vehicles.keys]
            note = next((n for n in notes if n is not None), None)
            roles.reasons[role].append(
                f"untilted: {best} of the {needed} month ends the tilt needs"
                + ("" if note is None else f" ({note})")
            )
            untilted.add(role)
            continue
        aliases[vehicles.primary] = found
        if not history.settled(found, cash_key, rule):
            roles.reasons[role].append(
                "its trend state rests on the first evaluable month of the stored history"
            )
    return HistoryView(history.history, history.month, aliases), frozenset(untilted)


# ── the plan ──


def _sales(
    planned: Sequence[Leg], book: BookState, sizing: Sizing, withheld: list[WithheldTrade]
) -> tuple[list[Leg], Decimal]:
    """The sales made and what they raise after their costs: a sale the cost model cannot price,
    or one that would cost more than it raises, is not made (no executor would place it)."""
    made: list[Leg] = []
    raised = ZERO
    for key, quantity in planned:
        conid = int(key)
        cost = sizing.sell_cost(conid, quantity)
        proceeds = None if cost is None else quantity * book.prices[conid] - cost
        if proceeds is None or proceeds < ZERO:
            why = (
                "the cost model cannot price it" if cost is None else "it costs more than it raises"
            )
            withheld.append(
                WithheldTrade(conid=conid, side=OrderSide.SELL, quantity=quantity, reason=why)
            )
            continue
        made.append((key, quantity))
        raised += proceeds
    return made, raised


def _buys_made(
    buys: Sequence[Leg],
    cash: Decimal,
    prices: Mapping[str, Decimal],
    sizing: Sizing,
    withheld: list[WithheldTrade],
) -> list[Leg]:
    priced: list[Leg] = []
    for key, quantity in buys:
        if sizing.buy_cost(int(key), quantity) is None:
            withheld.append(
                WithheldTrade(
                    conid=int(key),
                    side=OrderSide.BUY,
                    quantity=quantity,
                    reason="the cost model cannot price it",
                )
            )
            continue
        priced.append((key, quantity))

    def cost(key: str, quantity: Decimal) -> Decimal:
        found = sizing.buy_cost(int(key), quantity)
        return UNPRICEABLE if found is None else found

    return affordable(priced, max(cash, ZERO), prices, cost, whole_units=True)


def plan_portfolio(
    variant: Variant,
    choices: Mapping[str, RoleChoice],
    history: HistoryView,
    book: BookState,
    tax: TaxAnswers | None,
    sizing: Sizing,
) -> TargetPortfolio:
    """The variant's target portfolio on ``book``'s date and the trades toward it."""
    live = LiveBook(book, tax)
    roles = _role_vehicles(choices, live.units)
    view, untilted = _trend(variant, roles, history)
    rules = variant.rules(roles.vehicles, untilted=untilted)
    decision = rules.decide(view, live, always_weigh=True)
    weights = decision.weights or {}
    prices = {str(conid): price for conid, price in book.prices.items()}
    held = dict(live.units)
    notes: list[str] = []
    withheld: list[WithheldTrade] = []
    if tax is None and variant.tilts:
        notes.append("the tax lots cannot be read: every trend reduction is held")
    sells: list[Leg] = []
    buys: list[Leg] = []
    target_value = sum(weights.values(), ZERO) * book.budget
    held_value = sum((live.value(key) for key in held), ZERO)
    if not held:
        trigger = Trigger.START
        _, wanted = delta_trades(
            weights, held, prices, book.budget, band=sizing.no_trade_band, whole_units=True
        )
        buys = _buys_made(wanted, book.cash, prices, sizing, withheld)
    elif decision.outside:
        trigger = Trigger.BAND
        planned, wanted = delta_trades(
            weights, held, prices, book.budget, band=sizing.no_trade_band, whole_units=True
        )
        sells, raised = _sales(planned, book, sizing, withheld)
        buys = _buys_made(wanted, book.cash + raised, prices, sizing, withheld)
    elif target_value - held_value >= sizing.minimum:
        trigger = Trigger.CASH
        _, wanted = delta_trades(
            weights, held, prices, book.budget, band=sizing.no_trade_band, whole_units=True
        )
        buys = _buys_made(
            wanted, min(book.cash, target_value - held_value), prices, sizing, withheld
        )
    else:
        trigger = Trigger.HOLD
    return TargetPortfolio(
        variant=variant.name,
        as_of=book.on,
        trend_as_of=history.month if variant.tilts else None,
        budget=book.budget,
        trigger=trigger,
        roles=_role_targets(variant, choices, roles, decision),
        vehicles=_vehicle_targets(roles, weights, live, book),
        trades=_trades(_role_of(roles, held), sells, buys, book),
        withheld=tuple(withheld),
        notes=tuple(notes),
    )


def _role_targets(
    variant: Variant, choices: Mapping[str, RoleChoice], roles: _Roles, decision: CoreDecision
) -> tuple[RoleTarget, ...]:
    out: list[RoleTarget] = []
    for role in choices:
        vehicles = roles.vehicles[role]
        reasons = roles.reasons[role]
        target = decision.targets.get(role, ZERO)
        static = variant.weights.get(role, ZERO)
        scale = decision.scales.get(role, ONE)
        if variant.tilts and scale < ONE:
            reasons.append(f"trend scale {scale}")
        if role in decision.held:
            reasons.append("its trend reduction is held: the tax exceeds the benefit")
        if roles.status[role] is RoleStatus.HELD_ONLY and static > ZERO:
            reasons.append("its target is capped at what it holds; the rest stays in cash")
        elif roles.status[role] is RoleStatus.UNFILLED and static > ZERO:
            reasons.append("its weight stays in the book's cash")
        out.append(
            RoleTarget(
                role=role,
                status=roles.status[role],
                static_weight=static,
                scale=scale,
                target_weight=target,
                held_weight=decision.current.get(role, ZERO),
                reduction_held=role in decision.held,
                vehicles=tuple(int(k) for k in vehicles.keys if not k.startswith(UNFILLED)),
                reason="; ".join(reasons),
            )
        )
    return tuple(out)


def _role_of(roles: _Roles, held: Mapping[str, Decimal]) -> dict[str, str]:
    """Each vehicle's role; a held vehicle no role names is ``unlisted``."""
    owner = {key: role for role, vehicles in roles.vehicles.items() for key in vehicles.keys}
    for key in held:
        owner.setdefault(key, f"{UNLISTED}{key}")
    return owner


def _vehicle_targets(
    roles: _Roles, weights: Mapping[str, Decimal], live: LiveBook, book: BookState
) -> tuple[VehicleTarget, ...]:
    owner = _role_of(roles, live.units)
    keys = [key for key in owner if not key.startswith(UNFILLED)]
    return tuple(
        VehicleTarget(
            conid=int(key),
            role=owner[key],
            weight=weights.get(key, ZERO),
            held_units=live.units.get(key, ZERO),
            price=book.prices.get(int(key), ZERO),
        )
        for key in keys
        if key in weights or key in live.units
    )


def _trades(
    owner: Mapping[str, str], sells: Sequence[Leg], buys: Sequence[Leg], book: BookState
) -> tuple[PlannedTrade, ...]:
    legs = [(OrderSide.SELL, leg) for leg in sells] + [(OrderSide.BUY, leg) for leg in buys]
    return tuple(
        PlannedTrade(
            conid=int(key),
            role=owner[key],
            side=side,
            quantity=quantity,
            notional=quantity * book.prices[int(key)],
        )
        for side, (key, quantity) in legs
    )
