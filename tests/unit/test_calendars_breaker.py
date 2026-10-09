"""Breaker tests for the calendar facade: the calendar can block and never open.

Each test attacks one way the facade could call a venue tradable when it is not: an input it
cannot judge (a date it does not cover, a venue it does not know, a manual file it cannot
trust, a schedule that contradicts itself or fails), an edge one microsecond either side (the
order offsets, a lunch break, an auction, an incident and its grace), a daylight-saving weekend,
a half day, a holiday expiry, a source disagreement, the machine's own time zone. Every
blocking assertion sits beside a positive control, so a guard gutted to "always blocked" fails
as surely as one gutted to "always open".

Expected values come from the configuration under test, from ``zoneinfo`` applied to exchange
hours written out here, from instants worked out by hand in UTC, or from an oracle written out
in this file from DECISIONS D28. Reference date 2026-10-09. No network.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import contextlib
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from obliskiq.calendars.incidents import IncidentBook
from obliskiq.calendars.schedule import LibrarySchedule, MarketSchedule, SessionTimes
from obliskiq.calendars.service import Calendars, VenueRuntime, _build_venue
from obliskiq.calendars.status import BlockReason, Disagreement, SessionBounds
from obliskiq.config import ObliskConfig, load_config
from obliskiq.domain.calendar import (
    Incident,
    IncidentScope,
    IncidentSource,
    PhaseKind,
    SessionObservation,
)
from obliskiq.errors import (
    CalendarCoverageError,
    CalendarError,
    IncidentRejectedError,
    UnknownExchangeCodeError,
    UnknownVenueError,
)

DAY = date(2026, 10, 9)  # a Friday with no holiday on any venue built here
TICK = timedelta(microseconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
NOON_UTC = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)  # 14:00 in New York: inside the window
MANUAL = "manual-incidents.yaml"
CONIDS = (265598, 8314)
#: Built once for the module; XTKS and XHKG carry a lunch break, XLON and XHKG an auction
#: after the close, XNYS a closing auction inside the regular phase.
VENUES = ("XEUR", "XETR", "XHKG", "XLON", "XNYS", "XTKS")
FIRST, LAST = date(2025, 1, 2), date(2027, 12, 30)
FOREVER = datetime.max.replace(tzinfo=UTC)

GOOD_ENTRY = """\
  - id: halt-1
    venue: XNYS
    scope: venue
    start: 2026-10-09T15:00:00+00:00
    expected_end: 2026-10-09T16:00:00+00:00
    reason: technical halt
"""


# ── helpers ──


def local(day: date, clock: str, zone: str) -> datetime:
    """A wall-clock time on a day in a zone, as a UTC instant; ``zoneinfo`` does the offset."""
    hour, minute = (int(part) for part in clock.split(":"))
    wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(zone))
    return wall.astimezone(UTC)


def incident(**overrides: Any) -> Incident:
    fields: dict[str, Any] = {
        "incident_id": "halt-1",
        "origin": MANUAL,
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


def closed(venue: str, day: date) -> SessionObservation:
    return SessionObservation(
        venue=venue, day=day, source="ibkr", is_session=False, open=None, close=None
    )


class TableSource:
    """A cross-check source answering from a table; ``None`` where it has no information."""

    def __init__(self, table: Mapping[tuple[str, date], SessionObservation]) -> None:
        self.name = "ibkr"
        self.table = dict(table)

    def observe(self, venue: str, day: date, conid: int | None) -> SessionObservation | None:
        return self.table.get((venue, day))


def settings_with(config_dir: Path, owner_yaml: str) -> ObliskConfig:
    owner = config_dir / "owner.yaml"
    owner.write_text(owner.read_text(encoding="utf-8") + "\n" + owner_yaml, encoding="utf-8")
    return load_config(config_dir).settings


def assemble(
    config: ObliskConfig, runtimes: Mapping[str, VenueRuntime], **kwargs: Any
) -> Calendars:
    """A fresh facade over prebuilt venues: its own incidents, sources and alert memory."""
    calendars = config.calendars
    return Calendars(
        runtimes,
        {},
        incident_grace=timedelta(minutes=calendars.incidents.grace_minutes),
        tolerance=timedelta(minutes=calendars.cross_check.time_tolerance_minutes),
        **kwargs,
    )


def grace_of(config: ObliskConfig) -> timedelta:
    return timedelta(minutes=config.calendars.incidents.grace_minutes)


def offsets(config: ObliskConfig, mic: str) -> tuple[timedelta, timedelta]:
    """The order offsets a venue uses: its own override, else the default."""
    own = config.calendars.venue_order_offsets.get(mic)
    chosen = config.calendars.order_offsets if own is None else own
    return (
        timedelta(minutes=chosen.after_open_minutes),
        timedelta(minutes=chosen.before_close_minutes),
    )


def holes(config: ObliskConfig, mic: str, bounds: SessionBounds) -> list[tuple[datetime, datetime]]:
    """Every configured phase window of the session, and its lunch break: no order goes there."""
    found: list[tuple[datetime, datetime]] = []
    for window in config.venues[mic].phase_windows:
        anchor = bounds.open_utc if window.anchor == "session_open" else bounds.close_utc
        found.append(
            (
                anchor + timedelta(minutes=window.start_offset_minutes),
                anchor + timedelta(minutes=window.end_offset_minutes),
            )
        )
    if bounds.break_start_utc is not None and bounds.break_end_utc is not None:
        found.append((bounds.break_start_utc, bounds.break_end_utc))
    return found


def auction_start(config: ObliskConfig, mic: str, bounds: SessionBounds) -> datetime:
    """Where the configured closing auction of a session begins."""
    (window,) = (w for w in config.venues[mic].phase_windows if w.phase == "closing_auction")
    anchor = bounds.open_utc if window.anchor == "session_open" else bounds.close_utc
    return anchor + timedelta(minutes=window.start_offset_minutes)


def blocks(
    item: Incident, mic: str, conid: int | None, grace: timedelta
) -> tuple[datetime, datetime] | None:
    """What an incident blocks for a venue and instrument, written out from D28."""
    if item.venue != mic:
        return None
    if item.scope is IncidentScope.INSTRUMENT and (conid is None or item.conid != conid):
        return None
    end = FOREVER if item.expected_end is None else item.expected_end + grace
    return item.start, end


def may_trade(
    config: ObliskConfig,
    plain: Calendars,
    mic: str,
    at: datetime,
    noise: Sequence[Incident],
    conid: int | None,
) -> bool:
    """The oracle: inside the offsets, outside every phase window, lunch and incident."""
    bounds = plain.session_bounds(mic, at.astimezone(ZoneInfo(config.venues[mic].timezone)).date())
    if bounds is None:
        return False
    after, before = offsets(config, mic)
    if not bounds.open_utc + after <= at < bounds.close_utc - before:
        return False
    if any(lo <= at < hi for lo, hi in holes(config, mic, bounds)):
        return False
    spans = [span for item in noise if (span := blocks(item, mic, conid, grace_of(config)))]
    return not any(lo <= at < hi for lo, hi in spans)


#: An incident as plain numbers (start in minutes from an anchor, length or open-ended, scope,
#: a venue pick, a conid), so every draw has the same shape whatever the facade answers.
type IncidentSpec = tuple[int, int | None, IncidentScope, int, int]
#: An instant as plain numbers: on an edge or not, which edge, a nudge, or minutes from noon.
type InstantSpec = tuple[bool, int, timedelta, int]


def incident_specs(earliest: timedelta, latest: timedelta) -> st.SearchStrategy[IncidentSpec]:
    return st.tuples(
        st.integers(-(earliest // MINUTE), latest // MINUTE),
        st.one_of(st.none(), st.integers(1, 24 * 60)),
        st.sampled_from(IncidentScope),
        st.integers(0, 7),
        st.sampled_from(CONIDS),
    )


instant_specs = st.tuples(
    st.booleans(),
    st.integers(0, 63),
    st.sampled_from([-TICK, timedelta(0), TICK]),
    st.integers(-18 * 60, 18 * 60),
)


def materialise(
    specs: Sequence[IncidentSpec], anchor: datetime, venues: Sequence[str]
) -> tuple[Incident, ...]:
    """Manual incidents from their specs, each with its own id."""
    return tuple(
        Incident(
            incident_id=f"generated-{position}",
            origin=MANUAL,
            source=IncidentSource.MANUAL,
            scope=scope,
            venue=venues[pick % len(venues)],
            conid=conid if scope is IncidentScope.INSTRUMENT else None,
            start=anchor + offset * MINUTE,
            expected_end=None if length is None else anchor + (offset + length) * MINUTE,
            reason="generated",
        )
        for position, (offset, length, scope, pick, conid) in enumerate(specs)
    )


def pick_instant(spec: InstantSpec, points: Sequence[datetime], around: datetime) -> datetime:
    """An edge nudged by at most one microsecond, or a minute within 18 hours of ``around``."""
    on_edge, index, nudge, minutes = spec
    if on_edge and points:
        return points[index % len(points)] + nudge
    return around + minutes * MINUTE


# ── fixtures ──


@pytest.fixture(scope="module")
def runtimes(shipped_settings: ObliskConfig) -> dict[str, VenueRuntime]:
    """The module's venues built once, over the window ``from_settings`` would build."""
    calendars = shipped_settings.calendars
    start = DAY.replace(year=DAY.year - calendars.history_years)
    end = DAY.replace(year=DAY.year + calendars.horizon_years)
    return {mic: _build_venue(shipped_settings, mic, start, end) for mic in VENUES}


