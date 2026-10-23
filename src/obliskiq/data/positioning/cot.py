"""The CFTC's Commitments of Traders (legacy futures-only): history from the annual archives, each
week from Socrata, one report shape for both.

Ported from the old ObliskIQ's ``parse_cot_annual_zip`` and ``scripts/backfill_cot_history.py``
(the archive) and lite's ``CftcFetcher`` (Socrata). Both keep every leg the report gives
(non-commercial, commercial, non-reportable, open interest): the crowding component reads the
non-commercial share, H9 the commercial one. A row with no open interest is not a report (it has
no denominator); a leg left blank stays ``None``, never zero. Each report is dated by the source's
release rule (Tuesday positions, published the Friday after).

**The registry.** ``sources.positioning.contracts`` maps an id (what the universe and the board
name, ``COT_SP500``) to the CFTC's contract market code. The old ObliskIQ mapped the 30-year bond
to ``043607``, which is the ultra 10-year note; the bond is ``020601`` (docs/COT_REAL.md).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import csv
import functools
import io
import zipfile
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any, Literal

import httpx

from ...config.schema import ObliskConfig
from ...domain.sources import CotReport
from ...errors import UpstreamError
from ...store import sources as source_repo
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy, get_with_retry
from ...util.timeutil import parse_date
from ..market.plumbing import Collection, Pacer, failure_text

STEP = "cftc"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
#: The archive's columns (stripped of surrounding blanks before matching).
ANNUAL_COLUMNS = {
    "name": "Market and Exchange Names",
    "date": "As of Date in Form YYYY-MM-DD",
    "code": "CFTC Contract Market Code",
    "open_interest": "Open Interest (All)",
    "noncomm_long": "Noncommercial Positions-Long (All)",
    "noncomm_short": "Noncommercial Positions-Short (All)",
    "comm_long": "Commercial Positions-Long (All)",
    "comm_short": "Commercial Positions-Short (All)",
    "nonrept_long": "Nonreportable Positions-Long (All)",
    "nonrept_short": "Nonreportable Positions-Short (All)",
}
#: The same fields in Socrata's dataset 6dca-aqww.
SOCRATA_COLUMNS = {
    "name": "market_and_exchange_names",
    "date": "report_date_as_yyyy_mm_dd",
    "code": "cftc_contract_market_code",
    "open_interest": "open_interest_all",
    "noncomm_long": "noncomm_positions_long_all",
    "noncomm_short": "noncomm_positions_short_all",
    "comm_long": "comm_positions_long_all",
    "comm_short": "comm_positions_short_all",
    "nonrept_long": "nonrept_positions_long_all",
    "nonrept_short": "nonrept_positions_short_all",
}
#: The archive writes a blank count as these.
BLANK_COUNTS = frozenset({"", ".", "n/a", "NA"})
#: Socrata stamps a report date as midnight; the date part is the report's.
SOCRATA_DATE_LENGTH = 10
LEGS = ("noncomm_long", "noncomm_short", "comm_long", "comm_short", "nonrept_long", "nonrept_short")

type CotSource = Literal["cftc_annual", "cftc_socrata"]


def count(value: Any) -> int | None:
    """A CFTC count (``1,234``, ``.``, blank) as an integer, or ``None``."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in BLANK_COUNTS:
        return None
    try:
        number = int(float(text))
    except ValueError:
        return None
    return number if number >= 0 else None


def _report(
    row: Mapping[str, Any],
    columns: Mapping[str, str],
    *,
    lag_days: int,
    source: CotSource,
    collected_at: datetime,
) -> CotReport | None:
    report_date = parse_date(str(row.get(columns["date"]) or "")[:SOCRATA_DATE_LENGTH])
    open_interest = count(row.get(columns["open_interest"]))
    if report_date is None or not open_interest:
        return None
    legs = {leg: count(row.get(columns[leg])) for leg in LEGS}
    return CotReport(
        contract_code=str(row.get(columns["code"]) or "").strip(),
        report_date=report_date,
        release_date=report_date + timedelta(days=lag_days),
        market_name=str(row.get(columns["name"]) or "").strip() or "unnamed",
        open_interest=open_interest,
        source=source,
        collected_at=collected_at,
        **legs,
    )


