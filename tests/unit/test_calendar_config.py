"""The calendar configuration: the shipped venue table, its validators and its references.

Ported in intent from the legacy pulse ``test_config.py``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from obliskiq.calendars.incidents import load_manual_incidents
from obliskiq.config import ConfigInvalid, ObliskConfig, load_config
from obliskiq.config.schema_calendars import (
    LunchBreakConfig,
    PhaseWindowConfig,
    Provenance,
    SyntheticCalendarConfig,
    VenueConfig,
)

NOTE = {"origin": "test", "verify_live": False}


def _problems(config_dir: Path, owner_yaml: str) -> list[str]:
    owner = config_dir / "owner.yaml"
    owner.write_text(owner.read_text(encoding="utf-8") + "\n" + owner_yaml, encoding="utf-8")
    with pytest.raises(ConfigInvalid) as raised:
        load_config(config_dir)
    return [str(problem) for problem in raised.value.problems]


def _venue(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "name": "Test",
        "region": "Europe",
        "timezone": "Europe/Madrid",
        "phase_windows": [],
        "settlement_sessions": 2,
        "incident_source": {"type": "manual", "scope": "market_wide"},
        "sources": {"calendar": NOTE, "phases": NOTE, "settlement": NOTE},
    }
    fields.update(overrides)
    return fields


def _window(phase: str, anchor: str, start: int, end: int) -> dict[str, Any]:
    return {
        "phase": phase,
        "anchor": anchor,
        "start_offset_minutes": start,
        "end_offset_minutes": end,
    }


# ── the shipped table ──


def test_the_shipped_table_has_the_pulse_roster_plus_eurex(shipped_settings: ObliskConfig) -> None:
    venues = shipped_settings.venues
    assert len(venues) == 27
    assert "XEUR" in venues
    assert set(shipped_settings.synthetic_calendars) == {"CHIA", "XNSE", "XSHE", "XSPX"}


def test_the_roster_keeps_its_order_and_display_names(shipped_settings: ObliskConfig) -> None:
    by_region: dict[str, list[str]] = {}
    for config in shipped_settings.venues.values():
        by_region.setdefault(config.region, []).append(config.name)
    assert list(by_region) == ["America", "Europe", "Asia", "Oceania"]
    assert by_region["America"] == [
        "NYSE", "Nasdaq", "Toronto Stock Ex.", "B3 Bolsa do Brasil", "Bolsa de Méjico",
        "Bolsa de Santiago",
    ]  # fmt: skip
    assert by_region["Europe"] == [
        "London Stock Ex.", "Bolsa de Madrid", "Deutsche Börse Xetra", "Euronext Paris",
        "Euronext Amsterdam", "Euronext Milán", "SIX Swiss Ex.", "Eurex",
    ]  # fmt: skip
    assert by_region["Asia"] == [
        "Tokyo Stock Ex.", "Hong Kong Ex.", "Shanghai Stock Ex.", "Shenzhen Stock Ex.",
        "BSE India", "NSE India", "Korea Ex.", "Taiwan Stock Ex.", "Singapore Ex.",
    ]  # fmt: skip
    assert by_region["Oceania"] == [
        "Australian Securities Ex.", "Cboe Australia", "New Zealand", "South Pacific Stock Ex.",
    ]  # fmt: skip


def test_anchor_venues_are_configured_as_expected(shipped_settings: ObliskConfig) -> None:
    synthetic = shipped_settings.synthetic_calendars
    for mic in ("XNYS", "XMAD", "XTKS"):
        assert mic in shipped_settings.venues
        assert mic not in synthetic  # their calendar is the library's
    assert synthetic["XSPX"].open == "10:00"
    assert synthetic["XSPX"].close == "12:00"
    assert shipped_settings.venues["CHIA"].name == "Cboe Australia"
    assert "CHIA" in synthetic


def test_americas_identities(shipped_settings: ObliskConfig) -> None:
    venues = shipped_settings.venues
    assert {"XNYS", "XNAS", "XMEX", "XSGO"} <= set(venues)
    assert venues["XNAS"].timezone == "America/New_York"
    assert venues["XSGO"].timezone == "America/Santiago"


def test_nasdaq_owns_the_structured_halt_feed(shipped_settings: ObliskConfig) -> None:
    venues = shipped_settings.venues
    assert venues["XNAS"].incident_source.type == "structured_feed"
    assert venues["XNAS"].incident_source.scope == "single_stock"
    assert venues["XNYS"].incident_source.type == "manual"


def test_taiwan_models_no_pre_or_post_market(shipped_settings: ObliskConfig) -> None:
    """TWSE: only a closing call auction is documented, so none is modelled for the rest."""
    assert {w.phase for w in shipped_settings.venues["XTAI"].phase_windows} == {"closing_auction"}


def test_every_unverified_value_says_where_it_came_from(shipped_settings: ObliskConfig) -> None:
    for mic, venue in shipped_settings.venues.items():
        for note in (venue.sources.calendar, venue.sources.phases):
            assert note.origin, mic
    for section in (
        shipped_settings.lunch_breaks,
        shipped_settings.seasonal_closes,
        shipped_settings.ibkr_exchanges,
    ):
        for key, entry in section.items():
            source = entry.source
            assert source.origin, key
    assert all(entry.source.verify_live for entry in shipped_settings.ibkr_exchanges.values())


def test_the_shipped_file_is_plain_data_a_person_can_read() -> None:
    text = Path("config/defaults/venues.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert list(data) == [
        "calendars", "venues", "synthetic_calendars", "lunch_breaks", "seasonal_closes",
        "ibkr_exchanges",
    ]  # fmt: skip
    assert "'10:00'" in text or '"10:00"' in text  # times are quoted


# ── the manual incident example ships valid ──


def test_the_example_incident_file_is_valid_and_empty(shipped_settings: ObliskConfig) -> None:
    book = load_manual_incidents(
        Path("config/manual-incidents.example.yaml"), shipped_settings.venues
    )
    assert book.invalid is None
    assert book.incidents == ()


def test_the_commented_example_in_that_file_would_load(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    lines = Path("config/manual-incidents.example.yaml").read_text(encoding="utf-8").splitlines()
    start = lines.index("# incidents:")
    commented = [line for line in lines[start:] if line.startswith("#")]
    body = "\n".join(line[2:] for line in commented)
    path = tmp_path / "manual-incidents.yaml"
    path.write_text(body + "\n", encoding="utf-8")
    book = load_manual_incidents(path, shipped_settings.venues)
    assert book.invalid is None
    assert [i.incident_id for i in book.incidents] == ["nyse-matching-engine-2026-09-22"]


# ── phase windows ──


@pytest.mark.parametrize(
    ("window", "complaint"),
    [
        (_window("pre_market", "session_open", 0, -10), "end offset must be greater"),
        (_window("pre_market", "session_close", -60, 0), "anchor must be session_open"),
        (_window("closing_auction", "session_open", -10, 0), "anchor must be session_close"),
        (_window("pre_market", "session_open", -60, 5), "end no later than the session open"),
        (
            _window("closing_auction", "session_close", 5, 10),
            "start no later than the session close",
        ),
        (_window("post_market", "session_close", -5, 10), "at or after the session close"),
    ],
)
def test_an_incoherent_phase_window_is_refused(window: dict[str, Any], complaint: str) -> None:
    with pytest.raises(ValidationError, match=complaint):
        PhaseWindowConfig.model_validate(window)


def test_a_venue_cannot_repeat_a_phase_or_overlap_windows() -> None:
    twice = [
        _window("pre_market", "session_open", -60, 0),
        _window("pre_market", "session_open", -30, 0),
    ]
    with pytest.raises(ValidationError, match="cannot repeat a phase"):
        VenueConfig.model_validate(_venue(phase_windows=twice))
    overlap = [
        _window("pre_market", "session_open", -60, -10),
        _window("opening_auction", "session_open", -30, 0),
    ]
    with pytest.raises(ValidationError, match="overlap"):
        VenueConfig.model_validate(_venue(phase_windows=overlap))
    touching = [
        _window("pre_market", "session_open", -60, -30),
        _window("opening_auction", "session_open", -30, 0),
    ]
    assert VenueConfig.model_validate(_venue(phase_windows=touching))


def test_a_time_zone_must_exist() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        VenueConfig.model_validate(_venue(timezone="Mars/Olympus"))


# ── times, synthetic calendars, lunch ──


def _synthetic(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "open": "10:00",
        "close": "12:00",
        "trading_days": ["mon", "tue"],
        "holidays": [],
        "holidays_through": None,
    }
    fields.update(overrides)
    return fields


def test_an_unquoted_time_gets_a_plain_hint() -> None:
    with pytest.raises(ValidationError, match='quote times, such as "10:00"'):
        SyntheticCalendarConfig.model_validate(_synthetic(open=600))


@pytest.mark.parametrize("bad", ["9:30", "24:00", "10:60", "ten", "10:00:00", ""])
def test_a_clock_time_must_be_hh_mm(bad: str) -> None:
    with pytest.raises(ValidationError, match="HH:MM"):
        SyntheticCalendarConfig.model_validate(_synthetic(open=bad))


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"open": "12:00", "close": "10:00"}, "open must be before close"),
        ({"trading_days": []}, "trading_days must name each"),
        ({"trading_days": ["mon", "mon"]}, "trading_days must name each"),
        ({"holidays": ["2026-12-25"]}, "needs holidays_through"),
        (
            {"holidays": ["2027-01-01"], "holidays_through": "2026-12-31"},
            "falls after holidays_through",
        ),
    ],
)
def test_an_incoherent_synthetic_calendar_is_refused(
    overrides: dict[str, Any], complaint: str
) -> None:
    with pytest.raises(ValidationError, match=complaint):
        SyntheticCalendarConfig.model_validate(_synthetic(**overrides))


def test_a_lunch_break_must_run_forwards() -> None:
    with pytest.raises(ValidationError, match="start must be before end"):
        LunchBreakConfig.model_validate({"start": "13:00", "end": "12:00", "source": NOTE})
    assert LunchBreakConfig.model_validate({"start": "12:00", "end": "13:00", "source": NOTE})


def test_provenance_needs_both_fields() -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate({"origin": "x"})


# ── references and the loader ──


def test_an_unquoted_time_in_the_owner_file_is_explained(config_dir: Path) -> None:
    problems = _problems(
        config_dir,
        "lunch_breaks:\n  XNYS:\n    start: 11:30\n    end: 12:30\n"
        "    source: {origin: test, verify_live: false}\n",
    )
    assert any("quote times" in p and "lunch_breaks.XNYS.start" in p for p in problems)


@pytest.mark.parametrize(
    ("owner_yaml", "complaint"),
    [
        (
            "lunch_breaks:\n  NOPE:\n    start: '11:30'\n    end: '12:30'\n"
            "    source: {origin: test, verify_live: false}\n",
            "lunch_breaks.NOPE: not a venue declared in venues",
        ),
        (
            "calendars:\n  venue_order_offsets:\n    NOPE:\n      after_open_minutes: 1\n"
            "      before_close_minutes: 1\n",
            "calendars.venue_order_offsets.NOPE: not a venue",
        ),
        (
            "ibkr_exchanges:\n  XYZ:\n    venue: NOPE\n"
            "    source: {origin: test, verify_live: true}\n",
            "ibkr_exchanges.XYZ.venue: NOPE is not a venue",
        ),
        (
            "seasonal_closes:\n  XSPX:\n    reference_timezone: America/New_York\n"
            "    close: '11:00'\n    source: {origin: test, verify_live: true}\n",
            "seasonal_closes.XSPX: a synthetic calendar has no library close",
        ),
    ],
)
def test_references_to_unknown_venues_are_refused(
    config_dir: Path, owner_yaml: str, complaint: str
) -> None:
    assert any(complaint in problem for problem in _problems(config_dir, owner_yaml))


def test_an_ibkr_exchange_code_has_a_fixed_shape(config_dir: Path) -> None:
    problems = _problems(
        config_dir,
        "ibkr_exchanges:\n  smart:\n    venue: XNYS\n"
        "    source: {origin: test, verify_live: true}\n",
    )
    assert any("ibkr_exchanges.smart" in p for p in problems)


def test_an_unknown_key_in_a_venue_is_refused_with_a_suggestion(config_dir: Path) -> None:
    problems = _problems(config_dir, "venues:\n  XNYS:\n    timezon: America/New_York\n")
    assert any("timezon" in p for p in problems)
