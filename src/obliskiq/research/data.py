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
- **MSCI** daily index levels, public at the end of each calculation day.
- **ECB Data Portal** and **Bundesbank** daily series by dataflow and key (money-market rates,
  zero-coupon yields), public on their day.
- **Derived** series computed from stored ones: an index accruing overnight rates, and the index
  of a zero-coupon bond held at a constant maturity. Each value is public when every input it
  used was.

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
import re
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final, Protocol, TypeIs

import httpx

from ..config.schema import ObliskConfig
from ..config.schema_research import (
    AqrDataset,
    BundesbankDataset,
    ConstantMaturityZeroDataset,
    Dataset,
    EcbDataDataset,
    EcbDataset,
    FredDataset,
    KenFrenchDataset,
    MsciDataset,
    OvernightAccrualDataset,
    YahooDataset,
)
from ..domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from ..errors import DataSourceError, UpstreamError
from ..store import Store
from ..store.research import (
    replace_research_series,
    research_observations,
    research_series_info,
)
from ..util.http import RetryPolicy, get_with_retry
from .xlsx import excel_serial_date, read_sheet

#: A percent is a hundredth.
PERCENT: Final = 100.0
PERCENT_DECIMAL: Final = Decimal(100)
#: The fewest days a derived bond index is computed from (one day has no return).
MIN_DERIVED_DAYS: Final = 2
#: HTTP status of a successful download.
HTTP_OK: Final = 200
#: The ECB file's spelling of a rate it does not publish that day.
ECB_MISSING: Final = "N/A"
#: FRED's spelling of a missing observation.
FRED_MISSING: Final = "."
#: The ECB reference rates' own currency.
EURO: Final = "EUR"
#: Ken French's date stamps: YYYYMMDD for a day, YYYYMM for a month, YYYY for a year.
_DAY_STAMP_LENGTH: Final = 8
_MONTH_STAMP_LENGTH: Final = 6
_YEAR_STAMP_LENGTH: Final = 4
#: AQR's text dates, month/day/year (``07/30/1926``).
_MONTH_DAY_YEAR: Final = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
#: The Bundesbank's spelling of a day without a value.
BUNDESBANK_MISSING: Final = "."
#: A row of a daily series opens with its day.
_ISO_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
#: MSCI stamps a calculation day as the integer YYYYMMDD.
_MSCI_YEAR_SCALE: Final = 10_000
_MSCI_MONTH_SCALE: Final = 100

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
    ``section`` counts tables from 0; a stamp of eight digits is a day, of six a month (its last
    day), of four a year (31 December). Values are percent and become decimals; the library's
    own missing markers are left out.
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
    expected = {
        "daily": _DAY_STAMP_LENGTH,
        "monthly": _MONTH_STAMP_LENGTH,
        "annual": _YEAR_STAMP_LENGTH,
    }[dataset.frequency]
    out: dict[str, Points] = {name: [] for name in wanted}
    for row in rows:
        stamp, *cells = (cell.strip() for cell in row.split(","))
        if len(stamp) != expected or not stamp.isdigit():
            raise DataSourceError(f"{dataset.file}: {stamp!r} is not a {dataset.frequency} stamp")
        day = _stamp_day(stamp, dataset)
        if len(cells) != len(names):
            raise DataSourceError(f"{dataset.file}: row {stamp} has {len(cells)} values")
        for name, cell in zip(names, cells, strict=True):
            if name in out and cell not in missing_markers:
                out[name].append((day, _number(cell, f"{dataset.file} {stamp} {name}") / PERCENT))
    return out


def _stamp_day(stamp: str, dataset: KenFrenchDataset) -> date:
    """The day a Ken French stamp of the dataset's frequency names."""
    year = int(stamp[:_YEAR_STAMP_LENGTH])
    if dataset.frequency == "annual":
        return _year_end(year)
    month = int(stamp[_YEAR_STAMP_LENGTH:_MONTH_STAMP_LENGTH])
    try:
        if dataset.frequency == "monthly":
            return _month_end(year, month)
        return date(year, month, int(stamp[_MONTH_STAMP_LENGTH:]))
    except ValueError:  # calendar.monthrange and date refuse a month or day that does not exist
        raise DataSourceError(f"{dataset.file}: {stamp!r} is not a date") from None


