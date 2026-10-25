"""Futures curves: one session of term structure per tracked root. Data only; a future is never
a tradable candidate (``instruments.prohibited``).

Ported from lite's ``collect/curve_collector.py``. The ladder comes from ``/trsrv/futures`` (on
the native exchange; SMART answers empty), the nearest ``max_expiries_per_root`` expiries not yet
past are snapshotted, and each point's price is the mid of a two-sided book, else the last:
never a fabricated one. The roll yield of a deferred point against the front is annualised on
the calendar basis; the front's is NULL, not 0.0 (there is nothing to roll it against), and an
unpriced point has none, because a fictional flat point drags every contango reading toward
"fine". A ladder on which not one rung carried a price is degraded: the expiries are retrievable
any day, the prices are the part that has to be taken today. Contango and backwardation are
classified downstream (the commodity engine and its ContangoCheck), not here.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
from collections.abc import Callable, Iterable
from datetime import date, datetime

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...domain.market import CurvePoint
from ...ibkr.client import MarketData, Secdef
from ...ibkr.models import FuturesRung, Quote
from ...store import repos
from ...store.worker import StoreWorker
from .plumbing import MARKET_SOURCE, Collection, Pacer, PermanentCollectionError, failure_text
from .targets import RootTarget, venue_session

STEP = "curves"


def annualised_roll(
    *, front_price: float, deferred_price: float, days_apart: int, calendar_days_per_year: float
) -> float | None:
    """The annualised roll yield of a deferred contract against the front; ``None`` when it
    cannot be computed (never 0.0: a missing point is not a flat one)."""
    if front_price <= 0 or deferred_price <= 0 or days_apart <= 0:
        return None
    return float((front_price / deferred_price) ** (calendar_days_per_year / days_apart) - 1)


def reference_price(quote: Quote | None) -> float | None:
    if quote is None:
        return None
    for price in (quote.mid, quote.last):
        if price is not None and price > 0:
            return price
    return None


class CurveCollector:
    """Writes one session of each tracked root's curve."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        secdef: Secdef,
        marketdata: MarketData,
        worker: StoreWorker,
        pacer: Pacer,
        calendars: Calendars,
        now: Callable[[], datetime],
    ) -> None:
        self._curve = settings.collectors.curve
        self._basis = settings.numerics.bsm.day_count_basis
        self._secdef = secdef
        self._marketdata = marketdata
        self._worker = worker
        self._pacer = pacer
        self._calendars = calendars
        self._now = now

    async def run(self, roots: Iterable[RootTarget], *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        for target in roots:
            try:
                await self._pacer.retry(
                    target.root.root,
                    functools.partial(self._one, target, collection, dry_run=dry_run),
                )
            except Exception as exc:  # one root never stops the step
                collection.failed(target.root.root, failure_text(exc))
            await self._pacer.pace()
        if not collection.outcomes:
            collection.empty_reason = "no tracked futures root is on this venue"
        return collection

    async def _one(self, target: RootTarget, collection: Collection, *, dry_run: bool) -> None:
        root = target.root
        session = venue_session(self._calendars, target.venue, target.zone, self._now())
        ladders = await self._secdef.futures([root.root])
        await self._pacer.pace()
        ladder = [r for r in ladders.get(root.root, ()) if r.expiry >= session]
        ladder = ladder[: self._curve.max_expiries_per_root]
        if not ladder:
            raise PermanentCollectionError(
                f"{root.root} on {root.exchange} lists no future expiring on or after {session}"
            )
        quotes = await self._marketdata.snapshot([rung.conid for rung in ladder])
        at = self._now()
        points = self._points(target, ladder, quotes, session, at)
        if not dry_run:
            await self._worker.run(repos.upsert_curve_points, points)
        priced = [p for p in points if p.price is not None]
        short = len(ladder) < self._curve.min_expiries_for_roll
        reason = ""
        if not priced:
            reason = (
                "the ladder listed but no rung carried a price: no curve shape and no roll "
                "(a snapshot answering nothing points at a market-data entitlement)"
            )
        elif short:
            reason = f"{len(ladder)} expiry listed: too few for a curve shape"
        collection.succeeded(
            root.root,
            len(points),
            degraded=bool(reason),
            reason=reason,
            priced=len(priced),
            front_roll=points[1].annualised_roll if len(points) > 1 else None,
        )

    def _points(
        self,
        target: RootTarget,
        ladder: list[FuturesRung],
        quotes: dict[int, Quote],
        session: date,
        at: datetime,
    ) -> list[CurvePoint]:
        front = ladder[0]
        front_price = reference_price(quotes.get(front.conid))
        points: list[CurvePoint] = []
        for index, rung in enumerate(ladder):
            quote = quotes.get(rung.conid)
            price = reference_price(quote)
            roll = None
            if index > 0 and front_price is not None and price is not None:
                roll = annualised_roll(
                    front_price=front_price,
                    deferred_price=price,
                    days_apart=(rung.expiry - front.expiry).days,
                    calendar_days_per_year=self._basis,
                )
            points.append(
                CurvePoint(
                    snapshot_date=session,
                    root=target.root.root,
                    expiry=rung.expiry,
                    price=price,
                    annualised_roll=roll,
                    conid=rung.conid,
                    exchange=target.root.exchange,
                    currency=target.root.currency,
                    source=MARKET_SOURCE,
                    collected_at=at,
                    market_data_status=quote.market_data_status if quote is not None else None,
                    market_data_raw=quote.status_flag if quote is not None else None,
                    quote_delay_seconds=quote.delay_seconds if quote is not None else None,
                )
            )
        return points
