"""Performance: the account's time-weighted return, its attribution by sleeve, the references,
realised and unrealised profit and the realised tax (MASTER_PLAN §5.15, §5.19).

* **The NAV series** is the account's daily NAV readings chained into a time-weighted index
  (``capital.twr``: flows never look like profit), so the account and the references are compared
  on growth, whatever the owner deposited or withdrew.
* **The references** are bought-and-held indices of eligible vehicles whose daily closes the
  collectors store, converted to the base currency at each day's rate and rebalanced to their
  configured weights at each month end: global equity in ACWI's developed and emerging mix, and a
  global 60/40. A reference missing a price on a day is unknown from that day, never interpolated.
* **Attribution**: each sleeve's realised profit (average cost per instrument, in the base
  currency at each fill's rate, commissions included) and unrealised profit (what it holds at the
  day's marks less its remaining cost). What is left of the account's change after the sleeves and
  the owner's flows is the cash line's: interest, currency moves on cash, fees outside fills.
* **Tax**: the year's realised tax from the lot engine under the active jurisdiction's policy
  (``TaxBook``), provisional while the year is open.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..capital.twr import ExternalFlow, FlowTiming, Reading, time_weighted_return
from ..domain.orders import OrderSide, Sleeve
from ..store.orders import IntentFill

ZERO = Decimal(0)
ONE = Decimal(1)

#: A price in the base currency of an instrument on a day; ``None`` without one.
type BasePrice = Callable[[int, date], Decimal | None]
#: The base-currency value of one unit of a currency on a day; ``None`` without a rate.
type BaseRate = Callable[[str, date], Decimal | None]


@dataclass(frozen=True, slots=True)
class IndexPoint:
    on: date
    value: Decimal


@dataclass(frozen=True, slots=True)
class Series:
    """A growth index from 1 on its first day; ``reason`` says why it stops short when it does."""

    name: str
    points: tuple[IndexPoint, ...]
    reason: str | None

    @property
    def growth(self) -> Decimal | None:
        return None if len(self.points) < 2 else self.points[-1].value / self.points[0].value - 1


def nav_index(
    readings: Sequence[Reading], flows: Sequence[ExternalFlow], *, timing: FlowTiming
) -> Series:
    """The account's NAV readings as a time-weighted growth index."""
    result = time_weighted_return(readings, flows, timing=timing)
    if not result.periods:
        return Series("account", (), result.reason)
    value = ONE
    points = [IndexPoint(result.periods[0].start, value)]
    for period in result.periods:
        value *= 1 + period.ret
        points.append(IndexPoint(period.end, value))
    return Series("account", tuple(points), result.reason)


def reference_index(
    name: str, weights: Mapping[int, Decimal], days: Sequence[date], price: BasePrice
) -> Series:
    """A bought-and-held mix of ``weights`` over ``days``, rebalanced to its weights at each
    month end; it stops at the first day a member has no price."""
    if not days:
        return Series(name, (), "no day to measure")
    first = days[0]
    units: dict[int, Decimal] = {}
    for conid, weight in weights.items():
        start = price(conid, first)
        if start is None or start <= 0:
            return Series(name, (), f"no price of {conid} on {first.isoformat()}")
        units[conid] = weight / start
    points = [IndexPoint(first, ONE)]
    for index, day in enumerate(days):
        prices = {conid: price(conid, day) for conid in weights}
        missing = [conid for conid, value in prices.items() if value is None or value <= 0]
        if missing:
            return Series(name, tuple(points), f"no price of {missing[0]} on {day.isoformat()}")
        value = sum((units[c] * (prices[c] or ZERO) for c in weights), ZERO)
        if index > 0:
            points.append(IndexPoint(day, value))
        last_of_month = index + 1 < len(days) and days[index + 1].month != day.month
        if last_of_month:
            units = {c: value * weights[c] / (prices[c] or ONE) for c in weights}
    return Series(name, tuple(points), None)


