"""Venues, phases, windows, schedule observations and incidents.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from obliskiq.domain.calendar import (
    Incident,
    IncidentScope,
    IncidentSource,
    Phase,
    PhaseKind,
    Region,
    SessionObservation,
    TimeWindow,
    Venue,
)

T0 = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def test_a_venue():
    venue = Venue(mic="XNYS", name="NYSE", region=Region.AMERICA, timezone="America/New_York")
    assert venue.zone.key == "America/New_York"


@pytest.mark.parametrize("mic", ["xnys", "XNY", "XNYSX", "X N Y"])
def test_a_mic_is_four_capitals_or_digits(mic):
    with pytest.raises(ValidationError, match="market code"):
        Venue(mic=mic, name="x", region=Region.ASIA, timezone="Asia/Tokyo")


@pytest.mark.parametrize("zone", ["Nowhere/Land", "", "../etc/passwd"])
def test_a_venue_needs_a_known_time_zone(zone):
    with pytest.raises(ValidationError, match="time zone"):
        Venue(mic="XTKS", name="x", region=Region.ASIA, timezone=zone)


def test_a_window_is_half_open():
    window = TimeWindow(start=T0, end=T1)
    assert window.contains(T0)
    assert not window.contains(T1)
    assert window.duration == timedelta(hours=1)


def test_a_window_is_never_empty_or_reversed():
    with pytest.raises(ValidationError, match="start before it ends"):
        TimeWindow(start=T0, end=T0)
    with pytest.raises(ValidationError, match="start before it ends"):
        TimeWindow(start=T1, end=T0)


def test_a_phase_has_a_window():
    phase = Phase(kind=PhaseKind.REGULAR, start=T0, end=T1)
    assert phase.window == TimeWindow(start=T0, end=T1)
    assert phase.contains(T0)
    assert not phase.contains(T1)
    with pytest.raises(ValidationError, match="start before it ends"):
        Phase(kind=PhaseKind.CLOSED, start=T1, end=T0)


def test_the_seven_phases():
    assert [kind.value for kind in PhaseKind] == [
        "pre_market",
        "opening_auction",
        "regular",
        "lunch_break",
        "closing_auction",
        "post_market",
        "closed",
    ]


def test_a_session_observation():
    observed = SessionObservation(
        venue="XNYS", day=date(2026, 10, 9), source="ibkr", is_session=True, open=T0, close=T1
    )
    assert observed.is_session


def test_a_session_observation_may_leave_the_hours_out():
    SessionObservation(
        venue="XNYS", day=date(2026, 10, 9), source="ibkr", is_session=True, open=None, close=None
    )


def test_a_closed_day_has_no_hours():
    SessionObservation(
        venue="XNYS", day=date(2026, 10, 9), source="ibkr", is_session=False, open=None, close=None
    )
    with pytest.raises(ValidationError, match="closed day"):
        SessionObservation(
            venue="XNYS", day=date(2026, 10, 9), source="x", is_session=False, open=T0, close=None
        )


def test_open_and_close_come_together_and_in_order():
    with pytest.raises(ValidationError, match="together"):
        SessionObservation(
            venue="XNYS", day=date(2026, 10, 9), source="x", is_session=True, open=T0, close=None
        )
    with pytest.raises(ValidationError, match="open before it closes"):
        SessionObservation(
            venue="XNYS", day=date(2026, 10, 9), source="x", is_session=True, open=T1, close=T0
        )


def _incident(**overrides):
    fields = {
        "incident_id": "i-1",
        "origin": "manual-incidents.yaml",
        "source": IncidentSource.MANUAL,
        "scope": IncidentScope.VENUE,
        "venue": "XNYS",
        "conid": None,
        "start": T0,
        "expected_end": T1,
        "reason": "matching-engine outage",
    }
    fields.update(overrides)
    return Incident(**fields)


def test_a_venue_incident_and_an_open_ended_one():
    assert _incident().expected_end == T1
    assert _incident(expected_end=None).expected_end is None


def test_an_instrument_incident_names_its_instrument():
    incident = _incident(scope=IncidentScope.INSTRUMENT, conid=756733)
    assert incident.conid == 756733
    with pytest.raises(ValidationError, match="conid"):
        _incident(scope=IncidentScope.INSTRUMENT)


def test_a_venue_incident_names_no_instrument():
    with pytest.raises(ValidationError, match="no instrument"):
        _incident(conid=756733)


def test_an_incident_ends_after_it_starts():
    with pytest.raises(ValidationError, match="expected_end"):
        _incident(expected_end=T0)


def test_an_incident_needs_a_reason():
    with pytest.raises(ValidationError, match="must not be empty"):
        _incident(reason=" ")