def _aqr_day(stamp: object, dataset: AqrDataset, number: int) -> date | None:
    """The date in column A of a row, in the dataset's format; ``None`` for a row without one."""
    if stamp is None or stamp == "":
        return None
    if dataset.date_format == "excel_serial":
        if not isinstance(stamp, float):
            raise DataSourceError(f"{dataset.file} row {number}: date {stamp!r} is not a serial")
        return excel_serial_date(stamp)
    found = _MONTH_DAY_YEAR.fullmatch(str(stamp).strip())
    if found is None:
        raise DataSourceError(f"{dataset.file} row {number}: {stamp!r} is not month/day/year")
    month, day, year = (int(part) for part in found.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise DataSourceError(f"{dataset.file} row {number}: {stamp!r} is not a date") from None


def parse_aqr(content: bytes, dataset: AqrDataset) -> dict[str, Points]:
    """The named columns of an AQR workbook sheet: the header row names them, column A dates
    them (Excel serials or month/day/year text, as the dataset says), and every row below the
    header with a date is an observation; an empty cell is a missing value."""
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
        day = _aqr_day(row.get("A"), dataset, number)
        if day is None:
            continue
        for name in dataset.columns:
            cell = row.get(positions[name])
            if cell is None or cell == "":
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


def parse_msci(content: bytes, dataset: MsciDataset) -> Points:
    """MSCI's index levels by calculation day; an answer that names an error is refused."""
    try:
        payload = json.loads(content)
    except ValueError:
        raise DataSourceError(f"MSCI's answer for {dataset.id} is not JSON") from None
    if not isinstance(payload, dict):
        raise DataSourceError(f"MSCI's answer for {dataset.id} is not an object")
    if "error_message" in payload or "error_code" in payload:
        message = str(payload.get("error_message", "")).strip()
        raise DataSourceError(f"MSCI refused {dataset.id}: {message}")
    echo = (
        payload.get("msci_index_code"),
        payload.get("index_variant_type"),
        payload.get("ISO_currency_symbol"),
    )
    if echo != (dataset.index_code, dataset.variant, dataset.currency):
        raise DataSourceError(f"MSCI answered for {echo}, not {dataset.id}'s index")
    try:
        rows = payload["indexes"]["INDEX_LEVELS"]
    except KeyError, TypeError:
        raise DataSourceError(f"MSCI's answer for {dataset.id} has no levels") from None
    points: Points = []
    for row in rows:
        try:
            stamp = int(row["calc_date"])
            level = _number(str(row["level_eod"]), f"MSCI {dataset.id} {stamp}")
        except KeyError, TypeError, ValueError:
            raise DataSourceError(f"an MSCI level of {dataset.id} is malformed: {row}") from None
        try:
            day = date(
                stamp // _MSCI_YEAR_SCALE,
                stamp // _MSCI_MONTH_SCALE % _MSCI_MONTH_SCALE,
                stamp % _MSCI_MONTH_SCALE,
            )
        except ValueError:
            raise DataSourceError(f"MSCI {dataset.id}: {stamp} is not a date") from None
        if level <= 0:
            raise DataSourceError(f"MSCI {dataset.id} on {day}: {level} is not a level")
        points.append((day, level))
    return sorted(points)


def parse_ecb_data(content: bytes, flow: str, key: str) -> Points:
    """An ECB Data Portal CSV (``format=csvdata``): one row per period, ``TIME_PERIOD`` and
    ``OBS_VALUE``; an empty value is left out."""
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    fields = reader.fieldnames or []
    if not {"KEY", "TIME_PERIOD", "OBS_VALUE"} <= set(fields):
        raise DataSourceError(f"the ECB Data Portal's CSV for {flow}.{key} lacks columns: {fields}")
    points: Points = []
    for row in reader:
        if row["KEY"] != f"{flow}.{key}":
            raise DataSourceError(f"the ECB Data Portal answered {row['KEY']}, not {flow}.{key}")
        value = (row["OBS_VALUE"] or "").strip()
        if not value:
            continue
        day = _iso_date(row["TIME_PERIOD"].strip(), "ECB Data Portal")
        points.append((day, _number(value, f"ECB Data Portal {flow}.{key} {day}")))
    return sorted(points)


def parse_bundesbank(content: bytes, flow: str, key: str) -> Points:
    """A Bundesbank CSV (``format=csv``): a header naming the series, metadata rows, then one row
    per day (``YYYY-MM-DD,value,flag``); ``.`` marks a day without a value."""
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    if not rows or len(rows[0]) < 2 or rows[0][1] != f"{flow}.{key}":
        header = rows[0] if rows else []
        raise DataSourceError(f"the Bundesbank's CSV is not {flow}.{key}: header {header}")
    points: Points = []
    for row in rows[1:]:
        if not row or not _ISO_DAY.fullmatch(row[0]):
            continue  # a metadata row
        if len(row) < 2:
            raise DataSourceError(f"the Bundesbank's row {row} has no value")
        day = _iso_date(row[0], "Bundesbank")
        if row[1].strip() == BUNDESBANK_MISSING:
            continue
        points.append((day, _number(row[1], f"Bundesbank {flow}.{key} {day}")))
    return sorted(points)


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
        frequency = Frequency(dataset.frequency)
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
    if isinstance(dataset, MsciDataset):
        return await _load_msci(dataset, settings, sources, now)
    if isinstance(dataset, EcbDataDataset | BundesbankDataset):
        return await _load_keyed(dataset, settings, sources, now)
    if isinstance(dataset, OvernightAccrualDataset | ConstantMaturityZeroDataset):
        raise DataSourceError(
            f"{dataset.id} is derived from stored series: it is computed, not downloaded"
        )
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


async def _load_msci(
    dataset: MsciDataset, settings: ObliskConfig, sources: Sources, now: datetime
) -> tuple[LoadedSeries, ...]:
    msci = settings.research.data.msci
    start = max(dataset.start, msci.earliest_start)
    params = {
        "currency_symbol": dataset.currency,
        "index_variant": dataset.variant,
        "start_date": start.strftime("%Y%m%d"),
        "end_date": now.date().strftime("%Y%m%d"),
        "data_frequency": "DAILY",
        "index_codes": dataset.index_code,
    }
    content = await sources.fetch(msci.url, params)
    points = parse_msci(content, dataset)
    if not points:
        raise DataSourceError(f"MSCI returned no levels for {dataset.id}")
    series = _series(
        dataset,
        vendor=Vendor.MSCI,
        field="level",
        units="index level",
        currency=dataset.currency,
        frequency=Frequency.DAILY,
        point_in_time=PointInTime.AS_OBSERVED,
        url=f"{msci.url}?index_codes={dataset.index_code}&index_variant={dataset.variant}"
        f"&currency_symbol={dataset.currency}",
        downloaded_at=now,
        content=content,
        notes=msci.bias_notes,
    )
    return (LoadedSeries(series, _as_observed(points)),)


async def _load_keyed(
    dataset: EcbDataDataset | BundesbankDataset,
    settings: ObliskConfig,
    sources: Sources,
    now: datetime,
) -> tuple[LoadedSeries, ...]:
    """One series by dataflow and key, from the ECB Data Portal or the Bundesbank."""
    data = settings.research.data
    if isinstance(dataset, EcbDataDataset):
        vendor, base, notes = Vendor.ECB_DATA, data.ecb_data.base_url, data.ecb_data.bias_notes
        params = {"format": "csvdata", "startPeriod": dataset.start.isoformat()}
    else:
        vendor, base, notes = (
            Vendor.BUNDESBANK,
            data.bundesbank.base_url,
            data.bundesbank.bias_notes,
        )
        params = {"format": "csv", "lang": "en", "startPeriod": dataset.start.isoformat()}
    url = f"{base}/{dataset.flow}/{dataset.key}"
    content = await sources.fetch(url, params)
    parse = parse_ecb_data if vendor is Vendor.ECB_DATA else parse_bundesbank
    points = parse(content, dataset.flow, dataset.key)
    if not points:
        raise DataSourceError(f"{url} returned no values for {dataset.id}")
    series = _series(
        dataset,
        vendor=vendor,
        field="value",
        units=dataset.units,
        currency=None,
        frequency=Frequency.DAILY,
        point_in_time=PointInTime.AS_OBSERVED,
        url=url,
        downloaded_at=now,
        content=content,
        notes=notes,
    )
    return (LoadedSeries(series, _as_observed(points)),)


# ── derived series ──


type DerivedDataset = OvernightAccrualDataset | ConstantMaturityZeroDataset


def is_derived(dataset: Dataset) -> TypeIs[DerivedDataset]:
    """Whether ``dataset`` is computed from stored series rather than downloaded."""
    return isinstance(dataset, OvernightAccrualDataset | ConstantMaturityZeroDataset)


@dataclass(frozen=True, slots=True)
class _Input:
    series: ResearchSeries
    rows: dict[date, Observation]


def _input(store: Store, series_id: str, as_of: date, dataset_id: str) -> _Input:
    info = research_series_info(store, series_id)
    if info is None:
        raise DataSourceError(f"{dataset_id} needs {series_id}: load it first")
    rows = research_observations(store, series_id, as_of=as_of)
    if not rows:
        raise DataSourceError(f"{dataset_id} needs {series_id}, which has no rows")
    return _Input(info, {row.observed_on: row for row in rows})


def _accrual(dataset: OvernightAccrualDataset, inputs: Sequence[_Input]) -> list[Observation]:
    """Each published day's rate accrues until the next published day."""
    rates: list[Observation] = []
    since: date | None = None
    for segment, found in zip(dataset.segments, inputs, strict=True):
        rates += [
            row
            for day, row in sorted(found.rows.items())
            if (since is None or day > since)
            and (segment.through is None or day <= segment.through)
        ]
        since = segment.through
    if not rates:
        raise DataSourceError(f"{dataset.id} has no rate in its segments")
    basis = Decimal(dataset.day_count_basis)
    level = dataset.base_level
    out = [
        Observation(
            observed_on=rates[0].observed_on, available_on=rates[0].available_on,
            value=float(level),
        )
    ]  # fmt: skip
    for previous, current in pairwise(rates):
        days = Decimal((current.observed_on - previous.observed_on).days)
        level *= 1 + Decimal(repr(previous.value)) / PERCENT_DECIMAL * days / basis
        out.append(
            Observation(
                observed_on=current.observed_on,
                available_on=max(current.observed_on, previous.available_on),
                value=float(level),
            )
        )
    return out


def _zero_coupon(
    dataset: ConstantMaturityZeroDataset, long: _Input, short: _Input
) -> list[Observation]:
    """A zero bought at ``maturity_years`` on each day the curve has both maturities and sold on
    the next at the yield for the maturity left (linear in maturity between the two)."""
    days = sorted(set(long.rows) & set(short.rows))
    if len(days) < MIN_DERIVED_DAYS:
        raise DataSourceError(f"{dataset.id} needs at least {MIN_DERIVED_DAYS} common days")
    maturity = float(dataset.maturity_years)
    gap = maturity - float(dataset.shorter_maturity_years)
    basis = float(dataset.year_basis_days)

    def log_price(rate_percent: float, years: float) -> float:
        rate = rate_percent / PERCENT
        if dataset.compounding == "continuous":
            return -years * rate
        return -years * math.log1p(rate)

    log_level = math.log(float(dataset.base_level))
    first = days[0]
    out = [
        Observation(
            observed_on=first,
            available_on=max(long.rows[first].available_on, short.rows[first].available_on),
            value=float(dataset.base_level),
        )
    ]
    for previous, current in pairwise(days):
        held = (current - previous).days / basis
        left = maturity - held
        today_long = long.rows[current].value
        today_short = short.rows[current].value
        rate_left = today_long - held / gap * (today_long - today_short)
        log_level += log_price(rate_left, left) - log_price(long.rows[previous].value, maturity)
        available = max(current, long.rows[current].available_on, short.rows[current].available_on)
        out.append(
            Observation(observed_on=current, available_on=available, value=math.exp(log_level))
        )
    return out


def derive_dataset(
    dataset: DerivedDataset, settings: ObliskConfig, store: Store, *, now: datetime
) -> tuple[LoadedSeries, ...]:
    """Compute a derived series from the stored series it names, as they stand at ``now``."""
    as_of = now.date()
    if isinstance(dataset, OvernightAccrualDataset):
        inputs = [_input(store, seg.series, as_of, dataset.id) for seg in dataset.segments]
        observations = _accrual(dataset, inputs)
        described = "overnight accrual of " + ", then ".join(
            seg.series + ("" if seg.through is None else f" through {seg.through}")
            for seg in dataset.segments
        )
        units = "index level"
    else:
        inputs = [
            _input(store, dataset.yield_series, as_of, dataset.id),
            _input(store, dataset.shorter_yield_series, as_of, dataset.id),
        ]
        observations = _zero_coupon(dataset, inputs[0], inputs[1])
        described = (
            f"{dataset.maturity_years}-year constant-maturity zero from "
            f"{dataset.yield_series} and {dataset.shorter_yield_series}"
        )
        units = "index level"
    kinds = {found.series.point_in_time for found in inputs}
    if len(kinds) != 1:
        raise DataSourceError(f"{dataset.id} mixes inputs of kinds {sorted(kinds)}")
    content = json.dumps(
        {
            "dataset": dataset.model_dump(mode="json"),
            "inputs": {f.series.series_id: f.series.content_sha256 for f in inputs},
        },
        sort_keys=True,
    ).encode()
    input_notes = [note for found in inputs for note in found.series.bias_notes]
    series = _series(
        dataset,
        vendor=Vendor.DERIVED,
        field="level",
        units=units,
        currency=dataset.currency,
        frequency=Frequency.DAILY,
        point_in_time=kinds.pop(),
        url=f"derived: {described}",
        downloaded_at=now,
        content=content,
        notes=(*settings.research.data.derived.bias_notes, *dict.fromkeys(input_notes)),
    )
    return (LoadedSeries(series, tuple(observations)),)


@dataclass(frozen=True, slots=True)
class Listing:
    """An instrument whose research history is read from Yahoo: the universe's tradable
    candidates and the grid's stock candidates (``data/candidates.py``)."""

    key: str
    symbol: str
    exchange: str
    currency: str
    inception: date | None
    note: str


def yahoo_symbol(
    symbol: str, exchange: str, suffixes: Mapping[str, str], digits: Mapping[str, int]
) -> str | None:
    """Yahoo's symbol for ``symbol`` on the IBKR listing ``exchange``; ``None`` where Yahoo's
    suffix for the exchange is not configured. A share class's dot or space is Yahoo's dash
    (``RCI.B`` is ``RCI-B``), a trailing dot is dropped (``NG.``), and a venue whose numeric
    codes Yahoo pads (``digits``) is padded (Hong Kong's ``700`` is ``0700``)."""
    suffix = suffixes.get(exchange)
    if suffix is None:
        return None
    cleaned = symbol.strip().rstrip(".").replace(" ", "-").replace(".", "-")
    width = digits.get(exchange)
    if width is not None and cleaned.isdigit():
        cleaned = cleaned.zfill(width)
    return f"{cleaned}{suffix}"


def universe_listings(settings: ObliskConfig) -> tuple[Listing, ...]:
    """The universe's pinned tradable candidates as listings; a fund's ongoing charge is noted,
    since its closes are net of it."""
    listings: list[Listing] = []
    for declared in settings.universe.instruments:
        if not declared.tradable or declared.conid is None:
            continue
        fee = declared.attributes.fee
        charge = "" if fee is None else f" Its ongoing charge, {fee:.2%} a year, is in its price."
        listings.append(
            Listing(
                key=declared.symbol,
                symbol=declared.symbol,
                exchange=declared.listing_exchange,
                currency=declared.currency,
                inception=declared.attributes.inception,
                note=(
                    f"The universe's tradable candidate {declared.symbol} on "
                    f"{declared.listing_exchange}.{charge}"
                ),
            )
        )
    return tuple(listings)


#: Dataset ids of the universe's own listings and of stock candidates.
UNIVERSE_PREFIX: Final = "universe"
CANDIDATE_PREFIX: Final = "candidate"
#: The field of a Yahoo dataset whose returns include distributions.
ADJUSTED_CLOSE: Final = "adj_close"


def _dataset_id(prefix: str, key: str) -> str:
    return f"{prefix}_{re.sub(r'[^a-z0-9]+', '_', key.lower()).strip('_')}"


def universe_series_id(symbol: str, field: str = ADJUSTED_CLOSE) -> str:
    """The research series of a universe listing's history (``load-universe``)."""
    return f"{_dataset_id(UNIVERSE_PREFIX, symbol)}.{field}"


def universe_datasets(
    settings: ObliskConfig, listings: Iterable[Listing], *, prefix: str
) -> tuple[tuple[YahooDataset, ...], tuple[str, ...]]:
    """A Yahoo dataset per listing, and the listings that have no Yahoo symbol (with why)."""
    yahoo = settings.research.data.yahoo
    datasets: list[YahooDataset] = []
    skipped: list[str] = []
    for listing in listings:
        symbol = yahoo_symbol(listing.symbol, listing.exchange, yahoo.suffixes, yahoo.digits)
        if symbol is None:
            skipped.append(f"{listing.key}: no Yahoo suffix for {listing.exchange}")
            continue
        start = max(yahoo.universe_start, listing.inception or yahoo.universe_start)
        datasets.append(
            YahooDataset(
                id=_dataset_id(prefix, listing.key),
                vendor="yahoo",
                symbol=symbol,
                currency=listing.currency,
                start=start,
                bias_notes=[
                    listing.note,
                    f"Read from Yahoo as {symbol}; a listing Yahoo maps differently from IBKR is "
                    "a different series (VERIFY by its first close against IBKR's).",
                ],
            )
        )
    return tuple(datasets), tuple(skipped)


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
