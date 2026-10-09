"""The phases of a venue's day: pre-market, auctions, regular trading, lunch, post-market.

A session's regular block runs from the open to the close, split by a break if there is one.
The configured phase windows are minutes from the open or the close, so on a half day the
close moves and so do the windows anchored to it. A window that runs through regular trading
(a closing auction before the close) takes that stretch out of the regular phase; a window
outside it (pre-market, an auction after the close) adds a phase around it. Between phases the
venue is closed.

All instants are UTC and every duration is computed on UTC instants, never as the difference
of two wall-clock times in one zone (which is wrong across a daylight saving change).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise

from ..config.schema_calendars import PhaseWindowConfig
from ..domain.calendar import Phase, PhaseKind
from ..errors import CalendarCoverageError, ScheduleInconsistentError
from .intervals import subtract
from .schedule import MarketSchedule, SessionTimes

_WINDOW_KINDS = {
    "pre_market": PhaseKind.PRE_MARKET,
    "opening_auction": PhaseKind.OPENING_AUCTION,
    "closing_auction": PhaseKind.CLOSING_AUCTION,
    "post_market": PhaseKind.POST_MARKET,
}


@dataclass(frozen=True, slots=True)
class PhaseState:
    """What a venue is doing at an instant, and what comes next.

    ``since`` and ``until`` bound the current phase (``None`` when the schedule does not reach
    that far); ``next_phase`` is what begins at ``until``. Between sessions the phase is
    ``CLOSED`` and ``until`` is the next phase's start.
    """

    phase: PhaseKind
    session_date: date | None
    since: datetime | None
    until: datetime | None
    next_phase: PhaseKind | None


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    at: datetime
    from_phase: PhaseKind
    to_phase: PhaseKind


def session_phases(
    mic: str, windows: Sequence[PhaseWindowConfig], session: SessionTimes
) -> tuple[Phase, ...]:
    """The phases of one session in time order; the venue is closed between them."""
    custom = [_window_phase(window, session) for window in windows]
    custom.sort(key=lambda phase: (phase.start, phase.end))
    for left, right in pairwise(custom):
        if right.start < left.end:
            raise ScheduleInconsistentError(
                f"{mic} {session.day}: configured phase windows overlap"
            )

    if session.break_start is not None and session.break_end is not None:
        blocks = [
            (PhaseKind.REGULAR, session.open, session.break_start),
            (PhaseKind.LUNCH_BREAK, session.break_start, session.break_end),
            (PhaseKind.REGULAR, session.break_end, session.close),
        ]
    else:
        blocks = [(PhaseKind.REGULAR, session.open, session.close)]

    holes = [(phase.start, phase.end) for phase in custom]
    phases = list(custom)
    for kind, start, end in blocks:
        pieces = subtract([(start, end)], holes) if kind is PhaseKind.REGULAR else [(start, end)]
        phases.extend(Phase(kind=kind, start=lo, end=hi) for lo, hi in pieces)
    phases.sort(key=lambda phase: (phase.start, phase.end))
    for left, right in pairwise(phases):
        if right.start < left.end:
            raise ScheduleInconsistentError(f"{mic} {session.day}: phases overlap")
    return tuple(phases)


def _window_phase(window: PhaseWindowConfig, session: SessionTimes) -> Phase:
    anchor = session.open if window.anchor == "session_open" else session.close
    return Phase(
        kind=_WINDOW_KINDS[window.phase],
        start=anchor + timedelta(minutes=window.start_offset_minutes),
        end=anchor + timedelta(minutes=window.end_offset_minutes),
    )


def _phases_around(
    schedule: MarketSchedule, windows: Sequence[PhaseWindowConfig], day: date
) -> list[tuple[date, Phase]]:
    """The phases of the session before ``day``, on it, and after it, in time order."""
    days = {schedule.previous_session(day), schedule.next_session(day)}
    if schedule.is_session(day):
        days.add(day)
    found: list[tuple[date, Phase]] = []
    for session_day in sorted(d for d in days if d is not None):
        times = schedule.session(session_day)
        assert times is not None
        found.extend((session_day, phase) for phase in session_phases(schedule.mic, windows, times))
    found.sort(key=lambda item: (item[1].start, item[1].end))
    return found


def phase_state(
    schedule: MarketSchedule, windows: Sequence[PhaseWindowConfig], at: datetime
) -> PhaseState:
    """The phase at ``at``. Raises outside the schedule's coverage."""
    day = schedule.session_day(at)
    if not schedule.covers(day):
        raise CalendarCoverageError(
            f"{schedule.mic}: {at} is outside the calendar built (covers {schedule.coverage})"
        )
    around = _phases_around(schedule, windows, day)
    for index, (session_day, phase) in enumerate(around):
        if phase.contains(at):
            following = around[index + 1][1] if index + 1 < len(around) else None
            continues = following is not None and following.start == phase.end
            return PhaseState(
                phase=phase.kind,
                session_date=session_day,
                since=phase.start,
                until=phase.end,
                next_phase=following.kind if continues and following else PhaseKind.CLOSED,
            )
    earlier = [phase.end for _, phase in around if phase.end <= at]
    later = [phase for _, phase in around if phase.start > at]
    upcoming = later[0] if later else None
    return PhaseState(
        phase=PhaseKind.CLOSED,
        session_date=None,
        since=max(earlier) if earlier else None,
        until=upcoming.start if upcoming else None,
        next_phase=upcoming.kind if upcoming else None,
    )


def transitions(
    schedule: MarketSchedule, windows: Sequence[PhaseWindowConfig], day: date
) -> tuple[PhaseTransition, ...]:
    """Every change of phase on a session day, ending with the move to closed."""
    times = schedule.session(day)
    if times is None:
        return ()
    phases = session_phases(schedule.mic, windows, times)  # a session has at least one phase
    first, last = phases[0], phases[-1]
    changes = [PhaseTransition(first.start, PhaseKind.CLOSED, first.kind)]
    for before, after in pairwise(phases):
        if before.end == after.start:
            changes.append(PhaseTransition(after.start, before.kind, after.kind))
        else:
            changes.append(PhaseTransition(before.end, before.kind, PhaseKind.CLOSED))
            changes.append(PhaseTransition(after.start, PhaseKind.CLOSED, after.kind))
    changes.append(PhaseTransition(last.end, last.kind, PhaseKind.CLOSED))
    return tuple(changes)
