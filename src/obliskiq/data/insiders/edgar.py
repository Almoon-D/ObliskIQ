"""Form 4 filings of the US stock candidates, collected forward from EDGAR's daily index.

Each run reads the day's form index (``form.YYYYMMDD.idx``) for every day since the last one read
(at most ``lookback_days`` back), keeps the Form 4 lines of the issuers it follows, and fetches each
new filing's full submission once (the accession is the key; a filing held is never fetched
again). The issuers followed are the stock candidates of ``insiders.country`` (holdings lines) and
the universe's shares listed on ``insiders.venues``, mapped to their CIKs through the SEC's ticker
file. Every SEC request carries ``SEC_USER_AGENT`` and keeps to its fair-access pace.

A day whose index is absent (a weekend, a holiday, today before EDGAR posts it) is not a failure;
a past day is recorded as read with nothing, today's is asked again on the next run. A filing that
does not parse fails alone, with its day degraded.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ...config.schema import ObliskConfig
from ...domain.enums import AssetClass
from ...domain.sources import InsiderFiling
from ...domain.universe import Holding
from ...errors import UpstreamError
from ...store import sources as source_repo
from ...store import universe as universe_repo
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy, get_with_retry
from ..market.plumbing import Collection, Pacer, failure_text
from ..market.targets import venue_of
from .form4 import parse_submission

STEP = "sec_edgar"
SOURCE = "sec_edgar"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_FORBIDDEN = 403
#: What EDGAR's storage answers, with a 403, for a file that does not exist (2026-10-23).
STORAGE_DENIAL = "<Code>AccessDenied</Code>"
INDEX_PREFIX = "index:"
#: Weekday numbers of Saturday and Sunday: EDGAR posts no index for them.
WEEKEND = frozenset({5, 6})
#: Months in a quarter, for the index's QTRn folder.
MONTHS_PER_QUARTER = 3
#: One line of a form index: form type, company, CIK, date filed, file name.
_INDEX_LINE = re.compile(
    r"^(?P<form>\S+(?: \S+)*?)\s{2,}(?P<company>.+?)\s{2,}(?P<cik>\d+)\s{2,}"
    r"(?P<filed>\d{8})\s{2,}(?P<path>edgar/\S+\.txt)\s*$"
)
_ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})\.txt$")


@dataclass(frozen=True, slots=True)
class IndexLine:
    form: str
    cik: int
    path: str

    @property
    def accession(self) -> str | None:
        found = _ACCESSION.search(self.path)
        return None if found is None else found.group(1)


def parse_index(text: str) -> list[IndexLine]:
    """The filings a daily form index lists (the lines after its dashed rule)."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if set(line.strip()) == {"-"}), None)
    if start is None:
        raise UpstreamError("the form index has no dashed rule before its lines")
    found: list[IndexLine] = []
    for line in lines[start:]:
        match = _INDEX_LINE.match(line)
        if match is not None:
            found.append(IndexLine(match["form"], int(match["cik"]), match["path"]))
    return found


def normalised_ticker(ticker: str) -> str:
    """A ticker without punctuation or case: holdings write BRKB, the SEC BRK-B."""
    return re.sub(r"[^A-Z0-9]", "", ticker.upper())


def ticker_ciks(body: Any) -> dict[str, int]:
    """The SEC's ticker file (``{"0": {"cik_str": 320193, "ticker": "AAPL"}, ...}``)."""
    if not isinstance(body, dict):
        raise UpstreamError("the SEC's ticker file is not a mapping")
    found: dict[str, int] = {}
    for entry in body.values():
        if isinstance(entry, dict) and entry.get("ticker") and entry.get("cik_str"):
            found.setdefault(normalised_ticker(str(entry["ticker"])), int(entry["cik_str"]))
    return found


def followed_tickers(settings: ObliskConfig, holdings: Iterable[Holding]) -> set[str]:
    """The tickers of the candidates whose filings are read."""
    insiders = settings.sources.insiders
    tickers = {line.ticker for line in holdings if line.country == insiders.country}
    tickers |= {
        instrument.symbol
        for instrument in settings.universe.instruments
        if instrument.asset_class is AssetClass.STOCK
        and venue_of(settings, instrument.listing_exchange) in insiders.venues
    }
    return tickers


def absent(response: httpx.Response) -> bool:
    """Whether EDGAR says a file does not exist: a 404, or the 403 its storage answers for a
    missing key (an XML ``AccessDenied``). A 403 page refusing the client is not absence."""
    if response.status_code == HTTP_NOT_FOUND:
        return True
    return response.status_code == HTTP_FORBIDDEN and STORAGE_DENIAL in response.text


