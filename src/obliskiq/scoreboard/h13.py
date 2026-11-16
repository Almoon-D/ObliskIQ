"""H13's record: the thesis book against its matched twin, after tax at liquidation, read at its
pre-registered looks (``research/preregistrations/thesis-book.md``; DECISIONS D156, D171, D173).

This module is the arithmetic, on trades already valued in the base currency; ``h13_run.py``
reads them from the store.

**Two accounts per record.** The *book*: every thesis position from its entry fills to its exit
fills, at the fill prices, a buy costing its value and commission and a sale bringing its value
less commission. The *twin*: for every amount a thesis's entry spends, the equity reference
bought for that amount at that session's close, paying the cost model's spread, slippage and
commission out of it; at every exit of a thesis, the same share of that thesis's twin units sold
at that session's close, paying the same costs. Money in no thesis is in neither account.

**After tax, at liquidation.** At a month end an account's profit to date is what its trades
paid and brought plus its open positions at their marks, less the tax the active jurisdiction's
policy puts on that account taken alone: its own lots first in, first out, under the lot engine
the live book uses (``tax/lots.py``), every tax year settled in order with what is still held sold
that day at its mark.

**The statistic.** ``x_m = ln(1 + p) - ln(1 + q)``, where ``p`` and ``q`` are the change over
month ``m`` of the book's and the twin's after-tax profit over ``K``, the book's capital on the
last day before the month. A month with no thesis has ``x_m = 0``. At look ``k`` (month ``6k``):
``t_up = (x̄ - θ/12)/(s/√n)`` against ``c(0.05/10, n)`` and ``t_down = x̄/(s/√n)`` against
``-c(0.10/10, n)``, ``c(a, n)`` the ``1 - a`` quantile of Student's t with ``n - 1`` degrees of
freedom. A look with a month that cannot be measured (a mark older than seven days, a fill with no
rate, a security with no tax facts, no capital recorded) is not evaluable and decides nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal
from statistics import fmean, stdev
from typing import Literal

from ..domain.tax import SecurityIdentity
from ..errors import TaxError
from ..jurisdiction.tax import TaxPolicy
from ..quant.inference import student_t_quantile
from ..tax.lots import LotEngine

ZERO = Decimal(0)
ONE = Decimal(1)
MONTHS_PER_YEAR = 12
#: The twin's units are kept to this step, rounded down: a figure, never an order, whose lots
#: must add up exactly when they are sold (a sum at Decimal's precision could exceed them).
TWIN_UNIT = Decimal("1e-10")

type Side = Literal["buy", "sell"]
#: A security's value per unit in the base currency on a day; ``None``: no mark that fresh.
type UnitValue = Callable[[str, date], Decimal | None]


@dataclass(frozen=True, slots=True)
class Trade:
    """One fill of an account, in the base currency: ``value`` is the units at the fill price
    (times the contract multiplier) and ``costs`` what was paid beside it."""

    thesis_id: int
    security: str
    side: Side
    on: date
    quantity: Decimal
    value: Decimal
    costs: Decimal

    @property
    def cash(self) -> Decimal:
        """What the trade moved: a buy pays its value and costs, a sale brings its value less
        them."""
        return -(self.value + self.costs) if self.side == "buy" else self.value - self.costs


# ── months ──


def month_end(month: str) -> date:
    year, number = (int(part) for part in month.split("-"))
    return date(year, number, calendar.monthrange(year, number)[1])


def month_start(month: str) -> date:
    year, number = (int(part) for part in month.split("-"))
    return date(year, number, 1)


def month_of(day: date) -> str:
    return day.strftime("%Y-%m")


def months_from(first: str, last: str) -> list[str]:
    """The calendar months from ``first`` to ``last``, both counted whole."""
    year, number = (int(part) for part in first.split("-"))
    found: list[str] = []
    while f"{year:04d}-{number:02d}" <= last:
        found.append(f"{year:04d}-{number:02d}")
        year, number = (year + 1, 1) if number == MONTHS_PER_YEAR else (year, number + 1)
    return found


# ── the twin ──


@dataclass(frozen=True, slots=True)
class TwinInputs:
    """What the twin is bought with: the reference's key, its close in the base currency on a
    day, and the cost model's spread, slippage and commission of a leg (units, close, side, day),
    each ``None`` when it cannot be read."""

    security: str
    close: Callable[[date], Decimal | None]
    cost: Callable[[Decimal, Decimal, Side, date], Decimal | None]


def twin_trades(book: Sequence[Trade], twin: TwinInputs) -> list[Trade] | str:
    """The twin's trades for the book's: an entry's spending buys the reference, an exit sells
    the same share of that thesis's twin units. Why not, when a close or a cost is missing."""
    held: dict[int, Decimal] = {}
    units: dict[int, Decimal] = {}
    found: list[Trade] = []
    for trade in sorted(book, key=lambda t: (t.on, t.side != "buy")):
        close = twin.close(trade.on)
        if close is None or close <= ZERO:
            return f"the reference has no close within its age limit on {trade.on.isoformat()}"
        mine = trade.thesis_id
        if trade.side == "buy":
            spent = trade.value + trade.costs
            cost = twin.cost(spent / close, close, "buy", trade.on)
            if cost is None:
                return f"the cost model cannot price the twin's purchase on {trade.on.isoformat()}"
            bought = ((spent - cost) / close).quantize(TWIN_UNIT, rounding=ROUND_DOWN)
            if bought <= ZERO:
                return f"the twin's costs exceed the amount spent on {trade.on.isoformat()}"
            found.append(Trade(mine, twin.security, "buy", trade.on, bought, bought * close, cost))
            held[mine] = held.get(mine, ZERO) + trade.quantity
            units[mine] = units.get(mine, ZERO) + bought
            continue
        before = held.get(mine, ZERO)
        if before <= ZERO or trade.quantity > before:
            return f"thesis {mine} sells more than it bought on {trade.on.isoformat()}"
        sold = (
            units[mine]
            if trade.quantity == before
            else (units[mine] * trade.quantity / before).quantize(TWIN_UNIT, rounding=ROUND_DOWN)
        )
        cost = twin.cost(sold, close, "sell", trade.on)
        if cost is None:
            return f"the cost model cannot price the twin's sale on {trade.on.isoformat()}"
        found.append(Trade(mine, twin.security, "sell", trade.on, sold, sold * close, cost))
        held[mine] = before - trade.quantity
        units[mine] -= sold
    return found


