"""Stock candidates per grid cell: from the issuers' holdings files and the market scanner.

**Sources.** Each universe instrument that names a ``holdings`` source (iShares first) has its
file downloaded, its date read from the file itself ("Fund Holdings as of"), and every line
stored with the issuer's own words beside what they map to (``collectors.candidates``: sector,
location, exchange; an unmapped word maps to nothing, never a guess). A file is downloaded again
once the newest one held is ``refresh_days`` old. The scanner runs the universe declares
(``universe.candidates.scanners``) are stored ranked as returned. Source and date are kept per
file and per run (``store/universe.py``).

**Crediting.** A share line counts for the grid market of its location's country and for the
market its file is a source for (``candidates_for``: a country fund's holdings are that market's
even where a line is located elsewhere); it counts for its own sector's cell and for the market's
``broad`` cell. A scanner row counts for its run's market, broad cell only (a scanner names no
sector). The listings of one company are one candidate: lines are matched by country and by the
company's name with ``name_noise`` dropped, so a share held by a country fund and by MSCI World,
or two share classes, is one candidate with every listing it was seen on.

**Point in time.** Membership as of a date reads each source's newest file of that date and held
by then; a stock enters a cell on the date its first such file showed it (``since``). Every cell
of the grid has candidates or a reason: a gap the universe declares (``universe.candidates.gaps``)
or the problem :func:`coverage_problems` reports.

**Resolution on demand.** A candidate is resolved to a conid through IBKR's contract search only
when a thesis needs it (``obliskiq universe pin --candidate``), exact ticker on its listing
venue, its currency confirmed; then it is pinned, seeded and collected. VERIFY-LIVE(holdings
ticker to IBKR symbol per venue).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import csv
import hashlib
import io
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import NamedTuple

import httpx

from ..config.schema import ObliskConfig
from ..config.schema_collectors import CandidatesConfig
from ..domain.universe import CandidateResolution, Holding, HoldingsFile, ScannerRow
from ..errors import DataSourceError, UpstreamError
from ..ibkr.client import Scanner, Secdef
from ..ibkr.identity import resolve_identity
from ..store import universe as universe_repo
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy, get_with_retry
from .market.plumbing import Collection, Pacer, failure_text

STEP = "candidates"
ISHARES = "iShares"
HOLDINGS_AS_OF = "Fund Holdings as of"
HEADER_START = "Ticker,"
HTTP_OK = 200
#: A percent is a hundredth.
PERCENT = 100.0
#: The stock search's security type.
STOCK = "STK"
#: A scanner row's candidate key: it is already a contract.
SCANNER_KEY = "conid:{conid}"

type Fetch = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class ParsedHoldings:
    file: HoldingsFile
    lines: tuple[Holding, ...]


class Quoted(NamedTuple):
    """One listing of a candidate: its ticker, its IBKR exchange (``None`` where the venue is not
    configured) and the currency it trades in."""

    ticker: str
    exchange: str | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One company a cell may hold, with every listing and source it was seen on."""

    key: str
    name: str
    listings: tuple[Quoted, ...]
    country: str | None
    #: Its largest weight in any one source (a fraction of that fund).
    weight: float | None
    sources: tuple[str, ...]
    since: date | None

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted({listing.ticker for listing in self.listings}))

    def venue_listings(self) -> tuple[Quoted, ...]:
        """The listings on a configured venue whose currency is known, in order."""
        return tuple(q for q in self.listings if q.exchange is not None and q.currency is not None)


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell of the grid and its candidates, or why it has none."""

    market: str
    sector: str
    candidates: tuple[Candidate, ...]
    gap: str | None


# ── parsing ──


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = text.replace(",", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _word(text: str | None) -> str | None:
    cleaned = (text or "").strip()
    return None if cleaned in ("", "-") else cleaned


def holdings_date(line: str, formats: Sequence[str]) -> date:
    """The date of a ``Fund Holdings as of,"…"`` line, in either site's form."""
    value = next(csv.reader([line]))[1].strip() if "," in line else ""
    for form in formats:
        try:
            return datetime.strptime(value, form).date()
        except ValueError:
            continue
    raise DataSourceError(f"the holdings date {value!r} is in none of the known forms")


