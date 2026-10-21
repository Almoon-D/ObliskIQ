"""The calendar facade: status, tradability, incidents, source disagreement and expiries.

Fixed reference date 2026-10-09. Expected values come from rules written out here (computus,
nth weekday, ``zoneinfo``), from the configuration under test, from arithmetic on declared
hours, or from the library queried by a different route.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import exchange_calendars as xc
import pytest

from obliskiq.calendars.incidents import IncidentBook
from obliskiq.calendars.service import Calendars
from obliskiq.calendars.status import BlockReason, Disagreement, DisagreementKind
from obliskiq.config import ObliskConfig, load_config
from obliskiq.domain.calendar import (
    Incident,
    IncidentScope,
    IncidentSource,
    SessionObservation,
)
from obliskiq.errors import (
    CalendarCoverageError,
    CalendarError,
    ConfigError,
    IncidentRejectedError,
    UnknownExchangeCodeError,
    UnknownVenueError,
)

DAY = date(2026, 10, 9)  # a Friday: no holiday on any shipped venue
MINUTE = timedelta(minutes=1)
SYNTHETIC = ("CHIA", "XNSE", "XSHE", "XSPX")


def halt(**overrides: Any) -> Incident:
    fields: dict[str, Any] = {
        "incident_id": "halt-1",
        "origin": "manual-incidents.yaml",
        "source": IncidentSource.MANUAL,
        "scope": IncidentScope.VENUE,
        "venue": "XNYS",
        "conid": None,
        "start": datetime(2026, 10, 9, 15, 0, tzinfo=UTC),
        "expected_end": datetime(2026, 10, 9, 16, 0, tzinfo=UTC),
        "reason": "technical halt",
    }
    fields.update(overrides)
    return Incident(**fields)


def settings_with(config_dir: Path, owner_yaml: str) -> ObliskConfig:
    owner = config_dir / "owner.yaml"
    owner.write_text(owner.read_text(encoding="utf-8") + "\n" + owner_yaml, encoding="utf-8")
    return load_config(config_dir).settings


def build(settings: ObliskConfig, *venues: str, **kwargs: Any) -> Calendars:
    return Calendars.from_settings(settings, reference=DAY, venues=venues, **kwargs)


class FakeSource:
    """A cross-check source that answers from a table."""

    def __init__(
        self,
        name: str = "ibkr",
        table: dict[tuple[str, date], SessionObservation | None] | None = None,
    ) -> None:
        self.name = name
        self.table = table or {}
        self.calls = 0
        self.fail = False

    def observe(self, venue: str, day: date, conid: int | None) -> SessionObservation | None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("cache unavailable")
        return self.table.get((venue, day))


def observation(
    venue: str,
    day: date,
    *,
    session: bool,
    open_: datetime | None = None,
    close: datetime | None = None,
) -> SessionObservation:
    return SessionObservation(
        venue=venue, day=day, source="ibkr", is_session=session, open=open_, close=close
    )


# ── a day's status ──


def test_an_ordinary_day_is_ok_with_the_offsets_applied(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    status = calendars.venue_status("XNYS", DAY)
    assert status.ok
    assert status.reason is None
    bounds = calendars.session_bounds("XNYS", DAY)
    assert bounds is not None
    offsets = shipped_settings.calendars.order_offsets
    (window,) = calendars.tradable_windows("XNYS", DAY)
    assert window.start == bounds.open_utc + timedelta(minutes=offsets.after_open_minutes)
    assert window.end == bounds.close_utc - timedelta(minutes=offsets.before_close_minutes)


def test_a_lunch_day_has_two_windows_honouring_the_offsets(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    bounds = calendars.session_bounds("XTKS", DAY)
    assert bounds is not None
    assert bounds.break_start_utc is not None
    assert bounds.break_end_utc is not None
    offsets = shipped_settings.calendars.order_offsets
    morning, afternoon = calendars.tradable_windows("XTKS", DAY)
    assert morning.start == bounds.open_utc + timedelta(minutes=offsets.after_open_minutes)
    assert morning.end == bounds.break_start_utc
    assert afternoon.start == bounds.break_end_utc
    assert afternoon.end == bounds.close_utc - timedelta(minutes=offsets.before_close_minutes)


def test_a_venue_override_of_the_offsets_beats_the_default(config_dir: Path) -> None:
    settings = settings_with(
        config_dir,
        "calendars:\n  venue_order_offsets:\n    XNYS:\n"
        "      after_open_minutes: 0\n      before_close_minutes: 0\n",
    )
    calendars = build(settings, "XNYS", "XNAS")
    bounds = calendars.session_bounds("XNYS", DAY)
    assert bounds is not None
    (own,) = calendars.tradable_windows("XNYS", DAY)
    assert own.start == bounds.open_utc
    (default,) = calendars.tradable_windows("XNAS", DAY)
    assert default.start > bounds.open_utc


def test_offsets_that_leave_no_regular_time_block_the_day(config_dir: Path) -> None:
    settings = settings_with(
        config_dir,
        "calendars:\n  order_offsets:\n    after_open_minutes: 600\n    before_close_minutes: 0\n",
    )
    calendars = build(settings, "XNYS")
    status = calendars.venue_status("XNYS", DAY)
    assert status.reason is BlockReason.NO_TRADABLE_WINDOW
    assert calendars.tradable_windows("XNYS", DAY) == ()


def test_good_friday_is_not_a_session_and_the_next_open_is_the_next_session(
    calendars: Calendars, rules: Any
) -> None:
    good_friday = rules.easter_sunday(2026) - timedelta(days=2)
    for mic in ("XETR", "XEUR", "XPAR", "XMAD", "XLON", "XNYS"):
        status = calendars.venue_status(mic, good_friday)
        assert status.reason is BlockReason.NOT_A_SESSION, mic
        assert status.detail == "holiday"
        assert calendars.tradable_windows(mic, good_friday) == ()
        assert calendars.session_bounds(mic, good_friday) is None
    at = datetime.combine(good_friday, time(12), tzinfo=UTC)
    reopening = calendars.next_open("XETR", at)
    assert reopening is not None
    assert calendars.session_day("XETR", reopening) == rules.easter_sunday(2026) + timedelta(days=2)
    assert calendars.next_session("XNYS", good_friday) == good_friday + timedelta(days=3)
    assert calendars.previous_session("XETR", good_friday) == good_friday - timedelta(days=1)


def test_a_naive_datetime_is_refused_not_read_in_the_system_zone(calendars: Calendars) -> None:
    naive = datetime(2026, 10, 9, 12, 0)
    with pytest.raises(ValueError, match="time zone"):
        calendars.session_day("XNYS", naive)
    with pytest.raises(ValueError, match="time zone"):
        calendars.is_open("XNYS", naive)
    with pytest.raises(ValueError, match="time zone"):
        calendars.last_completed_session("XNYS", naive)


def test_a_weekend_says_weekend(calendars: Calendars) -> None:
    status = calendars.venue_status("XNYS", date(2026, 10, 10))
    assert status.reason is BlockReason.NOT_A_SESSION
    assert status.detail == "weekend"


def test_an_unknown_venue_is_blocked_not_open(calendars: Calendars) -> None:
    status = calendars.venue_status("NOPE", DAY)
    assert status.reason is BlockReason.UNKNOWN_VENUE
    assert calendars.tradability("NOPE", datetime(2026, 10, 9, 15, tzinfo=UTC)).reason is (
        BlockReason.UNKNOWN_VENUE
    )
    with pytest.raises(UnknownVenueError):
        calendars.session_bounds("NOPE", DAY)
    with pytest.raises(UnknownVenueError):
        calendars.venue("NOPE")


def test_a_date_beyond_the_window_is_blocked_not_open(calendars: Calendars) -> None:
    status = calendars.venue_status("XNYS", date(2032, 1, 4))
    assert status.reason is BlockReason.CALENDAR_NOT_COVERED
    assert calendars.venue_status("XNYS", date(2020, 1, 6)).reason is (
        BlockReason.CALENDAR_NOT_COVERED
    )
    assert not calendars.is_open("XNYS", datetime(2032, 1, 4, 15, tzinfo=UTC))


def test_a_precomputed_calendar_is_blocked_after_its_last_recorded_year(
    calendars: Calendars,
) -> None:
    coverage = calendars.coverage()["XSHG"]
    assert coverage is not None
    assert calendars.venue_status("XSHG", coverage[1]).reason is not (
        BlockReason.CALENDAR_NOT_COVERED
    )
    assert calendars.venue_status("XSHG", coverage[1] + timedelta(days=3)).reason is (
        BlockReason.CALENDAR_NOT_COVERED
    )


# ── synthetic venues ──


@pytest.mark.parametrize("mic", SYNTHETIC)
def test_a_shipped_synthetic_venue_is_blocked_until_its_holidays_are_sourced(
    calendars: Calendars, mic: str
) -> None:
    status = calendars.venue_status(mic, DAY)
    assert status.reason is BlockReason.CALENDAR_NOT_COVERED
    assert calendars.coverage()[mic] is None
    assert calendars.tradable_windows(mic, DAY) == ()
    assert not calendars.is_open(mic, datetime(2026, 10, 9, 2, tzinfo=UTC))


def test_a_synthetic_venue_with_a_declared_holiday_list_trades_inside_it(
    config_dir: Path,
) -> None:
    settings = settings_with(
        config_dir,
        "synthetic_calendars:\n  XSPX:\n    holidays: [2026-10-12]\n"
        "    holidays_through: 2026-12-31\n",
    )
    calendars = build(settings, "XSPX")
    assert calendars.coverage()["XSPX"] == (date(2024, 10, 9), date(2026, 12, 31))
    assert calendars.venue_status("XSPX", DAY).ok
    assert calendars.venue_status("XSPX", date(2026, 10, 12)).detail == "holiday"
    assert calendars.venue_status("XSPX", date(2026, 10, 10)).detail == "weekend"
    after = calendars.venue_status("XSPX", date(2027, 1, 4))
    assert after.reason is BlockReason.CALENDAR_NOT_COVERED
    (window,) = calendars.tradable_windows("XSPX", DAY)
    bounds = calendars.session_bounds("XSPX", DAY)
    assert bounds is not None
    assert window.start == bounds.open_utc + timedelta(minutes=30)
    assert window.end == bounds.close_utc - timedelta(minutes=15)


# ── building ──


def test_building_an_unconfigured_venue_is_an_error(shipped_settings: ObliskConfig) -> None:
    with pytest.raises(UnknownVenueError, match="NOPE"):
        build(shipped_settings, "XNYS", "NOPE")


def test_a_timezone_that_disagrees_with_the_library_is_a_build_error(config_dir: Path) -> None:
    settings = settings_with(config_dir, "venues:\n  XNYS:\n    timezone: Europe/London\n")
    with pytest.raises(ConfigError, match="not the library's time zone"):
        build(settings, "XNYS")


def test_a_lunch_override_where_the_library_has_a_break_is_a_build_error(
    config_dir: Path,
) -> None:
    settings = settings_with(
        config_dir,
        "lunch_breaks:\n  XTKS:\n    start: '11:30'\n    end: '12:30'\n"
        "    source:\n      origin: test\n      verify_live: false\n",
    )
    with pytest.raises(ConfigError, match="library now carries a break"):
        build(settings, "XTKS")


def test_overlapping_windows_on_a_short_session_make_it_inconsistent_not_open(
    config_dir: Path,
) -> None:
    settings = settings_with(
        config_dir,
        "venues:\n  XETR:\n    phase_windows:\n"
        "      - {phase: pre_market, anchor: session_open, start_offset_minutes: -60,"
        " end_offset_minutes: 0}\n"
        "      - {phase: closing_auction, anchor: session_close, start_offset_minutes: -600,"
        " end_offset_minutes: 0}\n",
    )
    calendars = build(settings, "XETR", "XNYS")
    status = calendars.venue_status("XETR", DAY)
    assert status.reason is BlockReason.SCHEDULE_INCONSISTENT
    assert calendars.tradable_windows("XETR", DAY) == ()
    with pytest.raises(CalendarCoverageError, match="schedule_inconsistent"):
        calendars.last_tradable_session("XETR", "XNYS", DAY)


# ── manual incidents: the planning view and the act-now view ──


def test_an_incident_blocks_the_planning_view_and_splits_the_act_now_view(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XNYS",), incidents=IncidentBook((halt(),)))
    status = calendars.venue_status("XNYS", DAY)
    assert status.reason is BlockReason.INCIDENT_OVERLAP
    assert status.partial
    assert [i.incident_id for i in status.incidents] == ["halt-1"]
    assert status.detail == "technical halt"
    before, after = calendars.tradable_windows("XNYS", DAY)
    assert before.end == datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
    assert after.start == datetime(2026, 10, 9, 16, 30, tzinfo=UTC)  # the end plus 30 min grace
    assert calendars.tradability("XNYS", before.start + MINUTE).ok
    assert calendars.tradability("XNYS", before.start + MINUTE).window == before


def test_the_act_now_view_says_why_each_instant_is_blocked(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XNYS",), incidents=IncidentBook((halt(),)))
    inside = calendars.tradability("XNYS", datetime(2026, 10, 9, 15, 30, tzinfo=UTC))
    assert inside.reason is BlockReason.INCIDENT_OVERLAP
    in_grace = calendars.tradability("XNYS", datetime(2026, 10, 9, 16, 15, tzinfo=UTC))
    assert in_grace.reason is BlockReason.INCIDENT_OVERLAP
    resumed = calendars.tradability("XNYS", datetime(2026, 10, 9, 17, 0, tzinfo=UTC))
    assert resumed.ok
    early = calendars.tradability("XNYS", datetime(2026, 10, 9, 13, 35, tzinfo=UTC))
    assert early.reason is BlockReason.OUTSIDE_WINDOW
    saturday = calendars.tradability("XNYS", datetime(2026, 10, 10, 15, 0, tzinfo=UTC))
    assert saturday.reason is BlockReason.NOT_A_SESSION


def test_an_incident_covering_the_whole_day_leaves_nothing_partial(
    build_calendars: Callable[..., Calendars],
) -> None:
    closure = halt(
        start=datetime(2026, 10, 9, 0, 0, tzinfo=UTC),
        expected_end=datetime(2026, 10, 9, 23, 0, tzinfo=UTC),
        reason="market closure",
    )
    calendars = build_calendars(("XNYS",), incidents=IncidentBook((closure,)))
    status = calendars.venue_status("XNYS", DAY)
    assert status.reason is BlockReason.INCIDENT_OVERLAP
    assert not status.partial
    assert calendars.tradable_windows("XNYS", DAY) == ()
    assert calendars.tradability("XNYS", datetime(2026, 10, 9, 15, 0, tzinfo=UTC)).reason is (
        BlockReason.INCIDENT_OVERLAP
    )


def test_an_open_ended_incident_blocks_every_later_day_until_it_is_removed(
    build_calendars: Callable[..., Calendars],
) -> None:
    open_ended = halt(expected_end=None)
    calendars = build_calendars(("XNYS",), incidents=IncidentBook((open_ended,)))
    later = date(2026, 10, 14)
    assert calendars.venue_status("XNYS", later).reason is BlockReason.INCIDENT_OVERLAP
    assert not calendars.venue_status("XNYS", later).partial
    calendars.replace_incidents(IncidentBook())
    assert calendars.venue_status("XNYS", later).ok


def test_an_incident_that_ended_does_not_block_the_next_day(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XNYS",), incidents=IncidentBook((halt(),)))
    assert calendars.venue_status("XNYS", date(2026, 10, 12)).ok
    assert calendars.venue_status("XNYS", date(2026, 10, 8)).ok


def test_an_instrument_incident_blocks_only_that_instrument(
    build_calendars: Callable[..., Calendars],
) -> None:
    one = halt(scope=IncidentScope.INSTRUMENT, conid=265598)
    calendars = build_calendars(("XNYS",), incidents=IncidentBook((one,)))
    assert calendars.venue_status("XNYS", DAY, conid=265598).reason is (
        BlockReason.INCIDENT_OVERLAP
    )
    assert calendars.venue_status("XNYS", DAY, conid=999).ok
    assert calendars.venue_status("XNYS", DAY).ok
    assert len(calendars.tradable_windows("XNYS", DAY, conid=265598)) == 2
    assert len(calendars.tradable_windows("XNYS", DAY, conid=999)) == 1


def test_an_incident_on_another_venue_does_not_touch_this_one(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XNYS", "XNAS"), incidents=IncidentBook((halt(),)))
    assert calendars.venue_status("XNAS", DAY).ok


def test_a_feed_may_raise_only_what_its_venue_declares(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XNYS", "XNAS"))
    feed = {"origin": "nasdaq-halts", "source": IncidentSource.FEED}
    single = halt(venue="XNAS", scope=IncidentScope.INSTRUMENT, conid=265598, **feed)
    calendars.accept_incidents("nasdaq-halts", [single])
    assert calendars.venue_status("XNAS", DAY, conid=265598).reason is (
        BlockReason.INCIDENT_OVERLAP
    )
    with pytest.raises(IncidentRejectedError, match="no incident feed"):
        calendars.accept_incidents(
            "nyse-halts",
            [halt(venue="XNYS", scope=IncidentScope.VENUE, **feed | {"origin": "nyse-halts"})],
        )
    with pytest.raises(IncidentRejectedError, match="instrument-scoped"):
        calendars.accept_incidents(
            "nasdaq-halts", [halt(venue="XNAS", scope=IncidentScope.VENUE, **feed)]
        )
    assert calendars.venue_status("XNAS", DAY, conid=265598).reason is (
        BlockReason.INCIDENT_OVERLAP
    )  # the refused replacement changed nothing


def test_building_without_a_data_directory_says_the_manual_file_is_not_read(
    build_calendars: Callable[..., Calendars], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="obliskiq.calendars"):
        build_calendars(("XNYS",))
    assert "manual incident file is not read" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="obliskiq.calendars"):
        build_calendars(("XNYS",), data_dir=tmp_path)
    assert "manual incident file is not read" not in caplog.text


def test_a_malformed_manual_file_blocks_every_venue_until_a_good_one_is_read(
    build_calendars: Callable[..., Calendars], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calendars = build_calendars(("XNYS", "XETR"))
    path = tmp_path / "manual-incidents.yaml"
    path.write_text("incidents:\n  - id: x\n    typo: 1\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="obliskiq.calendars"):
        book = calendars.reload_manual_incidents(path)
    assert book.invalid is not None
    assert "manual incident file unreadable" in caplog.text
    for mic in ("XNYS", "XETR"):
        assert calendars.venue_status(mic, DAY).reason is BlockReason.INCIDENT_FILE_INVALID
        assert calendars.tradable_windows(mic, DAY) == ()
        now = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
        assert calendars.tradability(mic, now).reason is BlockReason.INCIDENT_FILE_INVALID
    with pytest.raises(CalendarCoverageError, match="incident_file_invalid"):
        calendars.last_tradable_session("XETR", "XNYS", DAY)
    path.write_text("incidents: []\n", encoding="utf-8")
    assert calendars.reload_manual_incidents(path).invalid is None
    assert calendars.venue_status("XNYS", DAY).ok


def test_a_stale_incident_is_reported_once(
    build_calendars: Callable[..., Calendars], caplog: pytest.LogCaptureFixture
) -> None:
    calendars = build_calendars(("XNYS",), incidents=IncidentBook((halt(),)))
    late = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)
    assert calendars.stale_incidents(datetime(2026, 10, 9, 16, 0, tzinfo=UTC)) == ()
    with caplog.at_level(logging.WARNING, logger="obliskiq.calendars"):
        assert [i.incident_id for i in calendars.stale_incidents(late)] == ["halt-1"]
        assert [i.incident_id for i in calendars.stale_incidents(late)] == ["halt-1"]
    assert caplog.text.count("has run past its expected end") == 1


# ── cross-checks against another source ──


def test_a_disagreement_is_alerted_once_and_blocks_only_date_critical_queries(
    build_calendars: Callable[..., Calendars],
) -> None:
    source = FakeSource(table={("XNYS", DAY): observation("XNYS", DAY, session=False)})
    alerts: list[Disagreement] = []
    calendars = build_calendars(("XNYS",), sources=(source,), alert=alerts.append)
    ordinary = calendars.venue_status("XNYS", DAY)
    assert ordinary.ok
    assert [d.kind for d in ordinary.disagreements] == [DisagreementKind.IS_SESSION]
    assert calendars.venue_status("XNYS", DAY).ok
    assert len(alerts) == 1
    assert calendars.disagreements() == tuple(alerts)
    critical = calendars.venue_status("XNYS", DAY, date_critical=True)
    assert critical.reason is BlockReason.SOURCE_DISAGREEMENT
    assert "ibkr" in critical.detail
    assert len(alerts) == 1  # still one alert, however often it is asked


def test_the_tolerance_decides_whether_open_and_close_disagree(
    build_calendars: Callable[..., Calendars], shipped_settings: ObliskConfig
) -> None:
    tolerance = timedelta(minutes=shipped_settings.calendars.cross_check.time_tolerance_minutes)
    bare = build_calendars(("XNYS",))
    bounds = bare.session_bounds("XNYS", DAY)
    assert bounds is not None

    def disagreements(open_shift: timedelta, close_shift: timedelta) -> list[DisagreementKind]:
        seen = observation(
            "XNYS",
            DAY,
            session=True,
            open_=bounds.open_utc + open_shift,
            close=bounds.close_utc + close_shift,
        )
        calendars = build_calendars(("XNYS",), sources=(FakeSource(table={("XNYS", DAY): seen}),))
        return [d.kind for d in calendars.venue_status("XNYS", DAY).disagreements]

    assert disagreements(tolerance, -tolerance) == []
    assert disagreements(tolerance + MINUTE, timedelta(0)) == [DisagreementKind.OPEN]
    assert disagreements(timedelta(0), tolerance + MINUTE) == [DisagreementKind.CLOSE]
    assert disagreements(-tolerance - MINUTE, tolerance + MINUTE) == [
        DisagreementKind.OPEN,
        DisagreementKind.CLOSE,
    ]


def test_a_day_the_calendar_already_blocks_is_not_compared_with_a_source(
    build_calendars: Callable[..., Calendars],
) -> None:
    saturday = date(2026, 10, 10)
    seen = observation(
        "XNYS",
        saturday,
        session=True,
        open_=datetime(2026, 10, 10, 14, tzinfo=UTC),
        close=datetime(2026, 10, 10, 20, tzinfo=UTC),
    )
    calendars = build_calendars(("XNYS",), sources=(FakeSource(table={("XNYS", saturday): seen}),))
    status = calendars.venue_status("XNYS", saturday, date_critical=True)
    assert status.reason is BlockReason.NOT_A_SESSION  # the calendar's own block comes first
    assert [d.kind for d in calendars.disagreements()] == []  # a blocked day is not compared


def test_a_source_with_no_information_or_a_failure_never_blocks(
    build_calendars: Callable[..., Calendars], caplog: pytest.LogCaptureFixture
) -> None:
    silent = FakeSource(name="silent")
    broken = FakeSource(name="broken")
    broken.fail = True
    calendars = build_calendars(("XNYS",), sources=(silent, broken))
    with caplog.at_level(logging.ERROR, logger="obliskiq.calendars"):
        status = calendars.venue_status("XNYS", DAY, date_critical=True)
    assert status.ok
    assert silent.calls == 1
    assert "schedule source failed" in caplog.text


def test_a_failing_alert_sink_does_not_break_the_answer(
    build_calendars: Callable[..., Calendars], caplog: pytest.LogCaptureFixture
) -> None:
    def boom(_: Disagreement) -> None:
        raise RuntimeError("sink down")

    source = FakeSource(table={("XNYS", DAY): observation("XNYS", DAY, session=False)})
    calendars = build_calendars(("XNYS",), sources=(source,), alert=boom)
    with caplog.at_level(logging.ERROR, logger="obliskiq.calendars"):
        assert calendars.venue_status("XNYS", DAY).ok
    assert "disagreement alert sink failed" in caplog.text


def test_two_sources_are_alerted_separately(build_calendars: Callable[..., Calendars]) -> None:
    table = {("XNYS", DAY): observation("XNYS", DAY, session=False)}
    alerts: list[Disagreement] = []
    calendars = build_calendars(
        ("XNYS",),
        sources=(FakeSource("one", table), FakeSource("two", table)),
        alert=alerts.append,
    )
    calendars.venue_status("XNYS", DAY)
    calendars.venue_status("XNYS", DAY)
    assert sorted(a.source for a in alerts) == ["one", "two"]


# ── expiries: the last session both venues can trade ──


def _oracle(first: str, second: str, expiry: date) -> date:
    """Walk back to the last day both calendars list as a session, using the library alone."""
    one = xc.get_calendar(first, start=date(2024, 10, 9), end=date(2029, 10, 9))
    two = xc.get_calendar(second, start=date(2024, 10, 9), end=date(2029, 10, 9))
    day = expiry
    while not (one.is_session(day) and two.is_session(day)):
        day -= timedelta(days=1)
    return day


@pytest.mark.parametrize("instrument_venue", ["XEUR", "XPAR"])
def test_the_last_tradable_session_matches_the_library_for_every_day_of_two_years(
    calendars: Calendars, instrument_venue: str
) -> None:
    day = date(2026, 1, 1)
    while day <= date(2027, 12, 31):
        result = calendars.last_tradable_session(instrument_venue, "XNYS", day)
        assert result.session == _oracle(instrument_venue, "XNYS", day), day
        assert result.expiry == day
        assert result.session <= day
        day += timedelta(days=1)


def test_a_good_friday_expiry_lands_on_the_thursday(calendars: Calendars, rules: Any) -> None:
    good_friday = rules.easter_sunday(2026) - timedelta(days=2)
    result = calendars.last_tradable_session("XEUR", "XNYS", good_friday)
    assert result.session == good_friday - timedelta(days=1)
    assert [(s.venue, s.day, s.reason) for s in result.skipped] == [
        ("XEUR", good_friday, BlockReason.NOT_A_SESSION)
    ]


def test_easter_monday_with_a_us_underlying_lands_on_the_thursday_before(
    calendars: Calendars, rules: Any
) -> None:
    easter_monday = rules.easter_sunday(2026) + timedelta(days=1)
    result = calendars.last_tradable_session("XEUR", "XNYS", easter_monday)
    assert result.session == rules.easter_sunday(2026) - timedelta(days=3)  # Maundy Thursday
    assert calendars.venue_status("XNYS", easter_monday).ok  # New York is open that Monday
    assert result.skipped[0].venue == "XEUR"
    assert result.skipped[0].day == easter_monday
    assert len(result.skipped) == 4  # Easter Monday, Sunday, Saturday, Good Friday


def test_a_day_closed_for_only_one_venue_is_skipped_and_says_which(calendars: Calendars) -> None:
    saturday_fourth = date(2026, 7, 4)
    assert saturday_fourth.weekday() == 5  # so New York observes the holiday on Friday the 3rd
    friday = saturday_fourth - timedelta(days=1)
    assert calendars.venue_status("XEUR", friday).ok
    result = calendars.last_tradable_session("XEUR", "XNYS", friday)
    assert result.session == friday - timedelta(days=1)
    assert [(s.venue, s.reason) for s in result.skipped] == [("XNYS", BlockReason.NOT_A_SESSION)]


def test_a_normal_expiry_is_its_own_last_tradable_session(calendars: Calendars) -> None:
    result = calendars.last_tradable_session("XEUR", "XNYS", DAY)
    assert result.session == DAY
    assert result.skipped == ()


def test_a_source_disagreement_day_excludes_itself(
    build_calendars: Callable[..., Calendars],
) -> None:
    source = FakeSource(table={("XEUR", DAY): observation("XEUR", DAY, session=False)})
    calendars = build_calendars(("XEUR", "XNYS"), sources=(source,))
    result = calendars.last_tradable_session("XEUR", "XNYS", DAY)
    assert result.session == DAY - timedelta(days=1)
    assert [(s.venue, s.reason) for s in result.skipped] == [
        ("XEUR", BlockReason.SOURCE_DISAGREEMENT)
    ]


def test_an_incident_rules_a_day_out(build_calendars: Callable[..., Calendars]) -> None:
    calendars = build_calendars(("XEUR", "XNYS"), incidents=IncidentBook((halt(),)))
    result = calendars.last_tradable_session("XEUR", "XNYS", DAY)
    assert result.session == DAY - timedelta(days=1)
    assert result.skipped[0].reason is BlockReason.INCIDENT_OVERLAP


def test_an_expiry_beyond_the_calendars_raises_rather_than_guessing(calendars: Calendars) -> None:
    with pytest.raises(CalendarCoverageError, match="calendar_not_covered"):
        calendars.last_tradable_session("XEUR", "XNYS", date(2031, 1, 4))
    with pytest.raises(CalendarCoverageError):
        calendars.last_tradable_session("XSHG", "XNYS", date(2027, 6, 1))
    with pytest.raises(CalendarCoverageError, match="unknown_venue"):
        calendars.last_tradable_session("NOPE", "XNYS", DAY)


def test_a_walk_that_runs_out_of_calendar_raises(
    build_calendars: Callable[..., Calendars],
) -> None:
    forever = halt(start=datetime(2024, 1, 1, tzinfo=UTC), expected_end=None, venue="XEUR")
    calendars = build_calendars(("XEUR", "XNYS"), incidents=IncidentBook((forever,)))
    with pytest.raises(CalendarCoverageError, match="calendar_not_covered"):
        calendars.last_tradable_session("XEUR", "XNYS", DAY)


def test_session_before_counts_common_sessions_back_from_the_anchor(
    calendars: Calendars,
) -> None:
    thursday = date(2026, 10, 8)
    assert calendars.session_before("XEUR", "XNYS", DAY, 0) == DAY
    assert calendars.session_before("XEUR", "XNYS", DAY, 1) == thursday
    assert calendars.session_before("XEUR", "XNYS", DAY, 2) == date(2026, 10, 7)
    # Five sessions back over a weekend is the previous Friday.
    assert calendars.session_before("XEUR", "XNYS", DAY, 5) == date(2026, 10, 2)
    # Counting crosses the Good Friday gap.
    monday = date(2026, 4, 6)
    assert calendars.session_before("XEUR", "XNYS", date(2026, 4, 7), 1) == date(2026, 4, 2)
    assert calendars.venue_status("XNYS", monday).ok


def test_session_before_refuses_a_non_session_anchor_and_a_negative_count(
    calendars: Calendars,
) -> None:
    with pytest.raises(CalendarError, match="not a session of both venues"):
        calendars.session_before("XEUR", "XNYS", date(2026, 10, 10), 0)
    with pytest.raises(CalendarError, match="must not be negative"):
        calendars.session_before("XEUR", "XNYS", DAY, -1)


def test_common_sessions_between_lists_the_days_both_venues_trade(
    calendars: Calendars, rules: Any
) -> None:
    good_friday = rules.easter_sunday(2026) - timedelta(days=2)
    start, end = good_friday - timedelta(days=2), good_friday + timedelta(days=6)
    days = calendars.common_sessions_between("XEUR", "XNYS", start, end)
    expected = [
        d
        for offset in range((end - start).days + 1)
        if _oracle_is_common(d := start + timedelta(days=offset))
    ]
    assert list(days) == expected
    assert good_friday not in days
    assert good_friday + timedelta(days=3) not in days  # Easter Monday: Europe is closed


def _oracle_is_common(day: date) -> bool:
    one = xc.get_calendar("XEUR", start=date(2024, 10, 9), end=date(2029, 10, 9))
    two = xc.get_calendar("XNYS", start=date(2024, 10, 9), end=date(2029, 10, 9))
    return bool(one.is_session(day) and two.is_session(day))


# ── option legs: the venue and its underlying must both be open ──


def test_an_option_trades_only_where_the_option_and_the_underlying_venue_overlap(
    calendars: Calendars,
) -> None:
    common = calendars.common_windows(DAY, ("XEUR", None), ("XNYS", 265598))
    (eurex,) = calendars.tradable_windows("XEUR", DAY)
    (nyse,) = calendars.tradable_windows("XNYS", DAY)
    assert eurex.start < nyse.start  # Eurex's hours are wider than the US cash session
    assert len(common) == 1
    assert common[0].start == max(eurex.start, nyse.start)
    assert common[0].end == min(eurex.end, nyse.end)


def test_legs_with_no_overlap_have_no_common_window(calendars: Calendars) -> None:
    assert calendars.common_windows(DAY, ("XTKS", None), ("XNYS", None)) == ()


# ── the venue book ──


def test_the_ibkr_exchange_codes_resolve_to_the_governing_venue(calendars: Calendars) -> None:
    assert calendars.venue_for_ibkr("EUREX") == "XEUR"
    assert calendars.venue_for_ibkr("IBIS") == "XETR"
    assert calendars.venue_for_ibkr("TSEJ") == "XTKS"
    exchange = calendars.ibkr_exchange("FTA")
    assert exchange.venue == "XAMS"
    assert exchange.verify_live


@pytest.mark.parametrize("code", ["SMART", "NOPE", "", "eurex"])
def test_an_unmapped_exchange_code_raises_never_a_default(calendars: Calendars, code: str) -> None:
    with pytest.raises(UnknownExchangeCodeError):
        calendars.venue_for_ibkr(code)
    with pytest.raises(UnknownExchangeCodeError):
        calendars.ibkr_exchange(code)


def test_a_subset_build_knows_only_its_own_codes(
    build_calendars: Callable[..., Calendars],
) -> None:
    calendars = build_calendars(("XNYS",))
    assert calendars.venue_for_ibkr("NYSE") == "XNYS"
    with pytest.raises(UnknownExchangeCodeError):
        calendars.venue_for_ibkr("EUREX")
    assert [v.mic for v in calendars.venues()] == ["XNYS"]


def test_every_ibkr_code_points_at_a_built_venue(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    built = {venue.mic for venue in calendars.venues()}
    assert built == set(shipped_settings.venues)
    for code in shipped_settings.ibkr_exchanges:
        assert calendars.venue_for_ibkr(code) in built


def test_the_digest_reports_coverage_and_what_awaits_a_live_reading(
    calendars: Calendars,
) -> None:
    coverage = calendars.coverage()
    assert set(coverage) == {v.mic for v in calendars.venues()}
    assert [mic for mic, window in coverage.items() if window is None] == sorted(SYNTHETIC)
    pending = calendars.unverified()
    assert (
        "XSES",
        next(n for m, n in pending if m == "XSES" and n.what == "lunch_break"),
    ) in pending
    assert any(key == "EUREX" and note.what == "ibkr_exchange" for key, note in pending)
    provenance = calendars.provenance("XNYS")
    assert {n.what for n in provenance.notes} == {"calendar", "phases", "settlement"}


def test_is_open_follows_the_regular_phase_only(calendars: Calendars) -> None:
    bounds = calendars.session_bounds("XNYS", DAY)
    assert bounds is not None
    assert not calendars.is_open("XNYS", bounds.open_utc - 60 * MINUTE)  # pre-market
    assert calendars.is_open("XNYS", bounds.open_utc + MINUTE)
    assert not calendars.is_open("XNYS", bounds.close_utc - MINUTE)  # closing auction
    assert not calendars.is_open("XNYS", bounds.close_utc + MINUTE)


def test_session_bounds_carry_local_and_utc_times(calendars: Calendars) -> None:
    bounds = calendars.session_bounds("XTKS", DAY)
    assert bounds is not None
    assert bounds.open_local.utcoffset() == timedelta(hours=9)  # Tokyo has no daylight saving
    assert bounds.open_local == bounds.open_utc
    assert bounds.break_start_utc is not None
    assert bounds.open_utc < bounds.break_start_utc < bounds.close_utc
    assert not bounds.early_close


def test_next_open_and_close_and_last_completed_session_pass_through(
    calendars: Calendars,
) -> None:
    bounds = calendars.session_bounds("XNYS", DAY)
    assert bounds is not None
    assert calendars.next_open("XNYS", bounds.open_utc - MINUTE) == bounds.open_utc
    assert calendars.next_close("XNYS", bounds.open_utc) == bounds.close_utc
    assert calendars.last_completed_session("XNYS", bounds.close_utc) == DAY
    assert calendars.last_completed_session("XNYS", bounds.open_utc) == date(2026, 10, 8)
    assert calendars.session_day("XTKS", datetime(2026, 10, 9, 23, 30, tzinfo=UTC)) == date(
        2026, 10, 10
    )


def test_the_window_around_a_leap_day_reference_is_built(shipped_settings: ObliskConfig) -> None:
    calendars = Calendars.from_settings(
        shipped_settings, reference=date(2028, 2, 29), venues=("XNYS",)
    )
    window = calendars.coverage()["XNYS"]
    assert window is not None
    assert date(2026, 2, 28) <= window[0] <= date(2026, 3, 2)
    assert window[1] == date(2031, 2, 28)  # a Friday; 2031 has no 29 February


def test_a_venue_the_library_cannot_cover_in_the_window_is_blocked_everywhere(
    shipped_settings: ObliskConfig,
) -> None:
    calendars = Calendars.from_settings(
        shipped_settings, reference=date(2030, 1, 4), venues=("XSHG",)
    )
    assert calendars.coverage()["XSHG"] is None
    assert calendars.venue_status("XSHG", date(2030, 1, 4)).reason is (
        BlockReason.CALENDAR_NOT_COVERED
    )


def test_the_incident_book_can_be_read_back(
    build_calendars: Callable[..., Calendars],
) -> None:
    book = IncidentBook((halt(),))
    assert build_calendars(("XNYS",), incidents=book).incident_book is book


def test_a_source_that_names_a_session_without_times_is_not_compared(
    build_calendars: Callable[..., Calendars],
) -> None:
    seen = observation("XNYS", DAY, session=True)
    calendars = build_calendars(("XNYS",), sources=(FakeSource(table={("XNYS", DAY): seen}),))
    assert calendars.venue_status("XNYS", DAY, date_critical=True).ok


def test_the_manual_file_in_the_data_directory_is_read_when_built_and_reloaded(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    path = tmp_path / shipped_settings.paths.manual_incidents
    path.write_text(
        "incidents:\n  - id: halt-1\n    venue: XNYS\n    scope: venue\n"
        "    start: 2026-10-09T15:00:00+00:00\n    expected_end: 2026-10-09T16:00:00+00:00\n"
        "    reason: technical halt\n",
        encoding="utf-8",
    )
    calendars = Calendars.from_settings(
        shipped_settings, reference=DAY, venues=("XNYS",), data_dir=tmp_path
    )
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_OVERLAP
    path.write_text("incidents: []\n", encoding="utf-8")
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_OVERLAP
    calendars.reload_manual_incidents()
    assert calendars.venue_status("XNYS", DAY).ok


def test_an_absent_manual_file_means_no_incidents_and_reloading_needs_a_path(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    calendars = Calendars.from_settings(
        shipped_settings, reference=DAY, venues=("XNYS",), data_dir=tmp_path
    )
    assert calendars.venue_status("XNYS", DAY).ok
    bare = Calendars.from_settings(shipped_settings, reference=DAY, venues=("XNYS",))
    with pytest.raises(CalendarError, match="no manual incident file"):
        bare.reload_manual_incidents()
