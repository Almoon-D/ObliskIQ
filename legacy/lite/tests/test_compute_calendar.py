"""Trading-day arithmetic, and the mandatory close date it exists for.

The close date is computed and written to the ledger at ENTRY, months before
anyone looks at it. Counting back ten weekdays from a December expiry and landing
on Christmas Day produces a date the market cannot honour, and IBKR then
force-liquidates at market — the exact outcome the rule exists to prevent.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

import pytest

from obliskiq.compute.calendar import TradingCalendars
from obliskiq.errors import ConfigError


@pytest.fixture()
def calendars(config):
    return TradingCalendars.from_config(config)


def test_weekends_are_never_trading_days(calendars):
    nyse = calendars.by_id("XNYS")
    assert not nyse.is_trading_day(date(2026, 3, 7))  # Saturday
    assert not nyse.is_trading_day(date(2026, 3, 8))  # Sunday
    assert nyse.is_trading_day(date(2026, 3, 9))  # Monday


def test_a_declared_holiday_is_not_a_trading_day(calendars):
    nyse = calendars.by_id("XNYS")
    assert not nyse.is_trading_day(date(2026, 12, 25))  # Christmas, a Friday
    assert not nyse.is_trading_day(date(2026, 7, 3))  # 4 July observed


def test_the_mandatory_close_date_respects_holidays(config, calendars):
    """The headline requirement: ten trading days before expiry, holidays counted.

    A mid-January expiry counts back across New Year's Day. Skipping weekends
    alone lands one session later than the truth, and that session is the one the
    market is shut.
    """
    trading_days = config.require_int("tenor.mandatory_close_trading_days")
    expiry = date(2027, 1, 15)

    with_holidays = calendars.close_date(expiry, trading_days=trading_days, exchange="ARCA")
    weekends_only = calendars.close_date(expiry, trading_days=trading_days, exchange="UNKNOWN")

    assert with_holidays.close_date == date(2026, 12, 31)
    assert weekends_only.close_date == date(2027, 1, 1)
    assert with_holidays.close_date < weekends_only.close_date
    assert calendars.by_id("XNYS").is_trading_day(with_holidays.close_date)


def test_the_count_is_sessions_not_calendar_days(config, calendars):
    """A holiday inside the window pushes the close date one session earlier.

    Labor Day sits between these two dates, so ten sessions spans fifteen
    calendar days rather than fourteen.
    """
    trading_days = config.require_int("tenor.mandatory_close_trading_days")
    nyse = calendars.by_id("XNYS")
    expiry = date(2026, 9, 18)  # third Friday, a trading day

    close = calendars.close_date(expiry, trading_days=trading_days, exchange="ARCA")

    assert close.close_date == date(2026, 9, 3)
    assert (expiry - close.close_date).days == 15
    assert nyse.trading_days_between(close.close_date, expiry) == trading_days


def test_an_expiry_falling_on_a_holiday_still_counts_ten_live_sessions(calendars):
    """June 2026 Juneteenth lands on the third Friday, so expiry is not a session.

    The count is of sessions on which the position could still be closed. Expiry
    is never one of them, and here it could not be one even in principle.
    """
    nyse = calendars.by_id("XNYS")
    expiry = date(2026, 6, 19)
    assert not nyse.is_trading_day(expiry)

    close = calendars.close_date(expiry, trading_days=10, exchange="ARCA")
    assert close.close_date == date(2026, 6, 5)
    # Ten sessions strictly before expiry, the last of which is 18 June.
    assert nyse.trading_days_between(close.close_date, nyse.previous_trading_day(expiry)) == 9


def test_expiry_itself_is_not_counted_as_one_of_the_days(calendars):
    """ "Ten trading days before expiry" means ten sessions still available."""
    nyse = calendars.by_id("XNYS")
    expiry = date(2026, 3, 20)  # a Friday
    assert nyse.subtract_trading_days(expiry, 1) == date(2026, 3, 19)
    assert nyse.subtract_trading_days(expiry, 0) == expiry


def test_an_undeclared_venue_says_it_used_weekends_only(calendars):
    """A silent assumption is the failure mode; the result has to carry the gap."""
    result = calendars.close_date(date(2026, 12, 18), trading_days=10, exchange="KSE")
    assert result.calendar_id == "weekends_only"
    assert result.holidays_declared is False
    assert "weekends only" in result.provenance

    declared = calendars.close_date(date(2026, 12, 18), trading_days=10, exchange="ARCA")
    assert declared.holidays_declared is True
    assert "weekends only" not in declared.provenance


def test_every_exchange_in_the_universe_resolves_to_a_calendar(config, calendars):
    """Not every venue needs a holiday list, but every one must resolve to something."""
    from obliskiq.collect.universe import load_universe_file

    universe = load_universe_file(config.require_str("collectors.universe.path"), config)
    for instrument in universe.instruments:
        assert calendars.for_exchange(instrument.exchange) is not None


def test_the_venues_where_options_are_actually_held_have_holiday_lists(config, calendars):
    """A weekends-only close date on the venue we trade would be the real bug."""
    from obliskiq.collect.universe import load_universe_file

    universe = load_universe_file(config.require_str("collectors.universe.path"), config)
    chain_venues = {i.exchange for i in universe.instruments if i.collect_chain and i.exchange}
    undeclared = sorted(
        venue for venue in chain_venues if not calendars.for_exchange(venue).holidays_declared
    )
    assert not undeclared, (
        f"option chains are collected on {undeclared}, which have no holiday list in "
        "calendars.yaml. A mandatory close date computed there skips weekends only."
    )


def test_a_malformed_default_is_refused(config):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["calendars"]["default"] = "does_not_exist"
    with pytest.raises(ConfigError, match=r"calendars\.default"):
        TradingCalendars.from_config(Config(data, config.source))


def test_a_missing_calendar_file_names_the_config_key(config):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["calendars"]["path"] = "no-such-calendars.yaml"
    with pytest.raises(ConfigError, match=r"calendars\.path"):
        TradingCalendars.from_config(Config(data, config.source))


# ── a declared list still has to cover the year being asked about ──


def test_a_date_outside_the_declared_years_is_not_calendar_checked(calendars):
    """`holidays_declared` asks whether a list exists; a 2028 expiry passes that
    trivially against a 2026-2027 list while every 2028 holiday is absent.

    That is the Christmas-Day close date calendars.yaml's own header says it
    exists to prevent, arriving once the lists run out.
    """
    covered = calendars.close_date(date(2026, 12, 18), trading_days=10, exchange="ARCA")
    assert covered.holidays_declared
    assert covered.years_covered

    beyond = calendars.close_date(date(2029, 12, 21), trading_days=10, exchange="ARCA")
    assert beyond.holidays_declared, "the list exists"
    assert not beyond.years_covered, "and says nothing about 2029"
    assert "WEEKENDS ONLY IN PRACTICE" in beyond.provenance


def test_a_venue_with_no_list_at_all_is_still_reported_as_such(calendars):
    """The other flag keeps its own meaning; the two are not merged."""
    unknown = calendars.close_date(date(2026, 12, 18), trading_days=10, exchange="NOWHERE")
    assert not unknown.holidays_declared
    assert "weekends only; no holiday list declared" in unknown.provenance
