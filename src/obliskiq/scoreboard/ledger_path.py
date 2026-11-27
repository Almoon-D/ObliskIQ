"""A ledger call's path and its grade (H14, ``scoreboard.ledger``): where its closes took it, the
round trip's cost and its excess over the reference. Read by the forward ledger and by the
replay's grading (H15), which reach nothing of the account through it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Final, Literal

from ..domain.enums import AssetClass
from ..domain.market import Bar
from ..quant.costs import CostBook
from ..quant.costs import OrderTerms as CostTerms
from ..store.ledger import IdeaCall
from ..store.scoreboard import Call
from .grading import GradedCall, iso_week

type Outcome = Literal["target", "invalidation", "horizon"]

OPEN: Final = "open: neither level crossed and its horizon not passed"
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class CallPath:
    """Where a call's closes took it: ``outcome`` and the dates it started and resolved on, or why
    it has neither (``reason``: open, or not evaluable)."""

    outcome: Outcome | None
    start: date | None
    end: date | None
    reason: str | None
    start_close: Decimal | None = None
    end_close: Decimal | None = None

    @property
    def open(self) -> bool:
        return self.reason == OPEN


def call_path(call: IdeaCall, bars: Sequence[Bar], *, as_of: date, max_age_days: int) -> CallPath:
    """The closes after the day ``call`` was written, up to ``as_of`` and its horizon, read
    against its own levels. ``bars`` are the instrument's, oldest first."""
    written = call.known_at.date()
    age = timedelta(days=max_age_days)
    after = [bar for bar in bars if written < bar.session_date <= min(as_of, call.horizon)]
    if not after:
        if as_of < written + age and as_of < call.horizon:
            return CallPath(None, None, None, OPEN)
        return CallPath(
            None, None, None, f"no close within {max_age_days} days after it was written"
        )
    first = after[0]
    if first.session_date - written > age:
        return CallPath(
            None, None, None, f"no close within {max_age_days} days after it was written"
        )
    sign = Decimal(1) if call.rises else Decimal(-1)
    start = Decimal(repr(first.close))
    previous = first.session_date
    for bar in after:
        if bar.session_date - previous > age:
            return CallPath(
                None,
                first.session_date,
                None,
                f"no close between {previous.isoformat()} and {bar.session_date.isoformat()}: "
                "a level may have been crossed unseen",
            )
        previous = bar.session_date
        close = Decimal(repr(bar.close))
        crossed: Outcome | None = None
        if sign * (close - call.target) >= 0:
            crossed = "target"
        elif sign * (close - call.invalidation) <= 0:
            crossed = "invalidation"
        if crossed is not None:
            return CallPath(crossed, first.session_date, bar.session_date, None, start, close)
    if call.horizon > as_of:
        return CallPath(None, first.session_date, None, OPEN, start)
    last = after[-1]
    if call.horizon - last.session_date > age:
        return CallPath(
            None,
            first.session_date,
            None,
            f"no close within {max_age_days} days before its horizon {call.horizon.isoformat()}",
        )
    return CallPath(
        "horizon", first.session_date, last.session_date, None, start, Decimal(repr(last.close))
    )


# ── the round trip's cost ──


type CostOf = Callable[[IdeaCall, CallPath], float | str]


@dataclass(frozen=True, slots=True)
class Leg:
    """One leg of a round trip: its day, its close and the name's average traded value then."""

    day: date
    close: Decimal
    average_daily_value: Decimal | None


def round_trip(
    costs: CostBook,
    *,
    symbol: str,
    venue: str,
    asset_class: AssetClass,
    currency: str,
    trade_value: Decimal,
    rate_to_base: Decimal,
    buy: Leg,
    sell: Leg,
) -> float | str:
    """The round trip's cost as a share of its trade: two legs of the cost model's spread and
    slippage and of its commission, on a trade of ``trade_value`` in the base currency (a unit of
    ``currency`` is ``rate_to_base`` of it); or why it cannot be priced."""
    units = (trade_value / (buy.close * rate_to_base)).to_integral_value(rounding=ROUND_FLOOR)
    units = max(units, Decimal(1))
    share = ZERO
    for side, leg in (("buy", buy), ("sell", sell)):
        found = costs.leg_cost(
            CostTerms(
                venue=venue,
                asset_class=asset_class,
                currency=currency,
                quantity=units,
                price=leg.close,
            ),
            side="buy" if side == "buy" else "sell",
            on=leg.day,
            average_daily_value=leg.average_daily_value,
        )
        commission = found.commission.amount
        if (
            found.spread_cost is None
            or found.slippage_cost is None
            or commission is None
            or found.notional is None
            or found.notional <= ZERO
        ):
            gaps = ", ".join(gap.value for gap in found.gaps) or "a component without a price"
            return f"the cost model cannot price {symbol}'s round trip ({gaps})"
        share += (found.spread_cost + found.slippage_cost + commission) / found.notional
    return float(share)


# ── grading ──


@dataclass(frozen=True, slots=True)
class Graded:
    """A call, where its closes took it, and its graded form (``excess`` ``None`` while open or
    when not evaluable)."""

    call: IdeaCall
    path: CallPath
    graded: GradedCall

    @property
    def resolved(self) -> bool:
        return self.graded.graded

    @property
    def held_days(self) -> int | None:
        if self.path.start is None or self.path.end is None:
            return None
        return (self.path.end - self.path.start).days


def grade(
    call: IdeaCall,
    found: CallPath,
    *,
    price: Callable[[int, date], Decimal | None],
    reference: int,
    cost: CostOf,
) -> Graded:
    """``call`` graded from its path: the signed excess log return in the base currency less the
    round trip's cost, and whether its target came first."""
    as_call = Call(
        layer=call.segment,
        conid=call.conid,
        direction="up" if call.rises else "down",
        called_at=call.known_at,
        detail=f"thesis {call.thesis_id}",
        probability=call.probability,
    )
    week = iso_week(call.known_at)

    def unresolved(reason: str) -> Graded:
        return Graded(call, found, GradedCall(as_call, week, None, reason))

    if found.outcome is None or found.start is None or found.end is None:
        return unresolved(found.reason or OPEN)
    p0, p1 = price(call.conid, found.start), price(call.conid, found.end)
    r0, r1 = price(reference, found.start), price(reference, found.end)
    if p0 is None or p1 is None:
        return unresolved(f"{call.symbol} has no mark in the base currency")
    if r0 is None or r1 is None:
        return unresolved("no mark of the equity reference within the age allowed")
    charged = cost(call, found)
    if isinstance(charged, str):
        return unresolved(charged)
    sign = 1.0 if call.rises else -1.0
    excess = sign * (math.log(p1 / p0) - math.log(r1 / r0)) - charged
    return Graded(
        call, found, GradedCall(as_call, week, excess, None, hit=found.outcome == "target")
    )
