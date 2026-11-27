"""The replay's loaders: its own store filled from sources that date their own publication (H15).

Every row is stamped with the end of the day its source published it (UTC), so a sheet dated a
day reads exactly what was out by that evening:

* **filings**: the SEC's quarterly index of XBRL filings (``llm.replay.full_index_url_template``),
  the forms of ``filer_forms``, each known from the end of the day it was filed (EDGAR dates a
  filing accepted after its evening cut-off on the next business day);
* **names**: the SEC's ticker file today, one ticker per filing company (the first it lists), on
  any exchange; a company it does not list has none, so no price: not evaluable, counted;
* **listing snapshots** for the screen: at the history's start and at every sheet date, the
  common stocks of the kept exchanges among that date's filers;
* **SIC**: each listed company's, from its submissions today, stamped at the history's start
  (a company that changed industry since is read in its new one: a bias the report states);
* **fundamentals**: the screen's inputs from the SEC's bulk company facts, every value with the
  day it was filed, a restatement a new row; an annual value is one whose period lasts a year
  (``ANNUAL_DAYS``), labelled by the calendar year holding its midpoint as the SEC's frames do,
  an instant one by the calendar quarter end nearest it (within ``QUARTER_SLACK``);
* **stock closes**: Yahoo's, each with its total return from the session before, known at the end
  of its session's day;
* **the universe instruments' closes** (the sheet's instrument section and the equity
  reference) and **the ECB's reference rates** (a round trip's cost in the base currency).

Each step records what it read (``source_targets``, source ``replay``), so a stopped load resumes
where it stopped. Yahoo throttles silently with empty answers: a batch whose share of empty
answers passes ``stocks.bars.max_empty_share`` stops the load and records nothing of it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import calendar
import functools
import hashlib
import json
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

import httpx

from ...config.schema import ObliskConfig
from ...config.schema_sources import FrameInputConfig
from ...config.schema_universe import UniverseInstrument
from ...data.sec import sec_get
from ...data.stocks.parse import FIELDS, chain, parse_listings, parse_sic
from ...domain.instrument import Instrument
from ...domain.market import Bar
from ...domain.stocks import StockBar, StockFact
from ...errors import DataSourceError, UpstreamError
from ...grid.stock_screen import months_before
from ...store import repos
from ...store import sources as source_repo
from ...store import stocks as stocks_repo
from ...store.replay import Filing, Name, filers, names, save_filings, save_names
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy
from ...util.yahoo import yahoo_symbol
from ..data import Sources, YahooBar, YahooHistory, load_dataset, store_loaded, yahoo_sessions
from .plan import Dates

type Sleep = Callable[[float], Awaitable[None]]


def failure_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


SOURCE: Final = "replay"
HTTP_OK: Final = 200
#: An annual value's period, in days: a year, give or take a few weeks (the SEC's frames).
ANNUAL_DAYS: Final = (335, 395)
#: The most an instant may lie from the calendar quarter end it is labelled with.
QUARTER_SLACK: Final = timedelta(days=30)
MONTHS_PER_QUARTER: Final = 3
QUARTERS: Final = 4
#: The fields of the SEC's XBRL index rows.
INDEX_FIELDS: Final = 5
#: The source of an instrument's replayed bar.
YAHOO: Final = "yahoo"
#: The chunk a bulk download is written in.
CHUNK: Final = 1 << 20
MONTHS_PER_YEAR: Final = 12


def end_of(day: date) -> datetime:
    """The last second of ``day`` in UTC: what was published that day is known by then."""
    return datetime.combine(day, time.max.replace(microsecond=0), tzinfo=UTC)


def quarters_between(first: date, last: date) -> list[tuple[int, int]]:
    """The calendar quarters from the one holding ``first`` to the one holding ``last``."""
    found = []
    year, quarter = first.year, (first.month - 1) // MONTHS_PER_QUARTER + 1
    while (year, quarter) <= (last.year, (last.month - 1) // MONTHS_PER_QUARTER + 1):
        found.append((year, quarter))
        year, quarter = (year + 1, 1) if quarter == QUARTERS else (year, quarter + 1)
    return found


# ── parsing ──


def parse_xbrl_index(text: str, forms: Iterable[str]) -> list[Filing]:
    """The filings of ``forms`` an XBRL index lists (``CIK|Company Name|Form Type|Date
    Filed|Filename`` after a dashed line); an amendment (``10-K/A``) is not one of them.
    VERIFY-LIVE(the SEC's bulk files): the index and ``companyfacts.zip`` as served."""
    wanted = set(forms)
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("---")) + 1
    except StopIteration:
        raise DataSourceError("the SEC's index has no dashed line before its rows") from None
    found = []
    for line in lines[start:]:
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != INDEX_FIELDS:
            raise DataSourceError(f"the SEC's index holds a row of another shape: {line!r}")
        cik, name, form, filed, _ = (part.strip() for part in parts)
        if form not in wanted:
            continue
        try:
            day = date.fromisoformat(filed)
            found.append(Filing(int(cik), form, day, name or f"CIK {cik}", end_of(day)))
        except ValueError:
            raise DataSourceError(f"the SEC's index holds a row it cannot read: {line!r}") from None
    return found


