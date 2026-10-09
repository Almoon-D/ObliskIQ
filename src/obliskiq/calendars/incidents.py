"""Incidents: halts and exceptional closures. An incident can block trading, never open it.

Market-wide halts and closures are entered by hand in the manual incident file (no free
market-wide feed is trustworthy for most venues); single-stock halt feeds arrive in a later
session and replace their own entries wholesale. A feed may only raise incidents within the
scope its venue declares.

An incident blocks ``[start, expected_end + grace]``, or from ``start`` onward when it has no
expected end, until it is removed. That is a function of the record alone, so asking about a
past or a future day gives the same answer whenever it is asked. A manual file that cannot be
read is not "no incidents": the book is marked invalid and every venue stays blocked until a
good file replaces it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    PositiveInt,
    ValidationError,
)

from ..config.base import FrozenModel
from ..config.loader import ConfigInvalid, read_yaml_mapping
from ..domain.base import NonEmptyText
from ..domain.calendar import Incident, IncidentScope, IncidentSource, Mic
from ..errors import IncidentFileError, IncidentRejectedError
from .intervals import Span

#: The end of an open-ended incident.
FOREVER = datetime.max.replace(tzinfo=UTC)


def blocking_span(incident: Incident, grace: timedelta) -> Span:
    """When an incident blocks: its start to its expected end plus ``grace`` (or forever)."""
    end = _end_with_grace(incident, grace)
    return (incident.start, FOREVER if end is None else end)


def _end_with_grace(incident: Incident, grace: timedelta) -> datetime | None:
    """The instant the block ends, or ``None`` for one that never ends.

    An expected end so far out that adding the grace overflows is an open-ended block, not a
    crash: an incident can lengthen a block, never lift it.
    """
    if incident.expected_end is None:
        return None
    try:
        return incident.expected_end + grace
    except OverflowError:
        return None


@dataclass(frozen=True, slots=True)
class IncidentPolicy:
    """What a venue's incident source is trusted to raise.

    ``accepts_feed`` says whether the venue declares an incident feed at all, and
    ``feed_scope`` the one scope that feed may raise: a single-stock halt feed never closes a
    venue. Manual entries are accepted at either scope.
    """

    accepts_feed: bool
    feed_scope: IncidentScope


def check_scope(incident: Incident, policy: IncidentPolicy) -> None:
    """Refuse an incident beyond what its source is trusted to raise."""
    if not policy.accepts_feed:
        raise IncidentRejectedError(
            f"{incident.venue}: no incident feed is declared for this venue, so "
            f"'{incident.incident_id}' from {incident.origin} is not accepted"
        )
    if incident.scope is not policy.feed_scope:
        raise IncidentRejectedError(
            f"{incident.venue}: the feed is declared {policy.feed_scope.value}-scoped, so a "
            f"{incident.scope.value}-scoped incident '{incident.incident_id}' is not accepted"
        )


@dataclass(frozen=True, slots=True)
class IncidentBook:
    """Every known incident, or the reason the manual file could not be read."""

    incidents: tuple[Incident, ...] = ()
    invalid: str | None = None

    @classmethod
    def unreadable(cls, reason: str) -> Self:
        return cls((), reason)

    def affecting(
        self, venue: str, conid: int | None, grace: timedelta
    ) -> list[tuple[Incident, Span]]:
        """The incidents that bear on a venue, and on the instrument if one is named."""
        return [
            (incident, blocking_span(incident, grace))
            for incident in self.incidents
            if incident.venue == venue
            and (
                incident.scope is IncidentScope.VENUE
                or (conid is not None and incident.conid == conid)
            )
        ]

    def replace_origin(
        self,
        origin: str,
        incidents: Iterable[Incident],
        policies: Mapping[str, IncidentPolicy],
    ) -> Self:
        """The book with everything from ``origin`` replaced; nothing changes if one is refused.

        Every incident must be a feed incident from ``origin`` and sit within its venue's declared
        scope: manual entries come from the manual file, never through a feed's door.
        """
        fresh = tuple(incidents)
        for incident in fresh:
            if incident.source is not IncidentSource.FEED:
                raise IncidentRejectedError(
                    f"'{incident.incident_id}' is a {incident.source.value} incident; "
                    "only feed incidents are accepted from a feed"
                )
            if incident.origin != origin:
                raise IncidentRejectedError(
                    f"'{incident.incident_id}' names origin {incident.origin}, not {origin}"
                )
            policy = policies.get(incident.venue)
            if policy is None:
                raise IncidentRejectedError(
                    f"'{incident.incident_id}': unknown venue {incident.venue}"
                )
            check_scope(incident, policy)
        kept = tuple(i for i in self.incidents if i.origin != origin)
        return type(self)((*kept, *fresh), self.invalid)

    def with_manual(self, manual: IncidentBook, origin: str) -> IncidentBook:
        """This book with the manual file's entries and validity taken from ``manual``.

        ``origin`` is the manual file's name: only entries that came from that file are
        replaced, so an entry another origin raised survives a reload.
        """
        kept = tuple(i for i in self.incidents if i.origin != origin)
        return IncidentBook((*manual.incidents, *kept), manual.invalid)

    def stale(self, now: datetime, grace: timedelta) -> tuple[Incident, ...]:
        """Incidents whose block has run out while the entry is still in the book."""
        return tuple(
            incident
            for incident in self.incidents
            if (end := _end_with_grace(incident, grace)) is not None and end < now
        )


def _plain_int(value: object) -> object:
    """An identifier is a whole number as written: ``yes`` or ``true`` is not contract 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be a whole number")
    return value