class Bench:
    """The shipped configuration and the module's venues, with a short repr for reports."""

    def __init__(self, config: ObliskConfig, runtimes: Mapping[str, VenueRuntime]) -> None:
        self.config = config
        self.runtimes = runtimes

    def __repr__(self) -> str:
        return f"Bench({', '.join(self.runtimes)})"

    def facade(self, **kwargs: Any) -> Calendars:
        return assemble(self.config, self.runtimes, **kwargs)


@pytest.fixture(scope="module")
def bench(shipped_settings: ObliskConfig, runtimes: dict[str, VenueRuntime]) -> Bench:
    return Bench(shipped_settings, runtimes)


@pytest.fixture
def fresh(
    shipped_settings: ObliskConfig, runtimes: dict[str, VenueRuntime]
) -> Callable[..., Calendars]:
    def build(**kwargs: Any) -> Calendars:
        return assemble(shipped_settings, runtimes, **kwargs)

    return build


@pytest.fixture
def machine_zone() -> Iterator[Callable[[str], None]]:
    """Set the process's own time zone (``TZ`` plus ``tzset``); restored afterwards."""
    saved = os.environ.get("TZ")

    def use(zone: str) -> None:
        os.environ["TZ"] = zone
        time.tzset()

    yield use
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


# ── (1) what the calendars cannot judge is blocked, never open ──


def test_the_last_covered_day_is_judged_and_the_day_after_is_not(
    fresh: Callable[..., Calendars],
) -> None:
    calendars = fresh()
    coverage = calendars.coverage()["XNYS"]
    assert coverage is not None
    first, last = coverage
    week = timedelta(days=7)
    assert calendars.venue_status("XNYS", last).ok  # the exact limit is judged
    assert calendars.last_tradable_session("XNYS", "XNYS", last).session == last
    assert calendars.common_sessions_between("XNYS", "XNYS", last - week, last)[-1] == last
    beyond = last + timedelta(days=1)
    assert calendars.venue_status("XNYS", beyond).reason is BlockReason.CALENDAR_NOT_COVERED
    noon = local(beyond, "12:00", "America/New_York")
    assert calendars.tradability("XNYS", noon).reason is BlockReason.CALENDAR_NOT_COVERED
    assert calendars.tradable_windows("XNYS", beyond) == ()
    assert not calendars.is_open("XNYS", noon)
    with pytest.raises(CalendarCoverageError, match="calendar_not_covered"):
        calendars.last_tradable_session("XNYS", "XNYS", beyond)
    with pytest.raises(CalendarCoverageError, match="calendar_not_covered"):
        calendars.common_sessions_between("XNYS", "XNYS", last - week, beyond)
    earlier = first - timedelta(days=1)
    assert calendars.venue_status("XNYS", earlier).reason is BlockReason.CALENDAR_NOT_COVERED


def test_counting_sessions_back_stops_at_the_first_covered_day_and_raises_one_past_it(
    fresh: Callable[..., Calendars],
) -> None:
    calendars = fresh()
    coverage = calendars.coverage()["XNYS"]
    assert coverage is not None
    first = coverage[0]
    second = calendars.next_session("XNYS", first)
    assert second is not None
    assert calendars.session_before("XNYS", "XNYS", second, 1) == first
    with pytest.raises(CalendarCoverageError, match="calendar_not_covered"):
        calendars.session_before("XNYS", "XNYS", second, 2)
    with pytest.raises(CalendarCoverageError):
        calendars.session_before("XEUR", "XNYS", DAY, 10_000)
    with pytest.raises(CalendarCoverageError):
        calendars.common_sessions_between("XNYS", "XNYS", first - timedelta(days=1), second)


def test_the_venue_local_date_decides_coverage_not_the_utc_date(
    fresh: Callable[..., Calendars],
) -> None:
    calendars = fresh()
    coverage = calendars.coverage()["XTKS"]
    assert coverage is not None
    midnight = local(coverage[1] + timedelta(days=1), "00:00", "Asia/Tokyo")
    assert calendars.tradability("XTKS", midnight).reason is BlockReason.CALENDAR_NOT_COVERED
    late = calendars.tradability("XTKS", midnight - TICK)  # the last covered evening
    assert late.reason is BlockReason.OUTSIDE_WINDOW


@pytest.mark.parametrize("mic", ["NOPE", "xnys", "XNYS ", "", "SMART"])
def test_a_venue_that_is_not_built_is_blocked_in_every_view(calendars: Calendars, mic: str) -> None:
    assert calendars.tradability("XNYS", NOON_UTC).ok  # positive control
    assert calendars.venue_status(mic, DAY).reason is BlockReason.UNKNOWN_VENUE
    assert calendars.venue_status(mic, DAY, date_critical=True).reason is (
        BlockReason.UNKNOWN_VENUE
    )
    assert calendars.tradability(mic, NOON_UTC).reason is BlockReason.UNKNOWN_VENUE
    assert calendars.tradable_windows(mic, DAY) == ()
    assert calendars.common_windows(DAY, ("XNYS", None), (mic, None)) == ()
    with pytest.raises(CalendarCoverageError, match="unknown_venue"):
        calendars.last_tradable_session("XNYS", mic, DAY)
    with pytest.raises(CalendarCoverageError, match="unknown_venue"):
        calendars.session_before(mic, "XNYS", DAY, 0)
    with pytest.raises(UnknownVenueError):
        calendars.is_open(mic, NOON_UTC)
    with pytest.raises(UnknownVenueError):
        calendars.phase_at(mic, NOON_UTC)


def test_no_legs_vouch_for_no_window(calendars: Calendars) -> None:
    assert calendars.common_windows(DAY, ("XNYS", None), ("XNYS", None))  # positive control
    assert calendars.common_windows(DAY) == ()


@pytest.mark.parametrize("code", [" EUREX", "EUREX ", "EUREX\n", "Eurex", "EUREX/SMART", "XEUR"])
def test_a_near_miss_ibkr_code_maps_to_no_venue(calendars: Calendars, code: str) -> None:
    assert calendars.venue_for_ibkr("EUREX") == "XEUR"  # positive control
    with pytest.raises(UnknownExchangeCodeError):
        calendars.venue_for_ibkr(code)


def test_a_facade_built_with_no_venues_opens_nothing(shipped_settings: ObliskConfig) -> None:
    empty = Calendars.from_settings(shipped_settings, reference=DAY, venues=())
    assert empty.venues() == ()
    assert empty.venue_status("XNYS", DAY).reason is BlockReason.UNKNOWN_VENUE
    assert empty.tradability("XNYS", NOON_UTC).reason is BlockReason.UNKNOWN_VENUE
    with pytest.raises(UnknownExchangeCodeError):
        empty.venue_for_ibkr("NYSE")
    with pytest.raises(UnknownVenueError, match="xnys"):
        Calendars.from_settings(shipped_settings, reference=DAY, venues=("xnys",))


# ── (1) a manual incident file that cannot be trusted blocks everything ──


