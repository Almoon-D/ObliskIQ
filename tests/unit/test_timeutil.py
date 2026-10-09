"""UTC stamps and date parsing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from obliskiq.util.timeutil import (
    age_seconds,
    epoch_ms_to_stamp,
    iso_date,
    parse_date,
    parse_stamp,
    utc_now,
    utc_stamp,
    utc_today,
    years_before,
)


def test_utc_now_is_timezone_aware_utc():
    assert utc_now().tzinfo is UTC


def test_a_stamp_is_utc_whatever_the_input_zone():
    madrid_summer = timezone(timedelta(hours=2))
    moment = datetime(2026, 10, 8, 14, 30, 5, 999, tzinfo=madrid_summer)
    assert utc_stamp(moment) == "2026-10-08T12:30:05Z"


def test_a_stamp_round_trips():
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parse_stamp(utc_stamp(moment)) == moment


def test_utc_today_and_stamp_default_to_now():
    assert utc_today() == utc_now().date()
    assert parse_stamp(utc_stamp()) is not None


@pytest.mark.parametrize("value", ["", "yesterday", "2026-10-08 12:00:00"])
def test_an_unreadable_stamp_is_absent_not_fresh(value):
    assert parse_stamp(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-08", date(2026, 10, 8)),
        ("20261008", date(2026, 10, 8)),
        ("2026-10-08T15:00:00Z", date(2026, 10, 8)),
        ("20261008 15:00", date(2026, 10, 8)),
        (None, None),
        ("   ", None),
        ("not-a-date", None),
    ],
)
def test_parse_date_reads_iso_and_ibkr_forms(value, expected):
    assert parse_date(value) == expected


def test_iso_date():
    assert iso_date(date(2026, 3, 1)) == "2026-03-01"


def test_epoch_milliseconds_become_a_utc_stamp():
    assert epoch_ms_to_stamp(1_791_460_800_000) == "2026-10-08T12:00:00Z"


def test_age_seconds():
    now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
    assert age_seconds("2026-10-08T11:59:00Z", now=now) == 60.0
    assert age_seconds(None, now=now) is None
    assert age_seconds("", now=now) is None
    assert age_seconds("garbled", now=now) is None


def test_years_before_takes_the_year_length_from_the_caller():
    reference = date(2026, 10, 8)
    assert years_before(reference, 1, days_per_year=365) == date(2025, 10, 8)
    assert years_before(reference, 1, days_per_year=252) == reference - timedelta(days=252)
