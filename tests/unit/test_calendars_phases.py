"""Phases: pre-market, auctions, regular trading, lunch, post-market and the moves between.

The legacy pulse engine tests are ported onto the new API. Local times that pulse typed in are
derived here from the session's open and close and the configured offsets.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from obliskiq.calendars.phases import (
    PhaseTransition,
    phase_state,
    session_phases,
    transitions,
)
from obliskiq.calendars.schedule import SessionTimes, SyntheticSchedule
from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_calendars import PhaseWindowConfig, SyntheticCalendarConfig
from obliskiq.domain.calendar import PhaseKind
from obliskiq.errors import CalendarCoverageError, ScheduleInconsistentError

DAY = date(2026, 10, 9)  # a Friday with no holiday on any shipped venue
MINUTE = timedelta(minutes=1)


def _window(phase: str, anchor: str, start: int, end: int) -> PhaseWindowConfig:
    return PhaseWindowConfig.model_validate(
        {
            "phase": phase,
            "anchor": anchor,
            "start_offset_minutes": start,
            "end_offset_minutes": end,
        }
    )


def _bounds(calendars: Calendars, mic: str, day: date = DAY):  # type: ignore[no-untyped-def]
    bounds = calendars.session_bounds(mic, day)
    assert bounds is not None
    return bounds


def _offsets(settings: ObliskConfig, mic: str) -> dict[str, tuple[int, int]]:
    return {
        w.phase: (w.start_offset_minutes, w.end_offset_minutes)
        for w in settings.venues[mic].phase_windows
    }


# ── the engine tests of the legacy pulse bot, ported ──


def test_new_york_goes_from_pre_market_to_regular(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    bounds = _bounds(calendars, "XNYS")
    just_before = calendars.phase_at("XNYS", bounds.open_utc - 4 * MINUTE)
    assert just_before.phase is PhaseKind.PRE_MARKET
    assert just_before.next_phase is PhaseKind.REGULAR
    assert just_before.until == bounds.open_utc
    assert calendars.phase_at("XNYS", bounds.open_utc + 5 * MINUTE).phase is PhaseKind.REGULAR
    opening, _ = _offsets(shipped_settings, "XNYS")["pre_market"]
    assert just_before.since == bounds.open_utc + timedelta(minutes=opening)


def test_madrid_has_an_opening_auction_before_regular_trading(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    bounds = _bounds(calendars, "XMAD")
    start, _ = _offsets(shipped_settings, "XMAD")["opening_auction"]
    inside = calendars.phase_at("XMAD", bounds.open_utc + timedelta(minutes=start) + 15 * MINUTE)
    assert inside.phase is PhaseKind.OPENING_AUCTION
    assert inside.next_phase is PhaseKind.REGULAR
    before = calendars.phase_at("XMAD", bounds.open_utc + timedelta(minutes=start) - MINUTE)
    assert before.phase is PhaseKind.CLOSED
    assert before.next_phase is PhaseKind.OPENING_AUCTION


def test_the_minutes_to_a_phase_change_are_exact_on_utc_instants(calendars: Calendars) -> None:
    bounds = _bounds(calendars, "XMAD")
    state = calendars.phase_at("XMAD", bounds.open_utc - 2 * MINUTE)
    assert state.phase is PhaseKind.OPENING_AUCTION
    assert state.until is not None
    assert state.until - (bounds.open_utc - 2 * MINUTE) == 2 * MINUTE


def test_tokyo_has_a_lunch_break(calendars: Calendars) -> None:
    bounds = _bounds(calendars, "XTKS")
    assert bounds.break_start_utc is not None
    assert bounds.break_end_utc is not None
    middle = bounds.break_start_utc + (bounds.break_end_utc - bounds.break_start_utc) / 2
    state = calendars.phase_at("XTKS", middle)
    assert state.phase is PhaseKind.LUNCH_BREAK
    assert state.next_phase is PhaseKind.REGULAR
    assert state.since == bounds.break_start_utc
    assert state.until == bounds.break_end_utc


def test_a_synthetic_venue_runs_its_declared_hours() -> None:
    config = SyntheticCalendarConfig(
        open="10:00",
        close="12:00",
        trading_days=["mon", "tue", "wed", "thu", "fri"],
        holidays=[],
        holidays_through=date(2026, 12, 31),
    )
    schedule = SyntheticSchedule(
        "TEST", "Pacific/Fiji", config, None, date(2026, 10, 1), date(2027, 1, 1)
    )
    session = schedule.session(DAY)
    assert session is not None
    middle = session.open + (session.close - session.open) / 2
    assert phase_state(schedule, [], middle).phase is PhaseKind.REGULAR
    assert phase_state(schedule, [], session.close).phase is PhaseKind.CLOSED


def test_madrid_closing_auction_follows_the_continuous_session(calendars: Calendars) -> None:
    bounds = _bounds(calendars, "XMAD")
    state = calendars.phase_at("XMAD", bounds.close_utc + 2 * MINUTE)
    assert state.phase is PhaseKind.CLOSING_AUCTION
    assert state.next_phase is PhaseKind.POST_MARKET
    assert calendars.phase_at("XMAD", bounds.close_utc - MINUTE).phase is PhaseKind.REGULAR


def test_new_york_post_market_starts_at_the_official_close(calendars: Calendars) -> None:
    bounds = _bounds(calendars, "XNYS")
    assert calendars.phase_at("XNYS", bounds.close_utc).phase is PhaseKind.POST_MARKET
    assert calendars.phase_at("XNYS", bounds.close_utc - MINUTE).phase is PhaseKind.CLOSING_AUCTION


def test_hong_kong_pre_market_lunch_and_the_auction_after_the_close(
    calendars: Calendars,
) -> None:
    bounds = _bounds(calendars, "XHKG")
    assert bounds.break_start_utc is not None
    assert bounds.break_end_utc is not None
    assert calendars.phase_at("XHKG", bounds.open_utc - 15 * MINUTE).phase is PhaseKind.PRE_MARKET
    assert (
        calendars.phase_at("XHKG", bounds.break_start_utc + 15 * MINUTE).phase
        is PhaseKind.LUNCH_BREAK
    )
    assert (
        calendars.phase_at("XHKG", bounds.close_utc + 5 * MINUTE).phase is PhaseKind.CLOSING_AUCTION
    )


def test_paris_has_a_long_opening_call_and_a_post_market_after_the_closing_call(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    bounds = _bounds(calendars, "XPAR")
    start, _ = _offsets(shipped_settings, "XPAR")["opening_auction"]
    assert start <= -60  # a call lasting more than an hour, unlike most venues
    assert (
        calendars.phase_at("XPAR", bounds.open_utc - 30 * MINUTE).phase is PhaseKind.OPENING_AUCTION
    )
    _, end = _offsets(shipped_settings, "XPAR")["closing_auction"]
    assert (
        calendars.phase_at("XPAR", bounds.close_utc + timedelta(minutes=end) + MINUTE).phase
        is PhaseKind.POST_MARKET
    )


def test_taiwan_models_only_a_closing_auction(shipped_settings: ObliskConfig) -> None:
    assert set(_offsets(shipped_settings, "XTAI")) == {"closing_auction"}


def test_the_singapore_midday_break_is_now_detected(calendars: Calendars) -> None:
    """The legacy engine showed REGULAR straight through SGX's lunch (an xfail there)."""
    bounds = _bounds(calendars, "XSES")
    assert bounds.break_start_utc is not None
    assert (
        calendars.phase_at("XSES", bounds.break_start_utc + 10 * MINUTE).phase
        is PhaseKind.LUNCH_BREAK
    )