def parse_names(body: Any, *, read_at: datetime) -> list[Name]:
    """One ticker per company of the SEC's ticker file (the first it lists) and its exchange."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise UpstreamError("the SEC's ticker file is not {fields, data}")
    found: dict[int, Name] = {}
    for row in body["data"]:
        if not isinstance(row, list) or len(row) != len(FIELDS):
            raise UpstreamError(f"the SEC's ticker file holds a row of another shape: {row!r}")
        cik, _, ticker, exchange = row
        if not isinstance(cik, int) or not isinstance(ticker, str) or not ticker.strip():
            continue
        found.setdefault(
            cik,
            Name(cik, ticker.strip(), exchange if isinstance(exchange, str) else None, read_at),
        )
    return list(found.values())


def period_of(kind: str, start: date | None, end: date) -> str | None:
    """The frame an XBRL value belongs to, or ``None`` when it fits none: an annual duration by
    the calendar year of its midpoint, an instant by the quarter end within the slack."""
    if kind == "annual":
        if start is None or not ANNUAL_DAYS[0] <= (end - start).days <= ANNUAL_DAYS[1]:
            return None
        return f"CY{(start + (end - start) / 2).year}"
    if start is not None:
        return None
    ends = [
        (abs((quarter_end - end).days), quarter_end)
        for year in (end.year - 1, end.year, end.year + 1)
        for quarter_end in _quarter_ends(year)
    ]
    gap, nearest = min(ends)
    quarter = nearest.month // MONTHS_PER_QUARTER
    return f"CY{nearest.year}Q{quarter}I" if gap <= QUARTER_SLACK.days else None


def _quarter_ends(year: int) -> list[date]:
    return [
        date(year, month, calendar.monthrange(year, month)[1])
        for month in range(MONTHS_PER_QUARTER, MONTHS_PER_YEAR + 1, MONTHS_PER_QUARTER)
    ]


def parse_companyfacts(
    body: Any, *, cik: int, inputs: Mapping[str, FrameInputConfig]
) -> list[StockFact]:
    """The screen's inputs a company's facts hold, every value as filed, in the order filed."""
    if not isinstance(body, dict) or not isinstance(body.get("facts"), dict):
        raise DataSourceError(f"CIK {cik}'s company facts are not {{..., facts}}")
    found: list[StockFact] = []
    for name, frame in inputs.items():
        tag = body["facts"].get(frame.taxonomy, {}).get(frame.tag, {})
        values = tag.get("units", {}).get(frame.unit, []) if isinstance(tag, dict) else []
        for value in values:
            try:
                end = date.fromisoformat(value["end"])
                start = date.fromisoformat(value["start"]) if "start" in value else None
                filed = date.fromisoformat(value["filed"])
                period = period_of(frame.kind, start, end)
                if period is None:
                    continue
                found.append(
                    StockFact(
                        cik=cik,
                        input=name,
                        period=period,
                        period_end=end,
                        value=float(value["val"]),
                        accession=str(value["accn"]),
                        known_at=end_of(filed),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DataSourceError(
                    f"CIK {cik}'s {frame.tag} holds a value of another shape: {exc}"
                ) from None
    return sorted(found, key=lambda f: (f.known_at, f.input, f.period, f.accession))


def stock_bars(answer: Sequence[YahooBar], *, symbol: str, until: date) -> list[StockBar]:
    """A name's sessions, each with its total return from the session before, each known at the
    end of its session's day."""
    chained = chain(answer, symbol=symbol, after=None, until=until, known_at=end_of(until))
    return [bar.model_copy(update={"known_at": end_of(bar.session)}) for bar in chained]


