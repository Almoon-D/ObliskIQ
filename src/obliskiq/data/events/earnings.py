"""Earnings dates of the universe's stocks, research-grade (Yahoo Finance through yfinance).

Unofficial: an upcoming date may be Yahoo's estimate and may move, so every date is stored as
``research`` grade and never decides anything alone. Each read is stored whole under the day it
was made, so a read as of a date uses only what was listed then. A symbol is read again once
its newest read is ``refresh_days`` old; a read keeps the ``past_reports`` most recent reports and
the upcoming ones within ``horizon_days``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import functools
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

from ...config.schema import ObliskConfig
from ...domain.enums import AssetClass
from ...domain.sources import EarningsDate
from ...errors import UpstreamError
from ...store import sources as source_repo
from ...store.worker import StoreWorker
from ...util.yahoo import yahoo_symbol
from ..market.plumbing import Collection, Pacer, failure_text

STEP = "earnings"
GRADE: Final = "research"

#: Yahoo's symbol and the rows to ask for -> the report instants listed (timezone-aware).
type EarningsSource = Callable[[str, int], Sequence[datetime]]


def yfinance_earnings(symbol: str, limit: int) -> list[datetime]:  # pragma: no cover
    """The report instants yfinance lists (network; run in a worker thread)."""
    import yfinance  # imported where the network is touched

    frame = yfinance.Ticker(symbol).get_earnings_dates(limit=limit)
    if frame is None:
        return []
    return [stamp.to_pydatetime() for stamp in frame.index]


def select_dates(
    instants: Iterable[datetime], *, now: datetime, past_reports: int, horizon_days: int
) -> list[datetime]:
    """The ``past_reports`` most recent reports up to ``now`` and the upcoming ones within
    ``horizon_days``, in UTC and in order. An instant without a zone is refused."""
    stamps = sorted(set(instants))
    if any(stamp.tzinfo is None for stamp in stamps):
        raise UpstreamError("an earnings date came without its time zone")
    horizon = now + timedelta(days=horizon_days)
    past = [stamp for stamp in stamps if stamp <= now]
    upcoming = [stamp for stamp in stamps if now < stamp <= horizon]
    kept = (past[-past_reports:] if past_reports else []) + upcoming
    return [stamp.astimezone(UTC) for stamp in kept]


class EarningsCollector:
    """Reads the earnings dates of the universe's stocks whose last read is stale."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        worker: StoreWorker,
        pacer: Pacer,
        fetch: EarningsSource,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._config = settings.sources.catalysts.earnings
        self._worker = worker
        self._pacer = pacer
        self._fetch = fetch
        self._now = now

    async def run(self, *, dry_run: bool = False, force: bool = False) -> Collection:
        collection = Collection(STEP)
        now = self._now()
        today = now.astimezone(UTC).date()
        yahoo = self._settings.research.data.yahoo
        fresh = 0
        for instrument in self._settings.universe.instruments:
            if instrument.asset_class is not AssetClass.STOCK:
                continue
            symbol = instrument.symbol
            if not force and await self._fresh(symbol, today):
                fresh += 1
                continue
            ticker = yahoo_symbol(symbol, instrument.listing_exchange, yahoo.suffixes, yahoo.digits)
            if ticker is None:
                collection.succeeded(
                    symbol,
                    0,
                    degraded=True,
                    reason=f"no Yahoo suffix for {instrument.listing_exchange}",
                )
                continue
            try:
                instants = await self._pacer.retry(
                    symbol,
                    functools.partial(
                        asyncio.to_thread, self._fetch, ticker, self._config.request_limit
                    ),
                )
                kept = select_dates(
                    instants,
                    now=now,
                    past_reports=self._config.past_reports,
                    horizon_days=self._config.horizon_days,
                )
            except Exception as exc:  # one symbol never stops the others
                collection.failed(symbol, failure_text(exc))
                continue
            dates = [
                EarningsDate(
                    symbol=symbol,
                    collected_on=today,
                    report_at=stamp,
                    source=self._config.source,
                    grade=GRADE,
                )
                for stamp in kept
            ]
            if not dry_run:
                await self._worker.run(source_repo.save_earnings_dates, dates)
            upcoming = [d.report_at for d in dates if d.report_at > now]
            collection.succeeded(
                symbol,
                len(dates),
                degraded=not dates,
                reason="" if dates else "Yahoo listed no earnings date",
                next=str(upcoming[0].date()) if upcoming else None,
            )
            await self._pacer.pace()
        collection.notes["fresh"] = fresh
        if not collection.outcomes:
            collection.empty_reason = (
                f"every stock was read within {self._config.refresh_days} days"
                if fresh
                else "the universe holds no stock"
            )
        return collection

    async def _fresh(self, symbol: str, today: date) -> bool:
        last = await self._worker.run(source_repo.earnings_collected_on, symbol)
        return last is not None and (today - last).days < self._config.refresh_days