def normalised_name(name: str, noise: Iterable[str]) -> str:
    """The company's name without punctuation and the configured noise words."""
    dropped = set(noise)
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    kept = [word for word in words if word not in dropped]
    return " ".join(kept or words)


def candidate_key(country: str | None, name: str, noise: Iterable[str]) -> str:
    return f"{(country or 'xx').lower()}:{normalised_name(name, noise).lower().replace(' ', '_')}"


def parse_ishares(
    content: bytes, *, fund: str, url: str, downloaded_at: datetime, config: CandidatesConfig
) -> ParsedHoldings:
    """An iShares holdings CSV (the US site's or the UK site's form) as a file and its lines."""
    text = content.decode("utf-8-sig")
    rows = text.splitlines()
    dated = next((r for r in rows if r.startswith(HOLDINGS_AS_OF)), None)
    if dated is None:
        raise DataSourceError(f"{fund}: the file carries no '{HOLDINGS_AS_OF}' line")
    as_of = holdings_date(dated, config.date_formats)
    start = next((i for i, r in enumerate(rows) if r.startswith(HEADER_START)), None)
    if start is None:
        raise DataSourceError(f"{fund}: the file carries no holdings table")
    lines: list[Holding] = []
    for row in csv.DictReader(io.StringIO("\n".join(rows[start:]))):
        ticker, name, asset_class = (_word(row.get(k)) for k in ("Ticker", "Name", "Asset Class"))
        if ticker is None or name is None or asset_class is None:
            break  # the table ends; a disclaimer may follow
        equity = asset_class == config.equity_asset_class
        issuer_sector = _word(row.get("Sector"))
        location = _word(row.get("Location"))
        country = config.locations.get(location) if location else None
        exchange = _word(row.get("Exchange"))
        currency = _word(row.get("Market Currency"))
        weight = _number(row.get("Weight (%)"))
        lines.append(
            Holding(
                fund_symbol=fund,
                as_of=as_of,
                line=len(lines),
                ticker=ticker,
                name=name,
                asset_class=asset_class,
                issuer_sector=issuer_sector,
                sector=config.sectors.get(issuer_sector) if equity and issuer_sector else None,
                location=location,
                country=country,
                exchange=exchange,
                ibkr_exchange=config.exchanges.get(exchange) if exchange else None,
                currency=currency if currency and re.fullmatch(r"[A-Z]{3}", currency) else None,
                weight=None if weight is None else weight / PERCENT,
                candidate_key=candidate_key(country, name, config.name_noise) if equity else None,
            )
        )
    file = HoldingsFile(
        fund_symbol=fund,
        as_of=as_of,
        issuer=ISHARES,
        url=url,
        downloaded_at=downloaded_at,
        content_sha256=hashlib.sha256(content).hexdigest(),
        rows=len(lines),
    )
    return ParsedHoldings(file, tuple(lines))


# ── crediting ──


def _markets_by_country(settings: ObliskConfig) -> dict[str, str]:
    return {market.country: name for name, market in settings.universe.grid.markets.items()}


def _source_markets(settings: ObliskConfig) -> dict[str, str | None]:
    return {
        i.symbol: i.holdings.candidates_for for i in settings.universe.instruments if i.holdings
    }


@dataclass
class _Building:
    name: str
    country: str | None
    listings: set[Quoted]
    sources: set[str]
    weight: float | None
    since: date | None

    def add(self, listing: Quoted, source: str, weight: float | None) -> None:
        self.listings.add(listing)
        self.sources.add(source)
        if weight is not None and (self.weight is None or weight > self.weight):
            self.weight = weight

    def done(self, key: str) -> Candidate:
        return Candidate(
            key=key,
            name=self.name,
            listings=tuple(sorted(self.listings, key=lambda q: (q.exchange or "~", q.ticker))),
            country=self.country,
            weight=self.weight,
            sources=tuple(sorted(self.sources)),
            since=self.since,
        )