def universe_instrument(declared: UniverseInstrument, *, at: datetime) -> Instrument | None:
    """The store's record of a pinned universe instrument, created at ``at``."""
    if declared.conid is None:
        return None
    attrs = declared.attributes
    return Instrument(
        conid=declared.conid,
        symbol=declared.symbol,
        listing_exchange=declared.listing_exchange,
        currency=declared.currency,
        asset_class=declared.asset_class,
        domicile=attrs.domicile,
        ucits=attrs.ucits,
        kid_status=attrs.kid,
        share_class=attrs.share_class,
        lot_size=attrs.lot_size,
        option_exchange=declared.option_exchange,
        history_exchange=declared.history_exchange,
        roles=frozenset(declared.roles),
        superseded_by=None,
        created_at=at,
        superseded_at=None,
    )


def instrument_bars(conid: int, answer: Sequence[YahooBar], *, until: date) -> list[Bar]:
    return [
        Bar(
            conid=conid,
            session_date=bar.session,
            open=None,
            high=None,
            low=None,
            close=bar.close,
            volume=bar.volume,
            source=YAHOO,
        )
        for bar in answer
        if bar.session <= until
    ]


def universe_at(store: Any, day: date, *, lookback_months: int) -> dict[int, str]:
    """The companies with a filing of the registered forms known by the end of ``day`` and filed
    in the ``lookback_months`` before it."""
    return filers(store, as_of=day, since=months_before(day, lookback_months))


# ── the span ──


@dataclass(frozen=True, slots=True)
class Span:
    """What the loaders read: from the history's start to the last day of prices."""

    history_start: date
    sheets: tuple[date, ...]
    until: date

    @property
    def snapshot_days(self) -> tuple[date, ...]:
        return (self.history_start, *self.sheets)


def span_of(settings: ObliskConfig, dates: Iterable[Dates], *, today: date) -> Span:
    sheets = tuple(sorted({day for found in dates for day in found.all}))
    start = sheets[0] - timedelta(days=settings.llm.replay.history_days)
    return Span(start, sheets, today - timedelta(days=1))


# ── loading ──

