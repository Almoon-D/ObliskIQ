"""The time-weighted return over the NAV series (MASTER_PLAN §5.15: flows never look like profit
or loss).

The account's NAV readings (``nav_history``, one a day) split time into periods; each period's
return neutralises the external flows that fell in it, and the periods chain:
``TWR = prod(1 + r_i) - 1``. ``capital.twr.flow_timing`` says where a flow dated on a reading's
day belongs:

* ``in_reading``: the reading already holds it, so it ends the period it closes,
  ``r = (V1 - F) / V0 - 1``;
* ``after_reading``: it came after the reading, so it starts the next period,
  ``r = V1 / (V0 + F) - 1``.

External flows are the owner's deposits and withdrawals that reached the account (matched to its
cash): dividends, interest and fees are the book's own return, and a paper account's flows never
reach its NAV. A period that cannot be measured (a non-positive base, a flow with no rate) makes
the whole return unknown, never a partial number.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from ..config.schema import ObliskConfig
from ..domain.capital import CashFlow, Deposit, Withdrawal
from ..store import capital as capital_repo
from ..store import fx as fx_repo
from ..store import nav as nav_repo
from ..store.db import Store
from ..store.worker import StoreWorker

ZERO = Decimal(0)

type FlowTiming = Literal["in_reading", "after_reading"]


@dataclass(frozen=True, slots=True)
class Reading:
    """The NAV, in the base currency, read on ``on``."""

    on: date
    value: Decimal


@dataclass(frozen=True, slots=True)
class ExternalFlow:
    """An owner's flow into (positive) or out of (negative) the account, in the base currency."""

    on: date
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date
    start_value: Decimal
    end_value: Decimal
    flow: Decimal
    ret: Decimal


@dataclass(frozen=True, slots=True)
class TwrResult:
    """The chained return over ``periods``; ``value`` is ``None`` with the reason when a period
    cannot be measured or there is no period."""

    periods: tuple[Period, ...]
    value: Decimal | None
    reason: str | None

    @property
    def since(self) -> date | None:
        return self.periods[0].start if self.periods else None

    @property
    def until(self) -> date | None:
        return self.periods[-1].end if self.periods else None


def time_weighted_return(
    readings: Sequence[Reading], flows: Sequence[ExternalFlow], *, timing: FlowTiming
) -> TwrResult:
    """Chain each period's flow-neutral return over the readings, oldest first."""
    ordered = sorted(readings, key=lambda reading: reading.on)
    if len(ordered) < 2:
        return TwrResult((), None, "fewer than two NAV readings")
    periods: list[Period] = []
    growth = Decimal(1)
    for before, after in pairwise(ordered):
        if timing == "in_reading":
            flow = sum((f.amount for f in flows if before.on < f.on <= after.on), ZERO)
            base, end = before.value, after.value - flow
        else:
            flow = sum((f.amount for f in flows if before.on <= f.on < after.on), ZERO)
            base, end = before.value + flow, after.value
        if base <= ZERO:
            return TwrResult(
                tuple(periods),
                None,
                f"the period from {before.on.isoformat()} has no positive base to measure from",
            )
        ret = end / base - 1
        growth *= 1 + ret
        periods.append(Period(before.on, after.on, before.value, after.value, flow, ret))
    return TwrResult(tuple(periods), growth - 1, None)


def account_flows(
    flows: Sequence[CashFlow], *, base: str, rate: Callable[[str, date], Decimal | None]
) -> list[ExternalFlow] | str:
    """The owner's deposits and withdrawals that reached the account, in the base currency at the
    rate of their day; why, when one has no rate."""
    found: list[ExternalFlow] = []
    for flow in flows:
        if not isinstance(flow, Deposit | Withdrawal) or not flow.live or flow.matched_at is None:
            continue
        fx = Decimal(1) if flow.currency == base else rate(flow.currency, flow.on)
        if fx is None:
            return f"no {flow.currency} rate was recorded on {flow.on.isoformat()}"
        found.append(ExternalFlow(flow.on, flow.signed * fx))
    return found


async def account_twr(
    worker: StoreWorker, settings: ObliskConfig, *, since: date, as_of: date
) -> TwrResult:
    """The account's time-weighted return from ``since`` to ``as_of`` over its NAV readings."""

    def read(store: Store) -> TwrResult:
        readings = [
            Reading(r.as_of, r.nav_base)
            for r in nav_repo.nav_series(store, since=since, as_of=as_of)
        ]

        def rate(currency: str, on: date) -> Decimal | None:
            found = fx_repo.rate_on(store, currency, on)
            return None if found is None else found.rate_to_base

        flows = account_flows(
            capital_repo.cash_flows(store, as_of=as_of),
            base=settings.account.base_currency,
            rate=rate,
        )
        if isinstance(flows, str):
            return TwrResult((), None, flows)
        return time_weighted_return(readings, flows, timing=settings.capital.twr.flow_timing)

    return await worker.run(read)
