"""The trend tilt's history on the live book: month-end closes in the base currency, read from
the market store (MASTER_PLAN §5.4, §5.7).

**Monthly evaluation.** The tilt is evaluated on the last completed month end before the day
(:func:`last_month_end`), as Session 4c evaluated it at each month end: the state holds for the
month, and the daily cycles check only the bands.

**A month-end close** is a vehicle's last stored daily bar on or before the month's last day,
at most ``research.simulation.max_price_age_days`` older than it, converted at the rate of its own
session (the macro board's FX series, at most ``research.data.max_fx_age_days`` older, released by
the evaluation date): the closes and rates the tested core read, under the same freshness. A month
without one is a gap. The research harness stops at a gap; the live engine reads the run of
months after the latest gap, so a missed month costs history, never a guess across it.

**The trend** of a vehicle is ``quant.trend`` over the month ends it shares with the cash
vehicle, replayed from the first one held. A hysteresis state replayed from a shorter history can
differ from one replayed from a longer only while no measure after the first has crossed the
buffer; the state then rests on the first evaluable month alone, and :meth:`HistoryView.settled`
says so. Bars are the listing's closes: an accumulating fund's close is its total return, a
distributing one's falls by each distribution.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from ...config.schema import ObliskConfig
from ...quant.trend import TrendRule, TrendState, settled, trend_state
from ...store import repos
from ...store.db import Store
from ..fx.rates import BaseRates

#: Months in a year, for stepping through month ends.
MONTHS_PER_YEAR: Final = 12


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def last_month_end(day: date) -> date:
    """The last month end strictly before ``day``: the month the tilt is evaluated on."""
    first = day.replace(day=1)
    return first - timedelta(days=1)


def _next_month(day: date) -> date:
    year, month = divmod(day.year * MONTHS_PER_YEAR + day.month, MONTHS_PER_YEAR)
    return month_end(year, month + 1)


@dataclass(frozen=True, slots=True)
class VehicleHistory:
    """What to read for one vehicle: its conid and listing currency."""

    conid: int
    currency: str


@dataclass(frozen=True, slots=True)
class MonthEnds:
    """A vehicle's month-end closes in the base currency, oldest first: the run of months after
    the latest gap through the evaluation month. ``note`` says why the run is short or empty."""

    key: str
    closes: tuple[tuple[date, float], ...]
    note: str | None


def read_month_ends(
    store: Store,
    settings: ObliskConfig,
    vehicles: Mapping[str, VehicleHistory],
    *,
    as_of: date,
) -> dict[str, MonthEnds]:
    """Each vehicle's month-end closes through ``as_of`` (a month end), by key."""
    rates = BaseRates(store, settings, as_of=as_of)
    price_age = timedelta(days=settings.research.simulation.max_price_age_days)
    fx_age = settings.research.data.max_fx_age_days
    out: dict[str, MonthEnds] = {}
    for key, vehicle in vehicles.items():
        bars = repos.bar_series(store, vehicle.conid, as_of=as_of)
        if not bars:
            out[key] = MonthEnds(key, (), f"conid {vehicle.conid} has no stored daily bar")
            continue
        closes: list[tuple[date, float]] = []
        note: str | None = None
        index = 0
        end = month_end(bars[0].session_date.year, bars[0].session_date.month)
        while end <= as_of:
            while index + 1 < len(bars) and bars[index + 1].session_date <= end:
                index += 1
            bar = bars[index]
            gap: str | None = None
            if bar.session_date > end or end - bar.session_date > price_age:
                gap = f"no close of conid {vehicle.conid} within {price_age.days} days of {end}"
            else:
                rate = rates.per_base_on(vehicle.currency, bar.session_date, max_age_days=fx_age)
                if rate.value is None or bar.close <= 0:
                    gap = rate.reason if rate.value is None else f"a close of 0 on {end}"
                else:
                    value = Decimal(repr(bar.close)) / rate.value
                    closes.append((end, float(value)))
            if gap is not None:
                closes.clear()
                note = f"the run starts after {end}: {gap}"
            end = _next_month(end)
        out[key] = MonthEnds(key, tuple(closes), note)
    return out


@dataclass
class HistoryView:
    """The trend as of a month end, over the month-end closes read from the store (a
    ``TrendView`` for the core's rules). ``aliases`` reads a key's trend on another vehicle of
    the same exposure (the role's vehicle with the history)."""

    history: Mapping[str, MonthEnds]
    month: date
    aliases: Mapping[str, str] = field(default_factory=dict)
    _trends: dict[tuple[str, str, TrendRule], TrendState | None] = field(default_factory=dict)

    @property
    def as_of(self) -> date:
        return self.month

    def closes(self, key: str) -> MonthEnds:
        found = self.history.get(self.aliases.get(key, key))
        return found if found is not None else MonthEnds(key, (), f"{key} was not read")

    def _shared(self, key: str, cash: str) -> tuple[list[float], list[float]]:
        bills = dict(self.closes(cash).closes)
        pairs = [(day, value) for day, value in self.closes(key).closes if day in bills]
        return [value for _, value in pairs], [bills[day] for day, _ in pairs]

    def trend(self, key: str, cash: str, rule: TrendRule) -> TrendState | None:
        memo = (key, cash, rule)
        if memo not in self._trends:
            closes, bills = self._shared(key, cash)
            self._trends[memo] = trend_state(closes, bills, rule)
        return self._trends[memo]

    def months(self, key: str, cash: str) -> int:
        """How many month ends ``key`` shares with ``cash``."""
        return len(self._shared(key, cash)[0])

    def settled(self, key: str, cash: str, rule: TrendRule) -> bool:
        """Whether ``key``'s trend state would be the same on a longer history (the module
        docstring)."""
        return settled(*self._shared(key, cash), rule)