# ── an account after tax at liquidation ──


@dataclass(frozen=True, slots=True)
class Account:
    """An account's trades and what its tax rules need."""

    trades: tuple[Trade, ...]
    identities: Mapping[str, SecurityIdentity]
    policy: TaxPolicy


def profit(account: Account, on: date, value: UnitValue) -> Decimal | str:
    """The account's profit to ``on``'s end after the tax its lots would bear were everything
    still held sold that day at its mark; why not, when it cannot be computed."""
    trades = [t for t in account.trades if t.on <= on]
    missing = sorted({t.security for t in trades} - set(account.identities))
    if missing:
        return f"no tax facts for {', '.join(missing)}"
    engine = LotEngine(account.policy, account.identities.values())
    cash = ZERO
    try:
        for trade in sorted(trades, key=lambda t: (t.on, t.side != "buy")):
            cash += trade.cash
            if trade.side == "buy":
                engine.acquire(
                    trade.security,
                    on=trade.on,
                    quantity=trade.quantity,
                    cost=trade.value + trade.costs,
                )
            else:
                engine.dispose(
                    trade.security,
                    on=trade.on,
                    quantity=trade.quantity,
                    proceeds=max(ZERO, trade.value - trade.costs),
                )
        held = ZERO
        for security in engine.held():
            unit = value(security, on)
            if unit is None:
                return f"{security} has no mark within its age limit on {on.isoformat()}"
            units = engine.quantity(security)
            held += units * unit
            engine.dispose(security, on=on, quantity=units, proceeds=max(ZERO, units * unit))
        tax = ZERO
        for year in engine.ledger.years_with_activity():
            tax += engine.ledger.settle(year).tax_due
    except TaxError as exc:
        return f"the lots cannot account for it: {exc}"
    return cash + held - tax


