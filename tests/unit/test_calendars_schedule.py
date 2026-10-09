"""Schedules: sessions, holidays, breaks, daylight saving, coverage and the sanity rules.

Expected values come from rules written out in the test (computus, nth weekday, ``zoneinfo``),
from the configuration under test, or from the library queried by a different route; none is
typed from memory.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import pytest
from hypothesis import given
from hypothesis import strategies as st

from obliskiq.calendars.schedule import (
    LibrarySchedule,
    SyntheticSchedule,
    UncoveredSchedule,
    _library_calendar,
    _sanitise,
)
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_calendars import (
    LunchBreakConfig,
    Provenance,
    SeasonalCloseConfig,
    SyntheticCalendarConfig,
)
from obliskiq.errors import CalendarCoverageError, ConfigError, ScheduleInconsistentError

START = date(2024, 10, 9)
END = date(2029, 10, 9)
NOTE = Provenance(origin="test", verify_live=False)


@pytest.fixture(scope="module")
def library(shipped_settings: ObliskConfig) -> Callable[[str], LibrarySchedule]:
    """Library schedules built as the facade builds them, cached for the module."""
    cache: dict[str, LibrarySchedule] = {}

    def build(mic: str) -> LibrarySchedule:
        if mic not in cache:
            schedule = LibrarySchedule.build(
                mic,
                shipped_settings.venues[mic].timezone,
                START,
                END,
                shipped_settings.lunch_breaks.get(mic),
                shipped_settings.seasonal_closes.get(mic),
            )
            assert schedule is not None
            cache[mic] = schedule
        return cache[mic]

    return build


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ── a session is stored in UTC and follows the library ──


def test_a_session_is_in_utc_and_matches_the_library_read_another_way(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XETR")
    day = date(2026, 10, 9)
    session = schedule.session(day)
    assert session is not None
    oracle = xc.get_calendar("XETR", start=START, end=END)
    assert session.open == oracle.opens.loc[str(day)].to_pydatetime()
    assert session.close == oracle.closes.loc[str(day)].to_pydatetime()
    assert session.open.utcoffset() == timedelta(0)
    assert session.break_start is None
    assert not session.early_close


def test_coverage_is_the_library_window_inside_the_request(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XETR")
    assert schedule.coverage is not None
    first, last = schedule.coverage
    assert START <= first <= last <= END
    assert schedule.covers(first)
    assert schedule.covers(last)
    assert not schedule.covers(first - timedelta(days=1))
    assert not schedule.covers(last + timedelta(days=1))


def test_asking_outside_the_window_raises_rather_than_answering(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XETR")
    assert schedule.coverage is not None
    beyond = schedule.coverage[1] + timedelta(days=1)
    for ask in (
        lambda: schedule.session(beyond),
        lambda: schedule.is_session(beyond),
        lambda: schedule.sessions_between(beyond, beyond),
        lambda: schedule.last_completed_session(datetime.combine(beyond, time(12), tzinfo=UTC)),
        lambda: schedule.next_open(datetime.combine(beyond, time(12), tzinfo=UTC)),
        lambda: schedule.next_close(datetime.combine(beyond, time(12), tzinfo=UTC)),
    ):
        with pytest.raises(CalendarCoverageError, match="outside the calendar built"):
            ask()


def test_a_precomputed_calendar_ends_with_its_last_recorded_year(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XSHG")
    assert schedule.coverage is not None
    assert schedule.coverage[1] < END
    assert schedule.coverage[1].month == 12
    assert schedule.coverage[1].day == 31
    end = datetime.combine(schedule.coverage[1], time(12), tzinfo=UTC)
    assert schedule.next_open(end) is None
    assert schedule.next_close(end) is None
    assert schedule.next_session(schedule.coverage[1]) is None


def test_the_calendar_builder_clamps_a_window_that_leaves_the_library() -> None:
    clamped = _library_calendar("XSHG", date(2025, 1, 1), date(2031, 1, 1))
    assert clamped is not None
    assert clamped.last_session.date() < date(2031, 1, 1)
    lower = _library_calendar("XSHG", date(1980, 1, 1), date(2026, 1, 1))
    assert lower is not None
    assert lower.first_session.date() > date(1980, 1, 1)


def test_a_window_entirely_past_the_library_builds_nothing() -> None:
    assert _library_calendar("XSHG", date(2031, 1, 1), date(2032, 1, 1)) is None


def test_a_library_window_entirely_past_the_library_builds_no_schedule() -> None:
    assert (
        LibrarySchedule.build(
            "XSHG", "Asia/Shanghai", date(2031, 1, 1), date(2032, 1, 1), None, None
        )
        is None
    )


def test_an_unknown_calendar_name_is_a_configuration_error() -> None:
    with pytest.raises(ConfigError, match="not an exchange_calendars calendar"):
        _library_calendar("ZZZZ", START, END)


def test_a_zone_that_disagrees_with_the_library_is_a_configuration_error() -> None:
    with pytest.raises(ConfigError, match="not the library's time zone"):
        LibrarySchedule.build("XNYS", "Europe/London", START, END, None, None)


# ── holidays ──


def test_good_friday_and_easter_monday_are_closed_where_computus_says(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    good_friday = rules.easter_sunday(2026) - timedelta(days=2)
    easter_monday = rules.easter_sunday(2026) + timedelta(days=1)
    assert good_friday == date(2026, 4, 3)
    for mic in ("XETR", "XEUR", "XPAR", "XMAD", "XLON", "XNYS"):
        schedule = library(mic)
        assert schedule.session(good_friday) is None, mic
        assert schedule.is_business_weekday(good_friday), mic
    # Easter Monday is an exchange holiday in Europe and an ordinary day in New York.
    for mic in ("XETR", "XEUR", "XPAR", "XMAD", "XLON"):
        assert library(mic).session(easter_monday) is None, mic
    assert library("XNYS").session(easter_monday) is not None


def test_a_weekend_is_not_a_business_weekday(library: Callable[[str], LibrarySchedule]) -> None:
    schedule = library("XNYS")
    saturday = date(2026, 10, 10)
    assert saturday.weekday() == 5
    assert schedule.session(saturday) is None
    assert not schedule.is_business_weekday(saturday)


def test_next_and_previous_session_step_over_the_easter_holidays(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    thursday = rules.easter_sunday(2026) - timedelta(days=3)
    tuesday = rules.easter_sunday(2026) + timedelta(days=2)
    monday = rules.easter_sunday(2026) + timedelta(days=1)
    assert library("XETR").next_session(thursday) == tuesday
    assert library("XNYS").next_session(thursday) == monday
    assert library("XETR").previous_session(tuesday) == thursday
    assert library("XNYS").previous_session(tuesday) == monday


def test_next_session_before_the_window_starts_at_the_first_session(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XNYS")
    assert schedule.coverage is not None
    first = schedule.next_session(date(2000, 1, 1))
    assert first is not None
    assert first >= schedule.coverage[0]
    assert schedule.is_session(first)
    assert schedule.previous_session(date(2000, 1, 1)) is None
    assert schedule.previous_session(first) is None


def test_sessions_between_lists_session_days_only(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    schedule = library("XETR")
    good_friday = rules.easter_sunday(2026) - timedelta(days=2)
    days = schedule.sessions_between(good_friday - timedelta(days=1), good_friday + timedelta(7))
    assert good_friday not in days
    assert all(day.weekday() < 5 for day in days)
    assert days[0] == good_friday - timedelta(days=1)


# ── daylight saving: the local open is constant, the UTC open moves ──


def _local_open(schedule: LibrarySchedule, day: date) -> time:
    session = schedule.session(day)
    assert session is not None
    return session.open.astimezone(schedule.zone).time()


def test_the_european_local_open_is_constant_across_the_autumn_change(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    change = rules.last_weekday(2026, 10, 6)
    assert change == date(2026, 10, 25)
    friday, monday = change - timedelta(days=2), change + timedelta(days=1)
    for mic in ("XETR", "XPAR", "XMAD", "XEUR"):
        schedule = library(mic)
        assert _local_open(schedule, friday) == _local_open(schedule, monday), mic
        before, after = schedule.session(friday), schedule.session(monday)
        assert before is not None
        assert after is not None
        # Three days apart in UTC plus the hour the clocks went back.
        assert after.open - before.open == timedelta(days=3, hours=1), mic


def test_the_american_local_open_is_constant_across_the_autumn_change(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    change = rules.nth_weekday(2026, 11, 6, 1)
    assert change == date(2026, 11, 1)
    friday, monday = change - timedelta(days=2), change + timedelta(days=1)
    for mic in ("XNYS", "XNAS"):
        schedule = library(mic)
        assert _local_open(schedule, friday) == _local_open(schedule, monday), mic
        before, after = schedule.session(friday), schedule.session(monday)
        assert before is not None
        assert after is not None
        assert after.open - before.open == timedelta(days=3, hours=1), mic
        assert after.close - after.open == before.close - before.open, mic


def test_the_spring_changes_move_the_utc_open_the_other_way(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    europe = rules.last_weekday(2026, 3, 6)
    america = rules.nth_weekday(2026, 3, 6, 2)
    assert (europe, america) == (date(2026, 3, 29), date(2026, 3, 8))
    for mic, change in (("XETR", europe), ("XNYS", america)):
        schedule = library(mic)
        friday, monday = change - timedelta(days=2), change + timedelta(days=1)
        before, after = schedule.session(friday), schedule.session(monday)
        assert before is not None
        assert after is not None
        assert _local_open(schedule, friday) == _local_open(schedule, monday), mic
        assert after.open - before.open == timedelta(days=3, hours=-1), mic


def test_friday_close_to_monday_open_is_elapsed_time_not_wall_clock(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    """Subtracting two datetimes that share one ``ZoneInfo`` ignores the hour the clocks
    changed; the legacy pulse engine did that. Durations here are taken on UTC instants."""
    schedule = library("XETR")
    change = rules.last_weekday(2026, 10, 6)
    friday = schedule.session(change - timedelta(days=2))
    monday = schedule.session(change + timedelta(days=1))
    assert friday is not None
    assert monday is not None
    wall_clock = monday.open.astimezone(schedule.zone) - friday.close.astimezone(schedule.zone)
    elapsed = schedule.next_open(friday.close)
    assert elapsed is not None
    assert elapsed == monday.open
    assert elapsed - friday.close == wall_clock + timedelta(hours=1)


def test_a_synthetic_week_across_the_european_change_has_hand_computed_instants() -> None:
    config = SyntheticCalendarConfig(
        open="09:00",
        close="17:30",
        trading_days=["mon", "tue", "wed", "thu", "fri"],
        holidays=[],
        holidays_through=date(2026, 12, 31),
    )
    schedule = SyntheticSchedule("TEST", "Europe/Berlin", config, None, date(2026, 10, 1), END)
    friday_close = _utc(2026, 10, 23, 15, 30)  # 17:30 at UTC+2
    monday_open = _utc(2026, 10, 26, 8, 0)  # 09:00 at UTC+1, the clocks went back on Sunday
    friday = schedule.session(date(2026, 10, 23))
    assert friday is not None
    assert friday.close == friday_close
    assert schedule.next_open(friday_close) == monday_open
    assert monday_open - friday_close == timedelta(days=2, hours=16, minutes=30)


# ── lunch breaks ──


def test_a_native_lunch_break_sits_inside_the_session(
    library: Callable[[str], LibrarySchedule],
) -> None:
    for mic in ("XTKS", "XHKG"):
        session = library(mic).session(date(2026, 10, 9))
        assert session is not None
        assert session.break_start is not None
        assert session.break_end is not None
        assert session.open < session.break_start < session.break_end < session.close, mic


def test_a_hong_kong_half_day_has_no_lunch_break(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XHKG")
    early = sorted(
        stamp.date()
        for stamp in xc.get_calendar("XHKG", start=START, end=END).early_closes
        if date(2026, 1, 1) <= stamp.date() <= date(2026, 12, 31)
    )
    assert early
    for day in early:
        session = schedule.session(day)
        assert session is not None
        assert session.early_close
        assert session.break_start is None
        assert session.break_end is None


def test_a_break_at_or_after_the_close_is_dropped() -> None:
    open_, close = _utc(2026, 10, 9, 1), _utc(2026, 10, 9, 4)
    at_close = _sanitise("TEST", date(2026, 10, 9), open_, close, close, close + timedelta(hours=1))
    assert at_close == (None, None)
    after = _sanitise(
        "TEST", date(2026, 10, 9), open_, close, close + timedelta(hours=1), close + timedelta(2)
    )
    assert after == (None, None)
    assert _sanitise("TEST", date(2026, 10, 9), open_, close, None, None) == (None, None)


def test_a_break_inside_the_session_is_kept() -> None:
    open_, close = _utc(2026, 10, 9, 1), _utc(2026, 10, 9, 8)
    start, end = _utc(2026, 10, 9, 3), _utc(2026, 10, 9, 4)
    assert _sanitise("TEST", date(2026, 10, 9), open_, close, start, end) == (start, end)


@pytest.mark.parametrize(
    ("break_start", "break_end"),
    [
        (_utc(2026, 10, 9, 1), _utc(2026, 10, 9, 2)),  # starts at the open
        (_utc(2026, 10, 9, 4), _utc(2026, 10, 9, 3)),  # ends before it starts
        (_utc(2026, 10, 9, 3), _utc(2026, 10, 9, 8)),  # ends at the close
        (_utc(2026, 10, 9, 3), _utc(2026, 10, 9, 9)),  # ends after the close
    ],
)
def test_a_contradictory_break_is_an_inconsistent_schedule(
    break_start: datetime, break_end: datetime
) -> None:
    with pytest.raises(ScheduleInconsistentError, match="break does not fit"):
        _sanitise(
            "TEST",
            date(2026, 10, 9),
            _utc(2026, 10, 9, 1),
            _utc(2026, 10, 9, 8),
            break_start,
            break_end,
        )


def test_a_session_that_closes_before_it_opens_is_inconsistent() -> None:
    with pytest.raises(ScheduleInconsistentError, match="opens at or after it closes"):
        _sanitise("TEST", date(2026, 10, 9), _utc(2026, 10, 9, 8), _utc(2026, 10, 9, 8), None, None)


def test_the_singapore_midday_break_comes_from_configuration(
    library: Callable[[str], LibrarySchedule], shipped_settings: ObliskConfig
) -> None:
    lunch = shipped_settings.lunch_breaks["XSES"]
    schedule = library("XSES")
    assert not xc.get_calendar("XSES").has_break  # the library skips the break; config adds it
    session = schedule.session(date(2026, 10, 9))
    assert session is not None
    assert session.break_start is not None
    assert session.break_end is not None
    assert session.break_start.astimezone(schedule.zone).time() == time.fromisoformat(lunch.start)
    assert session.break_end.astimezone(schedule.zone).time() == time.fromisoformat(lunch.end)


def test_a_lunch_override_over_a_library_break_is_a_configuration_error() -> None:
    lunch = LunchBreakConfig(start="12:00", end="13:00", source=NOTE)
    with pytest.raises(ConfigError, match="library now carries a break"):
        LibrarySchedule.build("XTKS", "Asia/Tokyo", START, END, lunch, None)


# ── early closes ──


def test_the_day_after_us_thanksgiving_closes_early(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    thanksgiving = rules.nth_weekday(2026, 11, 3, 4)
    half_day = thanksgiving + timedelta(days=1)
    assert half_day == date(2026, 11, 27)
    for mic in ("XNYS", "XNAS"):
        schedule = library(mic)
        session = schedule.session(half_day)
        normal = schedule.session(half_day - timedelta(days=2))
        assert session is not None
        assert normal is not None
        assert session.early_close, mic
        assert not normal.early_close, mic
        assert session.close - session.open < normal.close - normal.open, mic


# ── a seasonal close (B3) ──


def test_the_b3_close_follows_new_york_daylight_saving_time(
    library: Callable[[str], LibrarySchedule], shipped_settings: ObliskConfig
) -> None:
    seasonal = shipped_settings.seasonal_closes["BVMF"]
    schedule = library("BVMF")
    oracle = xc.get_calendar("BVMF", start=START, end=END)
    new_york = ZoneInfo(seasonal.reference_timezone)
    assert schedule.coverage is not None
    shifted = unshifted = 0
    for day in schedule.sessions_between(*schedule.coverage):
        session = schedule.session(day)
        assert session is not None
        if session.early_close:
            continue
        if datetime.combine(day, time(12), tzinfo=new_york).dst():
            expected = datetime.combine(
                day, time.fromisoformat(seasonal.close), tzinfo=schedule.zone
            ).astimezone(UTC)
            shifted += 1
        else:
            expected = oracle.closes.loc[str(day)].to_pydatetime()
            unshifted += 1
        assert session.close == expected, day
    assert shifted > 100
    assert unshifted > 100


@pytest.mark.parametrize(
    "day", [date(2026, 3, 6), date(2026, 3, 9), date(2026, 10, 30), date(2026, 11, 3)]
)
def test_the_b3_close_moves_on_the_days_around_the_us_changes(
    library: Callable[[str], LibrarySchedule], shipped_settings: ObliskConfig, day: date
) -> None:
    seasonal = shipped_settings.seasonal_closes["BVMF"]
    session = library("BVMF").session(day)
    assert session is not None
    new_york_dst = bool(
        datetime.combine(day, time(12), tzinfo=ZoneInfo(seasonal.reference_timezone)).dst()
    )
    local_close = session.close.astimezone(library("BVMF").zone).time()
    assert (local_close == time.fromisoformat(seasonal.close)) is new_york_dst


def test_a_seasonal_override_that_changes_nothing_is_a_configuration_error() -> None:
    oracle = xc.get_calendar("BVMF", start=START, end=END)
    library_close = oracle.closes.iloc[0].tz_convert("America/Sao_Paulo").strftime("%H:%M")
    seasonal = SeasonalCloseConfig(
        reference_timezone="America/New_York", close=library_close, source=NOTE
    )
    with pytest.raises(ConfigError, match="never closes later than"):
        LibrarySchedule.build("BVMF", "America/Sao_Paulo", START, END, None, seasonal)


def test_a_seasonal_close_later_than_the_librarys_is_a_configuration_error() -> None:
    oracle = xc.get_calendar("BVMF", start=START, end=END)
    zone = ZoneInfo("America/Sao_Paulo")
    library_close = oracle.closes.iloc[0].tz_convert(zone)
    later = (library_close + timedelta(hours=1)).strftime("%H:%M")
    seasonal = SeasonalCloseConfig(reference_timezone="America/New_York", close=later, source=NOTE)
    with pytest.raises(ConfigError, match="can only shorten the day"):
        LibrarySchedule.build("BVMF", "America/Sao_Paulo", START, END, None, seasonal)


# ── last completed session, next open and close ──


def test_last_completed_session_is_never_today_while_it_is_open(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XNYS")
    today = schedule.session(date(2026, 10, 9))
    assert today is not None
    assert schedule.last_completed_session(today.open + timedelta(hours=1)) == date(2026, 10, 8)
    assert schedule.last_completed_session(today.close - timedelta(seconds=1)) == date(2026, 10, 8)
    assert schedule.last_completed_session(today.close) == date(2026, 10, 9)
    assert schedule.last_completed_session(today.open - timedelta(hours=1)) == date(2026, 10, 8)


def test_last_completed_session_on_a_weekend_or_holiday_is_the_session_before(
    library: Callable[[str], LibrarySchedule], rules: Any
) -> None:
    schedule = library("XETR")
    assert schedule.last_completed_session(_utc(2026, 10, 10, 12)) == date(2026, 10, 9)
    assert schedule.last_completed_session(_utc(2026, 10, 11, 12)) == date(2026, 10, 9)
    good_friday = rules.easter_sunday(2026) - timedelta(days=2)
    assert schedule.last_completed_session(
        datetime.combine(good_friday, time(12), tzinfo=UTC)
    ) == good_friday - timedelta(days=1)


def test_next_open_and_close_are_strictly_after_the_instant(
    library: Callable[[str], LibrarySchedule],
) -> None:
    schedule = library("XNYS")
    today = schedule.session(date(2026, 10, 9))
    following = schedule.session(date(2026, 10, 12))
    assert today is not None
    assert following is not None
    assert schedule.next_open(today.open - timedelta(minutes=1)) == today.open
    assert schedule.next_open(today.open) == following.open
    assert schedule.next_close(today.open) == today.close
    assert schedule.next_close(today.close) == following.close
    assert schedule.next_open(today.close) == following.open


def test_session_day_is_the_local_calendar_date(library: Callable[[str], LibrarySchedule]) -> None:
    tokyo = library("XTKS")
    assert tokyo.session_day(_utc(2026, 10, 9, 23, 30)) == date(2026, 10, 10)
    assert library("XNYS").session_day(_utc(2026, 10, 10, 1, 0)) == date(2026, 10, 9)


@given(
    instant=st.datetimes(
        min_value=datetime(2025, 1, 1), max_value=datetime(2026, 11, 30), timezones=st.just(UTC)
    )
)
def test_schedule_navigation_invariants(
    library: Callable[[str], LibrarySchedule], instant: datetime
) -> None:
    for mic in ("XETR", "XNYS", "XTKS", "XSES"):
        schedule = library(mic)
        opening = schedule.next_open(instant)
        closing = schedule.next_close(instant)
        completed = schedule.last_completed_session(instant)
        assert opening is not None
        assert closing is not None
        assert completed is not None
        assert opening > instant
        assert closing > instant
        today = schedule.session(schedule.session_day(instant))
        in_session = today is not None and today.open <= instant < today.close
        assert (closing < opening) is in_session
        done = schedule.session(completed)
        assert done is not None
        assert done.close <= instant
        day = schedule.session_day(instant)
        previous, following = schedule.previous_session(day), schedule.next_session(day)
        assert previous is not None
        assert following is not None
        assert previous < day < following


# ── the uncovered schedule ──


def test_an_uncovered_schedule_covers_nothing_and_says_so() -> None:
    schedule = UncoveredSchedule("ZZZZ", ZoneInfo("UTC"))
    day = date(2026, 10, 9)
    assert schedule.coverage is None
    assert not schedule.covers(day)
    assert schedule.next_session(day) is None
    assert schedule.previous_session(day) is None
    with pytest.raises(CalendarCoverageError, match="outside the calendar built"):
        schedule.session(day)
    with pytest.raises(CalendarCoverageError, match="not covered"):
        schedule._session(day)
    with pytest.raises(CalendarCoverageError, match="not covered"):
        schedule._is_session(day)
    with pytest.raises(CalendarCoverageError, match="not covered"):
        schedule.is_business_weekday(day)


# ── a synthetic venue ──


def _synthetic(
    holidays: list[date] | None = None,
    through: date | None = date(2026, 12, 31),
    lunch: LunchBreakConfig | None = None,
    start: date = date(2026, 10, 1),
) -> SyntheticSchedule:
    config = SyntheticCalendarConfig(
        open="10:00",
        close="14:00",
        trading_days=["mon", "tue", "wed", "thu", "fri"],
        holidays=holidays or [],
        holidays_through=through,
    )
    return SyntheticSchedule("TEST", "Pacific/Fiji", config, lunch, start, END)


def test_a_synthetic_session_is_the_declared_hours_in_the_local_zone() -> None:
    schedule = _synthetic()
    session = schedule.session(date(2026, 10, 9))
    assert session is not None
    fiji = ZoneInfo("Pacific/Fiji")
    assert session.open == datetime(2026, 10, 9, 10, 0, tzinfo=fiji).astimezone(UTC)
    assert session.close == datetime(2026, 10, 9, 14, 0, tzinfo=fiji).astimezone(UTC)
    assert session.break_start is None
    assert not session.early_close


def test_a_synthetic_weekend_and_declared_holiday_are_closed() -> None:
    holiday = date(2026, 10, 12)
    schedule = _synthetic(holidays=[holiday])
    assert schedule.session(date(2026, 10, 10)) is None
    assert not schedule.is_business_weekday(date(2026, 10, 10))
    assert schedule.session(holiday) is None
    assert schedule.is_business_weekday(holiday)
    assert schedule.next_session(date(2026, 10, 9)) == date(2026, 10, 13)


def test_a_synthetic_venue_covers_nothing_past_its_holiday_horizon() -> None:
    schedule = _synthetic(through=date(2026, 12, 31))
    assert schedule.coverage == (date(2026, 10, 1), date(2026, 12, 31))
    assert schedule.covers(date(2026, 12, 31))
    assert not schedule.covers(date(2027, 1, 1))
    with pytest.raises(CalendarCoverageError):
        schedule.session(date(2027, 1, 4))


def test_a_synthetic_venue_with_no_declared_holidays_covers_nothing() -> None:
    assert _synthetic(through=None).coverage is None
    assert _synthetic(through=date(2026, 9, 30)).coverage is None


def test_a_synthetic_lunch_break_is_added_inside_the_session() -> None:
    lunch = LunchBreakConfig(start="11:30", end="12:30", source=NOTE)
    schedule = _synthetic(lunch=lunch)
    session = schedule.session(date(2026, 10, 9))
    assert session is not None
    assert session.break_start is not None
    assert session.break_end is not None
    assert session.open < session.break_start < session.break_end < session.close


def test_a_synthetic_lunch_outside_the_hours_is_an_inconsistent_schedule() -> None:
    lunch = LunchBreakConfig(start="09:00", end="10:30", source=NOTE)
    schedule = _synthetic(lunch=lunch)
    with pytest.raises(ScheduleInconsistentError):
        schedule.session(date(2026, 10, 9))