type Download = Callable[[str, Path], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Loaded:
    """What one step did."""

    step: str
    rows: int
    detail: str


class ReplaySources:
    """Fills the replay's store, step by step, each step resumable."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        user_agent: str | None,
        yahoo: YahooHistory,
        research: Sources,
        download: Download,
        work_dir: Path,
        sleep: Sleep,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._replay = settings.llm.replay
        self._client = client
        self._policy = policy
        self._worker = worker
        self._user_agent = user_agent
        self._yahoo = yahoo
        self._research = research
        self._download = download
        self._work_dir = work_dir
        self._sleep = sleep
        self._now = now
        self._spacing = settings.sources.insiders.request_spacing_seconds

    async def _retry[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """``operation`` again after a failure, as the HTTP policy backs off; the last failure is
        raised, so its target is recorded failed, never empty."""
        attempt = 0
        while True:
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt >= self._policy.retries:
                    raise
                await self._sleep(self._policy.delay_for(attempt))
                attempt += 1

    async def _done(self, target: str) -> bool:
        return await self._worker.run(source_repo.target_succeeded_at, SOURCE, target) is not None

    async def _mark(
        self, target: str, *, rows: int, ok: bool = True, detail: str | None = None
    ) -> None:
        await self._worker.run(
            source_repo.record_target,
            SOURCE,
            target,
            at=self._now(),
            ok=ok,
            rows=rows,
            detail=detail,
        )

    async def _sec(self, url: str) -> httpx.Response:
        response = await sec_get(
            self._client,
            url,
            policy=self._policy,
            user_agent=self._user_agent,
            spacing=self._spacing,
            sleep=self._sleep,
        )
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"{url} answered HTTP {response.status_code}")
        return response

    async def load(self, span: Span) -> list[Loaded]:
        """Every step in order; a step that cannot finish stops the load (the later ones read
        what it fills)."""
        steps = (
            self._filings,
            self._names,
            self._companies,
            self._facts,
            self._stock_bars,
            self._instruments,
            self._fx,
        )
        done = []
        for step in steps:
            done.append(await step(span))
        return done

    async def _filings(self, span: Span) -> Loaded:
        first = months_before(span.history_start, self._replay.filer_lookback_months)
        rows = read = 0
        for year, quarter in quarters_between(first, span.sheets[-1]):
            target = f"index:{year}Q{quarter}"
            if await self._done(target):
                continue
            url = self._replay.full_index_url_template.format(year=year, quarter=quarter)
            response = await self._sec(url)
            filings = parse_xbrl_index(response.text, self._replay.filer_forms)
            saved = await self._worker.run(save_filings, filings)
            await self._mark(target, rows=saved)
            rows += saved
            read += 1
        return Loaded("filings", rows, f"{read} quarterly indexes read")

    async def _names(self, span: Span) -> Loaded:
        """Today's tickers, then the listing snapshots, in date order (once)."""
        if await self._done("names"):
            return Loaded("names", 0, "held")
        response = await self._sec(self._settings.sources.stocks.listings.url)
        body = response.json()
        now = self._now()
        found = parse_names(body, read_at=now)
        listings = parse_listings(body, self._settings.sources.stocks.listings)
        saved = await self._worker.run(save_names, found)
        digest = hashlib.sha256(response.content).hexdigest()
        snapshots = 0
        for day in span.snapshot_days:
            universe = await self._worker.run(
                universe_at, day, lookback_months=self._replay.filer_lookback_months
            )
            kept = [listing for listing in listings if listing.cik in universe]
            await self._worker.run(
                stocks_repo.save_snapshot, kept, known_at=end_of(day), content_sha256=digest
            )
            snapshots += 1
        await self._mark("names", rows=saved)
        return Loaded("names", saved, f"{snapshots} listing snapshots")

    async def _listed(self, span: Span) -> dict[str, int]:
        """Every ticker a listing snapshot held, with its company."""
        return await self._worker.run(stocks_repo.symbol_ciks, as_of=span.until)

    async def _companies(self, span: Span) -> Loaded:
        """The SIC of every company a snapshot lists, stamped at the history's start."""
        ciks = sorted(set((await self._listed(span)).values()))
        template = self._settings.sources.stocks.listings.submissions_url_template
        rows = failed = 0
        for cik in ciks:
            target = f"sic:{cik}"
            if await self._done(target):
                continue
            try:
                body = (await self._sec(template.format(cik=cik))).json()
                sic = parse_sic(body)
            except Exception as exc:  # one company never stops the others
                await self._mark(target, rows=0, ok=False, detail=failure_text(exc))
                failed += 1
                continue
            await self._worker.run(
                stocks_repo.save_company,
                cik,
                sic,
                known_at=datetime.combine(span.history_start, time.min, tzinfo=UTC),
                incorporated=None,
            )
            await self._mark(target, rows=1)
            rows += 1
        return Loaded("sic", rows, f"{failed} companies failed (read again on the next load)")

    async def _facts(self, span: Span) -> Loaded:
        """The screen's inputs of every listed company from the SEC's bulk facts (one file)."""
        if await self._done("facts"):
            return Loaded("facts", 0, "held")
        path = self._work_dir / Path(self._replay.companyfacts_zip_url).name
        await self._download(self._replay.companyfacts_zip_url, path)
        ciks = sorted(set((await self._listed(span)).values()))
        inputs = self._settings.sources.stocks.fundamentals.inputs
        member = self._replay.companyfacts_member_template
        rows = missing = 0
        with zipfile.ZipFile(path) as archive:
            held = set(archive.namelist())
            for cik in ciks:
                name = member.format(cik=cik)
                if name not in held:
                    missing += 1
                    continue
                body = json.loads(archive.read(name))
                facts = parse_companyfacts(body, cik=cik, inputs=inputs)
                rows += await self._worker.run(stocks_repo.save_facts, facts)
        await self._mark("facts", rows=rows)
        return Loaded("facts", rows, f"{missing} listed companies have no company facts")

    async def _stock_bars(self, span: Span) -> Loaded:
        """Yahoo's closes of every ticker a filing company has, in batches."""
        tickers = {n.ticker for n in (await self._worker.run(names)).values() if n.ticker}
        tickers |= set(await self._listed(span))
        config = self._settings.sources.stocks.bars
        wanted = [t for t in sorted(tickers) if not await self._done(f"bars:{t}")]
        rows = empty_names = 0
        for first in range(0, len(wanted), config.max_symbols_per_run):
            batch = wanted[first : first + config.max_symbols_per_run]
            answers: list[tuple[str, tuple[YahooBar, ...]]] = []
            for symbol in batch:
                try:
                    frame = await self._retry(
                        functools.partial(
                            asyncio.to_thread, self._yahoo, symbol, span.history_start, span.until
                        ),
                    )
                    answers.append((symbol, yahoo_sessions(frame, symbol)))
                except Exception as exc:  # one name never stops the others
                    await self._mark(f"bars:{symbol}", rows=0, ok=False, detail=failure_text(exc))
                await self._sleep(config.request_spacing_seconds)
            empty = [symbol for symbol, answer in answers if not answer]
            if answers and len(empty) / len(answers) > config.max_empty_share:
                raise DataSourceError(
                    f"throttled: {len(empty)} of {len(answers)} names answered nothing; nothing "
                    "of this batch is recorded (load again later)"
                )
            for symbol, answer in answers:
                bars = stock_bars(answer, symbol=symbol, until=span.until)
                saved = await self._worker.run(stocks_repo.save_bars, bars) if bars else 0
                await self._mark(
                    f"bars:{symbol}", rows=saved, detail=None if bars else "no history at Yahoo"
                )
                rows += saved
            empty_names += len(empty)
        return Loaded("bars", rows, f"{empty_names} names without a history at Yahoo")

    async def _instruments(self, span: Span) -> Loaded:
        """The universe instruments (and the equity reference), created at the history's start,
        and their closes."""
        yahoo = self._settings.research.data.yahoo
        at = datetime.combine(span.history_start, time.min, tzinfo=UTC)
        rows = skipped = 0
        for declared in self._settings.universe.instruments:
            instrument = universe_instrument(declared, at=at)
            target = f"instrument:{declared.symbol}"
            if instrument is None or await self._done(target):
                continue
            symbol = yahoo_symbol(
                declared.symbol, declared.listing_exchange, yahoo.suffixes, yahoo.digits
            )
            if symbol is None:
                skipped += 1
                await self._mark(target, rows=0, detail="no Yahoo symbol for its exchange")
                continue
            try:
                frame = await self._retry(
                    functools.partial(
                        asyncio.to_thread, self._yahoo, symbol, span.history_start, span.until
                    ),
                )
                answer = yahoo_sessions(frame, symbol)
            except Exception as exc:  # one instrument never stops the others
                await self._mark(target, rows=0, ok=False, detail=failure_text(exc))
                continue
            await self._worker.run(repos.upsert_instrument, instrument)
            bars = instrument_bars(instrument.conid, answer, until=span.until)
            saved = await self._worker.run(repos.upsert_bars, bars)
            await self._mark(target, rows=saved)
            rows += saved
            await self._sleep(self._settings.sources.stocks.bars.request_spacing_seconds)
        return Loaded("instruments", rows, f"{skipped} instruments without a Yahoo symbol")

    async def _fx(self, span: Span) -> Loaded:
        del span
        dataset = self._settings.research.dataset(self._replay.fx_dataset)
        if dataset is None:  # pragma: no cover - the configuration checks it
            raise DataSourceError(f"{self._replay.fx_dataset} is not a research dataset")
        loaded = await load_dataset(dataset, self._settings, self._research, now=self._now())
        rows = await self._worker.run(store_loaded, loaded)
        await self._mark("fx", rows=rows)
        return Loaded("fx", rows, f"{len(loaded)} rate series")


def http_download(
    client: httpx.AsyncClient, *, user_agent: str | None
) -> Download:  # pragma: no cover - network
    """A streaming download to a file, with the SEC's declared User-Agent."""

    async def download(url: str, path: Path) -> None:
        if user_agent is None:
            raise UpstreamError("SEC_USER_AGENT is unset; EDGAR refuses a request without one")
        partial = path.with_suffix(path.suffix + ".part")
        async with client.stream("GET", url, headers={"User-Agent": user_agent}) as response:
            if response.status_code != HTTP_OK:
                raise UpstreamError(f"{url} answered HTTP {response.status_code}")
            with partial.open("wb") as out:
                async for chunk in response.aiter_bytes(CHUNK):
                    out.write(chunk)
        partial.replace(path)

    return download