@pytest.mark.parametrize("mic", ["XNAS", "XMEX", "XSGO"])
def test_the_americas_venues_trade_a_regular_session(calendars: Calendars, mic: str) -> None:
    bounds = _bounds(calendars, mic)
    middle = bounds.open_utc + (bounds.close_utc - bounds.open_utc) / 2
    assert calendars.phase_at(mic, middle).phase is PhaseKind.REGULAR
    assert calendars.is_open(mic, middle)


@pytest.mark.parametrize(
    "day", [date(2026, 3, 6), date(2026, 3, 9), date(2026, 10, 30), date(2026, 11, 3)]
)
def test_the_b3_closing_auction_ends_at_the_shifted_close(
    calendars: Calendars, shipped_settings: ObliskConfig, day: date
) -> None:
    bounds = _bounds(calendars, "BVMF", day)
    start, end = _offsets(shipped_settings, "BVMF")["closing_auction"]
    assert end == 0
    state = calendars.phase_at("BVMF", bounds.close_utc + timedelta(minutes=start) + MINUTE)
    assert state.phase is PhaseKind.CLOSING_AUCTION
    assert state.until == bounds.close_utc


@pytest.mark.parametrize("mic", ["XMAD", "XMEX", "XTKS"])
def test_the_last_phase_of_the_day_is_followed_by_the_close(calendars: Calendars, mic: str) -> None:
    last = calendars.day_transitions(mic, DAY)[-1]
    assert last.to_phase is PhaseKind.CLOSED
    state = calendars.phase_at(mic, last.at - MINUTE)
    assert state.next_phase is PhaseKind.CLOSED
    assert state.until == last.at
    after = calendars.phase_at(mic, last.at + MINUTE)
    assert after.phase is PhaseKind.CLOSED
    assert after.until is not None
    assert after.until > last.at
    assert after.since == last.at


