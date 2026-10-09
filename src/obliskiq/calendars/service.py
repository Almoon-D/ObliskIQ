"""The calendar facade: is a venue tradable, when, and what is the last session an option can use?

:class:`Calendars` is built once from the configuration (and a reference date that anchors the
window it covers), then answers synchronously. It never blocks on I/O and never guesses: a date
it does not cover, a venue it does not know, a session whose own times contradict one another, a
manual incident file it cannot read, an IBKR exchange code it cannot map are all *blocked*, with
a stable reason, never open.

Two views of a day, because the two questions differ. :meth:`Calendars.venue_status` is the
**planning** view: any incident overlapping the session blocks the day (``partial`` says
whether tradable time remains), which is what an exit scheduled against an expiry needs.
:meth:`Calendars.tradability` is the **act-now** view: incident intervals are subtracted
exactly, so trading resumes after a halt. A pre-trade check calls the second.

Cross-check sources (IBKR's trading schedule, Session 3) never replace the calendar. A
disagreement is logged and alerted once; it blocks only queries marked ``date_critical``
(option exits near expiry), not ordinary orders.

An instance holds its schedules immutably and its incident book, alert memory and disagreement
log as plain mutable state replaced or appended atomically; it is safe to share between
threads for reads, and an incident reload is one attribute assignment.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo

from ..config.schema import ObliskConfig
from ..config.schema_calendars import PhaseWindowConfig
from ..domain.calendar import Incident, IncidentScope, PhaseKind, TimeWindow, Venue
from ..errors import (
    CalendarCoverageError,
    CalendarError,
    IncidentRejectedError,
    ScheduleInconsistentError,
    UnknownExchangeCodeError,
    UnknownVenueError,
)
from ..logging_setup import get_logger
from .incidents import IncidentBook, IncidentPolicy, load_manual_incidents
from .intervals import Span, contains, intersect, intersect_windows, overlaps, subtract, to_windows
from .phases import PhaseState, PhaseTransition, phase_state, session_phases, transitions
from .schedule import (
    LibrarySchedule,
    MarketSchedule,
    ScheduleSource,
    SessionTimes,
    SyntheticSchedule,
    UncoveredSchedule,
)
from .status import (
    BlockReason,
    Disagreement,
    DisagreementKind,
    LastTradableSession,
    SessionBounds,
    SkippedSession,
    Tradability,
    VenueStatus,
)

log = get_logger("calendars")

__all__ = ["Calendars", "intersect_windows"]


@dataclass(frozen=True, slots=True)
class ProvenanceNote:
    """Where one value came from, and whether it still awaits a live reading."""

    what: str
    origin: str
    verify_live: bool


@dataclass(frozen=True, slots=True)
class VenueProvenance:
    venue: str
    notes: tuple[ProvenanceNote, ...]


@dataclass(frozen=True, slots=True)
class IbkrExchange:
    """An IBKR exchange code and the venue whose calendar governs it."""

    code: str
    venue: str
    origin: str
    verify_live: bool


@dataclass(frozen=True, slots=True)
class VenueRuntime:
    venue: Venue
    schedule: MarketSchedule
    windows: tuple[PhaseWindowConfig, ...]
    after_open: timedelta
    before_close: timedelta
    incident_policy: IncidentPolicy
    provenance: VenueProvenance


@dataclass(frozen=True, slots=True)
class _DayView:
    """A day as seen by the planning and act-now views."""

    status: VenueStatus
    base: tuple[Span, ...]
    final: tuple[Span, ...]
    times: SessionTimes | None


def _shift_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February in a year without one
        return date(day.year + years, 3, 1) - timedelta(days=1)


def _build_venue(settings: ObliskConfig, mic: str, start: date, end: date) -> VenueRuntime:
    """One venue from the configuration; every field of its block is read here."""
    config = settings.venues[mic]
    venue = Venue(mic=mic, name=config.name, region=config.region, timezone=config.timezone)
    lunch = settings.lunch_breaks.get(mic)
    seasonal = settings.seasonal_closes.get(mic)
    synthetic = settings.synthetic_calendars.get(mic)
    schedule: MarketSchedule | None
    if synthetic is not None:
        schedule = SyntheticSchedule(mic, config.timezone, synthetic, lunch, start, end)
    else:
        schedule = LibrarySchedule.build(mic, config.timezone, start, end, lunch, seasonal)
        if schedule is None:
            schedule = UncoveredSchedule(mic, ZoneInfo(config.timezone))
    standard = settings.calendars.order_offsets
    own = settings.calendars.venue_order_offsets.get(mic)
    after_open, before_close = (
        (standard.after_open_minutes, standard.before_close_minutes)
        if own is None
        else (own.after_open_minutes, own.before_close_minutes)
    )
    notes = [
        ProvenanceNote(
            "calendar", config.sources.calendar.origin, config.sources.calendar.verify_live
        ),
        ProvenanceNote("phases", config.sources.phases.origin, config.sources.phases.verify_live),
    ]
    if lunch is not None:
        notes.append(ProvenanceNote("lunch_break", lunch.source.origin, lunch.source.verify_live))
    if seasonal is not None:
        notes.append(
            ProvenanceNote("seasonal_close", seasonal.source.origin, seasonal.source.verify_live)
        )
    feed_scope = (
        IncidentScope.VENUE
        if config.incident_source.scope == "market_wide"
        else IncidentScope.INSTRUMENT
    )
    return VenueRuntime(
        venue=venue,
        schedule=schedule,
        windows=tuple(config.phase_windows),
        after_open=timedelta(minutes=after_open),
        before_close=timedelta(minutes=before_close),
        incident_policy=IncidentPolicy(
            accepts_feed=config.incident_source.type == "structured_feed", feed_scope=feed_scope
        ),
        provenance=VenueProvenance(mic, tuple(notes)),
    )


def _describe(disagreements: tuple[Disagreement, ...]) -> str:
    return "; ".join(
        f"{d.source}: {d.kind.value} {d.observed} vs {d.calendar}" for d in disagreements
    )


def _disagreement_detail(view: _DayView) -> str:
    return _describe(view.status.disagreements)


class Calendars:
    """Venue calendars, phases, incidents and the last tradable session of an option."""

    def __init__(
        self,
        runtimes: Mapping[str, VenueRuntime],
        exchanges: Mapping[str, IbkrExchange],
        *,
        incident_grace: timedelta,
        tolerance: timedelta,
        sources: Sequence[ScheduleSource] = (),
        alert: Callable[[Disagreement], None] | None = None,
        incidents: IncidentBook | None = None,
        manual_incidents_path: Path | None = None,
    ) -> None:
        self._runtimes = dict(runtimes)
        self._manual_path = manual_incidents_path
        self._manual_origins: set[str] = (
            set() if manual_incidents_path is None else {manual_incidents_path.name}
        )
        self._exchanges = dict(exchanges)
        self._grace = incident_grace
        self._tolerance = tolerance
        self._sources = tuple(sources)
        self._alert = alert
        self._incidents = incidents if incidents is not None else IncidentBook()
        self._alerted: set[tuple[str, date, DisagreementKind, str]] = set()
        self._disagreements: list[Disagreement] = []
        self._stale_logged: set[str] = set()

    @classmethod
    def from_settings(
        cls,
        settings: ObliskConfig,
        *,
        reference: date,
        venues: Collection[str] | None = None,
        sources: Sequence[ScheduleSource] = (),
        alert: Callable[[Disagreement], None] | None = None,
        incidents: IncidentBook | None = None,
        data_dir: Path | None = None,
    ) -> Self:
        """Build every venue (or just ``venues``) for a window around ``reference``.

        Building is eager and takes a few seconds for all venues; do it once, off the event
        loop. The window is ``[reference - history_years, reference + horizon_years]``. With
        ``data_dir`` the manual incident file in it is read now and by
        :meth:`reload_manual_incidents`; a file that cannot be read blocks every venue.
        """
        calendars = settings.calendars
        start = _shift_years(reference, -calendars.history_years)
        end = _shift_years(reference, calendars.horizon_years)
        wanted = sorted(settings.venues if venues is None else venues)
        unknown = sorted(set(wanted) - set(settings.venues))
        if unknown:
            raise UnknownVenueError(f"not configured venues: {', '.join(unknown)}")
        runtimes = {mic: _build_venue(settings, mic, start, end) for mic in wanted}
        exchanges = {
            code: IbkrExchange(
                code=code,
                venue=entry.venue,
                origin=entry.source.origin,
                verify_live=entry.source.verify_live,
            )
            for code, entry in settings.ibkr_exchanges.items()
            if entry.venue in runtimes
        }
        manual_path = None if data_dir is None else data_dir / settings.paths.manual_incidents
        if manual_path is None and incidents is None:
            log.warning("no data directory given: the manual incident file is not read")
        if incidents is None and manual_path is not None:
            incidents = load_manual_incidents(manual_path, runtimes.keys())
        return cls(
            runtimes,
            exchanges,
            incident_grace=timedelta(minutes=calendars.incidents.grace_minutes),
            tolerance=timedelta(minutes=calendars.cross_check.time_tolerance_minutes),
            sources=sources,
            alert=alert,
            incidents=incidents,
            manual_incidents_path=manual_path,
        )

    # ── what is known about a venue ──

    def venue(self, mic: str) -> Venue:
        return self._runtime(mic).venue

    def venues(self) -> tuple[Venue, ...]:
        return tuple(runtime.venue for runtime in self._runtimes.values())

    def provenance(self, mic: str) -> VenueProvenance:
        return self._runtime(mic).provenance

    def unverified(self) -> tuple[tuple[str, ProvenanceNote], ...]:
        """Every value that still awaits a live reading, venue by venue and IBKR code by code."""
        pending = [
            (runtime.venue.mic, note)
            for runtime in self._runtimes.values()
            for note in runtime.provenance.notes
            if note.verify_live
        ]
        pending += [
            (code, ProvenanceNote("ibkr_exchange", exchange.origin, True))
            for code, exchange in self._exchanges.items()
            if exchange.verify_live
        ]
        return tuple(pending)

    def venue_for_ibkr(self, code: str) -> str:
        """The venue whose calendar governs an IBKR exchange code; unmapped codes raise."""
        exchange = self._exchanges.get(code)
        if exchange is None:
            raise UnknownExchangeCodeError(
                f"IBKR exchange code {code!r} maps to no venue (SMART is a router, not a venue)"
            )
        return exchange.venue

    def ibkr_exchange(self, code: str) -> IbkrExchange:
        self.venue_for_ibkr(code)
        return self._exchanges[code]

    def coverage(self) -> dict[str, tuple[date, date] | None]:
        """Each venue's covered window, for the digest: a venue with ``None`` covers nothing."""
        return {mic: runtime.schedule.coverage for mic, runtime in self._runtimes.items()}

    def _runtime(self, mic: str) -> VenueRuntime:
        runtime = self._runtimes.get(mic)
        if runtime is None:
            raise UnknownVenueError(f"no venue {mic!r} is configured")
        return runtime

    # ── incidents ──

    @property
    def incident_book(self) -> IncidentBook:
        return self._incidents

    def replace_incidents(self, book: IncidentBook) -> None:
        """Swap the whole incident book in one assignment."""
        self._incidents = book

    def reload_manual_incidents(self, path: Path | None = None) -> IncidentBook:
        """Re-read the manual file; an unreadable one blocks every venue until it is fixed.

        Without ``path`` the file in the data directory the facade was built with is read.
        """
        path = path if path is not None else self._manual_path
        if path is None:
            raise CalendarError("no manual incident file: build with data_dir or pass a path")
        manual = load_manual_incidents(path, self._runtimes.keys())
        if manual.invalid is not None:
            log.error(
                "manual incident file unreadable; all venues blocked",
                extra={"error": manual.invalid},
            )
        self._manual_origins.add(path.name)
        book = self._incidents.with_manual(manual, path.name)
        self._incidents = book
        return book

    def accept_incidents(self, origin: str, incidents: Sequence[Incident]) -> None:
        """Replace a source's incidents, refusing any beyond what its venue lets it raise."""
        if origin in self._manual_origins:
            raise IncidentRejectedError(
                f"'{origin}' is the manual incident file's own name; reload the file instead"
            )
        policies = {mic: runtime.incident_policy for mic, runtime in self._runtimes.items()}
        self._incidents = self._incidents.replace_origin(origin, incidents, policies)

    def stale_incidents(self, now: datetime) -> tuple[Incident, ...]:
        """Incidents whose block has run out but are still listed; each is logged once."""
        stale = self._incidents.stale(now, self._grace)
        for incident in stale:
            if incident.incident_id not in self._stale_logged:
                self._stale_logged.add(incident.incident_id)
                log.warning(
                    "incident has run past its expected end; remove it from %s",
                    incident.origin,
                    extra={"incident": incident.incident_id, "venue": incident.venue},
                )
        return stale

    def disagreements(self) -> tuple[Disagreement, ...]:
        """Every cross-check disagreement seen so far (each reported once)."""
        return tuple(self._disagreements)

    # ── sessions and phases ──

    def session_day(self, mic: str, at: datetime) -> date:
        return self._runtime(mic).schedule.session_day(at)

    def session_bounds(self, mic: str, day: date) -> SessionBounds | None:
        """The session on ``day`` in local time and UTC; ``None`` on a closed day."""
        runtime = self._runtime(mic)
        times = runtime.schedule.session(day)
        if times is None:
            return None
        zone = runtime.schedule.zone
        return SessionBounds(
            venue=mic,
            day=day,
            open_local=times.open.astimezone(zone),
            close_local=times.close.astimezone(zone),
            open_utc=times.open,
            close_utc=times.close,
            break_start_utc=times.break_start,
            break_end_utc=times.break_end,
            early_close=times.early_close,
        )

    def phase_at(self, mic: str, at: datetime) -> PhaseState:
        runtime = self._runtime(mic)
        return phase_state(runtime.schedule, runtime.windows, at)

    def is_open(self, mic: str, at: datetime) -> bool:
        """Is the venue in its regular continuous phase? Outside the covered window: no."""
        runtime = self._runtime(mic)
        if not runtime.schedule.covers(runtime.schedule.session_day(at)):
            return False
        return phase_state(runtime.schedule, runtime.windows, at).phase is PhaseKind.REGULAR

    def day_transitions(self, mic: str, day: date) -> tuple[PhaseTransition, ...]:
        runtime = self._runtime(mic)
        return transitions(runtime.schedule, runtime.windows, day)

    def next_open(self, mic: str, at: datetime) -> datetime | None:
        return self._runtime(mic).schedule.next_open(at)

    def next_close(self, mic: str, at: datetime) -> datetime | None:
        return self._runtime(mic).schedule.next_close(at)

    def previous_session(self, mic: str, day: date) -> date | None:
        return self._runtime(mic).schedule.previous_session(day)

    def next_session(self, mic: str, day: date) -> date | None:
        return self._runtime(mic).schedule.next_session(day)

    def last_completed_session(self, mic: str, at: datetime) -> date | None:
        """The session a cycle at ``at`` may read as its ``as_of`` (never today's open one)."""
        return self._runtime(mic).schedule.last_completed_session(at)

    # ── status, windows, tradability ──

    def venue_status(
        self, mic: str, day: date, *, conid: int | None = None, date_critical: bool = False
    ) -> VenueStatus:
        """The planning view of a day. ``date_critical`` makes a disagreement block it."""
        return self._day_view(mic, day, conid, date_critical).status

    def tradable_windows(
        self, mic: str, day: date, *, conid: int | None = None
    ) -> tuple[TimeWindow, ...]:
        """The regular phase inside the order offsets, minus lunch, auctions and incidents."""
        return to_windows(self._day_view(mic, day, conid, False).final)

    def tradability(
        self, mic: str, at: datetime, *, conid: int | None = None, date_critical: bool = False
    ) -> Tradability:
        """The act-now view: may an order go to this venue at this instant?"""
        runtime = self._runtimes.get(mic)
        if runtime is None:
            return Tradability(
                mic, at, BlockReason.UNKNOWN_VENUE, f"no venue {mic!r} is configured"
            )
        day = runtime.schedule.session_day(at)
        view = self._day_view(mic, day, conid, date_critical)
        reason = view.status.reason
        if reason is not None and reason is not BlockReason.INCIDENT_OVERLAP:
            return Tradability(mic, at, reason, view.status.detail)
        if contains(view.final, at):
            if date_critical and view.status.disagreements:
                return Tradability(
                    mic, at, BlockReason.SOURCE_DISAGREEMENT, _disagreement_detail(view)
                )
            window = next(w for w in to_windows(view.final) if w.contains(at))
            return Tradability(mic, at, None, "", window)
        if contains(view.base, at):
            return Tradability(mic, at, BlockReason.INCIDENT_OVERLAP, view.status.detail)
        return Tradability(mic, at, BlockReason.OUTSIDE_WINDOW, "outside the tradable window")

    def common_windows(self, day: date, *legs: tuple[str, int | None]) -> tuple[TimeWindow, ...]:
        """Windows tradable on every ``(venue, conid)`` leg: an option and its underlying."""
        return intersect_windows(
            *(self.tradable_windows(mic, day, conid=conid) for mic, conid in legs)
        )

    def _day_view(self, mic: str, day: date, conid: int | None, date_critical: bool) -> _DayView:
        runtime = self._runtimes.get(mic)
        if runtime is None:
            return self._blocked(
                mic, day, BlockReason.UNKNOWN_VENUE, f"no venue {mic!r} is configured"
            )
        if self._incidents.invalid is not None:
            return self._blocked(
                mic, day, BlockReason.INCIDENT_FILE_INVALID, self._incidents.invalid
            )
        schedule = runtime.schedule
        if not schedule.covers(day):
            return self._blocked(
                mic,
                day,
                BlockReason.CALENDAR_NOT_COVERED,
                f"{day} is outside the calendar built (covers {schedule.coverage})",
            )
        try:
            times = schedule.session(day)
            if times is None:
                kind = "holiday" if schedule.is_business_weekday(day) else "weekend"
                return self._blocked(mic, day, BlockReason.NOT_A_SESSION, kind)
            phases = session_phases(mic, runtime.windows, times)
        except ScheduleInconsistentError as error:
            return self._blocked(mic, day, BlockReason.SCHEDULE_INCONSISTENT, str(error))

        regular = [(p.start, p.end) for p in phases if p.kind is PhaseKind.REGULAR]
        inside = [(times.open + runtime.after_open, times.close - runtime.before_close)]
        base = tuple(intersect(regular, inside))
        if not base:
            return self._blocked(
                mic,
                day,
                BlockReason.NO_TRADABLE_WINDOW,
                "the order offsets leave no regular time",
                times,
            )

        affecting = self._incidents.affecting(mic, conid, self._grace)
        final = tuple(subtract(base, [span for _, span in affecting]))
        touching = tuple(
            incident for incident, span in affecting if overlaps([span], times.open, times.close)
        )
        disagreements = self._cross_check(mic, day, times, conid)
        if touching:
            status = VenueStatus(
                mic,
                day,
                BlockReason.INCIDENT_OVERLAP,
                "; ".join(i.reason for i in touching),
                partial=bool(final),
                incidents=touching,
                disagreements=disagreements,
            )
        elif disagreements and date_critical:
            status = VenueStatus(
                mic,
                day,
                BlockReason.SOURCE_DISAGREEMENT,
                _describe(disagreements),
                disagreements=disagreements,
            )
        else:
            status = VenueStatus(mic, day, None, disagreements=disagreements)
        return _DayView(status, base, final, times)

    @staticmethod
    def _blocked(
        mic: str, day: date, reason: BlockReason, detail: str, times: SessionTimes | None = None
    ) -> _DayView:
        return _DayView(VenueStatus(mic, day, reason, detail), (), (), times)

    # ── cross-checks ──

    def _cross_check(
        self, mic: str, day: date, times: SessionTimes, conid: int | None
    ) -> tuple[Disagreement, ...]:
        found: list[Disagreement] = []
        for source in self._sources:
            try:
                observed = source.observe(mic, day, conid)
            except Exception:
                log.exception("schedule source failed", extra={"source": source.name, "venue": mic})
                continue
            if observed is None:
                continue
            if not observed.is_session:
                found.append(
                    Disagreement(
                        mic,
                        day,
                        DisagreementKind.IS_SESSION,
                        source.name,
                        "session",
                        "closed",
                    )
                )
            elif observed.open is not None and observed.close is not None:
                if abs(observed.open - times.open) > self._tolerance:
                    found.append(
                        Disagreement(
                            mic,
                            day,
                            DisagreementKind.OPEN,
                            source.name,
                            times.open.isoformat(),
                            observed.open.isoformat(),
                        )
                    )
                if abs(observed.close - times.close) > self._tolerance:
                    found.append(
                        Disagreement(
                            mic,
                            day,
                            DisagreementKind.CLOSE,
                            source.name,
                            times.close.isoformat(),
                            observed.close.isoformat(),
                        )
                    )
        for disagreement in found:
            self._report(disagreement)
        return tuple(found)

    def _report(self, disagreement: Disagreement) -> None:
        key = (disagreement.venue, disagreement.day, disagreement.kind, disagreement.source)
        if key in self._alerted:
            return
        self._alerted.add(key)
        self._disagreements.append(disagreement)
        log.warning(
            "schedule sources disagree",
            extra={
                "venue": disagreement.venue,
                "day": disagreement.day.isoformat(),
                "kind": disagreement.kind.value,
                "source": disagreement.source,
            },
        )
        if self._alert is not None:
            try:
                self._alert(disagreement)
            except Exception:
                log.exception("disagreement alert sink failed")

    # ── date-critical instruments ──

    def last_tradable_session(
        self,
        instrument_venue: str,
        underlying_venue: str,
        expiry: date,
        *,
        instrument_conid: int | None = None,
        underlying_conid: int | None = None,
    ) -> LastTradableSession:
        """The last session on or before ``expiry`` when both venues can trade.

        A holiday expiry, or a Monday expiry after a Friday holiday, lands on an earlier
        session. Each candidate day is judged date-critically: a day with an incident or an
        unresolved source disagreement is ruled out, so the earlier date wins. A date the
        calendars do not cover cannot be judged and raises rather than being skipped.
        """
        skipped: list[SkippedSession] = []
        day = expiry
        while True:
            blocked = self._common_block(
                instrument_venue, underlying_venue, day, instrument_conid, underlying_conid
            )
            if blocked is None:
                return LastTradableSession(day, expiry, tuple(skipped))
            skipped.append(blocked)
            day -= timedelta(days=1)

    def session_before(
        self,
        instrument_venue: str,
        underlying_venue: str,
        anchor: date,
        sessions: int,
        *,
        instrument_conid: int | None = None,
        underlying_conid: int | None = None,
    ) -> date:
        """The ``sessions``-th common session before ``anchor``.

        With ``sessions`` of zero the anchor itself must be a common session. Date rules
        counted from an expiry use ``last_tradable_session`` as the anchor, minus a buffer.
        """
        if sessions < 0:
            raise CalendarError("sessions must not be negative")
        day = anchor
        remaining = sessions
        if remaining == 0:
            if (
                self._common_block(
                    instrument_venue, underlying_venue, anchor, instrument_conid, underlying_conid
                )
                is not None
            ):
                raise CalendarError(f"{anchor} is not a session of both venues")
            return anchor
        while True:
            day -= timedelta(days=1)
            if (
                self._common_block(
                    instrument_venue, underlying_venue, day, instrument_conid, underlying_conid
                )
                is None
            ):
                remaining -= 1
                if remaining == 0:
                    return day

    def common_sessions_between(
        self,
        instrument_venue: str,
        underlying_venue: str,
        start: date,
        end: date,
        *,
        instrument_conid: int | None = None,
        underlying_conid: int | None = None,
    ) -> tuple[date, ...]:
        """Every day from ``start`` to ``end`` inclusive that both venues can trade."""
        days: list[date] = []
        day = start
        while day <= end:
            if (
                self._common_block(
                    instrument_venue, underlying_venue, day, instrument_conid, underlying_conid
                )
                is None
            ):
                days.append(day)
            day += timedelta(days=1)
        return tuple(days)

    def _common_block(
        self,
        instrument_venue: str,
        underlying_venue: str,
        day: date,
        instrument_conid: int | None,
        underlying_conid: int | None,
    ) -> SkippedSession | None:
        """Why ``day`` is not a common tradable session; ``None`` when it is one.

        A closed day or an incident rules a day out; a venue or a date the calendars cannot
        speak for raises, because skipping it would silently move a date earlier or later.
        """
        for mic, conid in (
            (instrument_venue, instrument_conid),
            (underlying_venue, underlying_conid),
        ):
            status = self.venue_status(mic, day, conid=conid, date_critical=True)
            if status.ok:
                continue
            reason = status.reason
            assert reason is not None
            if reason in _UNJUDGEABLE:
                raise CalendarCoverageError(f"{mic} on {day}: {reason.value}: {status.detail}")
            return SkippedSession(mic, day, reason)
        return None


#: Reasons that mean the calendars cannot judge a day, as opposed to judging it closed.
_UNJUDGEABLE = frozenset(
    {
        BlockReason.UNKNOWN_VENUE,
        BlockReason.CALENDAR_NOT_COVERED,
        BlockReason.INCIDENT_FILE_INVALID,
        BlockReason.SCHEDULE_INCONSISTENT,
    }
)
