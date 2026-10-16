"""Research history: loaders for the vendors the harness reads, conversion to the base currency,
and coverage and survivorship reports (MASTER_PLAN §5.9; DECISIONS D64).

Every series lands in the research store source-tagged (vendor, URL, download stamp, the SHA-256
of what was downloaded, how its availability dates were derived, its bias notes) and point in
time: each observation carries the date it became public, and every read is bounded by it.

- **Yahoo** end-of-day prices through ``yfinance``: the close, the dividend-adjusted close and
  the dividends per unit, each public at the close of its session. yfinance's frame is read
  through its iteration methods only, so no pandas type leaves this module.
- **Ken French** and **AQR** data libraries: monthly factor returns, revised without vintages,
  public a declared lag after the month they describe.
- **FRED** without vintages (public a declared lag after the observation date) and **ALFRED**
  with them (each value public from the first day of its real-time period).
- **ECB** euro reference rates (units of a currency per euro), public on their day.

Parsers are pure functions of the downloaded bytes and are tested on recorded fixtures; the
fetchers are the only code that touches the network.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import calendar
import csv
import hashlib
import io
import json
import math
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Protocol

import httpx

from ..config.schema import ObliskConfig
from ..config.schema_research import (
    AqrDataset,
    Dataset,
    EcbDataset,
    FredDataset,
    KenFrenchDataset,
    YahooDataset,
)
from ..domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from ..errors import DataSourceError, UpstreamError
from ..store import Store
from ..store.research import replace_research_series, research_observations
from ..util.http import RetryPolicy, get_with_retry
from .xlsx import excel_serial_date, read_sheet

#: A percent is a hundredth.
PERCENT: Final = 100.0
#: HTTP status of a successful download.
HTTP_OK: Final = 200
#: The ECB file's spelling of a rate it does not publish that day.
ECB_MISSING: Final = "N/A"
#: FRED's spelling of a missing observation.
FRED_MISSING: Final = "."
#: The ECB reference rates' own currency.
EURO: Final = "EUR"
#: Ken French's date stamps: YYYYMM for a month, YYYY for a year.
_MONTH_STAMP_LENGTH: Final = 6
_YEAR_STAMP_LENGTH: Final = 4

type Points = list[tuple[date, float]]


@dataclass(frozen=True, slots=True)
class LoadedSeries:
    """One series as a loader produced it, ready for the store."""

    series: ResearchSeries
    observations: tuple[Observation, ...]


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _year_end(year: int) -> date:
    return date(year + 1, 1, 1) - timedelta(days=1)


def _lagged(points: Points, lag_days: int) -> tuple[Observation, ...]:
    return tuple(
        Observation(observed_on=day, available_on=day + timedelta(days=lag_days), value=value)
        for day, value in points
    )


def _as_observed(points: Points) -> tuple[Observation, ...]:
    return _lagged(points, 0)


# ── parsers: pure functions of what was downloaded ──


def _zip_text(content: bytes, encoding: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if len(names) != 1:
                raise DataSourceError(f"expected one file in the archive, found {names}")
            return archive.read(names[0]).decode(encoding)
    except zipfile.BadZipFile:
        raise DataSourceError("the download is not a zip archive") from None


def _number(text: str, where: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise DataSourceError(f"{where}: {text!r} is not a number") from None
    if not math.isfinite(value):
        raise DataSourceError(f"{where}: {text!r} is not a finite number")
    return value


def parse_ken_french(
    content: bytes, dataset: KenFrenchDataset, missing_markers: Sequence[str]
) -> dict[str, Points]:
    """The columns of one table of a Ken French CSV archive, in decimals.

    A table starts at a header row that begins with a comma and runs to the next blank line.
    ``section`` counts tables from 0; a stamp of six digits is a month (its last day), of four
    a year (31 December). Values are percent and become decimals; the library's own missing
    markers are left out.
    """
    lines = _zip_text(content, "latin-1").splitlines()
    tables: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith(","):
            current = [line]
            tables.append(current)
        elif current is not None and line.strip():
            current.append(line)
        else:
            current = None
    if dataset.section >= len(tables):
        raise DataSourceError(
            f"{dataset.file} has {len(tables)} table(s); section {dataset.section} is missing"
        )
    header, *rows = tables[dataset.section]
    names = [name.strip() for name in header.split(",")[1:]]
    wanted = names if dataset.columns is None else dataset.columns
    unknown = sorted(set(wanted) - set(names))
    if unknown:
        raise DataSourceError(f"{dataset.file} has no column(s) {unknown} (columns: {names})")
    expected = _MONTH_STAMP_LENGTH if dataset.frequency == "monthly" else _YEAR_STAMP_LENGTH
    out: dict[str, Points] = {name: [] for name in wanted}
    for row in rows:
        stamp, *cells = (cell.strip() for cell in row.split(","))
        if len(stamp) != expected or not stamp.isdigit():
            raise DataSourceError(f"{dataset.file}: {stamp!r} is not a {dataset.frequency} stamp")
        day = (
            _month_end(int(stamp[:_YEAR_STAMP_LENGTH]), int(stamp[_YEAR_STAMP_LENGTH:]))
            if dataset.frequency == "monthly"
            else _year_end(int(stamp))
        )
        if len(cells) != len(names):
            raise DataSourceError(f"{dataset.file}: row {stamp} has {len(cells)} values")
        for name, cell in zip(names, cells, strict=True):
            if name in out and cell not in missing_markers:
                out[name].append((day, _number(cell, f"{dataset.file} {stamp} {name}") / PERCENT))
    return out


def parse_aqr(content: bytes, dataset: AqrDataset) -> dict[str, Points]:
    """The named columns of an AQR workbook sheet: the header row names them, column A dates
    them (Excel serials), and every row below the header with a date is an observation."""
    sheet = read_sheet(content, dataset.sheet)
    header = sheet.get(dataset.header_row, {})
    positions = {str(value).strip(): letters for letters, value in header.items()}
    unknown = sorted(set(dataset.columns) - set(positions))
    if unknown:
        raise DataSourceError(
            f"{dataset.file} row {dataset.header_row} has no column(s) {unknown} "
            f"(header: {sorted(positions)})"
        )
    scale = PERCENT if dataset.units == "percent" else 1.0
    out: dict[str, Points] = {name: [] for name in dataset.columns}
    for number in sorted(n for n in sheet if n > dataset.header_row):
        row = sheet[number]
        stamp = row.get("A")
        if stamp is None:
            continue
        if not isinstance(stamp, float):
            raise DataSourceError(f"{dataset.file} row {number}: date {stamp!r} is not a serial")
        day = excel_serial_date(stamp)
        for name in dataset.columns:
            cell = row.get(positions[name])
            if cell is None:
                continue
            if not isinstance(cell, float):
                raise DataSourceError(f"{dataset.file} row {number} {name}: {cell!r}")
            out[name].append((day, cell / scale))
    return out


def parse_fred_csv(content: bytes, series: str) -> Points:
    """FRED's CSV (``observation_date,<series>``); ``.`` marks a missing value."""
    reader = csv.reader(io.StringIO(content.decode("utf-8")))
    header = next(reader, None)
    if header is None or header[1:] != [series]:
        raise DataSourceError(f"FRED's CSV for {series} has an unexpected header: {header}")
    points: Points = []
    for row in reader:
        if not row:
            continue
        stamp, value = row
        if value == FRED_MISSING:
            continue
        points.append((_iso_date(stamp, "FRED"), _number(value, f"FRED {series} {stamp}")))
    return points