# ── the state outside a session ──


def test_between_sessions_the_state_names_the_next_phase(calendars: Calendars) -> None:
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    state = calendars.phase_at("XNYS", saturday)
    assert state.phase is PhaseKind.CLOSED
    assert state.session_date is None
    assert state.next_phase is PhaseKind.PRE_MARKET
    assert state.since is not None
    assert state.until is not None
    assert state.since < saturday < state.until


def test_a_session_state_names_its_session_date(calendars: Calendars) -> None:
    bounds = _bounds(calendars, "XTKS")
    assert calendars.phase_at("XTKS", bounds.open_utc + MINUTE).session_date == DAY


def test_the_state_outside_the_calendar_raises(calendars: Calendars) -> None:
    with pytest.raises(CalendarCoverageError):
        calendars.phase_at("XSHG", datetime(2031, 1, 1, tzinfo=UTC))


def test_the_state_at_the_edges_of_the_window_has_no_neighbour() -> None:
    config = SyntheticCalendarConfig(
        open="10:00",
        close="12:00",
        trading_days=["mon", "tue", "wed", "thu", "fri"],
        holidays=[],
        holidays_through=date(2026, 10, 16),
    )
    schedule = SyntheticSchedule("TEST", "UTC", config, None, DAY, date(2026, 12, 31))
    first = schedule.session(DAY)
    last = schedule.session(date(2026, 10, 16))
    assert first is not None
    assert last is not None
    before = phase_state(schedule, [], first.open - timedelta(hours=3))
    assert before.phase is PhaseKind.CLOSED
    assert before.since is None
    assert before.until == first.open
    after = phase_state(schedule, [], last.close + MINUTE)
    assert after.phase is PhaseKind.CLOSED
    assert after.until is None
    assert after.next_phase is None


# ── transitions ──


def test_a_new_york_day_moves_through_every_phase_in_order(calendars: Calendars) -> None:
    kinds = [t.to_phase for t in calendars.day_transitions("XNYS", DAY)]
    assert kinds == [
        PhaseKind.PRE_MARKET,
        PhaseKind.REGULAR,
        PhaseKind.CLOSING_AUCTION,
        PhaseKind.POST_MARKET,
        PhaseKind.CLOSED,
    ]


