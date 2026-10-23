"""Prediction markets, read-only: Polymarket's Gamma API and Kalshi's public market data.

Each run records the markets it saw and one snapshot of each market's Yes probability, stamped
with the run's instant: a reading is public when it is collected (``as_collected``). Polymarket's
curated events are read by slug, and its tag discovery records open markets above a liquidity
floor as candidates for curation (``curated`` false). Kalshi's curated series are read whole,
open markets only.

The probability is the Yes outcome's price, never a stand-in: a Gamma market without a ``Yes``
outcome, or a Kalshi market with neither a tight quote nor a trade, is left out and counted, and
a curated target that yields no market is degraded with the reason.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ...config.schema import ObliskConfig
from ...domain.sources import EventMarket, EventSnapshot, PredictionVenue
from ...errors import UpstreamError
from ...store import sources as source_repo
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy, get_with_retry
from ...util.numeric import safe_float
from ...util.timeutil import parse_instant
from ..market.plumbing import Collection, Pacer, failure_text

POLYMARKET: PredictionVenue = "polymarket"
KALSHI: PredictionVenue = "kalshi"
HTTP_OK = 200
#: The outcome whose probability is recorded.
YES = "Yes"


@dataclass(frozen=True, slots=True)
class Reading:
    """A market and its snapshot from one read."""

    market: EventMarket
    snapshot: EventSnapshot


@dataclass(frozen=True, slots=True)
class Page:
    """What one answer held: its readings, the markets left out, and Kalshi's next cursor."""

    readings: tuple[Reading, ...]
    left_out: int
    cursor: str | None = None


def _unit(value: Any) -> float | None:
    number = safe_float(value, None)
    return number if number is not None and 0 <= number <= 1 else None


def _non_negative(value: Any) -> float | None:
    number = safe_float(value, None)
    return number if number is not None and number >= 0 else None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _json_list(value: Any) -> list[Any] | None:
    """Gamma's ``outcomes`` and ``outcomePrices`` are JSON lists encoded as strings."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) else None


def yes_probability(market: Mapping[str, Any]) -> float | None:
    """The price of a Gamma market's ``Yes`` outcome; ``None`` without one."""
    outcomes = _json_list(market.get("outcomes"))
    prices = _json_list(market.get("outcomePrices"))
    if outcomes is None or prices is None or len(outcomes) != len(prices):
        return None
    for label, price in zip(outcomes, prices, strict=True):
        if str(label).strip().lower() == YES.lower():
            return _unit(price)
    return None


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and 0 <= number <= 1 else None


def kalshi_probability(market: Mapping[str, Any], *, max_mid_spread: float) -> float | None:
    """The Yes midpoint when the quote is no wider than ``max_mid_spread``, else the last trade;
    ``None`` with neither (a last price of zero is no trade)."""
    bid = _decimal(market.get("yes_bid_dollars"))
    ask = _decimal(market.get("yes_ask_dollars"))
    if bid is not None and ask is not None and bid <= ask <= bid + Decimal(str(max_mid_spread)):
        return float((bid + ask) / 2)
    last = _decimal(market.get("last_price_dollars"))
    return float(last) if last else None


def polymarket_page(
    body: Any, *, curated: bool, observed_at: datetime, min_liquidity: float | None
) -> Page:
    """The open markets of Gamma events (``/events``) with a Yes price; with ``min_liquidity``,
    only those at least that liquid."""
    if not isinstance(body, list):
        raise UpstreamError("Gamma's answer is not a list of events")
    readings: list[Reading] = []
    left_out = 0
    for event in body:
        if not isinstance(event, dict):
            raise UpstreamError("Gamma listed an event that is not an object")
        tags = tuple(
            sorted(
                {
                    str(tag["slug"])
                    for tag in event.get("tags") or []
                    if isinstance(tag, dict) and _text(tag.get("slug"))
                }
            )
        )
        for market in event.get("markets") or []:
            if not isinstance(market, dict) or market.get("closed") or not market.get("active"):
                continue
            liquidity = _non_negative(market.get("liquidityNum"))
            if min_liquidity is not None and (liquidity is None or liquidity < min_liquidity):
                continue
            market_id = _text(market.get("id"))
            question = _text(market.get("question"))
            probability = yes_probability(market)
            if market_id is None or question is None or probability is None:
                left_out += 1
                continue
            readings.append(
                Reading(
                    EventMarket(
                        venue=POLYMARKET,
                        market_id=market_id,
                        event_id=_text(event.get("id")),
                        question=question,
                        outcome=YES,
                        closes_at=parse_instant(market.get("endDate")),
                        curated=curated,
                        tags=tags,
                    ),
                    EventSnapshot(
                        venue=POLYMARKET,
                        market_id=market_id,
                        observed_at=observed_at,
                        probability=probability,
                        bid=_unit(market.get("bestBid")),
                        ask=_unit(market.get("bestAsk")),
                        last_price=_unit(market.get("lastTradePrice")),
                        volume=_non_negative(market.get("volumeNum")),
                        liquidity=liquidity,
                    ),
                )
            )
    return Page(tuple(readings), left_out)


