"""When the service's jobs run: four decision cycles and a digest a day, from the calendars.

Each job is a moment of one venue's regular session (``runtime.cycles``, ``runtime.digest``): its
open or close moved by ``offset_minutes``, read through the calendars, so daylight saving and
half-days follow the venue and a venue with no session that day has no job that day; the other
venues' jobs still run. Nothing here is a UTC constant (MASTER_PLAN §5.12).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..calendars.service import Calendars
from ..config.schema_runtime import Anchor, RuntimeConfig
from ..errors import CalendarError

CYCLE = "cycle"
DIGEST = "digest"

#: How many days around a moment are searched for the next job: a long weekend and a holiday
#: on every anchor venue at once still find one.
SEARCH_DAYS = 10


@dataclass(frozen=True, slots=True, order=True)
class Job:
    """One scheduled run: when, what, and the venue session it belongs to."""

    at: datetime
    kind: str
    name: str
    venue: str
    session_date: date

    @property
    def command(self) -> str:
        """The run log's name for it: ``cycle:<name>`` or ``digest``."""
        return f"{self.kind}:{self.name}" if self.kind == CYCLE else self.kind


def moment(
    calendars: Calendars, venue: str, anchor: Anchor, offset_minutes: int, day: date
) -> datetime | None:
    """The venue's open or close on ``day`` moved by ``offset_minutes``; ``None`` when the venue
    has no session then (or the calendar does not cover it)."""
    try:
        bounds = calendars.session_bounds(venue, day)
    except CalendarError:
        return None
    if bounds is None:
        return None
    base = bounds.open_utc if anchor == "open" else bounds.close_utc
    return base + timedelta(minutes=offset_minutes)


def jobs_on(calendars: Calendars, runtime: RuntimeConfig, day: date) -> list[Job]:
    """Every job of the sessions dated ``day``, in time order."""
    jobs = []
    for cycle in runtime.cycles:
        at = moment(calendars, cycle.venue, cycle.anchor, cycle.offset_minutes, day)
        if at is not None:
            jobs.append(Job(at, CYCLE, cycle.name, cycle.venue, day))
    digest = runtime.digest
    at = moment(calendars, digest.venue, digest.anchor, digest.offset_minutes, day)
    if at is not None:
        jobs.append(Job(at, DIGEST, DIGEST, digest.venue, day))
    return sorted(jobs)


def jobs_between(
    calendars: Calendars, runtime: RuntimeConfig, start: datetime, end: datetime
) -> list[Job]:
    """Every job whose moment is in ``(start, end]``, in time order."""
    found: list[Job] = []
    day = start.date() - timedelta(days=1)
    while day <= end.date() + timedelta(days=1):
        found += [job for job in jobs_on(calendars, runtime, day) if start < job.at <= end]
        day += timedelta(days=1)
    return sorted(found)


def next_job(calendars: Calendars, runtime: RuntimeConfig, after: datetime) -> Job | None:
    """The first job strictly after ``after``; ``None`` when the calendars cover none soon."""
    upcoming = jobs_between(calendars, runtime, after, after + timedelta(days=SEARCH_DAYS))
    return upcoming[0] if upcoming else None
