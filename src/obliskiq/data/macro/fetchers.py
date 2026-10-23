"""One fetcher per macro source, each returning a series at its native frequency.

Ported from lite's ``macro/fetchers.py`` (FRED/ALFRED, the ECB, EIA, the ONS, Statistics Canada);
the Bank of Japan, the BIS and the Federal Reserve Board's excess bond premium are new. A fetcher
returns what the source says: the period, the value, the release date where the source states one
and the units where it reports them. Dating a row under the series' release rule, the units guard
and the frequency guard are the collector's (:mod:`.pull`). No fetcher sends a frequency or an
aggregation, except FRED's commodity history when ``commodity_history.frequency`` is monthly.

**FRED and the release date.** The plain observations endpoint gives no publication date.
ALFRED's ``output_type=4`` (initial release only) dates each row by its ``realtime_start``, the day
it first became public, and carries three conditions, each an HTTP 400 when broken (lite, verified
live 2026-09-04): a real-time window is required (the default, today, usually holds no vintage);
the window may span at most ``max_vintages_per_call`` vintages, so a daily series is split into
windows sized from its declared frequency; and every window must contain a vintage, so the window is
met with the range ``series/vintagedates`` reports. Windows are merged on the earliest release of
each period. A series read at ``value_vintage: latest`` (an index its publisher rebased) takes its
values from one plain call and its dates from the vintage walk.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from ...config.schema import ObliskConfig
from ...errors import UpstreamError
from ...util.http import RetryPolicy, get_with_retry, request_with_retry
from ...util.numeric import safe_float
from ...util.timeutil import parse_date
from .catalog import SeriesSpec
from .sdmx import decode, parse_period

#: ALFRED's output_type for "initial release only".
FRED_INITIAL_RELEASE_ONLY = "4"
#: FRED writes a missing observation as ".".
FRED_MISSING = "."
HTTP_OK = 200
#: How much of an error body a refusal quotes.
ERROR_EXCERPT_CHARS = 300
#: Releases a year of each declared frequency: a vintage is a release, so the vintages in a window
#: are its length times this rate (lite; over-estimating is the safe side: a window too long is a
#: 400, one too short is one more call). Daily counts business days (DGS10: 2,472 in ten years).
RELEASES_PER_YEAR = {
    "daily": 252.0,
    "weekly": 52.0,
    "monthly": 12.0,
    "quarterly": 4.0,
    "annual": 1.0,
}
#: StatCan's WDS pages by count; one period of slack keeps the window's first month inside.
STATCAN_SLACK_PERIODS = 1
#: StatCan's per-vector success marker.
STATCAN_SUCCESS = "SUCCESS"
#: The Bank of Japan's success status, and its date stamps' lengths (YYYYMMDD, YYYYMM).
BOJ_OK = 200
BOJ_DAY_DIGITS = 8
BOJ_MONTH_DIGITS = 6
#: The BIS CSV's columns.
BIS_PERIOD = "TIME_PERIOD"
BIS_VALUE = "OBS_VALUE"
BIS_UNIT = "UNIT_MEASURE"


@dataclass(frozen=True, slots=True)
class Fetched:
    """One observation as the source gave it: ``release_date`` where the source states it,
    ``units`` where it reports them (``None`` otherwise, never a guess)."""

    reference_date: date
    value: float | None
    release_date: date | None
    units: str | None


@dataclass(frozen=True, slots=True)
class Window:
    """What a fetch covers: the history from ``start``, as of the collection day ``today``."""

    start: date
    today: date


class Fetcher(Protocol):
    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]: ...


class _HttpFetcher:
    def __init__(self, client: httpx.AsyncClient, policy: RetryPolicy) -> None:
        self._client = client
        self._policy = policy

    async def _get(self, url: str, params: Mapping[str, Any], *, what: str) -> httpx.Response:
        response = await get_with_retry(self._client, url, policy=self._policy, params=params)
        if response.status_code != HTTP_OK:
            raise UpstreamError(
                f"{what} returned HTTP {response.status_code}: "
                f"{response.text[:ERROR_EXCERPT_CHARS]}"
            )
        return response

    async def _get_json(self, url: str, params: Mapping[str, Any], *, what: str) -> Any:
        response = await self._get(url, params, what=what)
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"{what} returned a body that is not JSON") from exc

    async def _post_json(self, url: str, body: Any, *, what: str) -> Any:
        response = await request_with_retry(
            self._client, "POST", url, policy=self._policy, json_body=body
        )
        if response.status_code != HTTP_OK:
            raise UpstreamError(
                f"{what} returned HTTP {response.status_code}: "
                f"{response.text[:ERROR_EXCERPT_CHARS]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"{what} returned a body that is not JSON") from exc


# ── FRED / ALFRED ──


class FredFetcher(_HttpFetcher):
    """FRED through ALFRED's initial-release view."""

    def __init__(
        self,
        settings: ObliskConfig,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        api_key: str | None,
    ) -> None:
        super().__init__(client, policy)
        self._fred = settings.sources.macro.fred
        self._history = settings.sources.macro.commodity_history
        self._days_per_year = settings.numerics.bsm.day_count_basis
        self._api_key = api_key

    def _base(self, spec: SeriesSpec) -> dict[str, Any]:
        if not self._api_key:
            raise UpstreamError(
                f"FRED_API_KEY is unset, so {spec.id} cannot be fetched; a macro series has no "
                "substitute value"
            )
        return {"series_id": spec.id, "api_key": self._api_key, "file_type": "json"}

    def _aggregation(self, spec: SeriesSpec) -> dict[str, str]:
        if spec.is_commodity_history and self._history.frequency == "monthly":
            return {"frequency": "m", "aggregation_method": self._history.aggregation_method}
        return {}

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        base = self._base(spec)
        extra = self._aggregation(spec)
        windows = self.windows(spec, window, await self._vintage_range(spec))
        units = await self._units(spec)
        merged: dict[date, Fetched] = {}
        for window_start, window_end in windows:
            body = await self._get_json(
                self._fred.observations_url,
                {
                    **base,
                    "observation_start": window_start.isoformat(),
                    "observation_end": window_end.isoformat(),
                    "realtime_start": window_start.isoformat(),
                    "realtime_end": window_end.isoformat(),
                    "output_type": FRED_INITIAL_RELEASE_ONLY,
                    **extra,
                },
                what=f"FRED {spec.id}",
            )
            for row in (body or {}).get("observations") or []:
                reference = parse_date(row.get("date"))
                if reference is None:
                    continue
                fetched = Fetched(
                    reference_date=reference,
                    value=safe_float(row.get("value"), None),
                    release_date=parse_date(row.get("realtime_start")),
                    units=units,
                )
                held = merged.get(reference)
                if held is None or _earlier(fetched, held):
                    merged[reference] = fetched
        if spec.value_vintage == "latest":
            merged = await self._latest_values(spec, merged, windows, extra, units)
        return [merged[reference] for reference in sorted(merged)]

    async def _latest_values(
        self,
        spec: SeriesSpec,
        dated: dict[date, Fetched],
        windows: list[tuple[date, date]],
        extra: dict[str, str],
        units: str | None,
    ) -> dict[date, Fetched]:
        """The same periods valued from the current vintage, dated by their first release. A
        period the current vintage no longer carries is withdrawn and dropped; one ALFRED has no
        vintage for keeps a NULL release date, never an invented one."""
        body = await self._get_json(
            self._fred.observations_url,
            {
                **self._base(spec),
                "observation_start": windows[0][0].isoformat(),
                "observation_end": windows[-1][1].isoformat(),
                **extra,
            },
            what=f"FRED {spec.id} (latest vintage)",
        )
        out: dict[date, Fetched] = {}
        for row in (body or {}).get("observations") or []:
            reference = parse_date(row.get("date"))
            if reference is None:
                continue
            held = dated.get(reference)
            out[reference] = Fetched(
                reference_date=reference,
                value=safe_float(row.get("value"), None),
                release_date=held.release_date if held is not None else None,
                units=units,
            )
        return out

    async def _units(self, spec: SeriesSpec) -> str | None:
        """FRED's ``units_short`` for the series, stored on every row."""
        body = await self._get_json(
            self._fred.series_url, self._base(spec), what=f"FRED series {spec.id}"
        )
        rows = (body or {}).get("seriess") or []
        return (rows[0].get("units_short") or None) if rows else None

    def windows(
        self, spec: SeriesSpec, window: Window, vintages: tuple[date, date]
    ) -> list[tuple[date, date]]:
        """The window asked for, met with ALFRED's vintage range, then split so each holds at
        most ``max_vintages_per_call`` vintages. Consecutive windows do not overlap; a row
        released after its window closes is picked up by the next one."""
        first, last = vintages
        begin = max(window.start, first)
        end = min(window.today, last)
        if end < begin:
            raise UpstreamError(
                f"ALFRED holds vintages of {spec.id} only from {first} to {last}, which does not "
                "meet the window asked for: no observation can carry a first-publication date"
            )
        if end == begin:
            return [(begin, begin)]
        per_year = RELEASES_PER_YEAR.get(spec.frequency, RELEASES_PER_YEAR["daily"])
        span = timedelta(
            days=max(1, int(self._fred.max_vintages_per_call / per_year * self._days_per_year))
        )
        windows: list[tuple[date, date]] = []
        cursor = begin
        while cursor < end:
            stop = min(cursor + span, end)
            windows.append((cursor, stop))
            cursor = stop + timedelta(days=1)
        return windows

    async def _vintage_range(self, spec: SeriesSpec) -> tuple[date, date]:
        """ALFRED's first and last vintage of the series, two one-date requests."""
        first = await self._one_vintage(spec, "asc")
        last = await self._one_vintage(spec, "desc")
        if first is None or last is None:
            raise UpstreamError(
                f"ALFRED holds no vintage of {spec.id}, so no observation can carry a "
                "first-publication date; the plain observations endpoint is not used instead"
            )
        return first, last

    async def _one_vintage(self, spec: SeriesSpec, order: str) -> date | None:
        body = await self._get_json(
            self._fred.vintage_dates_url,
            {**self._base(spec), "sort_order": order, "limit": 1},
            what=f"FRED vintage dates of {spec.id}",
        )
        dates = (body or {}).get("vintage_dates") or []
        return parse_date(dates[0]) if dates else None


