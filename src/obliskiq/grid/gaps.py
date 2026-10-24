"""The overnight-gap distribution of an instrument: what a protective stop cannot stop.

A stop-limit becomes a limit when it triggers, and through a gap past its limit it does not fill,
so the loss at the stop is not the maximum loss (DECISIONS D67). Sessions 17 and 5 size and space
protective exits on a quantile of this distribution; here it is measured.

A gap is the log move from one session's close to the next session's open, over the daily window.
Gaps across a known earnings report (one whose time falls between that close and that open, on the
listing venue's calendar) are counted apart: the distribution is wider across earnings, and a
report whose window the calendar cannot place is counted as unplaced rather than guessed. A venue
that restates the previous close as its open publishes no opening print, so its "gaps" are zeros
by construction: above ``grid.garman_klass_max_restated_open_share`` the distribution is refused.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from ..calendars.service import Calendars
from ..errors import CalendarError
from .frames import BarFrame


@dataclass(frozen=True, slots=True)
class Gap:
    """One overnight gap and whether a known earnings report fell inside it (``None``: the
    calendar could not place the window)."""

    session: date
    value: float
    earnings: bool | None


def gaps(bars: BarFrame) -> list[tuple[date, date, float]]:
    """``(previous session, session, ln(open / previous close))`` for consecutive bars with a
    positive open and close."""
    out: list[tuple[date, date, float]] = []
    for index in range(1, len(bars)):
        opened = bars.open[index]
        previous = bars.close[index - 1]
        if opened is None or opened <= 0 or previous <= 0:
            continue
        out.append((bars.dates[index - 1], bars.dates[index], math.log(opened / previous)))
    return out


def restated_share(bars: BarFrame) -> float | None:
    """The share of opens that are exactly the previous close."""
    pairs = [
        (bars.close[index - 1], bars.open[index])
        for index in range(1, len(bars))
        if bars.open[index] is not None
    ]
    if not pairs:
        return None
    return sum(1 for previous, opened in pairs if opened == previous) / len(pairs)


def flag_earnings(
    raw: Sequence[tuple[date, date, float]],
    reports: Sequence[datetime],
    *,
    calendars: Calendars,
    venue: str,
) -> list[Gap]:
    """Each gap with its earnings flag: true when a report's time falls after the previous
    session's close and at or before this session's open."""
    out: list[Gap] = []
    for previous, session, value in raw:
        try:
            before = calendars.session_bounds(venue, previous)
            after = calendars.session_bounds(venue, session)
        except CalendarError:
            before = after = None
        if before is None or after is None:
            out.append(Gap(session, value, None if reports else False))
            continue
        flagged = any(before.close_utc < report <= after.open_utc for report in reports)
        out.append(Gap(session, value, flagged))
    return out


def quantiles(values: Sequence[float], levels: Sequence[float]) -> list[float]:
    """Empirical quantiles (linear interpolation between order statistics)."""
    return [float(q) for q in np.quantile(np.asarray(values, dtype=float), levels)]
