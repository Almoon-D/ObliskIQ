"""The SEC's filings collected forward for the fact sheet's events (``sec_filings``; MASTER_PLAN
§5.5; DECISIONS D189, D200).

Each run reads EDGAR's daily form index of every day since the last one read (at most
``filings.lookback_days`` back, through insiders' template, archive, time zone and pace) and keeps:

* the forms whose header states item numbers (``item_forms``: the 8-K) of the companies of the
  newest listing snapshot, each with its items and acceptance read from its header;
* the registration forms and pricing prospectuses of every filer, from the index alone: an issuer
  is listed only after its offering, and the sheet shows a listed name's filings only.

Then it reads the pricing prospectuses of listed filers not read yet (at most
``prospectus_reads_per_run``, filed within ``prospectus_window_days``): the filing's largest HTML
document, as an initial public offering's terms when its cover says it is one. A filing is stored
once, by its accession, stamped with the instant it was collected (``known_at``).

A day whose index is absent (a weekend, a holiday, today before EDGAR posts it) is not a failure:
a past day is recorded as read with nothing, today's is asked again. A header or a prospectus that
cannot be fetched or read fails alone and degrades its day; a prospectus that failed is asked again
on the next run.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx

from ...config.schema import ObliskConfig
from ...domain.filings import Filing, IpoTerms
from ...errors import UpstreamError
from ...store import filings as filings_repo
from ...store import sources as source_repo
from ...store import stocks as stocks_repo
from ...store.db import Store
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy
from ..insiders.edgar import INDEX_PREFIX, WEEKEND, IndexLine, absent, index_url, parse_index
from ..market.plumbing import Collection, Pacer, failure_text
from ..sec import sec_get
from .filing_parse import folder, html_text, parse_header, parse_prospectus, primary_document

STEP: Final = "sec_filings"
SOURCE: Final = "sec_filings"
HTTP_OK: Final = 200
PROSPECTUSES: Final = "prospectuses"
#: How many failures a run names.
SHOWN: Final = 3


class FilingsCollector:
    """Reads the daily indexes and stores the kept filings, then reads new prospectuses."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        pacer: Pacer,
        user_agent: str | None,
        now: Callable[[], datetime],
        sleep: Callable[[float], Any],
    ) -> None:
        self._config = settings.sources.filings
        self._index = settings.sources.insiders
        self._zone = ZoneInfo(self._index.timezone)
        self._client = client
        self._policy = policy
        self._worker = worker
        self._pacer = pacer
        self._user_agent = user_agent
        self._now = now
        self._sleep = sleep

    async def _get(self, url: str) -> httpx.Response:
        return await sec_get(
            self._client,
            url,
            policy=self._policy,
            user_agent=self._user_agent,
            spacing=self._index.request_spacing_seconds,
            sleep=self._sleep,
        )

    async def run(self, *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        listed = await self._worker.run(_listed_ciks)
        today = self._now().astimezone(self._zone).date()
        for day in await self._days(today):
            try:
                await self._pacer.retry(
                    day.isoformat(),
                    functools.partial(self._day, day, today, listed, collection, dry_run),
                )
            except Exception as exc:  # one day never stops the next
                collection.failed(f"{INDEX_PREFIX}{day}", failure_text(exc))
        if not dry_run:
            await self._prospectuses(today, listed, collection)
        collection.notes["listed"] = len(listed)
        return collection

    async def _days(self, today: date) -> list[date]:
        done = await self._worker.run(source_repo.succeeded_targets, SOURCE, INDEX_PREFIX)
        floor = today - timedelta(days=self._config.lookback_days)
        newest = max((date.fromisoformat(t.removeprefix(INDEX_PREFIX)) for t in done), default=None)
        start = floor if newest is None else max(floor, newest + timedelta(days=1))
        days = [start + timedelta(days=n) for n in range((today - start).days + 1)]
        return [day for day in days if day.weekday() not in WEEKEND]

    def _kept(self, line: IndexLine, listed: frozenset[int]) -> bool:
        config = self._config
        if line.form in config.item_forms:
            return line.cik in listed
        return line.form in config.registration_forms or line.form in config.prospectus_forms

    async def _day(
        self,
        day: date,
        today: date,
        listed: frozenset[int],
        collection: Collection,
        dry_run: bool,
    ) -> None:
        target = f"{INDEX_PREFIX}{day}"
        response = await self._get(index_url(self._index.daily_index_url_template, day))
        if absent(response):
            if day < today and not dry_run:
                await self._record(target, rows=0, detail="no index: EDGAR was closed")
            return
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"the index of {day} answered HTTP {response.status_code}")
        lines = {
            line.accession: line
            for line in parse_index(response.text)
            if line.accession is not None and self._kept(line, listed)
        }
        held = await self._worker.run(filings_repo.held_accessions, list(lines))
        found, failed = [], []
        for accession, line in lines.items():
            if accession in held:
                continue
            try:
                found.append(await self._filing(accession, line, day))
            except Exception as exc:  # one filing never stops its day
                failed.append(f"{accession}: {failure_text(exc)}")
        saved = len(found)
        if not dry_run:
            saved = await self._worker.run(filings_repo.save_filings, found)
            await self._record(target, rows=saved, detail="; ".join(failed[:SHOWN]) or None)
        collection.succeeded(
            target,
            saved,
            degraded=bool(failed),
            reason="; ".join(failed[:SHOWN]),
            listed=len(lines),
        )

    async def _filing(self, accession: str, line: IndexLine, day: date) -> Filing:
        items: tuple[str, ...] | None = None
        accepted = None
        if line.form in self._config.item_forms:
            url = self._config.header_url_template.format(
                cik=line.cik, folder=folder(accession), accession=accession
            )
            response = await self._get(url)
            if response.status_code != HTTP_OK:
                raise UpstreamError(f"its header answered HTTP {response.status_code}")
            items, accepted = parse_header(response.text, zone=self._zone)
        return Filing(
            accession=accession,
            cik=line.cik,
            company=line.company or f"CIK {line.cik}",
            form=line.form,
            filed=day,
            path=line.path,
            items=items,
            accepted_at=accepted,
            known_at=self._now(),
        )

    async def _prospectuses(
        self, today: date, listed: frozenset[int], collection: Collection
    ) -> None:
        config = self._config
        unread = await self._worker.run(
            filings_repo.unread_prospectuses,
            forms=config.prospectus_forms,
            ciks=listed,
            since=today - timedelta(days=config.prospectus_window_days),
            limit=config.prospectus_reads_per_run,
        )
        if not unread:
            return
        read, failed = 0, []
        for filing in unread:
            try:
                terms = await self._prospectus(filing)
            except Exception as exc:  # one prospectus never stops the others; asked again
                failed.append(f"{filing.accession}: {failure_text(exc)}")
                continue
            await self._worker.run(filings_repo.save_ipo_terms, terms)
            read += 1
        collection.succeeded(
            PROSPECTUSES, read, degraded=bool(failed), reason="; ".join(failed[:SHOWN])
        )

    async def _prospectus(self, filing: Filing) -> IpoTerms:
        config = self._config
        place = {"cik": filing.cik, "folder": folder(filing.accession)}
        listing = await self._get(config.documents_url_template.format(**place))
        if listing.status_code != HTTP_OK:
            raise UpstreamError(f"its documents answered HTTP {listing.status_code}")
        name = primary_document(listing.json())
        response = await self._get(config.document_url_template.format(**place, name=name))
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"{name} answered HTTP {response.status_code}")
        if len(response.content) > config.max_document_bytes:
            raise UpstreamError(f"{name} is larger than {config.max_document_bytes} bytes")
        read = parse_prospectus(html_text(response.text), cover_chars=config.cover_chars)
        if not read.ipo:
            return IpoTerms(
                accession=filing.accession,
                cik=filing.cik,
                status="not_ipo",
                detail="its cover does not say it is an initial public offering",
                known_at=self._now(),
            )
        return IpoTerms(
            accession=filing.accession,
            cik=filing.cik,
            status="ipo",
            price=read.price,
            shares=read.shares,
            lockup_days=read.lockup_days,
            prospectus_day=read.prospectus_day,
            lockup_ends=read.lockup_ends(filing.filed),
            detail=None if read.lockup_days is not None else "no lock-up is stated",
            known_at=self._now(),
        )

    async def _record(self, target: str, *, rows: int, detail: str | None) -> None:
        await self._worker.run(
            source_repo.record_target,
            SOURCE,
            target,
            at=self._now(),
            ok=True,
            rows=rows,
            detail=detail,
        )


def _listed_ciks(store: Store) -> frozenset[int]:
    """The companies of the newest listing snapshot held."""
    snapshot = stocks_repo.latest_snapshot_held(store)
    if snapshot is None:
        return frozenset()
    return frozenset(listing.cik for listing in stocks_repo.listed(store, snapshot))
