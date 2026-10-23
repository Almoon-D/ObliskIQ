"""When the service's jobs run: four decision cycles and a digest a day, a collection job per
venue after its close, and each source's runs (``sources.providers.<id>.schedule``), from the
calendars.

Each job is a moment of one venue's regular session (``runtime.cycles``, ``runtime.digest``,
``collectors.schedule``): its open or close moved by an offset, read through the calendars, so
daylight saving and half-days follow the venue and a venue with no session that day has no job
that day; the other venues' jobs still run. A venue the calendars do not cover on a day has its
collection folded into the fallback venue's job of that day (:class:`CollectPlan`). Nothing here
is a UTC constant (MASTER_PLAN §5.12).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..calendars.service import Calendars
from ..config.schema_runtime import Anchor, RuntimeConfig
from ..errors import CalendarError

CYCLE = "cycle"
DIGEST = "digest"
COLLECT = "collect"
SOURCE = "source"
#: Kinds whose run-log command carries the job's name.
NAMED_KINDS = frozenset({CYCLE, COLLECT, SOURCE})

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
        """The run log's name for it: ``cycle:<name>``, ``collect:<MIC>``,
        ``source:<source>.<n>`` or ``digest``."""
        return f"{self.kind}:{self.name}" if self.kind in NAMED_KINDS else self.kind


@dataclass(frozen=True, slots=True)
class CollectPlan:
    """The venues the service collects on, how long after each close, and the venue whose job
    collects the venues the calendars do not cover that day."""

    venues: tuple[str, ...]
    after_close_minutes: int
    fallback_venue: str
    #: Every other configured venue: a stock candidate pinned there is collected in the
    #: fallback's job.
    other_venues: tuple[str, ...]

    def venues_of(self, calendars: Calendars, job: Job) -> tuple[str, ...]:
        """The venues ``job`` collects: its own; the fallback's job also every venue the
        calendars do not cover on its session date, and the venues outside the plan."""
        if job.venue != self.fallback_venue:
            return (job.venue,)
        uncovered = [v for v in self.venues if not covered(calendars, v, job.session_date)]
        return tuple(sorted({job.venue, *uncovered, *self.other_venues}))


@dataclass(frozen=True, slots=True)
class SourceMoment:
    """One scheduled run of a source: ``name`` is ``<source>.<n>``, its n-th moment a day."""

    name: str
    source: str
    venue: str
    anchor: Anchor
    offset_minutes: int


@dataclass(frozen=True, slots=True)
class SourcePlan:
    """The sources the service runs, at their moments."""

    moments: tuple[SourceMoment, ...]

    def source_of(self, job: Job) -> str:
        return next(m.source for m in self.moments if m.name == job.name)

    @property
    def venues(self) -> tuple[str, ...]:
        return tuple(sorted({m.venue for m in self.moments}))


def covered(calendars: Calendars, venue: str, day: date) -> bool:
    """Whether the calendars hold ``venue`` on ``day`` (a holiday is covered: it has no session)."""
    span = calendars.coverage().get(venue)
    return span is not None and span[0] <= day <= span[1]


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


def jobs_on(
    calendars: Calendars,
    runtime: RuntimeConfig,
    day: date,
    collect: CollectPlan | None = None,
    sources: SourcePlan | None = None,
) -> list[Job]:
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
    if collect is not None:
        for venue in collect.venues:
            at = moment(calendars, venue, "close", collect.after_close_minutes, day)
            if at is not None:
                jobs.append(Job(at, COLLECT, venue, venue, day))
    for planned in sources.moments if sources is not None else ():
        at = moment(calendars, planned.venue, planned.anchor, planned.offset_minutes, day)
        if at is not None:
            jobs.append(Job(at, SOURCE, planned.name, planned.venue, day))
    return sorted(jobs)


def jobs_between(
    calendars: Calendars,
    runtime: RuntimeConfig,
    start: datetime,
    end: datetime,
    collect: CollectPlan | None = None,
    sources: SourcePlan | None = None,
) -> list[Job]:
    """Every job whose moment is in ``(start, end]``, in time order."""
    found: list[Job] = []
    day = start.date() - timedelta(days=1)
    while day <= end.date() + timedelta(days=1):
        found += [
            job
            for job in jobs_on(calendars, runtime, day, collect, sources)
            if start < job.at <= end
        ]
        day += timedelta(days=1)
    return sorted(found)


def next_job(
    calendars: Calendars,
    runtime: RuntimeConfig,
    after: datetime,
    collect: CollectPlan | None = None,
    sources: SourcePlan | None = None,
) -> Job | None:
    """The first job strictly after ``after``; ``None`` when the calendars cover none soon."""
    upcoming = jobs_between(
        calendars, runtime, after, after + timedelta(days=SEARCH_DAYS), collect, sources
    )
    return upcoming[0] if upcoming else None