def kalshi_page(body: Any, *, series: str, observed_at: datetime, max_mid_spread: float) -> Page:
    """One page of Kalshi's ``/markets`` for a series."""
    if not isinstance(body, dict) or not isinstance(body.get("markets"), list):
        raise UpstreamError("Kalshi's answer holds no list of markets")
    readings: list[Reading] = []
    left_out = 0
    for market in body["markets"]:
        if not isinstance(market, dict):
            raise UpstreamError("Kalshi listed a market that is not an object")
        ticker = _text(market.get("ticker"))
        question = _text(market.get("title"))
        probability = kalshi_probability(market, max_mid_spread=max_mid_spread)
        if ticker is None or question is None or probability is None:
            left_out += 1
            continue
        readings.append(
            Reading(
                EventMarket(
                    venue=KALSHI,
                    market_id=ticker,
                    event_id=_text(market.get("event_ticker")),
                    question=question,
                    outcome=YES,
                    closes_at=parse_instant(market.get("close_time")),
                    curated=True,
                    tags=(series,),
                ),
                EventSnapshot(
                    venue=KALSHI,
                    market_id=ticker,
                    observed_at=observed_at,
                    probability=probability,
                    bid=_unit(market.get("yes_bid_dollars")),
                    ask=_unit(market.get("yes_ask_dollars")),
                    last_price=_unit(market.get("last_price_dollars")),
                    volume=_non_negative(market.get("volume_fp")),
                    liquidity=None,
                ),
            )
        )
    return Page(tuple(readings), left_out, _text(body.get("cursor")))


class PredictionMarkets:
    """Collects Polymarket and Kalshi snapshots into the store."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        pacer: Pacer,
        now: Callable[[], datetime],
    ) -> None:
        self._config = settings.sources.events
        self._client = client
        self._policy = policy
        self._worker = worker
        self._pacer = pacer
        self._now = now

    async def _json(self, url: str, params: Mapping[str, Any]) -> Any:
        response = await get_with_retry(self._client, url, policy=self._policy, params=params)
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"{url} answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"{url} answered with something that is not JSON") from exc

    async def _save(self, readings: Sequence[Reading], seen_at: datetime, dry_run: bool) -> None:
        if dry_run or not readings:
            return
        await self._worker.run(
            source_repo.save_event_markets, [r.market for r in readings], seen_at=seen_at
        )
        await self._worker.run(source_repo.save_event_snapshots, [r.snapshot for r in readings])

    async def polymarket(self, *, dry_run: bool = False) -> Collection:
        """The curated events by slug, then the open events under each discovery tag."""
        config = self._config.polymarket
        collection = Collection("polymarket")
        observed = self._now()
        url = f"{config.gamma_url}/events"
        for slug in config.curated_events:
            target = f"event:{slug}"
            try:
                body = await self._pacer.retry(
                    target, functools.partial(self._json, url, {"slug": slug})
                )
                page = polymarket_page(body, curated=True, observed_at=observed, min_liquidity=None)
            except Exception as exc:  # one event never stops the others
                collection.failed(target, failure_text(exc))
                continue
            await self._save(page.readings, observed, dry_run)
            self._record(
                collection,
                target,
                page,
                empty="no open market with a Yes price: the event ended or its slug changed",
            )
            await self._pacer.pace()
        for tag in config.discovery_tags:
            target = f"tag:{tag}"
            params = {"tag_slug": tag, "closed": "false", "limit": config.discovery_limit}
            try:
                body = await self._pacer.retry(target, functools.partial(self._json, url, params))
                page = polymarket_page(
                    body,
                    curated=False,
                    observed_at=observed,
                    min_liquidity=config.min_discovery_liquidity,
                )
            except Exception as exc:  # one tag never stops the others
                collection.failed(target, failure_text(exc))
                continue
            await self._save(page.readings, observed, dry_run)
            self._record(collection, target, page, empty=None)
            await self._pacer.pace()
        return collection

    async def kalshi(self, *, dry_run: bool = False) -> Collection:
        """Each curated series' open markets, up to ``max_markets_per_series``."""
        collection = Collection("kalshi")
        observed = self._now()
        for series in self._config.kalshi.curated_series:
            target = f"series:{series}"
            try:
                page = await self._pacer.retry(
                    target, functools.partial(self._kalshi_series, series, observed)
                )
            except Exception as exc:  # one series never stops the others
                collection.failed(target, failure_text(exc))
                continue
            await self._save(page.readings, observed, dry_run)
            self._record(collection, target, page, empty="no open market with a price")
            await self._pacer.pace()
        return collection

    async def _kalshi_series(self, series: str, observed: datetime) -> Page:
        config = self._config.kalshi
        readings: list[Reading] = []
        left_out = 0
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "series_ticker": series,
                "status": "open",
                "limit": config.max_markets_per_series - len(readings) - left_out,
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = kalshi_page(
                await self._json(f"{config.api_url}/markets", params),
                series=series,
                observed_at=observed,
                max_mid_spread=config.max_mid_spread,
            )
            readings += page.readings
            left_out += page.left_out
            cursor = page.cursor
            if cursor is None or len(readings) + left_out >= config.max_markets_per_series:
                return Page(tuple(readings), left_out)

    @staticmethod
    def _record(collection: Collection, target: str, page: Page, *, empty: str | None) -> None:
        """A curated target that yields nothing is degraded with ``empty``; a market left out
        is counted, not a failure (a strike nobody quotes is the venue's state, not ours)."""
        hollow = not page.readings and empty is not None
        collection.succeeded(
            target,
            len(page.readings),
            degraded=hollow,
            reason=(empty or "") if hollow else "",
            left_out=page.left_out,
        )
