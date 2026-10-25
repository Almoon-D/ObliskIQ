"""Month-end closes in the base currency, from a vehicle's daily closes (MASTER_PLAN §5.4).

The tilt reads one close per month end: the latest session's on or before it, not older than
``max_age_days``, converted at the rate of that session's day. A month without such a close ends
the reading: a signal over a gap is not computed, and the role's tilt is unavailable (the research
simulator ends its run the same way). The research panel and the account's stored bars both call
this, so a close is dated, aged and converted the same way in a test and in the account.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
from bisect import bisect_right
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal

from ...errors import TrendUnavailableError

#: Months in a year, for stepping from the first month to the last.
MONTHS_PER_YEAR = 12


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def month_end_closes(
    daily: Sequence[tuple[date, float]],
    convert: Callable[[date, Decimal], Decimal | None],
    *,
    through: date,
    max_age_days: int,
    name: str,
) -> tuple[tuple[date, float], ...]:
    """The base-currency close at each month end from the first session's month to ``through``'s
    (a month end), oldest first. ``daily`` is (session, close in the listing currency) oldest
    first; ``convert`` turns a close of a session into the base currency or says it cannot."""
    if not daily:
        return ()
    ordinals = [day.toordinal() for day, _ in daily]
    first = daily[0][0]
    last = (through.year - first.year) * MONTHS_PER_YEAR + through.month - first.month
    out: list[tuple[date, float]] = []
    for offset in range(last + 1):
        year, month = divmod(
            first.year * MONTHS_PER_YEAR + first.month - 1 + offset, MONTHS_PER_YEAR
        )
        on = month_end(year, month + 1)
        position = bisect_right(ordinals, on.toordinal()) - 1
        found = daily[position] if position >= 0 else None
        if found is None or on - found[0] > timedelta(days=max_age_days):
            raise TrendUnavailableError(
                f"{name} has no month-end close on {on} within the allowed age"
            )
        converted = convert(found[0], Decimal(repr(found[1])))
        if converted is None or converted <= 0:
            raise TrendUnavailableError(
                f"{name} has no month-end close on {on} within the allowed age"
            )
        out.append((on, float(converted)))
    return tuple(out)
