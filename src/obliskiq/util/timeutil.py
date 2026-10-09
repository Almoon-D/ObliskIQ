"""Time and date helpers.

Every stamp written to the store is UTC and second-resolution. A process running in one
time zone must not write a row that sorts differently from one written in another. Ported
from ObliskIQ-lite-MCP.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta

#: The stamp format of every stored timestamp.
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Date-only form.
DATE_FORMAT = "%Y-%m-%d"

#: IBKR serves expiry dates as a bare YYYYMMDD integer or string.
IBKR_DATE_FORMAT = "%Y%m%d"

#: IBKR bar timestamps are epoch milliseconds.
MILLISECONDS_PER_SECOND = 1000


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_stamp(moment: datetime | None = None) -> str:
    """Second-resolution UTC stamp."""
    return (moment or utc_now()).astimezone(UTC).strftime(STAMP_FORMAT)


def utc_today() -> date:
    return utc_now().date()


def iso_date(value: date) -> str:
    return value.strftime(DATE_FORMAT)


def parse_stamp(value: str) -> datetime | None:
    """Parse a stamp written by this package. ``None`` when it is unparseable.

    A stamp that cannot be read is treated as absent by every caller, which makes a
    corrupted row look stale rather than fresh — the safe direction.
    """
    try:
        return datetime.strptime(value, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError, TypeError:
        return None


def parse_date(value: str | None) -> date | None:
    """Parse ``YYYY-MM-DD``, or IBKR's ``YYYYMMDD``. ``None`` when unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in (DATE_FORMAT, IBKR_DATE_FORMAT):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Some endpoints append a time; take the leading date.
    head = text.split("T")[0].split(" ")[0]
    if head != text:
        return parse_date(head)
    return None


def epoch_ms_to_stamp(value: float) -> str:
    """IBKR bar timestamp (epoch ms) to a UTC stamp."""
    return utc_stamp(datetime.fromtimestamp(value / MILLISECONDS_PER_SECOND, tz=UTC))


def age_seconds(stamp: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds since ``stamp``. ``None`` when it is absent or unparseable."""
    if not stamp:
        return None
    parsed = parse_stamp(stamp)
    if parsed is None:
        return None
    return ((now or utc_now()) - parsed).total_seconds()


def years_before(reference: date, years: float, *, days_per_year: float) -> date:
    """``reference`` shifted back by ``years``, in whole days.

    ``days_per_year`` comes from config rather than being assumed, so a caller that means
    trading years and one that means calendar years cannot silently share an answer.
    """
    return reference - timedelta(days=round(years * days_per_year))