def _earlier(candidate: Fetched, held: Fetched) -> bool:
    """Is ``candidate`` the earlier publication of the same period? An undated row never
    displaces a dated one."""
    if candidate.release_date is None:
        return False
    if held.release_date is None:
        return True
    return candidate.release_date < held.release_date


# ── ECB ──


class EcbFetcher(_HttpFetcher):
    """The ECB Data Portal's SDMX-JSON; no key. The portal reports no units here."""

    def __init__(self, settings: ObliskConfig, client: httpx.AsyncClient, policy: RetryPolicy):
        super().__init__(client, policy)
        self._url = settings.sources.macro.ecb.data_url

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        body = await self._get_json(
            f"{self._url}/{spec.key}",
            {"format": "jsondata", "startPeriod": window.start.isoformat(), "detail": "dataonly"},
            what=f"ECB {spec.id}",
        )
        return [Fetched(reference, value, None, None) for reference, value in decode(body, spec.id)]


# ── EIA ──


class EiaFetcher(_HttpFetcher):
    """EIA v2: the level of a weekly inventory series; the seasonal comparison is the views'."""

    def __init__(
        self,
        settings: ObliskConfig,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        api_key: str | None,
    ) -> None:
        super().__init__(client, policy)
        self._url = settings.sources.macro.eia.base_url
        self._api_key = api_key

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        if not self._api_key:
            raise UpstreamError(f"EIA_API_KEY is unset, so {spec.id} cannot be fetched")
        body = await self._get_json(
            f"{self._url}/{spec.key}/data/",
            {
                "api_key": self._api_key,
                "data[0]": "value",
                "facets[series][]": spec.id,
                "start": window.start.isoformat(),
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
            },
            what=f"EIA {spec.id}",
        )
        rows = ((body or {}).get("response") or {}).get("data") or []
        out: list[Fetched] = []
        for row in rows:
            reference = parse_date(row.get("period"))
            if reference is None:
                continue
            out.append(
                Fetched(reference, safe_float(row.get("value"), None), None, row.get("units"))
            )
        return out


