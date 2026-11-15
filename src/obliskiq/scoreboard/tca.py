"""Transaction-cost analysis: arrival against fill against the cost model (MASTER_PLAN §5.19).

For each fill of the bot's own intents: the **arrival** price is the reference of the quote its
placement was cleared against (the mid when both sides were there and not crossed, else the last;
``QuoteEvidence.reference``); the **realised** cost is the side-signed distance from arrival to the
fill, as a fraction of arrival, plus the commission over the fill's value; the **modelled** cost is
the cost model's spread and slippage for the same leg, plus the same commission. The difference,
realised less modelled, is grouped by venue, order type and the arrival quote's status, with its
mean and a 95 % interval over the fills. A fill without an arrival quote, or whose leg the cost
model cannot price, is counted beside the groups and never filled in.

Paper fills are the broker's simulation: they measure the path, not the market's real costs,
which are first measured on the live account (MASTER_PLAN §5.19).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import NormalDist

from ..domain.orders import ExecutionMode, OrderSide

#: The two-sided level of each group's interval.
INTERVAL_LEVEL = 0.95
_NORMAL = NormalDist()
#: A group's interval needs at least this many fills.
MIN_FILLS_FOR_INTERVAL = 2


@dataclass(frozen=True, slots=True)
class Execution:
    """A fill with what it is measured against. ``arrival`` is ``None`` without a quote;
    ``modelled`` (a fraction of the fill's value: spread and slippage) is ``None`` where the cost
    model has no figure."""

    executed_at: datetime
    mode: ExecutionMode
    venue: str
    order_type: str
    quote_status: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    commission: Decimal | None
    arrival: Decimal | None
    modelled: Decimal | None


@dataclass(frozen=True, slots=True)
class Measured:
    """One execution's costs as fractions of its value: realised, modelled, and the difference."""

    execution: Execution
    realised: float
    modelled: float

    @property
    def excess(self) -> float:
        return self.realised - self.modelled


def measure(execution: Execution) -> Measured | str:
    """The costs of one execution, or why it cannot be measured."""
    if execution.arrival is None or execution.arrival <= 0:
        return "no arrival quote"
    if execution.modelled is None:
        return "the cost model has no figure for its leg"
    sign = 1 if execution.side is OrderSide.BUY else -1
    value = execution.price * execution.quantity
    commission = float(execution.commission / value) if execution.commission and value > 0 else 0.0
    slippage = float(sign * (execution.price - execution.arrival) / execution.arrival)
    return Measured(execution, slippage + commission, float(execution.modelled) + commission)


@dataclass(frozen=True, slots=True)
class Group:
    """The executions of one venue, order type and quote status."""

    venue: str
    order_type: str
    quote_status: str
    fills: int
    realised: float
    modelled: float
    excess: float
    interval: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class TcaReport:
    """Every measured group, the overall mean excess with its interval, and what was not
    measured (count by reason)."""

    groups: tuple[Group, ...]
    fills: int
    excess: float | None
    interval: tuple[float, float] | None
    unmeasured: dict[str, int]


def _interval(values: Sequence[float]) -> tuple[float, float] | None:
    n = len(values)
    if n < MIN_FILLS_FOR_INTERVAL:
        return None
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    half = _NORMAL.inv_cdf(1 - (1 - INTERVAL_LEVEL) / 2) * sd / math.sqrt(n)
    return mean - half, mean + half


def analyse(executions: Iterable[Execution]) -> TcaReport:
    """Group the measurable executions by venue, order type and quote status."""
    measured: list[Measured] = []
    unmeasured: dict[str, int] = {}
    for execution in executions:
        found = measure(execution)
        if isinstance(found, str):
            unmeasured[found] = unmeasured.get(found, 0) + 1
        else:
            measured.append(found)
    keyed: dict[tuple[str, str, str], list[Measured]] = {}
    for item in measured:
        e = item.execution
        keyed.setdefault((e.venue, e.order_type, e.quote_status), []).append(item)
    groups = tuple(
        Group(
            venue=venue,
            order_type=order_type,
            quote_status=status,
            fills=len(items),
            realised=sum(i.realised for i in items) / len(items),
            modelled=sum(i.modelled for i in items) / len(items),
            excess=sum(i.excess for i in items) / len(items),
            interval=_interval([i.excess for i in items]),
        )
        for (venue, order_type, status), items in sorted(keyed.items())
    )
    excess = [i.excess for i in measured]
    return TcaReport(
        groups=groups,
        fills=len(measured),
        excess=sum(excess) / len(excess) if excess else None,
        interval=_interval(excess),
        unmeasured=unmeasured,
    )