def test_a_lunch_day_moves_through_the_break_and_back(calendars: Calendars) -> None:
    kinds = [t.to_phase for t in calendars.day_transitions("XTKS", DAY)]
    assert kinds.count(PhaseKind.LUNCH_BREAK) == 1
    assert kinds.count(PhaseKind.REGULAR) == 2
    at = [t.at for t in calendars.day_transitions("XTKS", DAY)]
    assert at == sorted(at)


def test_a_closed_day_has_no_transitions(calendars: Calendars) -> None:
    assert calendars.day_transitions("XNYS", date(2026, 10, 10)) == ()


def test_a_gap_between_phases_passes_through_closed() -> None:
    config = SyntheticCalendarConfig(
        open="10:00",
        close="12:00",
        trading_days=["mon", "tue", "wed", "thu", "fri"],
        holidays=[],
        holidays_through=date(2026, 12, 31),
    )
    schedule = SyntheticSchedule("TEST", "UTC", config, None, DAY, date(2026, 12, 31))
    late = _window("post_market", "session_close", 30, 60)
    changes = transitions(schedule, [late], DAY)
    assert [(c.from_phase, c.to_phase) for c in changes] == [
        (PhaseKind.CLOSED, PhaseKind.REGULAR),
        (PhaseKind.REGULAR, PhaseKind.CLOSED),
        (PhaseKind.CLOSED, PhaseKind.POST_MARKET),
        (PhaseKind.POST_MARKET, PhaseKind.CLOSED),
    ]
    assert isinstance(changes[0], PhaseTransition)
    state = phase_state(schedule, [late], schedule.session(DAY).close + 10 * MINUTE)  # type: ignore[union-attr]
    assert state.phase is PhaseKind.CLOSED
    assert state.next_phase is PhaseKind.POST_MARKET


# ── a half day moves the close and every window anchored to it ──


def test_a_half_day_moves_the_closing_windows(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    normal = _bounds(calendars, "XNYS", date(2026, 11, 25))
    half = _bounds(calendars, "XNYS", date(2026, 11, 27))
    assert half.early_close
    start, end = _offsets(shipped_settings, "XNYS")["closing_auction"]
    in_auction = half.close_utc + timedelta(minutes=start) + MINUTE
    state = calendars.phase_at("XNYS", in_auction)
    assert state.phase is PhaseKind.CLOSING_AUCTION
    assert state.until == half.close_utc + timedelta(minutes=end)
    # The same clock time on a normal day is still regular trading.
    same_clock = normal.open_utc + (in_auction - half.open_utc)
    assert calendars.phase_at("XNYS", same_clock).phase is PhaseKind.REGULAR


# ── building a session's phases ──


def _times(minutes: int = 120) -> SessionTimes:
    open_ = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    return SessionTimes(DAY, open_, open_ + timedelta(minutes=minutes), None, None, False)


def test_windows_inside_the_regular_block_take_time_out_of_it() -> None:
    phases = session_phases("TEST", [_window("closing_auction", "session_close", -10, 0)], _times())
    assert [p.kind for p in phases] == [PhaseKind.REGULAR, PhaseKind.CLOSING_AUCTION]
    assert phases[0].end == phases[1].start == _times().close - 10 * MINUTE


def test_windows_that_overlap_each_other_are_an_inconsistent_session() -> None:
    windows = [
        _window("pre_market", "session_open", -60, 0),
        _window("closing_auction", "session_close", -130, 0),
    ]
    with pytest.raises(ScheduleInconsistentError, match="windows overlap"):
        session_phases("TEST", windows, _times())


def test_a_break_inside_a_window_is_an_inconsistent_session() -> None:
    open_ = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    session = SessionTimes(
        DAY, open_, open_ + timedelta(hours=4), open_ + timedelta(hours=2),
        open_ + timedelta(hours=3), False,
    )  # fmt: skip
    window = _window("closing_auction", "session_close", -150, 0)
    with pytest.raises(ScheduleInconsistentError, match="phases overlap"):
        session_phases("TEST", [window], session)
