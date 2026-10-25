"""What the core's trend tilt reads: month-end closes in the base currency (MASTER_PLAN §5.4).

The tilt is evaluated monthly. :func:`aligned_trend` is the one place a role's closes and the cash
role's are matched month by month and handed to :func:`~obliskiq.quant.trend.trend_state`, whether
the closes come from the research panel (``research.simulate.Pricing.trend``) or from the account's
stored bars (:class:`MonthEnds`), so a signal read live is the signal the test read.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ...quant.trend import TrendRule, TrendState, trend_state

type MonthEndCloses = Sequence[tuple[date, float]]


def aligned_trend(
    closes: MonthEndCloses, cash: MonthEndCloses, rule: TrendRule
) -> TrendState | None:
    """The tilt of an asset over the month ends it shares with cash (``None``: the history is
    shorter than the rule needs). Both are (month end, base-currency close), oldest first."""
    bills = dict(cash)
    shared = [(day, value) for day, value in closes if day in bills]
    return trend_state([value for _, value in shared], [bills[day] for day, _ in shared], rule)


class CoreView(Protocol):
    """What a core strategy may see of the market on its evaluation date."""

    @property
    def as_of(self) -> date: ...

    def trend(self, key: str, cash: str, rule: TrendRule) -> TrendState | None: ...


@dataclass(frozen=True, slots=True)
class MonthEnds:
    """The month-end closes of each vehicle through ``as_of``, a month end: the tilt of the
    month, evaluated once and held until the next month end."""

    as_of: date
    closes: Mapping[str, MonthEndCloses]
    #: Why a vehicle has no closes (a month without a fresh one, a missing rate), by vehicle.
    gaps: Mapping[str, str] = field(default_factory=dict)

    def trend(self, key: str, cash: str, rule: TrendRule) -> TrendState | None:
        return aligned_trend(self.closes.get(key, ()), self.closes.get(cash, ()), rule)