def parse_alfred(content: bytes, series: str) -> tuple[Observation, ...]:
    """ALFRED's observations with every vintage: each value public from its ``realtime_start``."""
    try:
        payload = json.loads(content)
        rows = payload["observations"]
    except ValueError, KeyError, TypeError:
        raise DataSourceError(f"ALFRED's answer for {series} has no observations") from None
    out: list[Observation] = []
    for row in rows:
        if row.get("value") == FRED_MISSING:
            continue
        try:
            observed = _iso_date(str(row["date"]), "ALFRED")
            available = _iso_date(str(row["realtime_start"]), "ALFRED")
            value = _number(str(row["value"]), f"ALFRED {series} {row['date']}")
        except KeyError as exc:
            raise DataSourceError(f"an ALFRED observation of {series} lacks {exc}") from None
        out.append(
            Observation(observed_on=observed, available_on=max(available, observed), value=value)
        )
    return tuple(out)


def parse_ecb(content: bytes, currencies: Sequence[str]) -> dict[str, Points]:
    """The ECB's historical reference rates: units of each currency per euro, by day."""
    reader = csv.reader(io.StringIO(_zip_text(content, "utf-8")))
    header = [name.strip() for name in next(reader, [])]
    missing = sorted(set(currencies) - set(header))
    if not header or header[0] != "Date" or missing:
        raise DataSourceError(f"the ECB file lacks Date or currencies {missing}")
    positions = {name: header.index(name) for name in currencies}
    out: dict[str, Points] = {name: [] for name in currencies}
    for row in reader:
        if not row or not row[0].strip():
            continue
        day = _iso_date(row[0].strip(), "ECB")
        for name, position in positions.items():
            cell = row[position].strip() if position < len(row) else ""
            if cell and cell != ECB_MISSING:
                out[name].append((day, _number(cell, f"ECB {name} {day}")))
    return {name: sorted(points) for name, points in out.items()}


