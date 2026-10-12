"""IBKR's trading schedule as the calendars' cross-check: cached and read-only; None is no
information."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from obliskiq.ibkr.client import Contracts
from obliskiq.ibkr.decode import decode_trading_schedule
from obliskiq.ibkr.schedule_source import SOURCE_NAME, IbkrTradingSchedule, observations

BASE = "https://gw.test/v1/api"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ibkr"
SPY = 756733


def mon_to_fri(venue: str, day: date) -> bool:
    """The trading weekdays most venues keep; the calendars supply each venue's own."""
    return day.weekday() < 5


def _schedule(body=None):
    data = (
        body if body is not None else json.loads((FIXTURES / "trading_schedule.json").read_text())
    )
    return decode_trading_schedule(SPY, None, data)


def test_liquid_hours_become_regular_sessions_in_utc():
    schedule = _schedule()
    assert schedule.timezone == "US/Eastern"
    first = schedule.sessions[0]
    assert first.day == date(2026, 10, 8)
    assert first.open == datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
    assert first.close == datetime(2026, 10, 8, 20, 0, tzinfo=UTC)


def test_observations_carry_the_source_and_the_session():
    found = observations("XNYS", _schedule(), missing_is_closed=True, is_trading_weekday=mon_to_fri)
    observed = found[date(2026, 10, 9)]
    assert observed.source == SOURCE_NAME
    assert observed.is_session
    assert observed.open == datetime(2026, 10, 9, 13, 30, tzinfo=UTC)


def test_a_weekday_missing_inside_the_span_reads_as_closed():
    body = json.loads((FIXTURES / "trading_schedule.json").read_text())
    del body["schedules"]["20261012"]
    found = observations(
        "XNYS", _schedule(body), missing_is_closed=True, is_trading_weekday=mon_to_fri
    )
    assert found[date(2026, 10, 12)].is_session is False
    assert date(2026, 10, 10) not in found, "a weekend is never read as a missing session"


def test_without_the_omission_rule_a_missing_day_is_no_information():
    body = json.loads((FIXTURES / "trading_schedule.json").read_text())
    del body["schedules"]["20261012"]
    found = observations(
        "XNYS", _schedule(body), missing_is_closed=False, is_trading_weekday=mon_to_fri
    )
    assert date(2026, 10, 12) not in found


def test_a_split_session_is_compared_from_first_open_to_last_close():
    body = {
        "schedules": {
            "20261009": {
                "liquid_hours": [
                    {"opening": 1791507600, "closing": 1791516600},
                    {"opening": 1791520200, "closing": 1791529200},
                ]
            }
        }
    }
    found = observations(
        "XTKS", _schedule(body), missing_is_closed=True, is_trading_weekday=mon_to_fri
    )
    observed = found[date(2026, 10, 9)]
    assert observed.open == datetime.fromtimestamp(1791507600, tz=UTC)
    assert observed.close == datetime.fromtimestamp(1791529200, tz=UTC)


def test_a_malformed_span_is_dropped():
    body = {"schedules": {"20261009": {"liquid_hours": [{"opening": 5, "closing": 1}, "x"]}}}
    assert _schedule(body).sessions == ()


def test_observe_only_reads_and_none_means_no_information(shipped_settings):
    source = IbkrTradingSchedule(shipped_settings.ibkr, is_trading_weekday=mon_to_fri)
    assert source.name == SOURCE_NAME
    assert source.observe("XNYS", date(2026, 10, 9), None) is None
    source.update("XNYS", _schedule())
    assert source.observe("XNYS", date(2026, 10, 9), None) is not None
    assert source.observe("XNYS", date(2026, 11, 9), None) is None, "outside the span"
    assert source.observe("XNAS", date(2026, 10, 9), None) is None, "another venue"


def test_a_skipped_venue_is_never_observed(shipped_settings):
    source = IbkrTradingSchedule(shipped_settings.ibkr, is_trading_weekday=mon_to_fri)
    assert "XEUR" in shipped_settings.ibkr.trading_schedule.skip_venues
    source.update("XEUR", _schedule())
    assert source.observe("XEUR", date(2026, 10, 9), None) is None


def test_a_contract_schedule_answers_for_that_contract_only(shipped_settings):
    source = IbkrTradingSchedule(shipped_settings.ibkr, is_trading_weekday=mon_to_fri)
    source.update("XNYS", _schedule(), for_contract=True)
    assert source.observe("XNYS", date(2026, 10, 9), SPY) is not None
    assert source.observe("XNYS", date(2026, 10, 9), None) is None


@respx.mock
async def test_refresh_fills_each_venue_and_keeps_what_a_failed_venue_had(
    make_session, shipped_settings
):
    body = json.loads((FIXTURES / "trading_schedule.json").read_text())
    route = respx.get(f"{BASE}/contract/trading-schedule")
    route.side_effect = [
        httpx.Response(200, json=body),
        httpx.Response(403, json={"error": "not entitled"}),
    ]
    source = IbkrTradingSchedule(shipped_settings.ibkr, is_trading_weekday=mon_to_fri)
    source.update("XNAS", _schedule())
    contracts = Contracts(make_session())
    failed = await source.refresh(
        contracts, {"XNYS": (SPY, None), "XNAS": (SPY, "ISLAND"), "XEUR": (1, None)}
    )
    assert failed == ["XNYS"] or failed == ["XNAS"]
    assert source.observe("XNAS", date(2026, 10, 9), None) is not None
    assert route.call_count == 2, "a skipped venue is not asked for"


async def test_the_calendars_report_a_disagreement_from_this_source(
    build_calendars, shipped_settings
):
    body = json.loads((FIXTURES / "trading_schedule.json").read_text())
    body["schedules"]["20261009"]["liquid_hours"][0]["closing"] -= 3 * 3600
    source = IbkrTradingSchedule(shipped_settings.ibkr, is_trading_weekday=mon_to_fri)
    source.update("XNYS", _schedule(body))
    alerts = []
    calendars = build_calendars(("XNYS",), sources=[source], alert=alerts.append)
    status = calendars.venue_status("XNYS", date(2026, 10, 9), date_critical=True)
    assert not status.ok
    assert alerts
    assert alerts[0].source == SOURCE_NAME


@pytest.mark.parametrize("key", ["2026-10-09", "20261399", "abc"])
def test_a_malformed_date_key_is_ignored(key):
    body = {"schedules": {key: {"liquid_hours": [{"opening": 1, "closing": 2}]}}}
    assert _schedule(body).sessions == ()


def test_a_venue_trading_sunday_to_thursday_reads_its_own_weekend():
    """The weekend is the venue's: for a Sunday-to-Thursday venue a missing Sunday is a closure
    and a missing Friday is its weekend, not a missing session."""

    def sun_to_thu(venue: str, day: date) -> bool:
        return day.weekday() in {6, 0, 1, 2, 3}

    body = json.loads((FIXTURES / "trading_schedule.json").read_text())
    del body["schedules"]["20261009"]
    found = observations(
        "XSAU", _schedule(body), missing_is_closed=True, is_trading_weekday=sun_to_thu
    )
    assert date(2026, 10, 9) not in found, "Friday is this venue's weekend"
    assert found[date(2026, 10, 11)].is_session is False, "a missing Sunday is a closure"
