"""GDELT DOC 2.0: each region and theme query's daily article volume and tone.

A run asks each query's hourly timelines over the configured span: ``timelinevolraw`` (matching
articles, and every article GDELT monitored in the hour) and ``timelinetone`` (the matching
articles' mean tone). Hours are summed into UTC days, and only whole days are stored: those
after the day of the span's first point (which starts mid-day) and before today. An hour GDELT
leaves out (every query misses the same hours: a gap in its data, 2026-10-23) counts in neither
the matching nor the monitored total, so a day's share of coverage is unbiased by it. A day
already held keeps its first reading (GDELT revises counts as it indexes).

GDELT allows one request every few seconds per address, and a cloud egress shared with others
meets that limit sooner: requests are spaced, a 429 waits the configured backoff and asks again,
and after ``give_up_after_failed_queries`` failed queries in a row the rest are not asked and are
reported as such.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from ...config.schema import ObliskConfig
from ...domain.sources import GdeltDay
from ...errors import UpstreamError
from ...store import sources as source_repo
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy, request_with_retry
from ...util.numeric import safe_float
from ..market.plumbing import Collection, Sleep, failure_text

STEP = "gdelt"
HTTP_OK = 200
HTTP_TOO_MANY = 429
VOLUME_MODE = "timelinevolraw"
TONE_MODE = "timelinetone"
HOUR_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class HourPoint:
    hour: datetime
    value: float
    norm: float | None


def hourly(body: Any) -> list[HourPoint]:
    """The points of a timeline answer's first series. An answer with no timeline (GDELT's
    ``{}`` when nothing matched) is no points."""
    if not isinstance(body, dict):
        raise UpstreamError("GDELT's answer is not an object")
    timeline = body.get("timeline")
    if timeline is None or timeline == []:
        return []
    if not isinstance(timeline, list) or not isinstance(timeline[0], dict):
        raise UpstreamError("GDELT's timeline is not a list of series")
    data = timeline[0].get("data")
    if not isinstance(data, list):
        raise UpstreamError("GDELT's series holds no list of points")
    points: list[HourPoint] = []
    for point in data:
        if not isinstance(point, dict):
            raise UpstreamError("GDELT listed a point that is not an object")
        try:
            hour = datetime.strptime(str(point.get("date")), HOUR_FORMAT).replace(tzinfo=UTC)
        except ValueError as exc:
            raise UpstreamError(f"GDELT's point date {point.get('date')!r} is unreadable") from exc
        value = safe_float(point.get("value"), None)
        if value is None:
            raise UpstreamError(f"GDELT's point at {hour:%Y-%m-%d %H:%M} has no value")
        points.append(HourPoint(hour, value, safe_float(point.get("norm"), None)))
    return points


def complete_days(
    volume: list[HourPoint],
    tone: list[HourPoint],
    *,
    query_id: str,
    today: date,
    collected_at: datetime,
) -> list[GdeltDay]:
    """The whole UTC days of a span: after the first point's day and before ``today``. Articles
    and monitored are summed over the day's hours; tone is the article-weighted mean of its
    hourly tones (``None`` where no hour has both)."""
    if not volume:
        return []
    first = min(point.hour for point in volume).date()
    by_day: defaultdict[date, dict[datetime, HourPoint]] = defaultdict(dict)
    for point in volume:
        by_day[point.hour.date()][point.hour] = point
    tones = {point.hour: point.value for point in tone}
    days: list[GdeltDay] = []
    for day, hours in sorted(by_day.items()):
        if day <= first or day >= today:
            continue
        articles = sum(point.value for point in hours.values())
        monitored = sum(point.norm or 0 for point in hours.values())
        if monitored <= 0:
            continue
        weighted = [(tones[h], p.value) for h, p in hours.items() if h in tones and p.value > 0]
        weight = sum(w for _, w in weighted)
        days.append(
            GdeltDay(
                query_id=query_id,
                day=day,
                articles=round(articles),
                monitored=round(monitored),
                tone=sum(t * w for t, w in weighted) / weight if weight > 0 else None,
                collected_at=collected_at,
            )
        )
    return days


class RateLimitedError(UpstreamError):
    """GDELT still answered 429 after the configured backoffs."""


class GdeltCollector:
    """Collects each configured query's complete days."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        now: Callable[[], datetime],
        sleep: Sleep,
    ) -> None:
        self._config = settings.sources.events.gdelt
        self._client = client
        self._policy = policy
        self._worker = worker
        self._now = now
        self._sleep = sleep

    async def _timeline(self, query: str, mode: str) -> list[HourPoint]:
        """One timeline, spaced from the request before it; a 429 waits and asks again."""
        params: Mapping[str, Any] = {
            "query": query,
            "mode": mode,
            "format": "json",
            "timespan": self._config.timespan,
        }
        for attempt in range(self._config.rate_limited_retries + 1):
            if attempt:
                await self._sleep(self._config.rate_limited_backoff_seconds)
            await self._sleep(self._config.request_spacing_seconds)
            response = await request_with_retry(
                self._client,
                "GET",
                self._config.doc_url,
                policy=self._policy,
                params=params,
                retry_status_codes=sorted(self._policy.retry_status_codes - {HTTP_TOO_MANY}),
            )
            if response.status_code == HTTP_TOO_MANY:
                continue
            if response.status_code != HTTP_OK:
                raise UpstreamError(f"GDELT answered HTTP {response.status_code} to {mode}")
            try:
                body = response.json()
            except ValueError as exc:
                raise UpstreamError(f"GDELT's {mode} answer is not JSON") from exc
            return hourly(body)
        raise RateLimitedError(f"GDELT kept answering 429 to {mode}")

    async def run(self, *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        now = self._now()
        today = now.astimezone(UTC).date()
        failed_in_a_row = 0
        for query_id, spec in self._config.queries.items():
            if failed_in_a_row >= self._config.give_up_after_failed_queries:
                collection.failed(query_id, "not asked: the queries before it failed in a row")
                continue
            try:
                volume = await self._timeline(spec.query, VOLUME_MODE)
                tone = await self._timeline(spec.query, TONE_MODE)
                days = complete_days(volume, tone, query_id=query_id, today=today, collected_at=now)
            except Exception as exc:  # one query never stops the next
                collection.failed(query_id, failure_text(exc))
                failed_in_a_row += 1
                continue
            failed_in_a_row = 0
            new = 0 if dry_run else await self._worker.run(source_repo.save_gdelt_days, days)
            collection.succeeded(
                query_id,
                new,
                degraded=not days,
                reason="" if days else "no complete day in the span",
                complete_days=len(days),
                newest=str(days[-1].day) if days else None,
                theme=spec.theme,
            )
        return collection
