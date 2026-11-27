"""The SEC's listings, snapshot by snapshot, and each listed company's SIC (``stock_listings``).

A run reads ``company_tickers_exchange.json`` (``{"fields": [...], "data": [[cik, name, ticker,
exchange], ...]}``), keeps the common stocks on the exchanges ``stocks.listings.exchanges`` names
(a ticker of ``ticker_pattern`` and not of ``excluded_pattern``: the SEC states no security
type), and stores the snapshot whole. A name in the previous snapshot and absent from this one is
recorded as ``left_listing`` with both dates; no row is ever deleted. A snapshot that keeps fewer
than ``min_kept_share`` of the previous one's names is refused and records nothing: a truncated
download is not thousands of departures.

Then the companies of the newest snapshot whose submissions were never read get their SIC and
their state or country of incorporation, at most ``sic_per_run`` a run
(``data.sec.gov/submissions/CIK##########.json``, fields ``sic`` and ``stateOfIncorporation``, an
EDGAR code; an empty one is stored as none). The incorporation gives a listed stock's issuer
country where it is a home state (``home_incorporations``), for its transaction tax (D195).
Every request carries ``SEC_USER_AGENT`` and keeps to the SEC's fair-access pace
(``insiders.request_spacing_seconds``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Final

import httpx

from ...config.schema import ObliskConfig
from ...errors import UpstreamError
from ...store import stocks as stocks_repo
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy
from ..market.plumbing import Collection, Sleep, failure_text
from ..sec import sec_get
from .parse import parse_incorporation, parse_listings, parse_sic

STEP: Final = "stock_listings"
HTTP_OK: Final = 200
SNAPSHOT: Final = "snapshot"
#: How many failed companies a run names.
SHOWN: Final = 3
BACKLOG: Final = "more companies wait for their SIC"


class ListingsCollector:
    """Keeps a snapshot of the SEC's listings and reads the SIC of companies that lack one."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        user_agent: str | None,
        sleep: Sleep,
        now: Callable[[], datetime],
    ) -> None:
        self._config = settings.sources.stocks.listings
        self._spacing = settings.sources.insiders.request_spacing_seconds
        self._client = client
        self._policy = policy
        self._worker = worker
        self._user_agent = user_agent
        self._sleep = sleep
        self._now = now

    async def _get(self, url: str) -> httpx.Response:
        return await sec_get(
            self._client,
            url,
            policy=self._policy,
            user_agent=self._user_agent,
            spacing=self._spacing,
            sleep=self._sleep,
        )

    async def run(self, *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        now = self._now()
        try:
            await self._snapshot(collection, now, dry_run=dry_run)
        except Exception as exc:  # no snapshot: the SIC backlog still moves on
            collection.failed(SNAPSHOT, failure_text(exc))
        if not dry_run:
            await self._sics(collection, now)
        return collection

    async def _snapshot(self, collection: Collection, now: datetime, *, dry_run: bool) -> None:
        response = await self._get(self._config.url)
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"the SEC's ticker file answered HTTP {response.status_code}")
        listings = parse_listings(response.json(), self._config)
        if not listings:
            raise UpstreamError("the SEC's ticker file lists no common stock on a kept exchange")
        previous = await self._worker.run(stocks_repo.latest_snapshot_held)
        if previous is not None and Decimal(len(listings)) < (
            self._config.min_kept_share * previous.listed
        ):
            raise UpstreamError(
                f"the file lists {len(listings)} names against {previous.listed} in the last "
                f"snapshot, below {self._config.min_kept_share:.0%} of it: refused, nothing is "
                "recorded as leaving"
            )
        if dry_run:
            collection.succeeded(SNAPSHOT, len(listings))
            return
        digest = hashlib.sha256(response.content).hexdigest()
        _, left = await self._worker.run(
            stocks_repo.save_snapshot, listings, known_at=now, content_sha256=digest
        )
        collection.succeeded(SNAPSHOT, len(listings), left=len(left))
        collection.notes["listed"] = len(listings)
        collection.notes["left_listing"] = len(left)

    async def _sics(self, collection: Collection, now: datetime) -> None:
        ciks = await self._worker.run(stocks_repo.ciks_without_sic, limit=self._config.sic_per_run)
        read, failed = 0, []
        for cik in ciks:
            try:
                response = await self._get(self._config.submissions_url_template.format(cik=cik))
                if response.status_code != HTTP_OK:
                    raise UpstreamError(f"answered HTTP {response.status_code}")
                body = response.json()
                sic, incorporated = parse_sic(body), parse_incorporation(body)
            except Exception as exc:  # one company never stops the others
                failed.append(f"CIK {cik}: {failure_text(exc)}")
                continue
            await self._worker.run(
                stocks_repo.save_company, cik, sic, known_at=now, incorporated=incorporated
            )
            read += 1
        if not ciks:
            return
        waiting = await self._worker.run(stocks_repo.ciks_without_sic, limit=1)
        reasons = [*failed[:SHOWN], *([BACKLOG] if waiting else [])]
        collection.succeeded("sic", read, degraded=bool(reasons), reason="; ".join(reasons))
