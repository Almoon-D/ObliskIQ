"""Half-open intervals of UTC instants: merge, subtract, intersect.

A venue's tradable time is its regular sessions with auctions, lunch breaks and incidents
taken out, and an option is tradable only where its own venue and its underlying's both are.
All of that is arithmetic on ``[start, end)`` intervals, kept here once.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from datetime import datetime

from ..domain.calendar import TimeWindow

type Span = tuple[datetime, datetime]


def merge(spans: Iterable[Span]) -> list[Span]:
    """Sorted, with empty spans dropped and overlapping or touching spans joined."""
    merged: list[Span] = []
    for start, end in sorted(span for span in spans if span[0] < span[1]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract(spans: Iterable[Span], holes: Iterable[Span]) -> list[Span]:
    """``spans`` with every instant of ``holes`` removed."""
    remaining = merge(spans)
    for hole_start, hole_end in merge(holes):
        pieces: list[Span] = []
        for start, end in remaining:
            if hole_end <= start or end <= hole_start:
                pieces.append((start, end))
                continue
            if start < hole_start:
                pieces.append((start, hole_start))
            if hole_end < end:
                pieces.append((hole_end, end))
        remaining = pieces
    return remaining


def intersect(left: Iterable[Span], right: Iterable[Span]) -> list[Span]:
    """The instants in both ``left`` and ``right``."""
    result: list[Span] = []
    for left_start, left_end in merge(left):
        for right_start, right_end in merge(right):
            start, end = max(left_start, right_start), min(left_end, right_end)
            if start < end:
                result.append((start, end))
    return merge(result)


def overlaps(spans: Iterable[Span], start: datetime, end: datetime) -> bool:
    """Does any span share an instant with ``[start, end)``?"""
    return any(span_start < end and start < span_end for span_start, span_end in spans)


def contains(spans: Iterable[Span], instant: datetime) -> bool:
    return any(start <= instant < end for start, end in spans)


def to_windows(spans: Iterable[Span]) -> tuple[TimeWindow, ...]:
    return tuple(TimeWindow(start=start, end=end) for start, end in merge(spans))


def from_windows(windows: Iterable[TimeWindow]) -> list[Span]:
    return [(window.start, window.end) for window in windows]


def intersect_windows(*groups: Iterable[TimeWindow]) -> tuple[TimeWindow, ...]:
    """The windows common to every group (an option's venue and its underlying's).

    No groups is no windows: nothing is vouched for.
    """
    if not groups:
        return ()
    common = from_windows(groups[0])
    for group in groups[1:]:
        common = intersect(common, from_windows(group))
    return to_windows(common)