def credit(
    settings: ObliskConfig,
    holdings: Iterable[Holding],
    scanned: Iterable[ScannerRow],
    since: Mapping[str, date] | None = None,
) -> dict[tuple[str, str], tuple[Candidate, ...]]:
    """Each cell's candidates, largest weight first; ``since`` is when each key was first held."""
    grid = settings.universe.grid
    by_country = _markets_by_country(settings)
    sources = _source_markets(settings)
    cells: dict[tuple[str, str], dict[str, _Building]] = defaultdict(dict)
    first = since or {}

    def entry(cell: tuple[str, str], key: str, seed: _Building) -> _Building:
        return cells[cell].setdefault(key, seed)

    for line in holdings:
        key = line.candidate_key
        if key is None:
            continue
        markets = {
            m for m in (by_country.get(line.country or ""), sources.get(line.fund_symbol)) if m
        }
        sectors = {grid.broad_sector, *([line.sector] if line.sector else [])}
        for cell in ((market, sector) for market in markets for sector in sectors):
            seed = _Building(line.name, line.country, set(), set(), None, first.get(key))
            listing = Quoted(line.ticker, line.ibkr_exchange, line.currency)
            entry(cell, key, seed).add(listing, line.fund_symbol, line.weight)
    for row in scanned:
        if row.market not in grid.markets:
            continue
        name = row.company_name or row.symbol or str(row.conid)
        seed = _Building(name, None, set(), set(), None, row.scanned_on)
        listing = Quoted(row.symbol or str(row.conid), row.listing_exchange, None)
        entry((row.market, grid.broad_sector), row.candidate_key, seed).add(
            listing, f"scanner {row.location}", None
        )
    return {
        cell: tuple(
            sorted(
                (b.done(k) for k, b in found.items()),
                key=lambda c: (-(c.weight or 0.0), c.key),
            )
        )
        for cell, found in cells.items()
    }


def candidate_cells(
    settings: ObliskConfig,
    holdings: Iterable[Holding],
    scanned: Iterable[ScannerRow],
    since: Mapping[str, date] | None = None,
) -> list[Cell]:
    """Every cell of the grid with its candidates or its declared gap, by market and sector."""
    found = credit(settings, holdings, scanned, since)
    gaps = {
        (gap.market, sector): gap.reason
        for gap in settings.universe.candidates.gaps
        for sector in gap.sectors
    }
    grid = settings.universe.grid
    return [
        Cell(market, sector, found.get((market, sector), ()), gaps.get((market, sector)))
        for market in grid.markets
        for sector in grid.sectors
    ]


def coverage_problems(cells: Iterable[Cell]) -> list[str]:
    """The cells with no candidate and no declared gap; and a declared gap that has candidates
    now, which is a stale declaration."""
    problems: list[str] = []
    for cell in cells:
        if not cell.candidates and cell.gap is None:
            problems.append(f"{cell.market}/{cell.sector}: no candidate and no declared gap")
        if cell.candidates and cell.gap is not None:
            problems.append(f"{cell.market}/{cell.sector}: declared a gap but has candidates")
    return problems


# ── refreshing ──


def http_fetch(client: httpx.AsyncClient, policy: RetryPolicy) -> Fetch:
    """A download over ``client`` with the shared retry policy; any status but 200 fails."""

    async def fetch(url: str) -> bytes:
        try:
            response = await get_with_retry(client, url, policy=policy)
        except UpstreamError:
            raise DataSourceError(f"{url} could not be reached") from None
        if response.status_code != HTTP_OK:
            raise DataSourceError(f"{url} answered HTTP {response.status_code}")
        return response.content

    return fetch