def thesis_profits(trades: Sequence[Trade], on: date, value: UnitValue) -> dict[int, Decimal]:
    """Each thesis's profit before tax to ``on``: what its trades moved and what it still holds
    at its marks (a holding without a mark counts nothing)."""
    found: dict[int, Decimal] = {}
    held: dict[tuple[int, str], Decimal] = {}
    for trade in trades:
        if trade.on > on:
            continue
        found[trade.thesis_id] = found.get(trade.thesis_id, ZERO) + trade.cash
        key = (trade.thesis_id, trade.security)
        sign = ONE if trade.side == "buy" else -ONE
        held[key] = held.get(key, ZERO) + sign * trade.quantity
    for (thesis, security), units in held.items():
        unit = value(security, on) if units > ZERO else None
        if unit is not None:
            found[thesis] += units * unit
    return found


def closed_theses(trades: Sequence[Trade], on: date) -> set[int]:
    """The theses whose positions were bought and are all sold by ``on``."""
    held: dict[int, Decimal] = {}
    bought: set[int] = set()
    for trade in trades:
        if trade.on > on:
            continue
        sign = ONE if trade.side == "buy" else -ONE
        held[trade.thesis_id] = held.get(trade.thesis_id, ZERO) + sign * trade.quantity
        if trade.side == "buy":
            bought.add(trade.thesis_id)
    return {thesis for thesis in bought if held[thesis] <= ZERO}


# ── the monthly series ──


@dataclass(frozen=True, slots=True)
class MonthX:
    """One month of the record: ``x`` is ``None`` and ``why`` says why when it cannot be
    measured."""

    month: str
    x: float | None
    capital: Decimal | None
    why: str | None = None


def series(
    months: Sequence[str],
    book: Account,
    twin: Account,
    *,
    capital: Callable[[str], Decimal | None],
    value: UnitValue,
    reference: str,
) -> list[MonthX]:
    """``x_m`` for each of ``months`` (consecutive, the first counted whole from nothing)."""
    found: list[MonthX] = []
    before: tuple[Decimal, Decimal] | None = (ZERO, ZERO)
    for month in months:
        end = month_end(month)
        k = capital(month)
        why: str | None = None
        if value(reference, end) is None:
            why = f"the reference has no mark within its age limit at {end.isoformat()}"
        mine = profit(book, end, value)
        theirs = profit(twin, end, value)
        if isinstance(mine, str):
            why = why or f"the book: {mine}"
        if isinstance(theirs, str):
            why = why or f"the twin: {theirs}"
        if why is not None or isinstance(mine, str) or isinstance(theirs, str):
            found.append(MonthX(month, None, k, why))
            before = None
            continue
        if before is None:
            found.append(MonthX(month, None, k, "the month before it cannot be measured"))
            before = (mine, theirs)
            continue
        change_book, change_twin = mine - before[0], theirs - before[1]
        before = (mine, theirs)
        if change_book == ZERO and change_twin == ZERO:
            found.append(MonthX(month, 0.0, k))
            continue
        if k is None or k <= ZERO:
            found.append(MonthX(month, None, k, "no capital of the book is recorded before it"))
            continue
        p, q = change_book / k, change_twin / k
        if p <= -ONE or q <= -ONE:
            found.append(MonthX(month, None, k, "a loss of the whole capital in the month"))
            continue
        found.append(MonthX(month, math.log1p(float(p)) - math.log1p(float(q)), k))
    return found


# ── a look ──


@dataclass(frozen=True, slots=True)
class LookRules:
    """H13's numbers for one look (the locked registration's)."""

    theta: float
    looks: int
    step_alpha: float
    back_alpha: float
    min_closed_first: int
    min_closed_between: int
    min_closed_back: int