@dataclass(frozen=True, slots=True)
class SleevePnl:
    """A sleeve's profit in the base currency: realised, unrealised, and what it holds."""

    sleeve: Sleeve
    realised: Decimal
    unrealised: Decimal
    held_value: Decimal
    commissions: Decimal
    unmarked: tuple[int, ...]


@dataclass
class _Position:
    units: Decimal = ZERO
    cost: Decimal = ZERO


def sleeve_pnl(
    fills: Sequence[IntentFill],
    *,
    currency: Callable[[int], str | None],
    multiplier: Callable[[int], Decimal],
    rate: BaseRate,
    price: BasePrice,
    on: date,
) -> list[SleevePnl] | str:
    """Each sleeve's realised and unrealised profit on ``on`` from its fills (average cost per
    instrument), or why it cannot be computed (a fill with no currency or rate)."""
    books: dict[Sleeve, dict[int, _Position]] = {}
    realised: dict[Sleeve, Decimal] = {}
    commissions: dict[Sleeve, Decimal] = {}
    for item in sorted(fills, key=lambda f: f.fill.executed_at):
        fill = item.fill
        if fill.executed_at.date() > on:
            continue
        code = currency(fill.conid)
        if code is None:
            return f"instrument {fill.conid} has no currency"
        fx = rate(code, fill.executed_at.date())
        if fx is None:
            return f"no {code} rate on {fill.executed_at.date().isoformat()}"
        size = multiplier(fill.conid)
        value = fill.quantity * fill.price * size * fx
        fee = (fill.commission or ZERO) * fx
        commissions[item.sleeve] = commissions.get(item.sleeve, ZERO) + fee
        position = books.setdefault(item.sleeve, {}).setdefault(fill.conid, _Position())
        if fill.side is OrderSide.BUY:
            position.units += fill.quantity
            position.cost += value + fee
            continue
        sold = min(fill.quantity, position.units)
        basis = position.cost * sold / position.units if position.units > 0 else ZERO
        realised[item.sleeve] = realised.get(item.sleeve, ZERO) + value - fee - basis
        position.units -= sold
        position.cost -= basis
    found: list[SleevePnl] = []
    for sleeve in sorted(set(books) | set(realised), key=lambda s: s.value):
        held = ZERO
        cost = ZERO
        unmarked: list[int] = []
        for conid, position in books.get(sleeve, {}).items():
            if position.units <= 0:
                continue
            mark = price(conid, on)
            if mark is None:
                unmarked.append(conid)
                continue
            held += position.units * mark * multiplier(conid)
            cost += position.cost
        found.append(
            SleevePnl(
                sleeve=sleeve,
                realised=realised.get(sleeve, ZERO),
                unrealised=held - cost,
                held_value=held,
                commissions=commissions.get(sleeve, ZERO),
                unmarked=tuple(sorted(unmarked)),
            )
        )
    return found


@dataclass(frozen=True, slots=True)
class Attribution:
    """The account's change over a period split by sleeve; ``cash`` is what is left after the
    sleeves and the owner's flows."""

    change: Decimal
    flows: Decimal
    sleeves: dict[Sleeve, Decimal]
    cash: Decimal


def attribution(
    start_nav: Decimal,
    end_nav: Decimal,
    flows: Decimal,
    start: Sequence[SleevePnl],
    end: Sequence[SleevePnl],
) -> Attribution:
    """Split the change of NAV between two days: each sleeve's total profit change, the flows,
    and the rest (the cash line)."""

    def total(rows: Sequence[SleevePnl]) -> dict[Sleeve, Decimal]:
        return {row.sleeve: row.realised + row.unrealised for row in rows}

    before, after = total(start), total(end)
    sleeves = {
        sleeve: after.get(sleeve, ZERO) - before.get(sleeve, ZERO)
        for sleeve in sorted(set(before) | set(after), key=lambda s: s.value)
    }
    change = end_nav - start_nav
    return Attribution(
        change=change,
        flows=flows,
        sleeves=sleeves,
        cash=change - flows - sum(sleeves.values(), ZERO),
    )