def _iso_date(text: str, where: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise DataSourceError(f"{where}: {text!r} is not a YYYY-MM-DD date") from None


# ── Yahoo through yfinance ──


@dataclass(frozen=True, slots=True)
class YahooBar:
    """One session of a Yahoo history: the close, the adjusted close and the dividend."""

    session: date
    close: float
    adj_close: float
    dividend: float


class _Stamp(Protocol):
    def date(self) -> date: ...


class _Row(Protocol):
    def __getitem__(self, key: str) -> Any: ...


class YahooFrame(Protocol):
    """What the loader reads of yfinance's history frame: its rows, in order."""

    def iterrows(self) -> Iterable[tuple[_Stamp, _Row]]: ...


#: yfinance's column names (``Ticker.history(auto_adjust=False, actions=True)``).
YAHOO_CLOSE: Final = "Close"
YAHOO_ADJ_CLOSE: Final = "Adj Close"
YAHOO_DIVIDENDS: Final = "Dividends"


def yahoo_bars(frame: YahooFrame, symbol: str) -> tuple[YahooBar, ...]:
    """The sessions of a yfinance history frame; the stamp's own date is the session."""
    bars: list[YahooBar] = []
    for stamp, row in frame.iterrows():
        try:
            close = float(row[YAHOO_CLOSE])
            adjusted = float(row[YAHOO_ADJ_CLOSE])
            dividend = float(row[YAHOO_DIVIDENDS])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataSourceError(f"Yahoo's history for {symbol} lacks a column: {exc}") from None
        session = stamp.date()
        if math.isnan(close) or math.isnan(adjusted):  # a session with no price
            continue
        if close <= 0 or adjusted <= 0 or dividend < 0:
            raise DataSourceError(f"Yahoo's history for {symbol} on {session} is not a price")
        bars.append(YahooBar(session, close, adjusted, dividend))
    if not bars:
        raise DataSourceError(f"Yahoo returned no prices for {symbol}")
    return tuple(sorted(bars, key=lambda bar: bar.session))


def _yahoo_content(bars: Sequence[YahooBar]) -> bytes:
    """A canonical text of the bars, hashed as the download's content."""
    return "\n".join(
        f"{b.session.isoformat()},{b.close!r},{b.adj_close!r},{b.dividend!r}" for b in bars
    ).encode()


class YahooHistory(Protocol):
    def __call__(self, symbol: str, start: date, end: date) -> YahooFrame: ...


def yfinance_history(symbol: str, start: date, end: date) -> YahooFrame:  # pragma: no cover
    """The daily history yfinance serves (network; the loader runs it in a worker thread)."""
    import yfinance  # imported where the network is touched

    frame: YahooFrame = yfinance.Ticker(symbol).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=True,
    )
    return frame


# ── fetching and loading ──


