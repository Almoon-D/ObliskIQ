"""Yahoo's daily bars of the listed names, collected forward (``stock_bars``).

Each run reads at most ``stocks.bars.max_symbols_per_run`` names of the newest listing snapshot,
those never read first, then the least recently read, one Yahoo request each, ``request_spacing_
seconds`` apart. A name's request starts at its newest session held, so the answer chains on to
it: each new session's ``change`` is its total return from the session before it in the same
answer (adjusted closes, so a split or a dividend between the two is not a move). A name read
for the first time starts ``first_read_days`` back and its first session has no change. Sessions
are dated in ``timezone``; the day the run is in is never stored, since its bar may still move.

**An empty answer is never a delisting and never a zero price.** For a name still on the SEC's
list it is ``unresolved``, recorded for that name. A run whose share of empty answers exceeds
``max_empty_share`` is ``throttled`` as a whole (Yahoo throttles silently, with empty frames,
``docs/knowledge/research-sources.md``): it is a failed run, and nothing is recorded about the
names that answered nothing. Only the SEC's listings say a name left (``left_listing``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import functools
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from ...config.schema import ObliskConfig
from ...research.data import YahooHistory, yahoo_sessions
from ...store import sources as source_repo
from ...store import stocks as stocks_repo
from ...store.worker import StoreWorker
from ..market.plumbing import Collection, Pacer, Sleep, failure_text
from .parse import chain

STEP: Final = "stock_bars"
SOURCE: Final = "stock_bars"
#: A name's target in the source's bookkeeping (``source_targets``).
TARGET_PREFIX: Final = "bars:"
UNRESOLVED: Final = "unresolved"
THROTTLED: Final = "throttled"


def read_order(symbols: Sequence[str], attempted: Mapping[str, datetime], limit: int) -> list[str]:
    """The names a run reads: never read first, then the least recently read, at most
    ``limit``."""
    never = [s for s in symbols if s not in attempted]
    read = sorted((s for s in symbols if s in attempted), key=lambda s: (attempted[s], s))
    return (never + read)[:limit]


class BarsCollector:
    """Reads the bars of the listed names whose turn it is."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        history: YahooHistory,
        worker: StoreWorker,
        pacer: Pacer,
        sleep: Sleep,
        now: Callable[[], datetime],
    ) -> None:
        self._config = settings.sources.stocks.bars
        self._zone = ZoneInfo(self._config.timezone)
        self._history = history
        self._worker = worker
        self._pacer = pacer
        self._sleep = sleep
        self._now = now

    async def run(self, *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        now = self._now()
        today = now.astimezone(self._zone).date()
        snapshot = await self._worker.run(stocks_repo.snapshot_as_of, as_of=now.date())
        if snapshot is None:
            collection.empty_reason = "no listing snapshot is held: stock_listings runs first"
            return collection
        listed = await self._worker.run(stocks_repo.listed, snapshot)
        symbols = sorted({listing.ticker for listing in listed})
        attempted = await self._worker.run(source_repo.target_attempts, SOURCE, TARGET_PREFIX)
        held = await self._worker.run(stocks_repo.last_sessions)
        chosen = read_order(symbols, attempted, self._config.max_symbols_per_run)
        until = today - timedelta(days=1)
        empty: list[str] = []
        asked = 0
        for symbol in chosen:
            last = held.get(symbol)
            if last is not None and last >= until:
                collection.succeeded(symbol, 0, reason="up to date")
                if not dry_run:
                    await self._record(symbol, now, rows=0, detail="up to date")
                continue
            start = last or today - timedelta(days=self._config.first_read_days)
            asked += 1
            try:
                frame = await self._pacer.retry(
                    symbol,
                    functools.partial(asyncio.to_thread, self._history, symbol, start, until),
                )
                answer = yahoo_sessions(frame, symbol)
            except Exception as exc:  # one name never stops the others
                collection.failed(symbol, failure_text(exc))
                if not dry_run:
                    await self._record(symbol, now, rows=0, detail=failure_text(exc), ok=False)
                await self._sleep(self._config.request_spacing_seconds)
                continue
            await self._sleep(self._config.request_spacing_seconds)
            if not answer:
                empty.append(symbol)
                continue
            bars = chain(answer, symbol=symbol, after=last, until=until, known_at=now)
            saved = len(bars) if dry_run else await self._worker.run(stocks_repo.save_bars, bars)
            collection.succeeded(symbol, saved)
            if not dry_run:
                await self._record(symbol, now, rows=saved, detail=None)
        await self._judge_empty(collection, empty, asked, now, dry_run=dry_run)
        collection.notes["asked"] = asked
        collection.notes["empty"] = len(empty)
        return collection

    async def _judge_empty(
        self,
        collection: Collection,
        empty: Sequence[str],
        asked: int,
        now: datetime,
        *,
        dry_run: bool,
    ) -> None:
        """Throttled when too many answered nothing; else each empty name is unresolved."""
        if not empty:
            return
        share = len(empty) / asked
        if share > self._config.max_empty_share:
            collection.run_failure = (
                f"{THROTTLED}: {len(empty)} of {asked} names answered nothing ({share:.0%}, above "
                f"{self._config.max_empty_share:.0%}); nothing is recorded about them"
            )
            collection.notes["status"] = THROTTLED
            return
        for symbol in empty:
            reason = f"{UNRESOLVED}: Yahoo answered nothing for a name the SEC still lists"
            collection.succeeded(symbol, 0, degraded=True, reason=reason, status=UNRESOLVED)
            if not dry_run:
                await self._record(symbol, now, rows=0, detail=reason, ok=False)

    async def _record(
        self, symbol: str, now: datetime, *, rows: int, detail: str | None, ok: bool = True
    ) -> None:
        await self._worker.run(
            source_repo.record_target,
            SOURCE,
            f"{TARGET_PREFIX}{symbol}",
            at=now,
            ok=ok,
            rows=rows,
            detail=detail,
        )