def index_url(template: str, day: date) -> str:
    quarter = (day.month - 1) // MONTHS_PER_QUARTER + 1
    return template.format(year=day.year, quarter=quarter, day=day.strftime("%Y%m%d"))


class Form4Collector:
    """Reads the daily indexes and stores the new Form 4 filings of the issuers followed."""

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
        self._settings = settings
        self._config = settings.sources.insiders
        self._zone = ZoneInfo(self._config.timezone)
        self._client = client
        self._policy = policy
        self._worker = worker
        self._pacer = pacer
        self._user_agent = user_agent
        self._now = now
        self._sleep = sleep

    async def _get(self, url: str) -> httpx.Response:
        if self._user_agent is None:
            raise UpstreamError("SEC_USER_AGENT is unset; EDGAR refuses a request without one")
        response = await get_with_retry(
            self._client, url, policy=self._policy, headers={"User-Agent": self._user_agent}
        )
        await self._sleep(self._config.request_spacing_seconds)
        return response

    async def run(self, *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        try:
            ciks = await self._issuers()
        except Exception as exc:  # without the issuers there is nothing to follow
            collection.failed("issuers", failure_text(exc))
            return collection
        if not ciks:
            collection.empty_reason = (
                f"no stock candidate of {self._config.country} maps to an SEC CIK"
            )
            return collection
        today = self._now().astimezone(self._zone).date()
        for day in await self._days(today):
            try:
                await self._pacer.retry(
                    day.isoformat(),
                    functools.partial(self._day, day, today, ciks, collection, dry_run),
                )
            except Exception as exc:  # one day never stops the next
                collection.failed(f"{INDEX_PREFIX}{day}", failure_text(exc))
        collection.notes["issuers"] = len(ciks)
        return collection

    async def _issuers(self) -> dict[int, str]:
        response = await self._get(self._config.tickers_url)
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"the SEC's ticker file answered HTTP {response.status_code}")
        by_ticker = ticker_ciks(response.json())
        holdings = await self._worker.run(
            universe_repo.candidate_holdings, as_of=self._now().date()
        )
        return {
            cik: ticker
            for ticker in sorted(followed_tickers(self._settings, holdings))
            if (cik := by_ticker.get(normalised_ticker(ticker))) is not None
        }

    async def _days(self, today: date) -> list[date]:
        done = await self._worker.run(source_repo.succeeded_targets, SOURCE, INDEX_PREFIX)
        floor = today - timedelta(days=self._config.lookback_days)
        newest = max((date.fromisoformat(t.removeprefix(INDEX_PREFIX)) for t in done), default=None)
        start = floor if newest is None else max(floor, newest + timedelta(days=1))
        days = [start + timedelta(days=n) for n in range((today - start).days + 1)]
        return [day for day in days if day.weekday() not in WEEKEND]

    async def _day(
        self,
        day: date,
        today: date,
        ciks: Mapping[int, str],
        collection: Collection,
        dry_run: bool,
    ) -> None:
        target = f"{INDEX_PREFIX}{day}"
        response = await self._get(index_url(self._config.daily_index_url_template, day))
        if absent(response):
            if day < today and not dry_run:
                await self._record(target, rows=0, detail="no index: EDGAR was closed")
            return
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"the index of {day} answered HTTP {response.status_code}")
        lines = [
            line
            for line in parse_index(response.text)
            if line.form in self._config.forms and line.cik in ciks
        ]
        saved, failed = 0, []
        seen: set[str] = set()
        for line in lines:
            accession = line.accession
            if accession is None or accession in seen:
                continue
            seen.add(accession)
            if await self._worker.run(source_repo.has_insider_filing, accession):
                continue
            try:
                filing = await self._filing(line)
            except Exception as exc:  # one filing never stops its day
                failed.append(f"{accession}: {failure_text(exc)}")
                continue
            if filing.issuer_cik not in ciks:
                continue  # listed under a followed owner's CIK, about another issuer
            if not dry_run:
                await self._worker.run(source_repo.save_insider_filing, filing)
            saved += 1
        if not dry_run:
            await self._record(target, rows=saved, detail="; ".join(failed) or None)
        collection.succeeded(
            target,
            saved,
            degraded=bool(failed),
            reason="; ".join(failed),
            listed=len(seen),
        )

    async def _filing(self, line: IndexLine) -> InsiderFiling:
        response = await self._get(f"{self._config.archive_url}/{line.path}")
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"{line.path} answered HTTP {response.status_code}")
        return parse_submission(response.text, zone=self._zone, collected_at=self._now())

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
