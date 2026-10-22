"""Daily bars: the window backfilled once, then extended at both ends and never re-fetched.

Ported from lite's ``collect/backfill.py`` on Session 3's history reader (``ibkr/history.py``,
which probes the per-call capacity, walks backwards judging progress by the data and steps a
refused span down). What this adds is the store's watermark: the newest bar held is re-asked
with ``collectors.backfill.overlap_days`` of overlap, so a partial final bar is corrected
without re-fetching the middle, and history older than the oldest bar held is asked for down to
``window_years`` before the session (or the inception the universe declares, whichever is
later). Gaps are tolerated by construction: the store holds what was served, and a missed day
costs one day.

A bar is dated by its listing venue's session (``Bar.session_date``): IBKR stamps a daily bar
with a UTC instant, which is read in the venue's own time zone, never as a UTC date (a Tokyo bar
stamped at its local midnight is the previous UTC day). VERIFY-LIVE(daily bar t stamp per venue).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...domain.market import Bar
from ...ibkr.errors import NoDataPermanentError
from ...ibkr.history import HistoryReader
from ...ibkr.models import HistoryBar
from ...store import repos
from ...store.worker import StoreWorker
from .plumbing import MARKET_SOURCE, Collection, Pacer, failure_text
from .targets import MarketTarget, venue_session

STEP = "backfill"


def years_before(day: date, years: int) -> date:
    """The same calendar day ``years`` earlier (29 February falls back to the 28th)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=day.day - 1)


def session_bars(conid: int, served: Iterable[HistoryBar], zone: ZoneInfo) -> list[Bar]:
    """The bars IBKR served, dated in the venue's time zone; a bar without a close is not a bar,
    and a session served twice keeps its last bar."""
    by_session: dict[date, Bar] = {}
    for bar in served:
        if bar.close is None or bar.close < 0:
            continue
        session = bar.start.astimezone(zone).date()
        by_session[session] = Bar(
            conid=conid,
            session_date=session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            source=MARKET_SOURCE,
        )
    return [by_session[session] for session in sorted(by_session)]


class Backfill:
    """Keeps every bar target's daily history over the configured window."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        history: HistoryReader,
        worker: StoreWorker,
        pacer: Pacer,
        calendars: Calendars,
        now: Callable[[], datetime],
    ) -> None:
        self._config = settings.collectors
        self._history = history
        self._worker = worker
        self._pacer = pacer
        self._calendars = calendars
        self._now = now

    async def run(self, targets: Iterable[MarketTarget], *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        for target in targets:
            if not target.collect_bars:
                continue
            try:
                await self._pacer.retry(
                    target.symbol, functools.partial(self._one, target, collection, dry_run=dry_run)
                )
            except Exception as exc:  # one target never stops the step
                collection.failed(target.symbol, failure_text(exc))
            await self._pacer.pace()
        if not collection.outcomes:
            collection.empty_reason = "no instrument of the universe collects daily bars"
        return collection

    async def _one(self, target: MarketTarget, collection: Collection, *, dry_run: bool) -> None:
        latest = venue_session(self._calendars, target.venue, target.zone, self._now())
        floor = years_before(latest, self._config.backfill.window_years)
        if target.inception is not None:
            floor = max(floor, target.inception)
        newest = await self._worker.run(repos.latest_bar_date, target.conid, as_of=latest)
        oldest = await self._worker.run(repos.earliest_bar_date, target.conid, as_of=latest)
        zone = target.zone
        spans: list[tuple[date, date]] = []
        if newest is None or oldest is None:
            spans.append((floor, latest))
        else:
            spans.append((newest - timedelta(days=self._config.backfill.overlap_days), latest))
            if oldest > floor:
                spans.append((floor, oldest - timedelta(days=1)))
        bars: list[Bar] = []
        for index, (earliest, last) in enumerate(spans):
            try:
                served = await self._history.daily_between(
                    target.conid, earliest=earliest, latest=last, exchange=target.history_exchange
                )
            except NoDataPermanentError:
                if index == 0:
                    raise
                # Older history refused: the instrument's history starts after the window's.
                continue
            finally:
                await self._pacer.pace()
            bars += [
                b for b in session_bars(target.conid, served.bars, zone) if b.session_date <= latest
            ]
        if not bars and newest is None:
            collection.failed(
                target.symbol,
                f"IBKR served no daily bar from {floor} to {latest}"
                + (f" with exchange {target.history_exchange}" if target.history_exchange else ""),
            )
            return
        if not dry_run:
            await self._worker.run(repos.upsert_bars, bars)
        held = max([newest, *(bar.session_date for bar in bars)], key=lambda d: d or date.min)
        stale = held is None or (latest - held).days > self._config.chain.bars_freshness_days
        collection.succeeded(
            target.symbol,
            len(bars),
            degraded=stale,
            reason=f"the newest bar is of {held}, the session {latest}" if stale else "",
            newest=str(held),
        )