type Fetch = Callable[[str, Mapping[str, str] | None], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class Sources:
    """Where the loaders get their bytes: an HTTP fetch and Yahoo's history (both injectable)."""

    fetch: Fetch
    yahoo: YahooHistory
    fred_api_key: str | None


def http_fetch(client: httpx.AsyncClient, policy: RetryPolicy) -> Fetch:
    """A fetch over ``client`` with the shared retry policy; any status but 200 fails."""

    async def fetch(url: str, params: Mapping[str, str] | None) -> bytes:
        try:
            response = await get_with_retry(client, url, policy=policy, params=params)
        except UpstreamError:
            # The transport error may quote the request, query string and key included.
            raise DataSourceError(f"{url} could not be reached") from None
        if response.status_code != HTTP_OK:
            raise DataSourceError(f"{url} answered HTTP {response.status_code}")
        return response.content

    return fetch


def _series(
    dataset: Dataset,
    *,
    vendor: Vendor,
    field: str,
    units: str,
    currency: str | None,
    frequency: Frequency,
    point_in_time: PointInTime,
    url: str,
    downloaded_at: datetime,
    content: bytes,
    notes: Sequence[str],
) -> ResearchSeries:
    return ResearchSeries(
        series_id=f"{dataset.id}.{field}",
        dataset_id=dataset.id,
        vendor=vendor,
        field=field,
        units=units,
        currency=currency,
        frequency=frequency,
        point_in_time=point_in_time,
        source_url=url,
        downloaded_at=downloaded_at,
        content_sha256=sha256(content),
        bias_notes=(*notes, *dataset.bias_notes),
    )


async def load_dataset(
    dataset: Dataset, settings: ObliskConfig, sources: Sources, *, now: datetime
) -> tuple[LoadedSeries, ...]:
    """Download ``dataset`` and turn it into series. Nothing is stored here."""
    data = settings.research.data
    if isinstance(dataset, YahooDataset):
        return await _load_yahoo(dataset, settings, sources, now)
    if isinstance(dataset, KenFrenchDataset):
        url = f"{data.ken_french.base_url}{dataset.file}"
        content = await sources.fetch(url, None)
        columns = parse_ken_french(content, dataset, data.ken_french.missing_markers)
        frequency = Frequency.MONTHLY if dataset.frequency == "monthly" else Frequency.ANNUAL
        return _factor_series(
            dataset,
            columns,
            Vendor.KEN_FRENCH,
            frequency,
            url,
            now,
            content,
            data.ken_french.bias_notes,
            dataset.release_lag_days,
        )
    if isinstance(dataset, AqrDataset):
        url = f"{data.aqr.base_url}{dataset.file}"
        content = await sources.fetch(url, None)
        columns = parse_aqr(content, dataset)
        return _factor_series(
            dataset,
            columns,
            Vendor.AQR,
            Frequency.MONTHLY,
            url,
            now,
            content,
            data.aqr.bias_notes,
            dataset.release_lag_days,
        )
    if isinstance(dataset, FredDataset):
        return await _load_fred(dataset, settings, sources, now)
    return await _load_ecb(dataset, settings, sources, now)


def _factor_series(
    dataset: Dataset,
    columns: Mapping[str, Points],
    vendor: Vendor,
    frequency: Frequency,
    url: str,
    now: datetime,
    content: bytes,
    notes: Sequence[str],
    lag: int,
) -> tuple[LoadedSeries, ...]:
    out: list[LoadedSeries] = []
    for name, points in columns.items():
        if not points:
            raise DataSourceError(f"{dataset.id}: column {name} has no values")
        out.append(
            LoadedSeries(
                _series(
                    dataset,
                    vendor=vendor,
                    field=name,
                    units="decimal_return",
                    currency=None,
                    frequency=frequency,
                    point_in_time=PointInTime.RELEASE_LAG,
                    url=url,
                    downloaded_at=now,
                    content=content,
                    notes=(*notes, f"Public {lag} days after the period it describes (assumed)."),
                ),
                _lagged(points, lag),
            )
        )
    return tuple(out)


async def _load_yahoo(
    dataset: YahooDataset, settings: ObliskConfig, sources: Sources, now: datetime
) -> tuple[LoadedSeries, ...]:
    frame = await asyncio.to_thread(sources.yahoo, dataset.symbol, dataset.start, now.date())
    bars = yahoo_bars(frame, dataset.symbol)
    content = _yahoo_content(bars)
    url = f"yfinance Ticker({dataset.symbol!r}).history(auto_adjust=False, actions=True)"
    notes = settings.research.data.yahoo.bias_notes

    def make(field: str, units: str, points: Points, extra: str) -> LoadedSeries:
        return LoadedSeries(
            _series(
                dataset,
                vendor=Vendor.YAHOO,
                field=field,
                units=units,
                currency=dataset.currency,
                frequency=Frequency.DAILY,
                point_in_time=PointInTime.AS_OBSERVED,
                url=url,
                downloaded_at=now,
                content=content,
                notes=(*notes, extra),
            ),
            _as_observed(points),
        )

    out = [
        make("close", "price", [(b.session, b.close) for b in bars], "The session close."),
        make(
            "adj_close",
            "price",
            [(b.session, b.adj_close) for b in bars],
            "Adjusted backwards for dividends: its level is not what traded; use its returns.",
        ),
    ]
    dividends = [(b.session, b.dividend) for b in bars if b.dividend > 0]
    if dividends:
        out.append(make("dividend", "per_unit", dividends, "Dividends per unit on the ex-date."))
    return tuple(out)


async def _load_fred(
    dataset: FredDataset, settings: ObliskConfig, sources: Sources, now: datetime
) -> tuple[LoadedSeries, ...]:
    fred = settings.research.data.fred
    frequency = Frequency(dataset.frequency)
    if dataset.vintages:
        if sources.fred_api_key is None:
            raise DataSourceError(f"{dataset.id} reads ALFRED's vintages and needs FRED_API_KEY")
        params = {
            "series_id": dataset.series,
            "file_type": "json",
            "observation_start": dataset.start.isoformat(),
            "realtime_start": fred.realtime_start.isoformat(),
            "realtime_end": fred.realtime_end.isoformat(),
            "api_key": sources.fred_api_key,
        }
        content = await sources.fetch(fred.api_url, params)
        observations = parse_alfred(content, dataset.series)
        if not observations:
            raise DataSourceError(f"ALFRED returned no observations for {dataset.series}")
        series = _series(
            dataset,
            vendor=Vendor.ALFRED,
            field="value",
            units=dataset.units,
            currency=None,
            frequency=frequency,
            point_in_time=PointInTime.VINTAGES,
            url=f"{fred.api_url}?series_id={dataset.series}",
            downloaded_at=now,
            content=content,
            notes=fred.bias_notes,
        )
        return (LoadedSeries(series, observations),)
    content = await sources.fetch(
        fred.csv_url, {"id": dataset.series, "cosd": dataset.start.isoformat()}
    )
    points = parse_fred_csv(content, dataset.series)
    if not points:
        raise DataSourceError(f"FRED returned no observations for {dataset.series}")
    lag = dataset.release_lag_days
    series = _series(
        dataset,
        vendor=Vendor.FRED,
        field="value",
        units=dataset.units,
        currency=None,
        frequency=frequency,
        point_in_time=PointInTime.RELEASE_LAG,
        url=f"{fred.csv_url}?id={dataset.series}",
        downloaded_at=now,
        content=content,
        notes=(*fred.bias_notes, f"Public {lag} days after the observation date (assumed)."),
    )
    return (LoadedSeries(series, _lagged(points, lag)),)


async def _load_ecb(
    dataset: EcbDataset, settings: ObliskConfig, sources: Sources, now: datetime
) -> tuple[LoadedSeries, ...]:
    ecb = settings.research.data.ecb
    content = await sources.fetch(ecb.url, None)
    columns = parse_ecb(content, dataset.currencies)
    out: list[LoadedSeries] = []
    for currency, points in columns.items():
        if not points:
            raise DataSourceError(f"the ECB file has no {currency} rates")
        series = _series(
            dataset,
            vendor=Vendor.ECB,
            field=currency,
            units=f"{currency} per {EURO}",
            currency=currency,
            frequency=Frequency.DAILY,
            point_in_time=PointInTime.AS_OBSERVED,
            url=ecb.url,
            downloaded_at=now,
            content=content,
            notes=ecb.bias_notes,
        )
        out.append(LoadedSeries(series, _as_observed(points)))
    return tuple(out)


def store_loaded(store: Store, loaded: Iterable[LoadedSeries]) -> int:
    """Replace each loaded series in the research store; the number of rows written."""
    return sum(replace_research_series(store, item.series, item.observations) for item in loaded)


# ── conversion to the base currency ──


class FxRates:
    """ECB reference rates by currency, read as of a date: units of the currency per euro."""

    def __init__(self, rates: Mapping[str, Sequence[tuple[date, float]]], *, max_age_days: int):
        self._rates = {currency: sorted(points) for currency, points in rates.items()}
        self._max_age = timedelta(days=max_age_days)

    @classmethod
    def from_store(
        cls,
        store: Store,
        dataset_id: str,
        currencies: Iterable[str],
        *,
        as_of: date,
        max_age_days: int,
    ) -> FxRates:
        rates = {
            currency: [
                (o.observed_on, o.value)
                for o in research_observations(store, f"{dataset_id}.{currency}", as_of=as_of)
            ]
            for currency in currencies
            if currency != EURO
        }
        return cls(rates, max_age_days=max_age_days)

    def per_euro(self, currency: str, on: date) -> float | None:
        """Units of ``currency`` per euro on ``on``: the latest rate not older than allowed."""
        if currency == EURO:
            return 1.0
        latest = None
        for day, value in self._rates.get(currency, []):
            if day > on:
                break
            latest = (day, value)
        if latest is None or on - latest[0] > self._max_age:
            return None
        return latest[1]

    def to_base(self, amount: float, currency: str, base: str, on: date) -> float | None:
        """``amount`` in ``currency`` expressed in ``base``; ``None`` without a fresh rate."""
        if currency == base:
            return amount
        local = self.per_euro(currency, on)
        target = self.per_euro(base, on)
        if local is None or target is None:
            return None
        return amount * target / local

    def convert(self, points: Points, currency: str, base: str) -> Points:
        """A series in ``base``; a day without a fresh rate is left out, never interpolated."""
        out: Points = []
        for day, value in points:
            converted = self.to_base(value, currency, base, day)
            if converted is not None:
                out.append((day, converted))
        return out


# ── coverage and survivorship ──


@dataclass(frozen=True, slots=True)
class YearCoverage:
    year: int
    members: int
    missing: tuple[str, ...]

    @property
    def missing_share(self) -> float:
        return len(self.missing) / self.members if self.members else 1.0


@dataclass(frozen=True, slots=True)
class MemberSpan:
    """A universe member's first and last observation public by ``as_of`` (``None``: no data)."""

    series_id: str
    first: date | None
    last: date | None


@dataclass(frozen=True, slots=True)
class SurvivorshipReport:
    """Who enters late, who ends early and who has nothing, over ``start`` to ``end``.

    A universe listed today cannot show members that disappeared before it was listed; the
    report measures what the list can show and says so.
    """

    start: date
    end: date
    spans: tuple[MemberSpan, ...]

    @property
    def without_data(self) -> tuple[str, ...]:
        return tuple(s.series_id for s in self.spans if s.first is None)

    @property
    def late_entries(self) -> tuple[str, ...]:
        return tuple(
            s.series_id for s in self.spans if s.first is not None and s.first > self.start
        )

    @property
    def early_exits(self) -> tuple[str, ...]:
        return tuple(s.series_id for s in self.spans if s.last is not None and s.last < self.end)


def coverage_by_year(
    store: Store, series_ids: Sequence[str], *, first_year: int, last_year: int, as_of: date
) -> tuple[YearCoverage, ...]:
    """For each year, the members with no observation in it public by the year's end (or by
    ``as_of`` when that is earlier)."""
    histories = {
        series_id: research_observations(store, series_id, as_of=as_of) for series_id in series_ids
    }
    out: list[YearCoverage] = []
    for year in range(first_year, last_year + 1):
        cutoff = min(_year_end(year), as_of)
        missing = tuple(
            series_id
            for series_id, rows in histories.items()
            if not any(o.observed_on.year == year and o.available_on <= cutoff for o in rows)
        )
        out.append(YearCoverage(year, len(series_ids), missing))
    return tuple(out)


def survivorship(
    store: Store, series_ids: Sequence[str], *, start: date, end: date, as_of: date
) -> SurvivorshipReport:
    spans: list[MemberSpan] = []
    for series_id in series_ids:
        rows = research_observations(store, series_id, as_of=as_of)
        spans.append(
            MemberSpan(
                series_id,
                rows[0].observed_on if rows else None,
                rows[-1].observed_on if rows else None,
            )
        )
    return SurvivorshipReport(start, end, tuple(spans))