# ── ONS ──


class OnsFetcher(_HttpFetcher):
    """UK CPI from the ONS website's JSON view of a time series (the API was retired in 2024).

    Every month carries its own ``updateDate``, a last-updated stamp: later than or equal to
    first publication, so it can delay a figure and never advance it. It is UTC and the ONS
    publishes in London: ``2026-08-18T23:00Z`` is the London Wednesday 2026-08-19 it was
    published, so the stamp is read in London (lite, verified live 2026-09-15).
    """

    #: English month names as the ONS writes them, matched by position (no locale).
    MONTHS = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )

    def __init__(self, settings: ObliskConfig, client: httpx.AsyncClient, policy: RetryPolicy):
        super().__init__(client, policy)
        self._ons = settings.sources.macro.ons
        self._zone = ZoneInfo(self._ons.timezone)

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        body = await self._get_json(
            f"{self._ons.base_url}/{spec.key}/data", {}, what=f"ONS {spec.id}"
        )
        rows = (body or {}).get("months") or []
        if not rows:
            raise UpstreamError(
                f"ONS {spec.id} returned no months: the route answered, so its shape changed"
            )
        units = ((body or {}).get("description") or {}).get("unit") or None
        out: list[Fetched] = []
        for row in rows:
            reference = self.reference(row)
            if reference is None or reference < window.start:
                continue
            out.append(
                Fetched(
                    reference,
                    safe_float(row.get("value"), None),
                    self.published(row.get("updateDate")),
                    units,
                )
            )
        return out

    @classmethod
    def reference(cls, row: Mapping[str, Any]) -> date | None:
        """The month a row describes, from its ``year`` and ``month`` fields."""
        try:
            year = int(str(row.get("year")).strip())
        except ValueError:
            return None
        name = str(row.get("month") or "").strip().lower()
        if name not in cls.MONTHS:
            return None
        return date(year, cls.MONTHS.index(name) + 1, 1)

    def published(self, stamp: Any) -> date | None:
        """The London date of a UTC update stamp."""
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(str(stamp))
        except ValueError:
            return None
        if moment.tzinfo is None:
            return moment.date()
        return moment.astimezone(self._zone).date()