MALFORMED = [
    pytest.param("incidents:\n  - id: halt-1\n    venue: XNYS\n    scope: ven", id="truncated"),
    pytest.param(
        "incidents:\n" + GOOD_ENTRY + "    start: 2026-10-09T18:00:00+00:00\n",
        id="a-key-twice-in-one-entry",
    ),
    pytest.param("incidents:\n" + GOOD_ENTRY + "incidents: []\n", id="a-second-list-would-win"),
    pytest.param("incidents: !!python/object/apply:os.system ['true']\n", id="python-tag"),
    pytest.param("incidents:\n\t- id: x\n", id="tab-indentation"),
    pytest.param("incidents:\x00\n", id="nul-byte"),
    pytest.param("incidents:\n  - - a\n", id="a-list-of-lists"),
    pytest.param("incidents:\n" + GOOD_ENTRY.replace("XNYS", "xnys"), id="lower-case-venue"),
    pytest.param("incidents:\n" + GOOD_ENTRY.replace("XNYS", "XPAR"), id="venue-not-built"),
    pytest.param(
        "incidents:\n" + GOOD_ENTRY.replace("scope: venue", "scope: instrument\n    conid: 0"),
        id="conid-zero",
    ),
    pytest.param("incidents:\n" + GOOD_ENTRY + "    conid: 265598\n", id="venue-scope-with-conid"),
    pytest.param("incidents:\n" + GOOD_ENTRY.replace("technical halt", "''"), id="empty-reason"),
    pytest.param(
        "incidents:\n" + GOOD_ENTRY.replace("16:00:00", "15:00:00"), id="ends-as-it-starts"
    ),
    pytest.param(
        "incidents:\n" + GOOD_ENTRY.replace("2026-10-09T15:00:00+00:00", "2026-10-09"),
        id="a-date-without-a-time",
    ),
    pytest.param(
        "incidents:\n" + GOOD_ENTRY.replace("16:00:00+00:00", "16:00:00"),
        id="an-end-without-an-offset",
    ),
]


@pytest.mark.parametrize("text", MALFORMED)
def test_a_malformed_manual_file_blocks_every_venue_and_every_date_walk(
    fresh: Callable[..., Calendars], tmp_path: Path, text: str
) -> None:
    calendars = fresh()
    assert calendars.tradability("XNYS", NOON_UTC).ok  # positive control
    path = tmp_path / MANUAL
    path.write_text(text, encoding="utf-8")
    assert calendars.reload_manual_incidents(path).invalid is not None
    for mic in VENUES:
        assert calendars.venue_status(mic, DAY).reason is BlockReason.INCIDENT_FILE_INVALID, mic
        assert calendars.tradable_windows(mic, DAY) == ()
    assert calendars.tradability("XNYS", NOON_UTC).reason is BlockReason.INCIDENT_FILE_INVALID
    assert calendars.common_windows(DAY, ("XEUR", None), ("XNYS", None)) == ()
    with pytest.raises(CalendarCoverageError, match="incident_file_invalid"):
        calendars.last_tradable_session("XEUR", "XNYS", DAY)
    with pytest.raises(CalendarCoverageError, match="incident_file_invalid"):
        calendars.session_before("XEUR", "XNYS", DAY, 1)
    with pytest.raises(CalendarCoverageError, match="incident_file_invalid"):
        calendars.common_sessions_between("XEUR", "XNYS", DAY, DAY)
    path.write_text("incidents: []\n", encoding="utf-8")
    assert calendars.reload_manual_incidents(path).invalid is None
    assert calendars.tradability("XNYS", NOON_UTC).ok


