"""Macro observations and the point-in-time series built from them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date

import pytest
from pydantic import ValidationError

from obliskiq.domain.macro import MacroSeries


def _series(observations, *, as_of=date(2026, 9, 1), unaligned=0):
    return MacroSeries(
        series_id="CPI_X",
        source="fred",
        as_of=as_of,
        observations=tuple(observations),
        unaligned=unaligned,
    )


def test_a_series_of_published_observations(make_observation):
    july = make_observation()
    august = make_observation(reference_date=date(2026, 8, 1), release_date=date(2026, 9, 11))
    series = _series([july, august], as_of=date(2026, 9, 11))
    assert len(series) == 2
    assert series.latest == august
    assert series.values() == [2.1, 2.1]


def test_an_empty_series_has_no_latest():
    series = _series([])
    assert len(series) == 0
    assert series.latest is None


def test_a_series_refuses_an_observation_not_yet_public(make_observation):
    august = make_observation(reference_date=date(2026, 8, 1), release_date=date(2026, 9, 11))
    with pytest.raises(ValidationError, match="released on or before as_of"):
        _series([august], as_of=date(2026, 9, 1))


def test_a_series_refuses_an_observation_with_no_release_date(make_observation):
    with pytest.raises(ValidationError, match="released on or before as_of"):
        _series([make_observation(release_date=None)])


def test_a_series_is_ordered_by_reference_date(make_observation):
    july = make_observation()
    august = make_observation(reference_date=date(2026, 8, 1), release_date=date(2026, 8, 20))
    with pytest.raises(ValidationError, match="ordered"):
        _series([august, july])


def test_declared_units_and_frequencies_are_collected(make_observation):
    july = make_observation(units="percent", frequency="monthly")
    august = make_observation(
        reference_date=date(2026, 8, 1),
        release_date=date(2026, 8, 20),
        units="index",
        frequency=None,
    )
    series = _series([july, august])
    assert series.units == {"percent", "index"}
    assert series.frequencies == {"monthly"}


def test_an_undeclared_unit_cannot_contradict_a_declared_one(make_observation):
    july = make_observation(units="percent")
    august = make_observation(
        reference_date=date(2026, 8, 1), release_date=date(2026, 8, 20), units=None
    )
    assert _series([july, august]).units == {"percent"}


def test_the_unaligned_count_is_carried():
    assert _series([], unaligned=3).unaligned == 3
    with pytest.raises(ValidationError):
        _series([], unaligned=-1)