# ── Statistics Canada ──


class StatcanFetcher(_HttpFetcher):
    """Statistics Canada's WDS, addressed by vector; every point carries its ``releaseTime``.

    ``releaseTime`` has no offset; it is Eastern, and 08:30 Eastern is the same day in UTC all
    year, so its date part is the release date. WDS answers HTTP 200 with a per-vector status, so
    the envelope is read before the points. WDS reports no unit string.
    """

    def __init__(self, settings: ObliskConfig, client: httpx.AsyncClient, policy: RetryPolicy):
        super().__init__(client, policy)
        self._url = settings.sources.macro.statcan.vectors_url
        self._days_per_year = settings.numerics.bsm.day_count_basis

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        try:
            vector = int(str(spec.key).lstrip("vV"))
        except ValueError as exc:
            raise UpstreamError(f"StatCan {spec.id}: key {spec.key!r} is not a vector id") from exc
        body = await self._post_json(
            self._url,
            [{"vectorId": vector, "latestN": self.periods(spec, window)}],
            what=f"StatCan {spec.id}",
        )
        out: list[Fetched] = []
        for row in self.points(spec, body):
            reference = parse_date(row.get("refPer"))
            if reference is None or reference < window.start:
                continue
            out.append(
                Fetched(
                    reference,
                    safe_float(row.get("value"), None),
                    _statcan_release(row.get("releaseTime")),
                    None,
                )
            )
        return out

    def periods(self, spec: SeriesSpec, window: Window) -> int:
        per_year = RELEASES_PER_YEAR.get(spec.frequency, RELEASES_PER_YEAR["daily"])
        days = max(1, (window.today - window.start).days)
        return ceil(days / self._days_per_year * per_year) + STATCAN_SLACK_PERIODS

    @staticmethod
    def points(spec: SeriesSpec, body: Any) -> list[dict[str, Any]]:
        if not isinstance(body, list) or not body:
            raise UpstreamError(f"StatCan {spec.id} returned no vector envelope")
        envelope = body[0] or {}
        status = envelope.get("status")
        if status != STATCAN_SUCCESS:
            raise UpstreamError(f"StatCan {spec.id} returned status {status!r}")
        found = envelope.get("object") or {}
        if found.get("terminated"):
            raise UpstreamError(
                f"StatCan {spec.id} is terminated by its publisher; its last value would read "
                "as a current print"
            )
        return list(found.get("vectorDataPoint") or [])


def _statcan_release(stamp: Any) -> date | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp)).date()
    except ValueError:
        return None


# ── Bank of Japan ──