def parse_annual(
    content: bytes, codes: Iterable[str], *, lag_days: int, collected_at: datetime
) -> list[CotReport]:
    """Every weekly report of the given contracts in one year's archive (``annual.txt``)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise UpstreamError("the CFTC archive is not a ZIP file") from exc
    member = next((n for n in archive.namelist() if n.lower().endswith(".txt")), None)
    if member is None:
        raise UpstreamError("the CFTC archive holds no .txt member")
    reader = csv.DictReader(io.StringIO(archive.read(member).decode("latin-1")))
    if reader.fieldnames is None or ANNUAL_COLUMNS["code"] not in {
        name.strip() for name in reader.fieldnames
    }:
        raise UpstreamError("the CFTC archive's header lacks the contract market code")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]
    wanted = set(codes)
    reports: list[CotReport] = []
    for row in reader:
        if str(row.get(ANNUAL_COLUMNS["code"]) or "").strip() not in wanted:
            continue
        report = _report(
            row, ANNUAL_COLUMNS, lag_days=lag_days, source="cftc_annual", collected_at=collected_at
        )
        if report is not None:
            reports.append(report)
    return reports


def parse_socrata(rows: Any, *, lag_days: int, collected_at: datetime) -> list[CotReport]:
    """Socrata's rows as reports; a body that is not a list of rows is refused."""
    if not isinstance(rows, list):
        raise UpstreamError("Socrata answered something other than a list of rows")
    reports: list[CotReport] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        report = _report(
            row,
            SOCRATA_COLUMNS,
            lag_days=lag_days,
            source="cftc_socrata",
            collected_at=collected_at,
        )
        if report is not None:
            reports.append(report)
    return reports


class CotCollector:
    """Collects the registry's contracts: each week from Socrata, history from the archives."""

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
        self._config = settings.sources.positioning
        self._lag = settings.sources.providers["cftc"].release_lag_days or 0
        self._client = client
        self._policy = policy
        self._worker = worker
        self._pacer = pacer
        self._now = now

    def _codes(self) -> dict[str, str]:
        return {contract.code: name for name, contract in self._config.contracts.items()}

    async def _get(self, url: str, params: Mapping[str, Any] | None) -> httpx.Response:
        return await get_with_retry(self._client, url, policy=self._policy, params=params)

    async def run(self, *, dry_run: bool = False) -> Collection:
        """Each contract's reports since its newest held, re-reading ``incremental_weeks`` (a
        revised report replaces the one held); a contract with none starts at the backfill year."""
        collection = Collection(STEP)
        for code, name in self._codes().items():
            try:
                reports = await self._pacer.retry(name, functools.partial(self._socrata, code))
            except Exception as exc:  # one contract never stops the others
                collection.failed(name, failure_text(exc))
                continue
            if not dry_run:
                await self._worker.run(source_repo.upsert_cot_reports, reports)
            newest = max((r.report_date for r in reports), default=None)
            collection.succeeded(name, len(reports), newest=str(newest))
            await self._pacer.pace()
        return collection

    async def _socrata(self, code: str) -> list[CotReport]:
        newest = await self._worker.run(source_repo.newest_cot_report, code)
        since = (
            date(self._config.backfill_start_year, 1, 1)
            if newest is None
            else newest - timedelta(weeks=self._config.incremental_weeks)
        )
        response = await self._get(
            self._config.socrata_url,
            {
                "$where": (
                    f"cftc_contract_market_code='{code}' AND "
                    f"report_date_as_yyyy_mm_dd >= '{since.isoformat()}T00:00:00.000'"
                ),
                "$order": "report_date_as_yyyy_mm_dd ASC",
                "$limit": self._config.socrata_page_size,
            },
        )
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"Socrata answered HTTP {response.status_code} for {code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError(f"Socrata's answer for {code} is not JSON") from exc
        return parse_socrata(body, lag_days=self._lag, collected_at=self._now())

    async def backfill(
        self, start_year: int, end_year: int, *, dry_run: bool = False
    ) -> Collection:
        """Every report of the registry's contracts from the annual archives of the years given;
        a year the CFTC has not published (404) is skipped and said so."""
        collection = Collection(f"{STEP}_backfill")
        codes = self._codes()
        for year in range(start_year, end_year + 1):
            target = str(year)
            url = self._config.annual_url_template.format(year=year)
            try:
                response = await self._pacer.retry(target, functools.partial(self._get, url, None))
                if response.status_code == HTTP_NOT_FOUND:
                    collection.succeeded(target, 0, degraded=True, reason="no archive for the year")
                    continue
                if response.status_code != HTTP_OK:
                    raise UpstreamError(
                        f"the archive of {year} answered HTTP {response.status_code}"
                    )
                reports = parse_annual(
                    response.content, codes, lag_days=self._lag, collected_at=self._now()
                )
            except Exception as exc:  # one year never stops the others
                collection.failed(target, failure_text(exc))
                continue
            if not dry_run:
                await self._worker.run(source_repo.upsert_cot_reports, reports)
            collection.succeeded(
                target, len(reports), contracts=len({r.contract_code for r in reports})
            )
            await self._pacer.pace()
        return collection
