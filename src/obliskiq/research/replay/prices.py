"""The replay's prices: a name's return over a call's horizon and each cohort's universe (H15).

**The horizon.** A call on a sheet dated ``D`` starts at the first session of the registered
venue after ``D`` (``s0``) and ends ``horizon_sessions`` sessions later (``s1``). A name's return
over it is its total return (Yahoo's adjusted closes, chained session by session) from its close
nearest after ``s0`` to its close nearest before ``s1``, each within ``max_mark_age_days``, with
no two closes between further apart than that and no session whose return is unknown; otherwise
the name is **not evaluable**, with the reason.

**A cohort** is the sheet date's universe (the companies filing in the registered months before
it, ``sources.universe_at``), each by the ticker the SEC's records give it: its evaluable names'
returns, the reasons of the others, and the benchmark, their equally weighted gross return. A
cohort whose not-evaluable share passes the registered limit has no benchmark: it is not
evaluable whole.

Prices are read from the replay's own store as loaded; what the grading reads is after the sheet
date by design (a call's outcome), never on the sheet.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import bisect
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

import exchange_calendars as xc

from ...domain.market import Bar
from ...store.db import Store
from ...store.replay import StoredCohort, stock_bar_rows
from ...store.replay import names as replay_names
from .sources import universe_at

NO_TICKER: Final = "no ticker in the SEC's records"
NO_HISTORY: Final = "no close at Yahoo"
DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


class Sessions:
    """The registered venue's sessions over the replay's span."""

    def __init__(self, venue: str, first: date, last: date) -> None:
        calendar = xc.get_calendar(venue, start=first.isoformat(), end=last.isoformat())
        # The calendar's own sessions: its bounds are sessions, so a holiday at either end of
        # the span is not one to ask for.
        self._days = [stamp.date() for stamp in calendar.sessions]

    def horizon(self, sheet: date, sessions: int) -> tuple[date, date] | None:
        """The first session after ``sheet`` and the one ``sessions`` later; ``None`` past the
        calendar's end."""
        start = bisect.bisect_right(self._days, sheet)
        end = start + sessions
        if end >= len(self._days):
            return None
        return self._days[start], self._days[end]


@dataclass(frozen=True, slots=True)
class History:
    """One name's sessions, closes and log returns from the session before (``nan``: unknown)."""

    sessions: tuple[date, ...]
    closes: tuple[float, ...]
    log_changes: tuple[float, ...]

    def growth(self, start: date, end: date, *, max_age_days: int) -> float | str:
        """The gross total return from the close nearest after ``start`` to the one nearest
        before ``end``; or why there is none."""
        age = timedelta(days=max_age_days)
        first = bisect.bisect_left(self.sessions, start)
        last = bisect.bisect_right(self.sessions, end) - 1
        if first >= len(self.sessions) or self.sessions[first] - start > age:
            return f"no close within {max_age_days} days after {start.isoformat()}"
        if last < first or end - self.sessions[last] > age:
            return f"no close within {max_age_days} days before {end.isoformat()}"
        total = 0.0
        for i in range(first + 1, last + 1):
            if self.sessions[i] - self.sessions[i - 1] > age:
                return (
                    f"no close between {self.sessions[i - 1].isoformat()} and "
                    f"{self.sessions[i].isoformat()}"
                )
            if math.isnan(self.log_changes[i]):
                return f"its return on {self.sessions[i].isoformat()} is unknown"
            total += self.log_changes[i]
        return math.exp(total)

    def bars(self, conid: int, *, after: date, until: date) -> tuple[Bar, ...]:
        """Its closes after ``after`` up to ``until``, as bars (the ledger's path reads them)."""
        first = bisect.bisect_right(self.sessions, after)
        last = bisect.bisect_right(self.sessions, until)
        return tuple(
            Bar(
                conid=conid,
                session_date=self.sessions[i],
                open=None,
                high=None,
                low=None,
                close=self.closes[i],
                volume=None,
                source="replay",
            )
            for i in range(first, last)
        )


def stock_histories(store: Store) -> dict[str, History]:
    """Every name's history in the replay's store."""
    found: dict[str, History] = {}
    current: str | None = None
    sessions: list[date] = []
    closes: list[float] = []
    changes: list[float] = []

    def keep() -> None:
        if current is not None:
            found[current] = History(tuple(sessions), tuple(closes), tuple(changes))

    for symbol, session, close, change in stock_bar_rows(store):
        if symbol != current:
            keep()
            current, sessions, closes, changes = symbol, [], [], []
        sessions.append(date.fromisoformat(session))
        closes.append(float(close))
        changes.append(math.nan if change is None else math.log1p(float(change)))
    keep()
    return found


@dataclass(frozen=True, slots=True)
class Cohort:
    """A sheet date's universe over its horizon (``benchmark`` ``None``: not evaluable whole)."""

    sheet: date
    start: date
    end: date
    universe: int
    growth: Mapping[str, float]
    reasons: Counter[str] = field(default_factory=Counter)
    benchmark: float | None = None

    @property
    def not_evaluable(self) -> int:
        return sum(self.reasons.values())

    @property
    def not_evaluable_share(self) -> float:
        return self.not_evaluable / self.universe if self.universe else 1.0


def cohort(
    store: Store,
    histories: Mapping[str, History],
    *,
    sheet: date,
    horizon: tuple[date, date],
    lookback_months: int,
    max_age_days: int,
    limit: Decimal,
) -> Cohort:
    """The cohort of ``sheet``: each universe name's return over ``horizon`` or why none, and
    the benchmark when the not-evaluable share is within ``limit``."""
    tickers = replay_names(store)
    universe = universe_at(store, sheet, lookback_months=lookback_months)
    start, end = horizon
    growth: dict[str, float] = {}
    reasons: Counter[str] = Counter()
    for cik in universe:
        name = tickers.get(cik)
        if name is None or name.ticker is None:
            reasons[NO_TICKER] += 1
            continue
        history = histories.get(name.ticker)
        if history is None:
            reasons[NO_HISTORY] += 1
            continue
        found = history.growth(start, end, max_age_days=max_age_days)
        if isinstance(found, str):
            reasons[_kind(found)] += 1
            continue
        growth[name.ticker] = found
    share = sum(reasons.values()) / len(universe) if universe else 1.0
    benchmark = (
        sum(growth.values()) / len(growth) if growth and Decimal(repr(share)) <= limit else None
    )
    return Cohort(sheet, start, end, len(universe), growth, reasons, benchmark)


def _kind(reason: str) -> str:
    """A reason without its dates, so a cohort's reasons count by kind."""
    return DATE.sub("…", reason)


def every_cohort(
    store: Store,
    histories: Mapping[str, History],
    *,
    sheets: Sequence[date],
    sessions: Sessions,
    horizon_sessions: int,
    lookback_months: int,
    max_age_days: int,
    limit: Decimal,
    until: date,
) -> list[Cohort]:
    """The cohort of every sheet date whose horizon has closed by ``until``."""
    found = []
    for sheet in sheets:
        horizon = sessions.horizon(sheet, horizon_sessions)
        if horizon is None or horizon[1] > until:
            continue
        found.append(
            cohort(
                store,
                histories,
                sheet=sheet,
                horizon=horizon,
                lookback_months=lookback_months,
                max_age_days=max_age_days,
                limit=limit,
            )
        )
    return found


def stored(found: Cohort) -> StoredCohort:
    return StoredCohort(
        sheet_date=found.sheet,
        start=found.start,
        end=found.end,
        universe=found.universe,
        evaluable=len(found.growth),
        reasons=dict(found.reasons),
        benchmark=found.benchmark,
    )