class BojFetcher(_HttpFetcher):
    """The Bank of Japan's Time-Series Data Search API (``getDataCode``); no key.

    ``key`` is ``<database>/<series code>``. The answer carries its own status, a ``NEXTPOSITION``
    when it continues, and the series' unit; dates are ``YYYYMMDD`` (daily) or ``YYYYMM``.
    """

    def __init__(self, settings: ObliskConfig, client: httpx.AsyncClient, policy: RetryPolicy):
        super().__init__(client, policy)
        self._url = settings.sources.macro.boj.data_url

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        database, _, code = str(spec.key).partition("/")
        if not database or not code:
            raise UpstreamError(f"BoJ {spec.id}: key {spec.key!r} is not <database>/<code>")
        params: dict[str, Any] = {
            "format": "json",
            "lang": "en",
            "db": database,
            "code": code,
            "startDate": f"{window.start.year:04d}{window.start.month:02d}",
        }
        out: list[Fetched] = []
        while True:
            body = await self._get_json(self._url, params, what=f"BoJ {spec.id}")
            status = (body or {}).get("STATUS")
            if status != BOJ_OK:
                raise UpstreamError(f"BoJ {spec.id} answered {status}: {body.get('MESSAGE')}")
            for entry in body.get("RESULTSET") or []:
                if entry.get("SERIES_CODE") != code:
                    continue
                units = entry.get("UNIT") or None
                values = entry.get("VALUES") or {}
                for stamp, value in zip(
                    values.get("SURVEY_DATES") or [], values.get("VALUES") or [], strict=False
                ):
                    reference = _boj_date(stamp)
                    if reference is not None and reference >= _month_start(window.start):
                        out.append(Fetched(reference, safe_float(value, None), None, units))
            following = body.get("NEXTPOSITION")
            if not following:
                break
            params = {**params, "startPosition": following}
        return sorted(out, key=lambda fetched: fetched.reference_date)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _boj_date(stamp: Any) -> date | None:
    text = str(stamp).strip()
    try:
        if len(text) == BOJ_DAY_DIGITS:
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        if len(text) == BOJ_MONTH_DIGITS:
            return date(int(text[:4]), int(text[4:]), 1)
    except ValueError:
        return None
    return None


# ── BIS ──


class BisFetcher(_HttpFetcher):
    """The BIS Data Portal's SDMX API as CSV; no key. ``key`` is ``<dataflow>/<series key>``.

    The CSV carries ``UNIT_MEASURE`` on every row (BIS's code, 368 for per cent a year), which the
    units guard compares with the declared code; the JSON form carries it nowhere a series reads.
    """

    def __init__(self, settings: ObliskConfig, client: httpx.AsyncClient, policy: RetryPolicy):
        super().__init__(client, policy)
        self._url = settings.sources.macro.bis.data_url

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        response = await self._get(
            f"{self._url}/{spec.key}",
            {"startPeriod": window.start.isoformat(), "format": "csv"},
            what=f"BIS {spec.id}",
        )
        reader = csv.DictReader(io.StringIO(response.text))
        fields = set(reader.fieldnames or [])
        if not {BIS_PERIOD, BIS_VALUE, BIS_UNIT} <= fields:
            raise UpstreamError(f"BIS {spec.id} answered a CSV without {BIS_PERIOD}/{BIS_VALUE}")
        out: list[Fetched] = []
        for row in reader:
            reference = parse_period(row.get(BIS_PERIOD) or "")
            if reference is None:
                continue
            out.append(
                Fetched(
                    reference,
                    safe_float(row.get(BIS_VALUE), None),
                    None,
                    (row.get(BIS_UNIT) or "").strip() or None,
                )
            )
        return sorted(out, key=lambda fetched: fetched.reference_date)


# ── Federal Reserve Board: the excess bond premium ──


class FrbFetcher(_HttpFetcher):
    """The Federal Reserve Board's excess bond premium file (Gilchrist and Zakrajšek, monthly):
    a CSV of ``date`` and one column per series; ``key`` names the column. No units are stated."""

    def __init__(self, settings: ObliskConfig, client: httpx.AsyncClient, policy: RetryPolicy):
        super().__init__(client, policy)
        self._url = settings.sources.macro.frb.ebp_url

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        response = await self._get(self._url, {}, what=f"FRB {spec.id}")
        reader = csv.DictReader(io.StringIO(response.text))
        if spec.key not in (reader.fieldnames or []) or "date" not in (reader.fieldnames or []):
            raise UpstreamError(f"FRB {spec.id}: the file has no column {spec.key!r}")
        out: list[Fetched] = []
        for row in reader:
            reference = parse_date(row.get("date"))
            if reference is None or reference < _month_start(window.start):
                continue
            out.append(Fetched(reference, safe_float(row.get(spec.key or ""), None), None, None))
        return out


def build_fetchers(
    settings: ObliskConfig,
    client: httpx.AsyncClient,
    policy: RetryPolicy,
    *,
    fred_api_key: str | None,
    eia_api_key: str | None,
) -> dict[str, Fetcher]:
    """A fetcher for every built macro source, by source name."""
    return {
        "fred": FredFetcher(settings, client, policy, fred_api_key),
        "ecb": EcbFetcher(settings, client, policy),
        "eia": EiaFetcher(settings, client, policy, eia_api_key),
        "ons": OnsFetcher(settings, client, policy),
        "statcan": StatcanFetcher(settings, client, policy),
        "boj": BojFetcher(settings, client, policy),
        "bis": BisFetcher(settings, client, policy),
        "frb": FrbFetcher(settings, client, policy),
    }
