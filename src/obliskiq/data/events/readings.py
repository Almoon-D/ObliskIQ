"""What the event components read, network-free and point in time: a prediction market's
probability and its velocity, and a GDELT query's daily pulse as Welford z-scores.

Every reading takes what was known by its instant or date and nothing after it; the store reads
already bound rows by ``as_of``, and these functions bound them again by the instant asked.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ...config.schema_sources import EventsConfig, GdeltConfig
from ...domain.sources import EventReading, EventSnapshot, GdeltDay, PulseReading

#: Observations a sample variance needs.
MIN_VARIANCE_COUNT = 2


def event_reading(
    snapshots: Sequence[EventSnapshot], *, at: datetime, config: EventsConfig
) -> EventReading | None:
    """The newest snapshot observed by ``at``, and its probability change against the newest one
    at least ``velocity_hours`` older (``None`` velocity without one); ``None`` with no snapshot."""
    known = sorted((s for s in snapshots if s.observed_at <= at), key=lambda s: s.observed_at)
    if not known:
        return None
    newest = known[-1]
    horizon = newest.observed_at - timedelta(hours=config.velocity_hours)
    older = [s for s in known if s.observed_at <= horizon]
    reference = older[-1] if older else None
    return EventReading(
        venue=newest.venue,
        market_id=newest.market_id,
        at=at,
        observed_at=newest.observed_at,
        probability=newest.probability,
        velocity=None if reference is None else newest.probability - reference.probability,
        reference_at=None if reference is None else reference.observed_at,
    )


@dataclass(frozen=True, slots=True)
class Welford:
    """A running mean and sum of squared deviations (Welford's algorithm)."""

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, value: float) -> Welford:
        count = self.count + 1
        delta = value - self.mean
        mean = self.mean + delta / count
        return Welford(count, mean, self.m2 + delta * (value - mean))

    @classmethod
    def of(cls, values: Iterable[float]) -> Welford:
        stats = cls()
        for value in values:
            stats = stats.add(value)
        return stats

    def z(self, value: float) -> float | None:
        """``value``'s distance from the mean in sample standard deviations; ``None`` without a
        variance (fewer than two values, or all equal)."""
        if self.count < MIN_VARIANCE_COUNT:
            return None
        deviation = math.sqrt(self.m2 / (self.count - 1))
        return (value - self.mean) / deviation if deviation > 0 else None


def pulse_reading(
    days: Sequence[GdeltDay], *, query_id: str, as_of: date, config: GdeltConfig
) -> PulseReading:
    """The newest day collected by the end of ``as_of``, scored against every earlier day then
    known: the day's article share and tone as z-scores of the distribution before it, so a spike
    is not absorbed into its own baseline. Fewer than ``min_history_days`` earlier days is warming
    up, and the scores are ``None``."""
    known = sorted(
        (d for d in days if d.query_id == query_id and d.collected_at.date() <= as_of),
        key=lambda d: d.day,
    )
    if not known:
        return PulseReading(
            query_id=query_id,
            as_of=as_of,
            day=None,
            share=None,
            tone=None,
            share_z=None,
            tone_z=None,
            history=0,
            warming_up=True,
        )
    min_history_days = config.min_history_days
    newest, earlier = known[-1], known[:-1]
    warming_up = len(earlier) < min_history_days
    share_z = None if warming_up else Welford.of(d.share for d in earlier).z(newest.share)
    tones = [d.tone for d in earlier if d.tone is not None]
    tone_z = (
        None
        if warming_up or newest.tone is None or len(tones) < min_history_days
        else Welford.of(tones).z(newest.tone)
    )
    return PulseReading(
        query_id=query_id,
        as_of=as_of,
        day=newest.day,
        share=newest.share,
        tone=newest.tone,
        share_z=share_z,
        tone_z=tone_z,
        history=len(earlier),
        warming_up=warming_up,
    )