class CandidateCollector:
    """Refreshes the holdings files on their cadence and runs the declared scanners."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        fetch: Fetch,
        scanner: Scanner | None,
        worker: StoreWorker,
        pacer: Pacer,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._config = settings.collectors.candidates
        self._fetch = fetch
        self._scanner = scanner
        self._worker = worker
        self._pacer = pacer
        self._now = now

    async def run(self, *, dry_run: bool = False, force: bool = False) -> Collection:
        collection = Collection(STEP)
        now = self._now()
        held = {
            f.fund_symbol: f
            for f in await self._worker.run(universe_repo.holdings_files, as_of=now.date())
        }
        fresh = timedelta(days=self._config.refresh_days)
        for declared in self._settings.universe.instruments:
            source = declared.holdings
            if source is None:
                continue
            latest = held.get(declared.symbol)
            if latest is not None and not force and now - latest.downloaded_at < fresh:
                collection.succeeded(declared.symbol, 0, held=str(latest.as_of))
                continue
            try:
                # The download's own retries are the HTTP layer's (``http``); a refused or
                # unreadable file is the same file on a second attempt.
                parsed = await self._download(declared.symbol, source.url, now)
            except Exception as exc:  # one source never stops the refresh
                collection.failed(declared.symbol, failure_text(exc))
                continue
            if not dry_run:
                await self._worker.run(universe_repo.save_holdings, parsed.file, parsed.lines)
            collection.succeeded(declared.symbol, len(parsed.lines), held=str(parsed.file.as_of))
        await self._scan(collection, now, dry_run=dry_run)
        return collection

    async def _download(self, fund: str, url: str, now: datetime) -> ParsedHoldings:
        content = await self._fetch(url)
        return parse_ishares(content, fund=fund, url=url, downloaded_at=now, config=self._config)

    async def _scan(self, collection: Collection, now: datetime, *, dry_run: bool) -> None:
        for run in self._settings.universe.candidates.scanners:
            label = f"scanner {run.location} {run.scan_type}"
            if self._scanner is None:
                collection.failed(label, "no IBKR session: the scanner was not run")
                continue
            try:
                found = await self._scanner.run(
                    instrument=run.instrument, scan_type=run.scan_type, location=run.location
                )
            except Exception as exc:  # a scanner that fails leaves the holdings standing
                collection.failed(label, failure_text(exc))
                continue
            rows = [
                ScannerRow(
                    scanned_on=now.date(),
                    location=run.location,
                    scan_type=run.scan_type,
                    rank=rank,
                    market=run.market,
                    conid=contract.conid,
                    symbol=contract.symbol,
                    listing_exchange=contract.listing_exchange,
                    company_name=contract.company_name,
                    candidate_key=SCANNER_KEY.format(conid=contract.conid),
                    scanned_at=now,
                )
                for rank, contract in enumerate(found)
            ]
            if rows and not dry_run:
                await self._worker.run(universe_repo.save_scanner_rows, rows)
            empty = "the scan returned nothing" if not rows else ""
            collection.succeeded(label, len(rows), degraded=not rows, reason=empty)
            await self._pacer.pace()


# ── resolving ──


async def resolve_candidate(
    secdef: Secdef, candidate: Candidate, *, at: datetime, pinned_for: str | None
) -> CandidateResolution:
    """The one contract a candidate's listing names (exact ticker on its venue, its currency
    confirmed), or the failure. Each listing the candidate was seen on is tried in turn."""
    failures: list[str] = []
    for ticker, exchange, currency in candidate.venue_listings():
        if exchange is None or currency is None:  # pragma: no cover - venue_listings keeps none
            continue
        try:
            identity = await resolve_identity(
                secdef, ticker, listing_exchange=exchange, currency=currency, sec_type=STOCK
            )
        except Exception as exc:  # the next listing may resolve
            failures.append(f"{ticker} on {exchange}: {failure_text(exc)}")
            continue
        return CandidateResolution(
            candidate_key=candidate.key,
            resolved_at=at,
            conid=identity.conid,
            symbol=identity.symbol,
            listing_exchange=identity.listing_exchange,
            currency=identity.currency,
            pinned=pinned_for is not None,
            pinned_for=pinned_for,
            failure=None,
        )
    reason = "; ".join(failures) or "no listing on a configured venue with a known currency"
    return CandidateResolution(
        candidate_key=candidate.key,
        resolved_at=at,
        conid=None,
        symbol=None,
        listing_exchange=None,
        currency=None,
        pinned=False,
        pinned_for=None,
        failure=reason,
    )