@dataclass(frozen=True, slots=True)
class LookOutcome:
    """What one look found and decided."""

    look: int
    months: int
    closed: int
    xs: tuple[MonthX, ...]
    mean: float | None = None
    sd: float | None = None
    t_up: float | None = None
    t_down: float | None = None
    c_up: float | None = None
    c_down: float | None = None
    without_best: float | None = None
    step: bool = False
    back: bool = False
    evaluable: bool = True
    detail: str = ""
    notes: list[str] = field(default_factory=list)


def critical(alpha: float, looks: int, months: int) -> float:
    """``c(alpha / looks, n)``: the ``1 - alpha/looks`` quantile of Student's t, ``n - 1`` df."""
    return student_t_quantile(1 - alpha / looks, months - 1)


def judge(
    look: int,
    xs: Sequence[MonthX],
    *,
    rules: LookRules,
    closed: int,
    can_step: bool,
    can_back: bool,
    closed_at_last_step: int | None,
    without_best: Sequence[MonthX] | None,
) -> LookOutcome:
    """A look at ``xs``: its statistics, a step (when ``can_step``) and a return to shadow
    (when ``can_back``). ``closed_at_last_step`` is the count of closed theses at the previous
    step (``None``: no step yet)."""
    n = len(xs)
    missing = [m for m in xs if m.x is None]
    if missing:
        first = missing[0]
        return LookOutcome(
            look,
            n,
            closed,
            tuple(xs),
            evaluable=False,
            detail=f"not evaluable: {first.month}: {first.why}",
        )
    values = [m.x for m in xs if m.x is not None]
    if n < 2:
        return LookOutcome(
            look, n, closed, tuple(xs), evaluable=False, detail="not evaluable: one month"
        )
    mean, sd = fmean(values), stdev(values)
    c_up = critical(rules.step_alpha, rules.looks, n)
    c_down = critical(rules.back_alpha, rules.looks, n)
    if sd == 0:
        return LookOutcome(
            look,
            n,
            closed,
            tuple(xs),
            mean=mean,
            sd=sd,
            c_up=c_up,
            c_down=c_down,
            detail="x does not vary: no statistic, nothing decided",
        )
    scale = sd / math.sqrt(n)
    t_up = (mean - rules.theta / MONTHS_PER_YEAR) / scale
    t_down = mean / scale
    best_mean: float | None = None
    if without_best is not None and all(m.x is not None for m in without_best):
        best_mean = fmean([m.x for m in without_best if m.x is not None])
    notes: list[str] = []
    step = False
    if can_step and t_up >= c_up:
        needed = (
            rules.min_closed_first
            if closed_at_last_step is None
            else closed_at_last_step + rules.min_closed_between
        )
        if closed < needed:
            notes.append(f"{closed} theses closed, {needed} needed for a step")
        elif best_mean is None:
            notes.append("x without the best thesis cannot be measured")
        elif best_mean <= 0:
            notes.append(f"x without the best thesis is {best_mean:.5f}, not above zero")
        else:
            step = True
    back = can_back and t_down <= -c_down and closed >= rules.min_closed_back
    if can_back and t_down <= -c_down and not back:
        notes.append(f"{closed} theses closed, {rules.min_closed_back} needed to go back")
    verdict = "a step" if step else "back to shadow" if back else "no change"
    detail = (
        f"look {look} at {n} months: mean {mean:.5f}, sd {sd:.5f}, t_up {t_up:.3f} against "
        f"{c_up:.3f}, t_down {t_down:.3f} against {-c_down:.3f}, {closed} closed: {verdict}"
    )
    if notes:
        detail += f" ({'; '.join(notes)})"
    return LookOutcome(
        look,
        n,
        closed,
        tuple(xs),
        mean=mean,
        sd=sd,
        t_up=t_up,
        t_down=t_down,
        c_up=c_up,
        c_down=c_down,
        without_best=best_mean,
        step=step,
        back=back,
        detail=detail,
        notes=notes,
    )
