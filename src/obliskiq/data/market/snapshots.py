"""Underlying snapshots: one row per target and session, with the availability the quote came in.

The quote is read on the underlying field set through the client's settle loop
(``ibkr/client.py``: a row is settled only when a price field arrives) and kept whole: the bid,
ask and last, the quoted implied and historical volatility and the trailing dividend yield, with
the route's own status flag verbatim beside the decoded one. Per-strike implied volatility cannot
be retrieved later, and neither can the underlying's, so a row not collected today is gone.

**Availability against the plan.** Each target's quote is judged against the owner's market-data
plan (``market_data.plan``): where a planned feed streams the venue and asset class, anything
but a real-time quote is degraded (the subscription is missing, not yet active, or not shared
with this session); elsewhere a delayed quote is what the plan buys and is recorded as such. A
target with no price at all is a failure, except an index, whose snapshot IBKR often serves
empty (its level comes from the bars), which is degraded. VERIFY-LIVE(snapshot availability by
venue against the plan).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date, datetime

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...domain.enums import AssetClass, MarketDataStatus
from ...domain.market import Snapshot
from ...ibkr.client import MarketData
from ...ibkr.models import Quote
from ...store import repos
from ...store.worker import StoreWorker
from .plumbing import MARKET_SOURCE, Collection, failure_text
from .targets import MarketTarget, venue_session

STEP = "snapshots"

#: What a quoted dividend yield is: IBKR serves a trailing one, stored as such beside the figure
#: (Black-Scholes-Merton wants a continuous yield; a later reader must tell the two apart).
DIVIDEND_YIELD_BASIS = "trailing_12m"


def snapshot_of(quote: Quote, session: date, at: datetime) -> Snapshot:
    """``quote`` as the store keeps it."""
    return Snapshot(
        snapshot_date=session,
        conid=quote.conid,
        last=quote.last,
        bid=quote.bid,
        ask=quote.ask,
        volume=quote.volume,
        implied_vol=quote.implied_vol,
        implied_vol_source=MARKET_SOURCE if quote.implied_vol is not None else None,
        historical_vol_30d=quote.historical_vol_30d,
        dividend_yield=quote.dividend_yield,
        dividend_yield_basis=DIVIDEND_YIELD_BASIS if quote.dividend_yield is not None else None,
        implied_vol_solved=None,
        implied_vol_solved_source=None,
        source=MARKET_SOURCE,
        collected_at=at,
        market_data_status=quote.market_data_status,
        market_data_raw=quote.status_flag,
        quote_delay_seconds=quote.delay_seconds,
    )


def availability(settings: ObliskConfig, target: MarketTarget, quote: Quote) -> tuple[str, bool]:
    """How the quote came, in words, and whether that falls short of the plan."""
    feed = settings.market_data.planned_feed(target.venue, target.asset_class)
    status = quote.market_data_status.value if quote.market_data_status else "unreported"
    if feed is None:
        return f"{status} (no feed of the plan covers {target.venue})", False
    if quote.market_data_status is MarketDataStatus.REALTIME:
        return f"realtime ({feed.id})", False
    return f"{status} although the plan's {feed.id} streams {target.venue} in real time", True


class SnapshotCollector:
    """Snapshots every target in one batched read and records each quote's availability."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        marketdata: MarketData,
        worker: StoreWorker,
        calendars: Calendars,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._marketdata = marketdata
        self._worker = worker
        self._calendars = calendars
        self._now = now

    async def run(self, targets: Sequence[MarketTarget], *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        if not targets:
            collection.empty_reason = "no instrument of the universe is collected on this venue"
            return collection
        try:
            quotes = await self._marketdata.snapshot([t.conid for t in targets], underlying=True)
        except Exception as exc:  # the batch failed: every target failed with it
            for target in targets:
                collection.failed(target.symbol, failure_text(exc))
            return collection
        at = self._now()
        rows: list[Snapshot] = []
        venues: Counter[str] = Counter()
        for target in targets:
            quote = quotes.get(target.conid)
            if quote is None or not quote.priced:
                if target.asset_class is AssetClass.INDEX:
                    collection.succeeded(
                        target.symbol,
                        0,
                        degraded=True,
                        reason="the index snapshot carried no level; its bars carry it",
                    )
                else:
                    collection.failed(target.symbol, "the snapshot carried no price")
                continue
            said, short = availability(self._settings, target, quote)
            venues[f"{target.venue}: {said.split(' ', 1)[0]}"] += 1
            rows.append(
                snapshot_of(
                    quote, venue_session(self._calendars, target.venue, target.zone, at), at
                )
            )
            collection.succeeded(target.symbol, 1, degraded=short, reason=said if short else "")
        if rows and not dry_run:
            await self._worker.run(repos.upsert_snapshots, rows)
        collection.notes["availability"] = dict(sorted(venues.items()))
        return collection
