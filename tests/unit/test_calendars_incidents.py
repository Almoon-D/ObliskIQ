"""Incidents: what blocks, for how long, who may raise them, and a manual file that fails closed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from obliskiq.calendars.incidents import (
    FOREVER,
    IncidentBook,
    IncidentPolicy,
    blocking_span,
    check_scope,
    load_manual_incidents,
)
from obliskiq.domain.calendar import Incident, IncidentScope, IncidentSource
from obliskiq.errors import IncidentRejectedError

START = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
GRACE = timedelta(minutes=30)
VENUES = ("XNYS", "XNAS", "XETR")


def make_incident(**overrides: Any) -> Incident:
    fields: dict[str, Any] = {
        "incident_id": "halt-1",
        "origin": "manual-incidents.yaml",
        "source": IncidentSource.MANUAL,
        "scope": IncidentScope.VENUE,
        "venue": "XNYS",
        "conid": None,
        "start": START,
        "expected_end": START + timedelta(hours=1),
        "reason": "technical halt",
    }
    fields.update(overrides)
    return Incident(**fields)


def feed_incident(**overrides: Any) -> Incident:
    fields: dict[str, Any] = {
        "origin": "nasdaq-halts",
        "source": IncidentSource.FEED,
        "scope": IncidentScope.INSTRUMENT,
        "venue": "XNAS",
        "conid": 265598,
        "incident_id": "LULD-1",
    }
    fields.update(overrides)
    return make_incident(**fields)


# ── the span an incident blocks ──


def test_an_incident_blocks_from_its_start_to_its_expected_end_plus_grace() -> None:
    incident = make_incident()
    assert blocking_span(incident, GRACE) == (START, START + timedelta(hours=1) + GRACE)


def test_an_open_ended_incident_blocks_until_it_is_removed() -> None:
    assert blocking_span(make_incident(expected_end=None), GRACE) == (START, FOREVER)


def test_the_span_does_not_depend_on_when_it_is_asked() -> None:
    incident = make_incident()
    assert blocking_span(incident, GRACE) == blocking_span(incident, GRACE)


# ── the book ──


def test_an_instrument_incident_blocks_only_that_instrument() -> None:
    venue_wide = make_incident(incident_id="a")
    one_stock = feed_incident(incident_id="b", venue="XNYS")
    book = IncidentBook((venue_wide, one_stock))
    both = {i.incident_id for i, _ in book.affecting("XNYS", 265598, GRACE)}
    assert both == {"a", "b"}
    other = {i.incident_id for i, _ in book.affecting("XNYS", 999, GRACE)}
    assert other == {"a"}
    unnamed = {i.incident_id for i, _ in book.affecting("XNYS", None, GRACE)}
    assert unnamed == {"a"}


def test_an_incident_on_another_venue_does_not_bear_on_this_one() -> None:
    book = IncidentBook((make_incident(venue="XNAS"),))
    assert book.affecting("XNYS", None, GRACE) == []


def test_stale_incidents_are_those_whose_block_has_run_out() -> None:
    ended = make_incident(incident_id="ended")
    open_ended = make_incident(incident_id="open", expected_end=None)
    book = IncidentBook((ended, open_ended))
    inside_grace = START + timedelta(hours=1, minutes=29)
    after_grace = START + timedelta(hours=1, minutes=31)
    assert book.stale(inside_grace, GRACE) == ()
    assert [i.incident_id for i in book.stale(after_grace, GRACE)] == ["ended"]


# ── who may raise an incident ──


FEED_POLICY = IncidentPolicy(accepts_feed=True, feed_scope=IncidentScope.INSTRUMENT)
NO_FEED = IncidentPolicy(accepts_feed=False, feed_scope=IncidentScope.VENUE)


def test_manual_entries_are_accepted_at_either_scope_and_for_any_policy() -> None:
    check_scope(make_incident(), NO_FEED)
    check_scope(feed_incident(source=IncidentSource.MANUAL), NO_FEED)


def test_a_feed_within_its_declared_scope_is_accepted() -> None:
    check_scope(feed_incident(), FEED_POLICY)


def test_a_feed_for_a_venue_without_one_is_refused() -> None:
    with pytest.raises(IncidentRejectedError, match="no incident feed is declared"):
        check_scope(feed_incident(), NO_FEED)


def test_a_single_stock_feed_never_closes_a_venue() -> None:
    venue_wide = feed_incident(scope=IncidentScope.VENUE, conid=None)
    with pytest.raises(IncidentRejectedError, match="instrument-scoped"):
        check_scope(venue_wide, FEED_POLICY)


def test_replacing_a_source_replaces_only_its_own_incidents() -> None:
    manual = make_incident(incident_id="manual")
    old = feed_incident(incident_id="old")
    new = feed_incident(incident_id="new")
    policies = {"XNAS": FEED_POLICY, "XNYS": NO_FEED}
    book = IncidentBook((manual, old)).replace_origin("nasdaq-halts", [new], policies)
    assert {i.incident_id for i in book.incidents} == {"manual", "new"}


def test_a_refused_replacement_changes_nothing() -> None:
    old = feed_incident(incident_id="old")
    book = IncidentBook((old,))
    policies = {"XNAS": FEED_POLICY}
    bad = feed_incident(incident_id="bad", scope=IncidentScope.VENUE, conid=None)
    with pytest.raises(IncidentRejectedError):
        book.replace_origin("nasdaq-halts", [feed_incident(incident_id="ok"), bad], policies)
    assert book.incidents == (old,)


def test_an_incident_naming_another_origin_is_refused() -> None:
    with pytest.raises(IncidentRejectedError, match="names origin"):
        IncidentBook().replace_origin("nasdaq-halts", [feed_incident(origin="other")], {})


def test_an_incident_for_an_unknown_venue_is_refused() -> None:
    with pytest.raises(IncidentRejectedError, match="unknown venue"):
        IncidentBook().replace_origin("nasdaq-halts", [feed_incident()], {})


def test_reloading_the_manual_file_keeps_the_fed_incidents() -> None:
    fed = feed_incident()
    fresh = IncidentBook((make_incident(incident_id="new-manual"),))
    book = IncidentBook((make_incident(incident_id="old-manual"), fed)).with_manual(fresh)
    assert {i.incident_id for i in book.incidents} == {"new-manual", fed.incident_id}
    assert book.invalid is None


def test_an_unreadable_manual_file_marks_the_book_invalid_but_keeps_fed_incidents() -> None:
    fed = feed_incident()
    book = IncidentBook((fed,)).with_manual(IncidentBook.unreadable("bad file"))
    assert book.invalid == "bad file"
    assert book.incidents == (fed,)


# ── the manual file ──

GOOD_ENTRY = """\
  - id: halt-1
    venue: XNYS
    scope: venue
    start: 2026-10-09T14:00:00+00:00
    expected_end: 2026-10-09T15:00:00+00:00
    reason: technical halt