def test_a_malformed_file_at_build_time_blocks_from_the_first_query(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    (tmp_path / shipped_settings.paths.manual_incidents).write_text(
        "incidents: [unclosed\n", encoding="utf-8"
    )
    calendars = Calendars.from_settings(
        shipped_settings, reference=DAY, venues=("XNYS",), data_dir=tmp_path
    )
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_FILE_INVALID
    assert calendars.tradability("XNYS", NOON_UTC).reason is BlockReason.INCIDENT_FILE_INVALID


def test_a_feed_update_while_the_file_is_invalid_does_not_lift_the_block(
    fresh: Callable[..., Calendars], tmp_path: Path
) -> None:
    calendars = fresh()
    path = tmp_path / MANUAL
    path.write_text("incidents: nope\n", encoding="utf-8")
    calendars.reload_manual_incidents(path)
    calendars.accept_incidents("some-feed", [])
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_FILE_INVALID
    calendars.replace_incidents(IncidentBook())  # the explicit swap is the only way out
    assert calendars.venue_status("XNYS", DAY).ok


def test_a_manual_file_that_is_not_utf8_blocks_every_venue(
    fresh: Callable[..., Calendars], tmp_path: Path
) -> None:
    calendars = fresh()
    assert calendars.venue_status("XNYS", DAY).ok
    path = tmp_path / MANUAL
    latin1 = b"incidents:\n" + GOOD_ENTRY.encode("ascii").replace(b"technical", b"arr\xeat")
    path.write_bytes(latin1)
    assert calendars.reload_manual_incidents(path).invalid is not None
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_FILE_INVALID


@pytest.mark.parametrize(
    "entry",
    [
        *(
            pytest.param(
                GOOD_ENTRY.replace("scope: venue", f"scope: instrument\n    conid: {word}"),
                id=f"conid-{word}",
            )
            for word in ("yes", "true", "on")
        ),
        pytest.param(
            GOOD_ENTRY.replace("2026-10-09T15:00:00+00:00", "1791558000"),
            id="start-as-unix-seconds",
        ),
    ],
)
def test_a_manual_value_of_the_wrong_type_is_refused_not_coerced(
    fresh: Callable[..., Calendars], tmp_path: Path, entry: str
) -> None:
    path = tmp_path / MANUAL
    path.write_text("incidents:\n" + entry, encoding="utf-8")
    assert fresh().reload_manual_incidents(path).invalid is not None


def test_an_end_far_in_the_future_blocks_rather_than_crashing(
    fresh: Callable[..., Calendars], tmp_path: Path
) -> None:
    path = tmp_path / MANUAL
    far = GOOD_ENTRY.replace("2026-10-09T16:00:00+00:00", "9999-12-31T23:59:59+00:00")
    path.write_text("incidents:\n" + far, encoding="utf-8")
    calendars = fresh()
    book = calendars.reload_manual_incidents(path)
    expected = (
        BlockReason.INCIDENT_FILE_INVALID
        if book.invalid is not None
        else BlockReason.INCIDENT_OVERLAP
    )
    assert calendars.venue_status("XNYS", DAY).reason is expected
    assert calendars.tradability("XNYS", NOON_UTC).reason is expected
    assert calendars.last_tradable_session("XNYS", "XNYS", DAY).session < DAY


def test_a_manual_incident_taken_on_the_feed_path_survives_a_manual_reload(
    fresh: Callable[..., Calendars], tmp_path: Path
) -> None:
    calendars = fresh()
    desk = incident(incident_id="desk-1", origin="ops-desk")
    try:
        calendars.accept_incidents("ops-desk", [desk])
    except IncidentRejectedError:
        return  # refusing a manual-labelled incident on the feed path is a fix as well
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_OVERLAP
    path = tmp_path / MANUAL
    path.write_text("incidents: []\n", encoding="utf-8")
    calendars.reload_manual_incidents(path)
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_OVERLAP


def test_the_feed_path_cannot_erase_the_manual_files_entries(
    fresh: Callable[..., Calendars], tmp_path: Path
) -> None:
    calendars = fresh()
    path = tmp_path / MANUAL
    path.write_text("incidents:\n" + GOOD_ENTRY, encoding="utf-8")
    calendars.reload_manual_incidents(path)
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_OVERLAP
    with contextlib.suppress(IncidentRejectedError):
        calendars.accept_incidents(path.name, [])
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.INCIDENT_OVERLAP


def test_a_manual_file_link_whose_target_is_gone_is_unreadable_not_empty(
    fresh: Callable[..., Calendars], tmp_path: Path
) -> None:
    path = tmp_path / MANUAL
    path.symlink_to(tmp_path / "moved-away.yaml")
    assert fresh().reload_manual_incidents(path).invalid is not None


def test_reloading_or_accepting_the_same_thing_twice_changes_nothing(
    fresh: Callable[..., Calendars], tmp_path: Path
) -> None:
    path = tmp_path / MANUAL
    path.write_text("incidents:\n" + GOOD_ENTRY, encoding="utf-8")
    calendars = fresh()
    first = calendars.reload_manual_incidents(path)
    second = calendars.reload_manual_incidents(path)
    assert first == second
    assert len(second.incidents) == 1
    restarted = fresh()
    restarted.reload_manual_incidents(path)
    assert restarted.tradable_windows("XNYS", DAY) == calendars.tradable_windows("XNYS", DAY)
    assert restarted.venue_status("XNYS", DAY) == calendars.venue_status("XNYS", DAY)
    desk = incident(incident_id="desk-1", origin="ops-desk", venue="XETR")
    calendars.accept_incidents("ops-desk", [desk])
    calendars.accept_incidents("ops-desk", [desk])
    assert sum(i.incident_id == "desk-1" for i in calendars.incident_book.incidents) == 1


# ── (1) a schedule that contradicts itself, fails or knows nothing ──


class ContradictingLibrary:
    """A library calendar reporting one contradictory session; every other answer is real."""

    def __init__(self, real: Any, day: date, fault: str) -> None:
        self._real = real
        self._day = day
        self._fault = fault

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def session_close(self, day: date) -> Any:
        if day == self._day and self._fault == "closes-before-it-opens":
            return self._real.session_open(day) - HOUR
        if day == self._day and self._fault == "closes-as-it-opens":
            return self._real.session_open(day)
        return self._real.session_close(day)

    def session_has_break(self, day: date) -> bool:
        if day == self._day and self._fault.startswith("break"):
            return True
        return bool(self._real.session_has_break(day))

    def session_break_start(self, day: date) -> Any:
        if self._fault == "break-before-the-open":
            return self._real.session_open(day) - HOUR
        if self._fault == "break-ends-before-it-starts":
            return self._real.session_open(day) + 2 * HOUR
        return self._real.session_open(day) + HOUR

    def session_break_end(self, day: date) -> Any:
        if self._fault == "break-past-the-close":
            return self._real.session_close(day) + HOUR
        if self._fault == "break-ends-before-it-starts":
            return self._real.session_open(day) + HOUR
        return self._real.session_open(day) + 2 * HOUR


@pytest.fixture(scope="module")
def nyse_library(runtimes: dict[str, VenueRuntime]) -> Any:
    coverage = runtimes["XNYS"].schedule.coverage
    assert coverage is not None
    return xc.get_calendar("XNYS", start=coverage[0], end=coverage[1])


@pytest.mark.parametrize(
    "fault",
    [
        "closes-before-it-opens",
        "closes-as-it-opens",
        "break-past-the-close",
        "break-before-the-open",
        "break-ends-before-it-starts",
    ],
)
def test_a_library_session_that_contradicts_itself_is_never_open(
    shipped_settings: ObliskConfig,
    runtimes: dict[str, VenueRuntime],
    nyse_library: Any,
    fault: str,
) -> None:
    zone = runtimes["XNYS"].schedule.zone
    schedule = LibrarySchedule(
        "XNYS", zone, ContradictingLibrary(nyse_library, DAY, fault), None, None
    )
    venues = {"XNYS": replace(runtimes["XNYS"], schedule=schedule), "XEUR": runtimes["XEUR"]}
    calendars = assemble(shipped_settings, venues)
    yesterday = DAY - timedelta(days=1)
    assert calendars.venue_status("XNYS", yesterday).ok  # positive control: only DAY is bad
    assert calendars.tradability("XNYS", NOON_UTC - timedelta(days=1)).ok
    assert calendars.venue_status("XNYS", DAY).reason is BlockReason.SCHEDULE_INCONSISTENT
    assert calendars.tradability("XNYS", NOON_UTC).reason is BlockReason.SCHEDULE_INCONSISTENT
    assert calendars.tradable_windows("XNYS", DAY) == ()
    assert calendars.common_windows(DAY, ("XEUR", None), ("XNYS", None)) == ()
    with pytest.raises(CalendarCoverageError, match="schedule_inconsistent"):
        calendars.last_tradable_session("XEUR", "XNYS", DAY)
    with pytest.raises(CalendarCoverageError, match="schedule_inconsistent"):
        calendars.session_before("XEUR", "XNYS", DAY + timedelta(days=3), 1)


class ScriptedSchedule(MarketSchedule):
    """A schedule whose every session comes from a script: it may fail or know nothing."""

    def __init__(self, answer: Callable[[date], SessionTimes | None]) -> None:
        zone = ZoneInfo("America/New_York")
        super().__init__("XNYS", zone, (DAY - timedelta(days=30), DAY + timedelta(days=30)))
        self._answer = answer

    def _session(self, day: date) -> SessionTimes | None:
        return self._answer(day)

    def _is_session(self, day: date) -> bool:
        return self._answer(day) is not None

    def is_business_weekday(self, day: date) -> bool:
        return day.weekday() < 5


def _unavailable(day: date) -> SessionTimes | None:
    raise RuntimeError(f"schedule source unavailable for {day}")


def _nothing(day: date) -> SessionTimes | None:
    return None


def _inverted(day: date) -> SessionTimes | None:
    open_ = local(day, "16:00", "America/New_York")
    return SessionTimes(day, open_, open_ - 6 * HOUR, None, None, False)


def scripted(
    config: ObliskConfig,
    runtimes: Mapping[str, VenueRuntime],
    answer: Callable[[date], SessionTimes | None],
    *,
    windows: bool = True,
) -> Calendars:
    runtime = replace(runtimes["XNYS"], schedule=ScriptedSchedule(answer))
    if not windows:
        runtime = replace(runtime, windows=())
    return assemble(config, {"XNYS": runtime})


def test_a_schedule_that_fails_is_an_error_never_an_open_venue(
    shipped_settings: ObliskConfig, runtimes: dict[str, VenueRuntime]
) -> None:
    calendars = scripted(shipped_settings, runtimes, _unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        calendars.venue_status("XNYS", DAY)
    with pytest.raises(RuntimeError, match="unavailable"):
        calendars.tradability("XNYS", NOON_UTC)
    with pytest.raises(RuntimeError, match="unavailable"):
        calendars.last_tradable_session("XNYS", "XNYS", DAY)


def test_a_schedule_that_knows_no_session_is_closed_and_the_walk_raises_at_its_edge(
    shipped_settings: ObliskConfig, runtimes: dict[str, VenueRuntime]
) -> None:
    calendars = scripted(shipped_settings, runtimes, _nothing)
    status = calendars.venue_status("XNYS", DAY)
    assert status.reason is BlockReason.NOT_A_SESSION
    assert status.detail == "holiday"
    assert calendars.tradability("XNYS", NOON_UTC).reason is BlockReason.NOT_A_SESSION
    assert calendars.tradable_windows("XNYS", DAY) == ()
    with pytest.raises(CalendarCoverageError, match="calendar_not_covered"):
        calendars.last_tradable_session("XNYS", "XNYS", DAY)


@pytest.mark.parametrize("windows", [True, False], ids=["with-phase-windows", "without"])
def test_an_unsanitised_inverted_session_is_never_open(
    shipped_settings: ObliskConfig, runtimes: dict[str, VenueRuntime], windows: bool
) -> None:
    calendars = scripted(shipped_settings, runtimes, _inverted, windows=windows)
    status = calendars.venue_status("XNYS", DAY)
    assert status.reason in {BlockReason.SCHEDULE_INCONSISTENT, BlockReason.NO_TRADABLE_WINDOW}
    assert calendars.tradable_windows("XNYS", DAY) == ()
    for hour in range(24):
        at = datetime(2026, 10, 9, hour, 30, tzinfo=UTC)
        assert not calendars.tradability("XNYS", at).ok, at


# ── (2) the act-now view: never inside an offset, lunch, auction or incident ──


def test_the_order_offsets_hold_to_the_microsecond(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    after, before = offsets(shipped_settings, "XNYS")
    opens = local(DAY, "09:30", "America/New_York")
    closes = local(DAY, "16:00", "America/New_York")
    bounds = calendars.session_bounds("XNYS", DAY)
    assert bounds is not None
    assert (bounds.open_utc, bounds.close_utc) == (opens, closes)
    assert calendars.tradability("XNYS", opens + after).ok
    assert calendars.tradability("XNYS", opens + after - TICK).reason is BlockReason.OUTSIDE_WINDOW
    assert calendars.tradability("XNYS", closes - before - TICK).ok
    assert calendars.tradability("XNYS", closes - before).reason is BlockReason.OUTSIDE_WINDOW


@pytest.mark.parametrize("mic", ["XTKS", "XHKG"])
def test_the_lunch_break_is_closed_from_its_first_to_its_last_microsecond(
    calendars: Calendars, mic: str
) -> None:
    bounds = calendars.session_bounds(mic, DAY)
    assert bounds is not None
    assert bounds.break_start_utc is not None
    assert bounds.break_end_utc is not None
    assert calendars.tradability(mic, bounds.break_start_utc - TICK).ok
    assert not calendars.tradability(mic, bounds.break_start_utc).ok
    assert not calendars.tradability(mic, bounds.break_end_utc - TICK).ok
    assert calendars.tradability(mic, bounds.break_end_utc).ok
    for window in calendars.tradable_windows(mic, DAY):
        assert window.end <= bounds.break_start_utc or bounds.break_end_utc <= window.start


def test_with_zero_offsets_the_auctions_still_bound_the_window(config_dir: Path) -> None:
    zero = "{after_open_minutes: 0, before_close_minutes: 0}"
    config = settings_with(
        config_dir,
        f"calendars:\n  venue_order_offsets:\n    XNYS: {zero}\n    XTKS: {zero}\n"
        f"    XLON: {zero}\n",
    )
    calendars = Calendars.from_settings(config, reference=DAY, venues=("XLON", "XNYS", "XTKS"))

    nyse = calendars.session_bounds("XNYS", DAY)
    assert nyse is not None
    auction = auction_start(config, "XNYS", nyse)
    assert nyse.open_utc < auction < nyse.close_utc  # New York's auction is inside the session
    assert calendars.tradable_windows("XNYS", DAY)[0].start == nyse.open_utc
    assert calendars.tradable_windows("XNYS", DAY)[-1].end == auction
    assert calendars.tradability("XNYS", nyse.open_utc).ok
    assert not calendars.tradability("XNYS", nyse.open_utc - TICK).ok  # pre-market
    assert calendars.tradability("XNYS", auction - TICK).ok
    assert not calendars.tradability("XNYS", auction).ok
    assert calendars.phase_at("XNYS", auction).phase is PhaseKind.CLOSING_AUCTION

    tokyo = calendars.session_bounds("XTKS", DAY)
    assert tokyo is not None
    morning, afternoon = calendars.tradable_windows("XTKS", DAY)
    assert (morning.start, morning.end) == (tokyo.open_utc, tokyo.break_start_utc)
    assert (afternoon.start, afternoon.end) == (
        tokyo.break_end_utc,
        auction_start(config, "XTKS", tokyo),
    )

    london = calendars.session_bounds("XLON", DAY)
    assert london is not None
    (window,) = calendars.tradable_windows("XLON", DAY)
    assert (window.start, window.end) == (london.open_utc, london.close_utc)
    assert not calendars.tradability("XLON", london.open_utc - TICK).ok  # opening auction
    assert not calendars.tradability("XLON", london.close_utc).ok  # closing auction


@pytest.mark.parametrize("minutes", [0, 1], ids=["exact", "one-less"])
def test_offsets_that_use_the_whole_session_leave_nothing_and_one_minute_less_leaves_one(
    calendars: Calendars, config_dir: Path, minutes: int
) -> None:
    bounds = calendars.session_bounds("XNYS", DAY)
    assert bounds is not None
    length = (bounds.close_utc - bounds.open_utc) // MINUTE
    before = 10  # exactly New York's closing auction
    config = settings_with(
        config_dir,
        "calendars:\n  venue_order_offsets:\n    XNYS:\n"
        f"      after_open_minutes: {length - before - minutes}\n"
        f"      before_close_minutes: {before}\n",
    )
    narrow = Calendars.from_settings(config, reference=DAY, venues=("XNYS",))
    windows = narrow.tradable_windows("XNYS", DAY)
    assert sum((w.duration for w in windows), timedelta()) == minutes * MINUTE
    last = bounds.close_utc - before * MINUTE
    if minutes == 0:
        assert narrow.venue_status("XNYS", DAY).reason is BlockReason.NO_TRADABLE_WINDOW
        assert narrow.tradability("XNYS", last - TICK).reason is BlockReason.NO_TRADABLE_WINDOW
    else:
        (window,) = windows
        assert window.end == last
        assert narrow.tradability("XNYS", window.start).ok
        assert not narrow.tradability("XNYS", window.start - TICK).ok
        assert narrow.tradability("XNYS", window.end - TICK).ok
        assert not narrow.tradability("XNYS", window.end).ok


def test_an_incident_blocks_from_its_first_instant_until_its_end_plus_grace(
    fresh: Callable[..., Calendars], shipped_settings: ObliskConfig
) -> None:
    halt = incident()
    assert halt.expected_end is not None
    calendars = fresh(incidents=IncidentBook((halt,)))
    resume = halt.expected_end + grace_of(shipped_settings)
    assert calendars.tradability("XNYS", halt.start - TICK).ok
    assert calendars.tradability("XNYS", halt.start).reason is BlockReason.INCIDENT_OVERLAP
    assert calendars.tradability("XNYS", resume - TICK).reason is BlockReason.INCIDENT_OVERLAP
    assert calendars.tradability("XNYS", resume).ok


def test_with_no_grace_an_incident_ends_exactly_at_its_expected_end(config_dir: Path) -> None:
    config = settings_with(config_dir, "calendars:\n  incidents:\n    grace_minutes: 0\n")
    halt = incident()
    assert halt.expected_end is not None
    calendars = Calendars.from_settings(
        config, reference=DAY, venues=("XNYS",), incidents=IncidentBook((halt,))
    )
    assert calendars.tradability("XNYS", halt.expected_end - TICK).reason is (
        BlockReason.INCIDENT_OVERLAP
    )
    assert calendars.tradability("XNYS", halt.expected_end).ok


def test_an_instrument_halt_blocks_that_instrument_at_every_instant_and_no_other(
    fresh: Callable[..., Calendars],
) -> None:
    halt = incident(scope=IncidentScope.INSTRUMENT, conid=CONIDS[0])
    calendars = fresh(incidents=IncidentBook((halt,)))
    inside = halt.start + MINUTE
    assert calendars.tradability("XNYS", inside, conid=CONIDS[0]).reason is (
        BlockReason.INCIDENT_OVERLAP
    )
    assert calendars.tradability("XNYS", inside, conid=CONIDS[1]).ok
    legs = calendars.common_windows(DAY, ("XEUR", None), ("XNYS", CONIDS[0]))
    assert all(not w.contains(inside) for w in legs)
    assert any(w.contains(inside) for w in calendars.common_windows(DAY, ("XNYS", CONIDS[1])))


def test_a_date_critical_order_stays_blocked_by_a_disagreement_when_a_halt_also_touches_the_day(
    fresh: Callable[..., Calendars],
) -> None:
    source = TableSource({("XNYS", DAY): closed("XNYS", DAY)})
    halt = IncidentBook((incident(),))
    only_disagreement = fresh(sources=(source,))
    assert only_disagreement.tradability("XNYS", NOON_UTC, date_critical=True).reason is (
        BlockReason.SOURCE_DISAGREEMENT
    )
    only_halt = fresh(incidents=halt)
    assert only_halt.tradability("XNYS", NOON_UTC, date_critical=True).ok  # clear of the halt
    both = fresh(sources=(source,), incidents=halt)
    assert not both.tradability("XNYS", NOON_UTC, date_critical=True).ok


@settings(max_examples=150)
@given(
    mic=st.sampled_from(VENUES),
    day=st.dates(FIRST, LAST),
    conid=st.sampled_from([None, *CONIDS]),
    specs=st.lists(incident_specs(30 * HOUR, 18 * HOUR), max_size=3),
    instant=instant_specs,
)
def test_tradability_is_exactly_the_session_minus_offsets_phases_lunch_and_incidents(
    bench: Bench,
    mic: str,
    day: date,
    conid: int | None,
    specs: list[IncidentSpec],
    instant: InstantSpec,
) -> None:
    plain = bench.facade()
    noon = local(day, "12:00", bench.config.venues[mic].timezone)
    noise = materialise(specs, noon, [mic, *VENUES])
    calendars = bench.facade(incidents=IncidentBook(noise))
    bounds = plain.session_bounds(mic, day)
    points: list[datetime] = []
    if bounds is not None:
        after, before = offsets(bench.config, mic)
        points += [bounds.open_utc + after, bounds.close_utc - before]
        points += [edge for span in holes(bench.config, mic, bounds) for edge in span]
    for item in noise:
        span = blocks(item, item.venue, item.conid, grace_of(bench.config))
        assert span is not None
        points += [edge for edge in span if edge != FOREVER]
    at = pick_instant(instant, points, noon)

    verdict = calendars.tradability(mic, at, conid=conid)
    expected = may_trade(bench.config, plain, mic, at, noise, conid)
    assert verdict.ok is expected, verdict
    if verdict.ok:
        assert verdict.window is not None
        assert verdict.window.contains(at)
        for edge in (verdict.window.start, verdict.window.end - TICK):
            assert may_trade(bench.config, plain, mic, edge, noise, conid), edge
    else:
        assert verdict.reason is not None
        assert verdict.window is None


# ── (3) the planning view: any incident touching the session blocks the day ──


@dataclass(frozen=True, slots=True)
class Edges:
    """What a halt is placed against: the session, the incident grace, the opening offset."""

    open: datetime
    close: datetime
    grace: timedelta
    after: timedelta


HALT_EDGES = [
    pytest.param(
        lambda e: (e.open - 2 * HOUR, e.open - e.grace),
        False,
        id="block-ends-exactly-at-the-open",
    ),
    pytest.param(
        lambda e: (e.open - 2 * HOUR, e.open - e.grace + TICK),
        True,
        id="block-ends-one-tick-into-the-session",
    ),
    pytest.param(lambda e: (e.close, e.close + HOUR), False, id="starts-exactly-at-the-close"),
    pytest.param(
        lambda e: (e.close - TICK, e.close + HOUR), True, id="starts-one-tick-before-the-close"
    ),
    pytest.param(
        lambda e: (e.open - 3 * HOUR, e.open - 2 * HOUR - e.grace),
        False,
        id="wholly-in-the-pre-market",
    ),
    pytest.param(
        lambda e: (e.open - HOUR, e.open + e.after - e.grace),
        True,
        id="inside-the-opening-offset-only",
    ),
]


@pytest.mark.parametrize(("span", "blocked"), HALT_EDGES)
def test_the_planning_view_blocks_exactly_the_days_a_halt_touches(
    fresh: Callable[..., Calendars],
    shipped_settings: ObliskConfig,
    span: Callable[[Edges], tuple[datetime, datetime]],
    blocked: bool,
) -> None:
    clean = fresh()
    bounds = clean.session_bounds("XNYS", DAY)
    assert bounds is not None
    after, _ = offsets(shipped_settings, "XNYS")
    start, end = span(Edges(bounds.open_utc, bounds.close_utc, grace_of(shipped_settings), after))
    calendars = fresh(incidents=IncidentBook((incident(start=start, expected_end=end),)))
    status = calendars.venue_status("XNYS", DAY)
    if blocked:
        assert status.reason is BlockReason.INCIDENT_OVERLAP
        assert status.partial
        with pytest.raises(CalendarError):
            calendars.session_before("XNYS", "XNYS", DAY, 0)
    else:
        assert status.ok
        assert calendars.tradable_windows("XNYS", DAY) == clean.tradable_windows("XNYS", DAY)


def test_a_halt_inside_the_lunch_break_blocks_the_planning_view_only(
    fresh: Callable[..., Calendars],
) -> None:
    clean = fresh()
    bounds = clean.session_bounds("XTKS", DAY)
    assert bounds is not None
    assert bounds.break_start_utc is not None
    lunch = incident(
        venue="XTKS", start=bounds.break_start_utc, expected_end=bounds.break_start_utc + MINUTE
    )
    calendars = fresh(incidents=IncidentBook((lunch,)))
    status = calendars.venue_status("XTKS", DAY)
    assert status.reason is BlockReason.INCIDENT_OVERLAP
    assert status.partial
    assert calendars.tradable_windows("XTKS", DAY) == clean.tradable_windows("XTKS", DAY)
    assert calendars.last_tradable_session("XTKS", "XTKS", DAY).session < DAY


@settings(max_examples=150)
@given(
    mic=st.sampled_from(VENUES),
    day=st.dates(FIRST, LAST),
    conid=st.sampled_from([None, *CONIDS]),
    specs=st.lists(incident_specs(36 * HOUR, 18 * HOUR), max_size=3),
)
def test_the_planning_view_is_blocked_whenever_an_incident_touches_the_session(
    bench: Bench,
    mic: str,
    day: date,
    conid: int | None,
    specs: list[IncidentSpec],
) -> None:
    plain = bench.facade()
    noon = local(day, "12:00", bench.config.venues[mic].timezone)
    noise = materialise(specs, noon, [mic, *VENUES])
    calendars = bench.facade(incidents=IncidentBook(noise))
    status = calendars.venue_status(mic, day, conid=conid)
    windows = calendars.tradable_windows(mic, day, conid=conid)
    bounds = plain.session_bounds(mic, day)
    if bounds is None:
        assert status.reason is BlockReason.NOT_A_SESSION
        assert windows == ()
        return
    grace = grace_of(bench.config)
    spans = [span for item in noise if (span := blocks(item, mic, conid, grace))]
    touching = {
        item.incident_id
        for item in noise
        if (span := blocks(item, mic, conid, grace))
        and span[0] < bounds.close_utc
        and bounds.open_utc < span[1]
    }
    if touching:
        assert status.reason is BlockReason.INCIDENT_OVERLAP
        assert {item.incident_id for item in status.incidents} == touching
        assert status.partial is bool(windows)
    else:
        assert status.ok
        assert windows == plain.tradable_windows(mic, day, conid=conid)
    after, before = offsets(bench.config, mic)
    for window in windows:
        assert bounds.open_utc + after <= window.start
        assert window.end <= bounds.close_utc - before
        for lo, hi in [*holes(bench.config, mic, bounds), *spans]:
            assert not (lo < window.end and window.start < hi), (window, lo, hi)


# ── (4) the last tradable session: date-critical, never past coverage, earlier wins ──


def test_disagreements_on_both_legs_walk_back_to_the_first_day_both_agree(
    fresh: Callable[..., Calendars],
) -> None:
    source = TableSource(
        {
            ("XNYS", DAY): closed("XNYS", DAY),
            ("XEUR", DAY - timedelta(days=1)): closed("XEUR", DAY - timedelta(days=1)),
        }
    )
    calendars = fresh(sources=(source,))
    result = calendars.last_tradable_session("XEUR", "XNYS", DAY)
    assert result.session == DAY - timedelta(days=2)
    assert [(s.venue, s.day, s.reason) for s in result.skipped] == [
        ("XNYS", DAY, BlockReason.SOURCE_DISAGREEMENT),
        ("XEUR", DAY - timedelta(days=1), BlockReason.SOURCE_DISAGREEMENT),
    ]
    assert calendars.session_before("XEUR", "XNYS", DAY, 1) == DAY - timedelta(days=2)
    assert calendars.tradability("XNYS", NOON_UTC).ok  # ordinary orders are not blocked
    assert calendars.tradability("XNYS", NOON_UTC, date_critical=True).reason is (
        BlockReason.SOURCE_DISAGREEMENT
    )


@pytest.mark.parametrize(
    ("excess", "session"),
    [(timedelta(0), DAY), (TICK, DAY - timedelta(days=1))],
    ids=["at-the-tolerance", "one-tick-past"],
)
def test_a_close_one_tick_past_the_tolerance_moves_the_expiry_session_earlier(
    fresh: Callable[..., Calendars],
    shipped_settings: ObliskConfig,
    excess: timedelta,
    session: date,
) -> None:
    bounds = fresh().session_bounds("XNYS", DAY)
    assert bounds is not None
    tolerance = timedelta(minutes=shipped_settings.calendars.cross_check.time_tolerance_minutes)
    seen = SessionObservation(
        venue="XNYS",
        day=DAY,
        source="ibkr",
        is_session=True,
        open=bounds.open_utc,
        close=bounds.close_utc - tolerance - excess,
    )
    calendars = fresh(sources=(TableSource({("XNYS", DAY): seen}),))
    assert calendars.last_tradable_session("XEUR", "XNYS", DAY).session == session


def test_a_disagreement_the_day_before_a_holiday_expiry_takes_the_day_before_that(
    fresh: Callable[..., Calendars], rules: Any
) -> None:
    good_friday = rules.easter_sunday(2026) - timedelta(days=2)
    thursday = good_friday - timedelta(days=1)
    calendars = fresh(sources=(TableSource({("XNYS", thursday): closed("XNYS", thursday)}),))
    result = calendars.last_tradable_session("XEUR", "XNYS", good_friday)
    assert result.session == thursday - timedelta(days=1)
    assert [s.reason for s in result.skipped] == [
        BlockReason.NOT_A_SESSION,
        BlockReason.SOURCE_DISAGREEMENT,
    ]


def test_a_disagreement_is_alerted_once_however_many_views_ask(
    fresh: Callable[..., Calendars],
) -> None:
    alerts: list[Disagreement] = []
    source = TableSource({("XNYS", DAY): closed("XNYS", DAY)})
    calendars = fresh(sources=(source,), alert=alerts.append)
    calendars.venue_status("XNYS", DAY)
    calendars.tradability("XNYS", NOON_UTC, date_critical=True)
    calendars.tradable_windows("XNYS", DAY)
    calendars.last_tradable_session("XEUR", "XNYS", DAY)
    calendars.common_sessions_between("XEUR", "XNYS", DAY, DAY)
    assert len(alerts) == 1
    restarted = fresh(sources=(source,), alert=alerts.append)
    assert restarted.venue_status("XNYS", DAY).ok
    restarted.venue_status("XNYS", DAY)
    assert len(alerts) == 2  # a restart alerts again, once


LEGS = [("XEUR", "XNYS"), ("XNYS", "XNYS"), ("XTKS", "XNYS"), ("XETR", "XEUR"), ("XLON", "XHKG")]


@settings(max_examples=80)
@given(
    legs=st.sampled_from(LEGS),
    expiry=st.dates(FIRST + timedelta(days=30), LAST),
    conid=st.sampled_from([None, *CONIDS]),
    specs=st.lists(incident_specs(10 * 24 * HOUR, 30 * HOUR), max_size=3),
    disputed=st.lists(st.tuples(st.integers(0, 1), st.integers(0, 10)), max_size=4, unique=True),
)
def test_the_last_tradable_session_is_judged_date_critically_and_only_ever_moves_earlier(
    bench: Bench,
    legs: tuple[str, str],
    expiry: date,
    conid: int | None,
    specs: list[IncidentSpec],
    disputed: list[tuple[int, int]],
) -> None:
    instrument, underlying = legs
    clean = bench.facade().last_tradable_session(*legs, expiry)
    midnight = datetime(expiry.year, expiry.month, expiry.day, tzinfo=UTC)
    noise = materialise(specs, midnight, legs)
    table: dict[tuple[str, date], SessionObservation] = {}
    for leg, back in disputed:
        day = expiry - timedelta(days=back)
        table[legs[leg], day] = closed(legs[leg], day)
    calendars = bench.facade(incidents=IncidentBook(noise), sources=(TableSource(table),))
    result = calendars.last_tradable_session(
        instrument, underlying, expiry, instrument_conid=conid, underlying_conid=conid
    )
    assert result.session <= clean.session <= expiry
    for mic in legs:
        assert calendars.venue_status(mic, result.session, conid=conid, date_critical=True).ok
        assert calendars.tradable_windows(mic, result.session, conid=conid)
    expected = []
    day = expiry
    while day > result.session:
        statuses = [
            calendars.venue_status(mic, day, conid=conid, date_critical=True) for mic in legs
        ]
        first_block = next((s for s in statuses if not s.ok), None)
        assert first_block is not None, day
        expected.append((first_block.venue, day, first_block.reason))
        day -= timedelta(days=1)
    assert [(s.venue, s.day, s.reason) for s in result.skipped] == expected


# ── (5) no dependence on the machine's zone, the clock or the build date ──

CLOCK_READS = frozenset(
    {"now", "utcnow", "today", "localtime", "gmtime", "mktime", "time_ns", "monotonic"}
)


def clock_reads(source: str) -> list[str]:
    """Calls that read the clock or the machine's zone, found in Python source."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        bare = not node.args and not node.keywords
        stamp = name in {"fromtimestamp", "utcfromtimestamp"} and len(node.args) < 2
        stamp = stamp and not any(keyword.arg == "tz" for keyword in node.keywords)
        wall = (
            name == "time"
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "time"
        )
        if name in CLOCK_READS or (name == "astimezone" and bare) or stamp or wall:
            found.append(f"line {node.lineno}: {name}")
    return found


def test_the_clock_scanner_finds_what_it_looks_for() -> None:
    sample = "a = datetime.now(UTC)\nb = x.astimezone()\nc = time.time()\nd = x.astimezone(UTC)\n"
    assert clock_reads(sample) == ["line 1: now", "line 2: astimezone", "line 3: time"]


@pytest.mark.parametrize(
    "path",
    sorted((Path(__file__).resolve().parents[2] / "src/obliskiq/calendars").glob("*.py")),
    ids=lambda path: path.name,
)
def test_the_calendar_package_never_reads_the_clock_or_the_machine_zone(path: Path) -> None:
    assert clock_reads(path.read_text(encoding="utf-8")) == []


ZONE_VENUES = ("XASX", "XETR", "XEUR", "XNYS", "XTKS")
ZONE_DAYS = (
    DAY,
    date(2026, 3, 6),
    date(2026, 3, 9),
    date(2026, 3, 27),
    date(2026, 3, 30),
    date(2026, 4, 2),
    date(2026, 4, 7),
    date(2026, 10, 2),
    date(2026, 10, 5),
    date(2026, 10, 23),
    date(2026, 10, 26),
    date(2026, 10, 30),
    date(2026, 11, 2),
    date(2026, 11, 27),
)


def snapshot(config: ObliskConfig) -> list[object]:
    calendars = Calendars.from_settings(config, reference=DAY, venues=ZONE_VENUES)
    seen: list[object] = [calendars.coverage()]
    for mic in ZONE_VENUES:
        for day in ZONE_DAYS:
            bounds = calendars.session_bounds(mic, day)
            seen += [calendars.venue_status(mic, day), calendars.tradable_windows(mic, day)]
            if bounds is not None:
                seen += [bounds, bounds.open_local.isoformat(), bounds.close_local.isoformat()]
            midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
            seen += [calendars.tradability(mic, midnight + h * HOUR) for h in range(0, 24, 3)]
    seen.append(calendars.last_tradable_session("XEUR", "XNYS", date(2026, 4, 3)))
    return seen


@pytest.mark.parametrize(
    "zone", ["Pacific/Kiritimati", "America/Adak", "Asia/Kathmandu", "Australia/Lord_Howe"]
)
def test_the_machines_own_time_zone_changes_no_answer(
    shipped_settings: ObliskConfig, machine_zone: Callable[[str], None], zone: str
) -> None:
    machine_zone("UTC")
    baseline = snapshot(shipped_settings)
    machine_zone(zone)
    assert time.localtime().tm_gmtoff != 0  # positive control: the zone really changed
    assert snapshot(shipped_settings) == baseline


@pytest.mark.parametrize(
    "ask",
    [
        pytest.param(lambda c, at: c.tradability("XNYS", at), id="tradability"),
        pytest.param(
            lambda c, at: c.tradability("XNYS", at, date_critical=True), id="tradability-critical"
        ),
        pytest.param(lambda c, at: c.phase_at("XNYS", at), id="phase_at"),
        pytest.param(lambda c, at: c.is_open("XNYS", at), id="is_open"),
        pytest.param(lambda c, at: c.next_open("XNYS", at), id="next_open"),
        pytest.param(lambda c, at: c.next_close("XNYS", at), id="next_close"),
        pytest.param(lambda c, at: c.session_day("XTKS", at), id="session_day"),
        pytest.param(lambda c, at: c.last_completed_session("XNYS", at), id="last_completed"),
    ],
)
def test_a_naive_instant_is_refused_by_every_instant_query(
    calendars: Calendars, ask: Callable[[Calendars, datetime], object]
) -> None:
    ask(calendars, NOON_UTC)  # positive control: the aware instant is answered
    with pytest.raises(ValueError, match="time zone"):
        ask(calendars, NOON_UTC.replace(tzinfo=None))


ZONES = [
    UTC,
    ZoneInfo("America/New_York"),
    ZoneInfo("Asia/Kathmandu"),
    ZoneInfo("Australia/Lord_Howe"),
    timezone(timedelta(hours=-3, minutes=-30)),
    timezone(timedelta(hours=14)),
]


@given(
    mic=st.sampled_from(VENUES),
    instant=st.datetimes(datetime(2025, 1, 2), datetime(2027, 12, 30), timezones=st.just(UTC)),
    zone=st.sampled_from(ZONES),
)
def test_the_same_instant_written_in_any_zone_gets_the_same_answer(
    calendars: Calendars, mic: str, instant: datetime, zone: Any
) -> None:
    written = instant.astimezone(zone)
    one, other = calendars.tradability(mic, instant), calendars.tradability(mic, written)
    assert (one.reason, one.window, one.detail) == (other.reason, other.window, other.detail)
    assert calendars.phase_at(mic, instant) == calendars.phase_at(mic, written)


@pytest.fixture(scope="module")
def two_references(shipped_settings: ObliskConfig) -> tuple[Calendars, Calendars]:
    venues = ("XEUR", "XNYS", "XTKS")
    return (
        Calendars.from_settings(shipped_settings, reference=DAY, venues=venues),
        Calendars.from_settings(shipped_settings, reference=date(2027, 6, 15), venues=venues),
    )


@given(
    mic=st.sampled_from(["XEUR", "XNYS", "XTKS"]),
    day=st.dates(date(2025, 7, 1), date(2029, 10, 8)),
    minute=st.integers(0, 24 * 60 - 1),
)
def test_the_build_date_changes_only_the_window_never_an_answer_inside_it(
    two_references: tuple[Calendars, Calendars], mic: str, day: date, minute: int
) -> None:
    early, late = two_references
    assert early.venue_status(mic, day) == late.venue_status(mic, day)
    assert early.tradable_windows(mic, day) == late.tradable_windows(mic, day)
    at = datetime(day.year, day.month, day.day, tzinfo=UTC) + minute * MINUTE
    assert early.tradability(mic, at) == late.tradability(mic, at)
    assert early.last_tradable_session("XEUR", mic, day) == late.last_tradable_session(
        "XEUR", mic, day
    )


# ── (6) daylight saving: local hours constant, durations exact in UTC ──

DST_WEEKENDS = [
    pytest.param(date(2026, 3, 8), "XNYS", "09:30", "16:00", 345, id="us-spring"),
    pytest.param(date(2026, 11, 1), "XNYS", "09:30", "16:00", 345, id="us-autumn"),
    pytest.param(date(2026, 3, 29), "XETR", "09:00", "17:30", 465, id="eu-spring-xetr"),
    pytest.param(date(2026, 10, 25), "XETR", "09:00", "17:30", 465, id="eu-autumn-xetr"),
    pytest.param(date(2026, 3, 29), "XLON", "08:00", "16:30", 465, id="uk-spring"),
    pytest.param(date(2026, 10, 25), "XLON", "08:00", "16:30", 465, id="uk-autumn"),
    pytest.param(date(2026, 4, 5), "XASX", "10:00", "16:00", 315, id="au-autumn"),
    pytest.param(date(2026, 10, 4), "XASX", "10:00", "16:00", 315, id="au-spring"),
]


@pytest.mark.parametrize(("sunday", "mic", "opens", "closes", "minutes"), DST_WEEKENDS)
def test_a_clock_change_weekend_keeps_local_hours_and_exact_utc_durations(
    calendars: Calendars,
    shipped_settings: ObliskConfig,
    sunday: date,
    mic: str,
    opens: str,
    closes: str,
    minutes: int,
) -> None:
    zone = shipped_settings.venues[mic].timezone
    assert sunday.weekday() == 6
    before = calendars.previous_session(mic, sunday)
    after = calendars.next_session(mic, sunday)
    assert before is not None
    assert after is not None

    def offset(day: date) -> timedelta:
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(zone)).utcoffset()
        assert noon is not None
        return noon

    shift = offset(after) - offset(before)
    assert abs(shift) == HOUR  # positive control: the weekend really moves the clock
    after_open, _ = offsets(shipped_settings, mic)
    for day in (before, after):
        bounds = calendars.session_bounds(mic, day)
        assert bounds is not None
        assert (bounds.open_utc, bounds.close_utc) == (
            local(day, opens, zone),
            local(day, closes, zone),
        )
        (window,) = calendars.tradable_windows(mic, day)
        assert window.duration == minutes * MINUTE
        start = local(day, opens, zone) + after_open
        assert window.start == start
        assert calendars.tradability(mic, start).ok
        assert calendars.tradability(mic, start - TICK).reason is BlockReason.OUTSIDE_WINDOW
    first = calendars.session_bounds(mic, before)
    second = calendars.session_bounds(mic, after)
    assert first is not None
    assert second is not None
    assert second.open_utc - first.open_utc == (after - before).days * timedelta(days=1) - shift
    assert calendars.venue_status(mic, sunday).reason is BlockReason.NOT_A_SESSION
    assert calendars.last_tradable_session(mic, mic, sunday).session == before


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        (date(2026, 3, 6), "15:00", "16:15"),  # both on winter time
        (date(2026, 3, 9), "14:00", "16:15"),  # New York on summer time, Frankfurt not yet
        (date(2026, 3, 27), "14:00", "16:15"),
        (date(2026, 3, 30), "14:00", "15:15"),  # both on summer time
        (date(2026, 10, 23), "14:00", "15:15"),
        (date(2026, 10, 26), "14:00", "16:15"),  # Frankfurt back on winter time, New York not
        (date(2026, 10, 30), "14:00", "16:15"),
        (date(2026, 11, 2), "15:00", "16:15"),  # both on winter time
    ],
)
def test_the_frankfurt_new_york_overlap_follows_both_clock_changes(
    calendars: Calendars, day: date, start: str, end: str
) -> None:
    (window,) = calendars.common_windows(day, ("XETR", None), ("XNYS", 265598))
    assert (window.start, window.end) == (local(day, start, "UTC"), local(day, end, "UTC"))


def test_a_halt_across_the_european_clock_change_is_measured_in_utc(
    fresh: Callable[..., Calendars], shipped_settings: ObliskConfig
) -> None:
    friday, monday = date(2026, 10, 23), date(2026, 10, 26)
    halt = incident(
        venue="XETR",
        start=datetime(2026, 10, 23, 14, 0, tzinfo=UTC),
        expected_end=datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
    )
    assert halt.expected_end is not None
    calendars = fresh(incidents=IncidentBook((halt,)))
    resume = halt.expected_end + grace_of(shipped_settings)
    (on_friday,) = calendars.tradable_windows("XETR", friday)
    assert (on_friday.start, on_friday.end) == (
        datetime(2026, 10, 23, 7, 30, tzinfo=UTC),  # 09:30 summer time
        datetime(2026, 10, 23, 14, 0, tzinfo=UTC),
    )
    (on_monday,) = calendars.tradable_windows("XETR", monday)
    assert (on_monday.start, on_monday.end) == (resume, datetime(2026, 10, 26, 16, 15, tzinfo=UTC))
    for day in (friday, monday):
        status = calendars.venue_status("XETR", day)
        assert status.reason is BlockReason.INCIDENT_OVERLAP
        assert status.partial
    assert calendars.tradability("XETR", resume - TICK).reason is BlockReason.INCIDENT_OVERLAP
    assert calendars.tradability("XETR", resume).ok
    result = calendars.last_tradable_session("XETR", "XETR", monday)
    assert result.session == date(2026, 10, 22)
    assert [s.reason for s in result.skipped] == [
        BlockReason.INCIDENT_OVERLAP,
        BlockReason.NOT_A_SESSION,
        BlockReason.NOT_A_SESSION,
        BlockReason.INCIDENT_OVERLAP,
    ]


# ── half days and holidays ──


def test_a_us_half_day_ends_the_window_early_and_still_counts_as_a_session(
    calendars: Calendars, shipped_settings: ObliskConfig, rules: Any
) -> None:
    thanksgiving = rules.nth_weekday(2026, 11, 3, 4)
    half, full = thanksgiving + timedelta(days=1), thanksgiving - timedelta(days=1)
    zone = "America/New_York"
    _, before = offsets(shipped_settings, "XNYS")
    bounds = calendars.session_bounds("XNYS", half)
    assert bounds is not None
    assert bounds.early_close
    assert bounds.close_utc == local(half, "13:00", zone)
    (window,) = calendars.tradable_windows("XNYS", half)
    assert window.end == local(half, "13:00", zone) - before
    assert calendars.tradability("XNYS", window.end - TICK).ok
    assert calendars.tradability("XNYS", window.end).reason is BlockReason.OUTSIDE_WINDOW
    assert calendars.tradability("XNYS", local(half, "14:00", zone)).reason is (
        BlockReason.OUTSIDE_WINDOW
    )
    assert calendars.tradability("XNYS", local(full, "14:00", zone)).ok  # same clock, full day
    assert calendars.phase_at("XNYS", local(half, "12:55", zone)).phase is (
        PhaseKind.CLOSING_AUCTION
    )
    assert calendars.venue_status("XNYS", thanksgiving).detail == "holiday"
    assert calendars.last_tradable_session("XNYS", "XNYS", thanksgiving).session == full
    assert calendars.last_tradable_session("XNYS", "XNYS", half).session == half


def test_a_london_christmas_eve_half_day_keeps_the_auction_after_its_early_close(
    calendars: Calendars, shipped_settings: ObliskConfig
) -> None:
    eve = date(2026, 12, 24)
    _, before = offsets(shipped_settings, "XLON")
    close = local(eve, "12:30", "Europe/London")
    bounds = calendars.session_bounds("XLON", eve)
    assert bounds is not None
    assert bounds.close_utc == close
    (window,) = calendars.tradable_windows("XLON", eve)
    assert window.end == close - before
    assert calendars.tradability("XLON", close - before - TICK).ok
    assert not calendars.tradability("XLON", close - before).ok
    assert calendars.phase_at("XLON", close).phase is PhaseKind.CLOSING_AUCTION
    assert not calendars.tradability("XLON", close).ok
    assert calendars.tradability("XLON", local(eve - timedelta(days=1), "13:00", "UTC")).ok
