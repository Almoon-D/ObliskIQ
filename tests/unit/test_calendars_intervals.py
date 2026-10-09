"""Interval arithmetic on UTC instants.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from obliskiq.calendars.intervals import (
    contains,
    from_windows,
    intersect,
    intersect_windows,
    merge,
    overlaps,
    subtract,
    to_windows,
)
from obliskiq.domain.calendar import TimeWindow

BASE = datetime(2026, 10, 9, tzinfo=UTC)


def at(minute: int) -> datetime:
    return BASE + timedelta(minutes=minute)


def span(start: int, end: int) -> tuple[datetime, datetime]:
    return (at(start), at(end))


def test_merge_sorts_joins_overlaps_and_touching_spans_and_drops_empty_ones():
    merged = merge([span(30, 40), span(0, 10), span(5, 20), span(20, 25), span(50, 50)])
    assert merged == [span(0, 25), span(30, 40)]


def test_merge_of_nothing_is_nothing():
    assert merge([]) == []


def test_subtract_splits_trims_and_removes():
    spans = [span(0, 100)]
    assert subtract(spans, [span(40, 60)]) == [span(0, 40), span(60, 100)]
    assert subtract(spans, [span(-10, 20)]) == [span(20, 100)]
    assert subtract(spans, [span(90, 200)]) == [span(0, 90)]
    assert subtract(spans, [span(0, 100)]) == []
    assert subtract(spans, []) == spans
    assert subtract(spans, [span(200, 300)]) == spans


def test_subtract_several_holes_from_several_spans():
    result = subtract([span(0, 50), span(60, 100)], [span(10, 20), span(45, 70), span(95, 99)])
    assert result == [span(0, 10), span(20, 45), span(70, 95), span(99, 100)]


def test_a_hole_that_only_touches_removes_nothing():
    assert subtract([span(0, 10)], [span(10, 20)]) == [span(0, 10)]


def test_intersect():
    assert intersect([span(0, 50)], [span(30, 80)]) == [span(30, 50)]
    assert intersect([span(0, 10), span(20, 30)], [span(5, 25)]) == [span(5, 10), span(20, 25)]
    assert intersect([span(0, 10)], [span(10, 20)]) == []
    assert intersect([], [span(0, 10)]) == []


def test_overlaps_is_half_open():
    spans = [span(10, 20)]
    assert overlaps(spans, at(15), at(16))
    assert overlaps(spans, at(0), at(11))
    assert not overlaps(spans, at(20), at(30))
    assert not overlaps(spans, at(0), at(10))


def test_contains_is_half_open():
    spans = [span(10, 20)]
    assert contains(spans, at(10))
    assert contains(spans, at(19))
    assert not contains(spans, at(20))
    assert not contains(spans, at(9))


def test_windows_round_trip_and_merge():
    windows = to_windows([span(0, 10), span(10, 20), span(30, 40)])
    assert windows == (
        TimeWindow(start=at(0), end=at(20)),
        TimeWindow(start=at(30), end=at(40)),
    )
    assert from_windows(windows) == [span(0, 20), span(30, 40)]


def test_windows_common_to_every_group():
    a = [TimeWindow(start=at(0), end=at(60))]
    b = [TimeWindow(start=at(30), end=at(90))]
    c = [TimeWindow(start=at(40), end=at(50)), TimeWindow(start=at(80), end=at(100))]
    assert intersect_windows(a, b) == (TimeWindow(start=at(30), end=at(60)),)
    assert intersect_windows(a, b, c) == (TimeWindow(start=at(40), end=at(50)),)


def test_a_single_group_is_itself_and_no_group_vouches_for_nothing():
    a = [TimeWindow(start=at(0), end=at(60))]
    assert intersect_windows(a) == tuple(a)
    assert intersect_windows() == ()


_spans = st.lists(
    st.tuples(st.integers(0, 200), st.integers(0, 200)).map(lambda p: span(*sorted(p))),
    max_size=6,
)


def _minutes(spans):
    """The set of whole minutes the spans cover, so set algebra checks the arithmetic."""
    return {
        minute
        for start, end in spans
        for minute in range(
            int((start - BASE).total_seconds() // 60), int((end - BASE).total_seconds() // 60)
        )
    }


@given(_spans)
def test_merge_is_sorted_disjoint_idempotent_and_keeps_coverage(spans):
    merged = merge(spans)
    assert merge(merged) == merged
    assert all(a_end < b_start for (_, a_end), (b_start, _) in pairwise(merged))
    assert _minutes(merged) == _minutes(spans)


@given(_spans, _spans)
def test_subtract_and_intersect_are_set_algebra(left, right):
    assert _minutes(subtract(left, right)) == _minutes(left) - _minutes(right)
    assert _minutes(intersect(left, right)) == _minutes(left) & _minutes(right)
    assert intersect(left, right) == intersect(right, left)


@pytest.mark.parametrize("hole", [span(0, 5), span(5, 10)])
def test_subtract_keeps_order_and_never_returns_empty_spans(hole):
    for start, end in subtract([span(0, 10)], [hole]):
        assert start < end