def _written_time(value: object) -> object:
    """A time is written with an explicit UTC offset, not as a count of seconds."""
    if isinstance(value, bool | int | float) or value is None:
        raise ValueError("must be a date and time with an explicit UTC offset")
    return value


class _ManualEntry(FrozenModel):
    """One entry of the manual file; unknown keys are errors, never silently ignored."""

    id: NonEmptyText
    venue: Mic
    scope: IncidentScope
    conid: Annotated[PositiveInt, BeforeValidator(_plain_int)] | None = Field(default=None)
    start: Annotated[AwareDatetime, BeforeValidator(_written_time)]
    expected_end: Annotated[AwareDatetime, BeforeValidator(_written_time)] | None = Field(
        default=None
    )
    reason: NonEmptyText


def load_manual_incidents(path: Path, venues: Collection[str]) -> IncidentBook:
    """The incidents in the manual file; the book is invalid if the file cannot be trusted.

    A missing file is the normal state and means no incidents. A file that exists but is
    malformed (bad YAML, an unknown key, a naive time, an unknown venue, a duplicate id, an end
    before the start) makes the whole book invalid: one typo must never silently drop the
    entries beside it.
    """
    if not path.exists() and not path.is_symlink():
        return IncidentBook()
    try:
        return IncidentBook(_read_entries(path, venues))
    except IncidentFileError as error:
        return IncidentBook.unreadable(str(error))


def _read_entries(path: Path, venues: Collection[str]) -> tuple[Incident, ...]:
    origin = path.name
    try:
        data = read_yaml_mapping(path, origin)
    except (ConfigInvalid, UnicodeDecodeError, OSError) as error:
        raise IncidentFileError(f"{origin}: cannot be read ({error})") from None
    unknown = sorted(set(data) - {"incidents"})
    if unknown:
        raise IncidentFileError(f"{origin}: unknown top-level key(s) {', '.join(unknown)}")
    entries = data.get("incidents")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise IncidentFileError(f"{origin}: 'incidents' must be a list of entries")
    incidents: list[Incident] = []
    seen: set[str] = set()
    for position, raw in enumerate(entries, start=1):
        where = f"{origin}: entry {position}"
        if not isinstance(raw, dict):
            raise IncidentFileError(f"{where}: must be a mapping of keys")
        try:
            entry = _ManualEntry.model_validate(raw)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in error.errors(include_url=False)
            )
            raise IncidentFileError(f"{where}: {problems}") from None
        if entry.id in seen:
            raise IncidentFileError(f"{where}: duplicate id '{entry.id}'")
        seen.add(entry.id)
        if entry.venue not in venues:
            raise IncidentFileError(f"{where}: unknown venue {entry.venue}")
        try:
            incidents.append(
                Incident(
                    incident_id=entry.id,
                    origin=origin,
                    source=IncidentSource.MANUAL,
                    scope=entry.scope,
                    venue=entry.venue,
                    conid=entry.conid,
                    start=entry.start,
                    expected_end=entry.expected_end,
                    reason=entry.reason,
                )
            )
        except ValidationError as error:
            problems = "; ".join(item["msg"] for item in error.errors(include_url=False))
            raise IncidentFileError(f"{where}: {problems}") from None
    return tuple(incidents)
