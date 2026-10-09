"""Properties of the calendar facade over generated days and incidents.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from obliskiq.calendars.incidents import IncidentBook
from obliskiq.calendars.service import Calendars
from obliskiq.calendars.status import BlockReason
from obliskiq.config import ObliskConfig
from obliskiq.domain.calendar import Incident, IncidentScope, IncidentSource

VENUES = ("XEUR", "XNYS", "XTKS")
FIRST, LAST = date(2025, 1, 1), date(2027, 12, 31)


@pytest.fixture(scope="module")
def facade(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(shipped_settings, reference=date(2026, 10, 9), venues=VENUES)


days = st.dates(min_value=FIRST, max_value=LAST)
venues = st.sampled_from(VENUES)


@st.composite
def incidents(draw: st.DrawFn) -> Incident:
    day = draw(days)
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(
        minutes=draw(st.integers(0, 24 * 60))
    )
    open_ended = draw(st.booleans())
    end = None if open_ended else start + timedelta(minutes=draw(st.integers(1, 3 * 24 * 60)))
    return Incident(
        incident_id=f"i{draw(st.integers(0, 10**6))}",
        origin="manual-incidents.yaml",
        source=IncidentSource.MANUAL,
        scope=IncidentScope.VENUE,
        venue=draw(venues),
        conid=None,
        start=start,
        expected_end=end,
        reason="generated",
    )


@given(mic=venues, day=days)
def test_windows_are_ordered_disjoint_and_inside_the_session(
    facade: Calendars, mic: str, day: date
) -> None:
    windows = facade.tradable_windows(mic, day)
    bounds = facade.session_bounds(mic, day)
    if bounds is None:
        assert windows == ()
        return
    for window in windows:
        assert bounds.open_utc <= window.start < window.end <= bounds.close_utc
    for earlier, later in pairwise(windows):
        assert earlier.end <= later.start


@given(mic=venues, day=days, noise=st.lists(incidents(), max_size=4))
def test_an_incident_can_only_remove_trading_time(
    facade: Calendars, mic: str, day: date, noise: list[Incident]
) -> None:
    clean = facade.tradable_windows(mic, day)
    facade.replace_incidents(IncidentBook(tuple(noise)))
    try:
        dirty = facade.tradable_windows(mic, day)
        status = facade.venue_status(mic, day)
    finally:
        facade.replace_incidents(IncidentBook())
    for window in dirty:
        assert any(c.start <= window.start and window.end <= c.end for c in clean)
    if status.ok:
        assert dirty == clean
    else:
        assert status.reason is not None
    assert sum((w.end - w.start for w in dirty), timedelta()) <= sum(
        (w.end - w.start for w in clean), timedelta()
    )


@given(mic=venues, day=days, minute=st.integers(0, 24 * 60 - 1))
def test_tradability_agrees_with_the_windows(
    facade: Calendars, mic: str, day: date, minute: int
) -> None:
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(minutes=minute)
    verdict = facade.tradability(mic, at)
    local_day = facade.session_day(mic, at)
    inside = any(w.contains(at) for w in facade.tradable_windows(mic, local_day))
    assert verdict.ok is inside
    assert (verdict.window is not None) is inside
    if not inside:
        assert verdict.reason is not None


@given(
    first=st.sampled_from(["XEUR", "XNYS"]), second=st.sampled_from(["XTKS", "XNYS"]), expiry=days
)
def test_the_last_tradable_session_is_a_common_session_and_nothing_later_is(
    facade: Calendars, first: str, second: str, expiry: date
) -> None:
    result = facade.last_tradable_session(first, second, expiry)
    assert result.session <= expiry
    assert facade.venue_status(first, result.session, date_critical=True).ok
    assert facade.venue_status(second, result.session, date_critical=True).ok
    day = result.session + timedelta(days=1)
    while day <= expiry:
        both = (
            facade.venue_status(first, day, date_critical=True).ok
            and facade.venue_status(second, day, date_critical=True).ok
        )
        assert not both
        day += timedelta(days=1)
    assert len(result.skipped) == (expiry - result.session).days
    assert all(result.session < s.day <= expiry for s in result.skipped)
    assert all(s.reason is BlockReason.NOT_A_SESSION for s in result.skipped)


@given(mic=venues, day=days)
def test_session_before_zero_is_the_anchor_only_on_a_session(
    facade: Calendars, mic: str, day: date
) -> None:
    session = facade.session_bounds(mic, day) is not None
    if session:
        assert facade.session_before(mic, mic, day, 0) == day
    else:
        previous = facade.previous_session(mic, day)
        assert previous is not None
        assert facade.session_before(mic, mic, day, 1) == previous