"""


@pytest.fixture
def write_file(tmp_path: Path) -> Callable[[str], Path]:
    def write(text: str) -> Path:
        path = tmp_path / "manual-incidents.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


def test_a_missing_file_means_no_incidents(tmp_path: Path) -> None:
    book = load_manual_incidents(tmp_path / "absent.yaml", VENUES)
    assert book == IncidentBook()


def test_a_good_file_loads_its_entries(write_file: Callable[[str], Path]) -> None:
    text = (
        "incidents:\n"
        + GOOD_ENTRY
        + "  - id: one-stock\n    venue: XNAS\n    scope: instrument\n    conid: 265598\n"
        "    start: '2026-10-09T10:00:00+02:00'\n    reason: LULD pause\n"
    )
    book = load_manual_incidents(write_file(text), VENUES)
    assert book.invalid is None
    first, second = book.incidents
    assert first.incident_id == "halt-1"
    assert first.source is IncidentSource.MANUAL
    assert first.origin == "manual-incidents.yaml"
    assert first.start == START
    assert second.conid == 265598
    assert second.start == datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
    assert second.expected_end is None


def test_an_empty_file_or_an_empty_list_means_no_incidents(
    write_file: Callable[[str], Path],
) -> None:
    assert load_manual_incidents(write_file(""), VENUES).incidents == ()
    assert load_manual_incidents(write_file("incidents:\n"), VENUES).incidents == ()
    assert load_manual_incidents(write_file("incidents: []\n"), VENUES).invalid is None


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("incidents: [unclosed\n", "not valid YAML"),
        ("- just\n- a list\n", "mapping"),
        ("incident: []\n", "unknown top-level key"),
        ("incidents: nope\n", "must be a list"),
        ("incidents:\n  - just text\n", "must be a mapping"),
        ("incidents:\n" + GOOD_ENTRY + "    surprise: 1\n", "surprise"),
        ("incidents:\n" + GOOD_ENTRY.replace("+00:00", ""), "start"),
        ("incidents:\n" + GOOD_ENTRY.replace("XNYS", "XXXX"), "unknown venue XXXX"),
        ("incidents:\n" + GOOD_ENTRY + GOOD_ENTRY, "duplicate id 'halt-1'"),
        (
            "incidents:\n" + GOOD_ENTRY.replace("2026-10-09T15:00:00", "2026-10-09T13:00:00"),
            "expected_end must be after start",
        ),
        ("incidents:\n" + GOOD_ENTRY.replace("    reason: technical halt\n", ""), "reason"),
        ("incidents:\n" + GOOD_ENTRY.replace("scope: venue", "scope: instrument"), "conid"),
        ("incidents:\n" + GOOD_ENTRY.replace("scope: venue", "scope: market"), "scope"),
    ],
)
def test_a_malformed_file_makes_the_whole_book_invalid(
    write_file: Callable[[str], Path], text: str, complaint: str
) -> None:
    book = load_manual_incidents(write_file(text), VENUES)
    assert book.incidents == ()
    assert book.invalid is not None
    assert complaint in book.invalid


def test_a_good_entry_beside_a_bad_one_is_not_loaded(write_file: Callable[[str], Path]) -> None:
    text = (
        "incidents:\n" + GOOD_ENTRY + GOOD_ENTRY.replace("halt-1", "typo").replace("XNYS", "NOPE")
    )
    book = load_manual_incidents(write_file(text), VENUES)
    assert book.incidents == ()
    assert book.invalid is not None


def test_a_file_that_cannot_be_read_is_invalid_not_empty(tmp_path: Path) -> None:
    unreadable = tmp_path / "manual-incidents.yaml"
    unreadable.mkdir()  # exists, but cannot be read as a file
    book = load_manual_incidents(unreadable, VENUES)
    assert book.invalid is not None
    assert "cannot be read" in book.invalid
