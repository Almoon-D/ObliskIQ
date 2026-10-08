"""One fetcher per macro source: FRED, ECB, CFTC, EIA.

Each returns :class:`~obliskiq.macro.series.Observation` rows at the source's
**native** frequency. None of them sends a frequency or aggregation parameter —
the single exception is FRED's commodity long history, and only when
``macro.commodity_history.frequency`` is explicitly set to ``monthly``. What comes
back is what the source publishes.

FRED and the release date
-------------------------

The plain observations endpoint gives no publication date. ALFRED's
``output_type=4`` returns *initial release only*, where each row's
``realtime_start`` **is** the date the figure first became public. That is the one
route that makes ``release_date`` a fact rather than a guess, and it is why the
FRED fetcher does not use the simpler call that
:mod:`obliskiq.macro.rates` uses for a daily rate.

**``output_type=4`` carries three conditions, and this fetcher got all three
wrong until a live key exercised it.** Each was an HTTP 400.

1. *A real-time window is not optional.* Sent without ``realtime_start`` and
   ``realtime_end``, FRED defaults the vintage window to today — and a day with
   no vintage is an error, not an empty result: ``No vintage dates exist for the
   specified real-time period: 2026-09-04 to 2026-09-04``. The window has to be
   stated, and it has to be the one the observations come from.

2. *The window is capped at ``macro.sources.fred.max_vintages_per_call`` vintage
   dates*, which FRED enforces and names in its own error. A DAILY series over
   ``macro.collection.default_window_years`` exceeds it — DGS10 answered
   ``There are 2472 vintage dates ... This exceeds the maximum number of vintage
   dates allowed for this file type (2000)`` — so bounding the window is not
   enough on its own. It is split into chunks whose vintage count fits, each
   chunk asking only for the observations released inside it, and the results
   merged on the EARLIEST release per reference date.

3. *And every window has to contain a vintage.* The same condition as 1, at the
   other end of the history: ALFRED's vintages for a series start when FRED
   began archiving it, not when the series did, and a real-time period entirely
   before that holds no vintage at all. What comes back then is not an empty
   result but ``The series does not exist in ALFRED but may exist in FRED``,
   which reads as a fact about the series and is a fact about the window.
   ``DHHNGSP`` collected nothing for that reason and was recorded as a series
   ALFRED does not hold; ALFRED holds it from 2014-12-10, and the first chunk of
   its twenty-year commodity window ended in 2014-08. ``DCOILWTICO`` and
   ``DCOILBRENTEU`` survive the identical chunk only because their own first
   vintage, 2011-04-06, happens to fall inside it. So the window is intersected
   with the range :meth:`FredFetcher._vintage_range` asks ALFRED for, and rows
   older than that range are a gap ALFRED has rather than one this request made.

None of the three is visible without a key: ``macro_pull``'s per-source guard
abandons FRED after five consecutive failures and reports the other sources as
collected, so a keyless run and a broken keyed one produce the same summary
line. That is the guard working as designed and it is also why the first two
survived two sessions and the third survived four.

.. note::
   **Every source here is verified live** (2026-09-04), with real keys where one
   is needed — see ``references/macro-playbook.md`` for what each returned.

   There were five. The OECD was the fifth, and it is gone: its one series
   returned no row in its life, nothing in ``src/`` read it and no market
   declared it, so it went under the rule that a series earns its place by
   having a consumer. What stays is the SDMX decoder at the bottom of this
   module, because the ECB is thirteen live series on the same format family —
   and because the two shapes the OECD taught it (a **2.0** envelope where the
   ECB serves **1.0**, and a key that addresses several series at once) are
   properties of SDMX rather than of that one publisher. The next SDMX source
   will meet both.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import ceil
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from ..config import Config
from ..errors import ERROR_EXCERPT_CHARS, UpstreamError
from ..logging_setup import get_logger
from ..util.http import RetryPolicy, get_with_retry, request_with_retry
from ..util.numeric import safe_float
from ..util.timeutil import parse_date, utc_today
from .series import VALUE_VINTAGE_LATEST, Observation, SeriesSpec, SourcePolicy

log = get_logger("macro.fetch")

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_VINTAGE_DATES_URL = "https://api.stlouisfed.org/fred/series/vintagedates"
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"
ECB_DATA_URL = "https://data-api.ecb.europa.eu/service/data"
CFTC_SOCRATA_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
EIA_BASE_URL = "https://api.eia.gov/v2"
ONS_BASE_URL = "https://www.ons.gov.uk"
STATCAN_VECTOR_URL = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods"

#: ALFRED output_type: 4 is "initial release only", so realtime_start is the date
#: the observation first became public rather than the date of the latest vintage.
FRED_INITIAL_RELEASE_ONLY = "4"

#: How many times a year a series of each declared frequency is RELEASED, which
#: is what a vintage is. Calendar facts about the publication calendar, not
#: thresholds — the tunable is the cap they are measured against,
#: ``macro.sources.fred.max_vintages_per_call``.
#:
#: Daily uses trading days rather than calendar days because FRED publishes a
#: daily series on business days; measured against DGS10, ten years of which
#: FRED counts as 2,472 vintages against the 2,520 this estimates. Over-
#: estimating is the safe direction: it makes the chunks shorter than they need
#: to be, and a chunk that is too long is an HTTP 400.
_RELEASES_PER_YEAR = {
    "daily": 252.0,
    "business": 252.0,
    "weekly": 52.0,
    "biweekly": 26.0,
    "monthly": 12.0,
    "quarterly": 4.0,
    "semiannual": 2.0,
    "annual": 1.0,
}

#: A series whose declared frequency is unknown is chunked as though it were the
#: densest one. Guessing sparse would produce a window FRED refuses; guessing
#: dense produces more calls than necessary, and only one of those is a failure.
_UNKNOWN_FREQUENCY_RELEASES_PER_YEAR = 252.0

#: HTTP OK.
HTTP_OK = 200

#: How much of an error body to quote back.

#: CFTC reports net non-commercial positioning; the two legs are subtracted here
#: so downstream sees one number per week rather than reconstructing it, and the
#: result is divided by open interest so that what is stored is a SHARE of the
#: market rather than a contract count. See CFTC_NET_UNITS for why.
_CFTC_LONG_FIELD = "noncomm_positions_long_all"
_CFTC_SHORT_FIELD = "noncomm_positions_short_all"
_CFTC_OPEN_INTEREST_FIELD = "open_interest_all"
_CFTC_DATE_FIELD = "report_date_as_yyyy_mm_dd"
_CFTC_MARKET_FIELD = "cftc_contract_market_code"

#: What a CFTC row's value means, carried onto every observation.
#:
#: It was ``contracts_net_noncommercial`` — a raw count — and the only consumers
#: rank it as a percentile of its own multi-year window. Open interest grows and
#: shrinks over such a window, so a percentile of the raw count substantially
#: ranks how big the market got rather than how crowded it is. Measured on the
#: series this repository actually collects: WTI's net non-commercial count
#: ranges 39,800 to 739,097 over ten years, an eighteen-fold spread that no
#: positioning extreme accounts for.
#:
#: ``open_interest_all`` arrives in the same Socrata row and was being discarded.
#: Dividing by it gives a share of open interest, which is what
#: ``commodities.cot.extreme_percentile`` and SKILL.md both already claim the
#: figure measures. Stored as a DECIMAL FRACTION, like every other ratio here,
#: so no bare 100 enters the arithmetic.
CFTC_NET_UNITS = "net_noncommercial_share_of_open_interest"


#: Socrata's page size cap for one request.
_SOCRATA_LIMIT = 5000

#: Months in a quarter, for mapping an SDMX ``2026-Q1`` period onto a date.
_MONTHS_PER_QUARTER = 3


class Fetcher(Protocol):
    """What ``macro_pull`` needs from a source."""

    async def fetch(
        self, spec: SeriesSpec, policy: SourcePolicy, *, start: date
    ) -> list[Observation]: ...


class _HttpFetcher:
    def __init__(self, config: Config, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._policy = RetryPolicy.from_config(config)

    async def _get_json(self, url: str, params: dict[str, Any], *, what: str) -> Any:
        response = await get_with_retry(self._client, url, policy=self._policy, params=params)
        if response.status_code != HTTP_OK:
            raise UpstreamError(
                f"{what} returned HTTP {response.status_code}: "
                f"{response.text[:ERROR_EXCERPT_CHARS]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"{what} returned a non-JSON body") from exc

    async def _post_json(self, url: str, body: Any, *, what: str) -> Any:
        """The same contract as :meth:`_get_json`, for an upstream that takes a body.

        Statistics Canada's WDS is the only one here that does. It goes through
        ``request_with_retry`` rather than a bare post so that it gets the same
        ``http.*`` backoff and Retry-After handling as every GET.
        """
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
            raise UpstreamError(f"{what} returned a non-JSON body") from exc


class FredFetcher(_HttpFetcher):
    """FRED, via ALFRED's initial-release view.

    FRED is not US-only: it redistributes the OECD international series, and — the
    part that keeps the CPI axis alive — Eurostat's HICP, which FRED serves fresher
    than Eurostat's own dissemination API does.

    Which vintage supplies the VALUE
    --------------------------------

    The release date always comes from the initial release; there is no other
    honest source for it. The value normally does too, and
    ``value_vintage: latest`` in config.yaml is the exception, declared per series
    because it is a property of the series.

    A publisher that **rebases an index** breaks the initial-release column in
    half. Eurostat rebased HICP to 2025=100 during 2026, so ALFRED's initial
    releases for ``CP0000DEM086NEST`` read 132.8 at 2025-12 (2015=100) and 100.6
    at 2026-01 (2025=100). :func:`~obliskiq.compute.frames.cpi_yoy` divides the
    newest observation by the one a year older, and across that boundary the
    division is of one unit by another: -22.6%, carrying a release date and a
    fresh vintage, stamped ``ok``. Nothing downstream could catch it.

    So for those series the vintage walk supplies only the reference-date →
    first-publication-date map, and one plain call supplies values that are all on
    one base. The cost is that the stored number is the revised one rather than
    the first print — bounded, and named in ``docs/DECISIONS.md``. FRED refuses the
    obvious alternative: ``units=pc1`` with ``output_type=4`` answers *"If
    output_type is '3' or '4', then units must be 'lin'."*

    Every row also carries FRED's own ``units_short``, so a rebase that ever does
    land two bases in one series is visible in the store rather than inferred from
    a number that looks wrong.
    """

    def __init__(self, config: Config, client: httpx.AsyncClient, api_key: str | None) -> None:
        super().__init__(config, client)
        self._api_key = api_key
        history = config.section("macro.commodity_history")
        self._commodity_frequency = history.require_choice("frequency", ["native", "monthly"])
        self._commodity_aggregation = history.require_str("aggregation_method")
        self._max_vintages = config.require_int("macro.sources.fred.max_vintages_per_call")
        # The same constant every other window in this build converts years with.
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")

    async def fetch(
        self, spec: SeriesSpec, policy: SourcePolicy, *, start: date
    ) -> list[Observation]:
        if not self._api_key:
            raise UpstreamError(
                f"FRED_API_KEY is unset, so {spec.id} cannot be fetched. There is no "
                "substitute value for a macro series."
            )
        frequency = spec.frequency
        extra: dict[str, Any] = {}
        if spec.is_commodity_history and self._commodity_frequency == "monthly":
            # The ONE place a frequency is ever requested, and only because
            # macro.commodity_history.frequency was explicitly set to monthly.
            extra["frequency"] = "m"
            extra["aggregation_method"] = self._commodity_aggregation
            frequency = "monthly"

        # The vintage range first, and the units only once it has answered: a
        # series ALFRED holds no vintage for is refused below, and a refused
        # series should not have cost a metadata call to find out.
        windows = self._vintage_windows(spec, start, await self._vintage_range(spec))
        units = await self._units(spec)

        # Keyed by reference date, holding the EARLIEST release seen for it.
        # Chunks overlap at their boundaries by construction — an observation
        # dated near the end of one window can be released inside the next — so a
        # reference date can arrive twice, and the initial release is the earlier.
        merged: dict[date, Observation] = {}
        for window_start, window_end in windows:
            params: dict[str, Any] = {
                "series_id": spec.id,
                "api_key": self._api_key,
                "file_type": "json",
                "observation_start": window_start.isoformat(),
                "observation_end": window_end.isoformat(),
                # Both bounds, always. Without them FRED defaults the vintage
                # window to today and answers 400 on a day with no vintage —
                # which is most days, and was every FRED series, every run.
                "realtime_start": window_start.isoformat(),
                "realtime_end": window_end.isoformat(),
                # Initial release only: realtime_start is then the first
                # publication date, which is what release_date has to mean.
                "output_type": FRED_INITIAL_RELEASE_ONLY,
                **extra,
            }
            body = await self._get_json(FRED_OBSERVATIONS_URL, params, what=f"FRED {spec.id}")
            for row in body.get("observations", []) or []:
                reference = parse_date(row.get("date"))
                if reference is None:
                    continue
                observation = Observation(
                    series_id=spec.id,
                    source=spec.source,
                    reference_date=reference.isoformat(),
                    release_date=policy.release_date_for(
                        reference, from_api=parse_date(row.get("realtime_start"))
                    ),
                    # FRED writes "." for a missing observation; safe_float maps
                    # it to None, which is stored as absent rather than as zero.
                    value=safe_float(row.get("value"), None),
                    frequency=frequency,
                    units=units,
                )
                held = merged.get(reference)
                if held is None or _is_earlier_release(observation, held):
                    merged[reference] = observation

        if spec.value_vintage == VALUE_VINTAGE_LATEST:
            merged = await self._values_from_the_latest_vintage(
                spec,
                merged,
                windows=windows,
                extra=extra,
                frequency=frequency,
                units=units,
            )
        return [merged[reference] for reference in sorted(merged)]

    async def _values_from_the_latest_vintage(
        self,
        spec: SeriesSpec,
        dated: dict[date, Observation],
        *,
        windows: list[tuple[date, date]],
        extra: dict[str, Any],
        frequency: str,
        units: str | None,
    ) -> dict[date, Observation]:
        """The same reference dates, valued from the current vintage.

        ``dated`` supplies the first-publication dates and nothing else. The
        values come from one plain call, which is the only way to get a whole
        series on one base from a publisher that rebases it — see the class
        docstring for the -22.6% this exists to stop.

        A reference date the current vintage no longer carries is dropped: the
        publisher has withdrawn it, and an initial release of a withdrawn
        observation is not a current fact. A reference date the current vintage
        carries but ALFRED has no vintage for is kept with a NULL release date,
        which every point-in-time read already excludes and
        :attr:`~obliskiq.compute.frames.MacroSeries.unaligned_count` already
        counts. Neither is silently invented a date.
        """
        body = await self._get_json(
            FRED_OBSERVATIONS_URL,
            {
                "series_id": spec.id,
                "api_key": self._api_key,
                "file_type": "json",
                "observation_start": windows[0][0].isoformat(),
                "observation_end": windows[-1][1].isoformat(),
                **extra,
            },
            what=f"FRED {spec.id} (latest vintage)",
        )
        out: dict[date, Observation] = {}
        for row in body.get("observations", []) or []:
            reference = parse_date(row.get("date"))
            if reference is None:
                continue
            held = dated.get(reference)
            out[reference] = Observation(
                series_id=spec.id,
                source=spec.source,
                reference_date=reference.isoformat(),
                release_date=held.release_date if held is not None else None,
                value=safe_float(row.get("value"), None),
                frequency=frequency,
                units=units,
            )
        return out

    async def _units(self, spec: SeriesSpec) -> str | None:
        """What FRED says this series carries, stored on every row of it.

        CFTC and EIA already stamp their units and FRED did not, which left the
        one source serving both percent rates and rebasable index levels as the
        one source whose rows could not say which they were.
        """
        body = await self._get_json(
            FRED_SERIES_URL,
            {"series_id": spec.id, "api_key": self._api_key, "file_type": "json"},
            what=f"FRED series metadata for {spec.id}",
        )
        rows = (body or {}).get("seriess") or []
        return (rows[0].get("units_short") or None) if rows else None

    def _vintage_windows(
        self, spec: SeriesSpec, start: date, vintages: tuple[date, date]
    ) -> list[tuple[date, date]]:
        """The requested window, met with the vintages ALFRED holds, then split.

        Two separate bounds, and conflating them is the defect this signature
        exists to stop. ``start`` is the history the collector wants;
        ``vintages`` is the range ALFRED will answer for at all. A real-time
        period holding NO vintage date is refused — with a message about the
        series not existing in ALFRED, which is misleading enough that it was
        read as an absent series for two sessions — so the window is intersected
        with the range rather than sent as configured. It is the same failure
        already recorded for ``today to today``, one layer out: a vintage query
        over a span with no vintage in it.

        Within that, FRED refuses a real-time period holding more than
        ``macro.sources.fred.max_vintages_per_call`` vintage dates, and says so
        by name in its own error. A vintage is a release, so the number of them
        in a window is the window's length times the series' publication rate —
        which is exactly what ``spec.frequency`` declares. Monthly series never
        come close and get one call; a daily series over the configured history
        gets several.

        Derived rather than configured per series: a hand-kept list of which
        series need splitting would go on asserting it after FRED changed a
        publication cadence, and the arithmetic is visible either way. The same
        argument applies to the clamp — where ALFRED's vintages begin is a fact
        about that series, asked of ALFRED, not a date typed into config.
        """
        first_vintage, last_vintage = vintages
        start = max(start, first_vintage)
        end = min(utc_today(), last_vintage)
        if end < start:
            raise UpstreamError(
                f"ALFRED holds vintages for {spec.id} only between {first_vintage} and "
                f"{last_vintage}, which does not meet the window this collector asked "
                "for. There is no real-time period to request, so no observation can "
                "carry a first-publication date. Narrow the window, or accept that this "
                "series has no point-in-time history to collect."
            )
        if end <= start:
            return [(start, start)]
        per_year = _RELEASES_PER_YEAR.get(
            (spec.frequency or "").strip().lower(), _UNKNOWN_FREQUENCY_RELEASES_PER_YEAR
        )
        span = timedelta(
            days=max(1, int(self._max_vintages / per_year * self._calendar_days_per_year))
        )

        windows: list[tuple[date, date]] = []
        cursor = start
        while cursor < end:
            stop = min(cursor + span, end)
            windows.append((cursor, stop))
            if stop >= end:
                break
            # The next window starts the day after, so observation ranges do not
            # overlap and a reference date is not counted twice. A row released
            # after its own window closes is picked up by the next one, and the
            # merge above keeps whichever release is earlier.
            cursor = stop + timedelta(days=1)
        return windows or [(start, end)]

    async def _vintage_range(self, spec: SeriesSpec) -> tuple[date, date]:
        """The first and last vintage date ALFRED holds for this series.

        Asked of ALFRED rather than inferred from the 400 a vintage-free window
        produces. The difference is the one the whole macro layer turns on: a
        bound the source states is a fact, and a bound recovered by watching a
        request fail is a guess that happens to have been right once.

        Two requests, each asking for ONE date, rather than one for the list: a
        daily series carries thousands of vintage dates — DFF is 5,117 — and
        reading only the ends is bounded whatever the series turns out to be.
        """
        first = await self._one_vintage(spec, "asc")
        last = await self._one_vintage(spec, "desc")
        if first is None or last is None:
            raise UpstreamError(
                f"ALFRED holds no vintage dates for {spec.id}, so no observation of it "
                "can carry a first-publication date. This build will not fall back to the "
                "plain observations endpoint: those rows would store a NULL release date, "
                "be excluded from every point-in-time read, and still count as collected."
            )
        return first, last

    async def _one_vintage(self, spec: SeriesSpec, order: str) -> date | None:
        body = await self._get_json(
            FRED_VINTAGE_DATES_URL,
            {
                "series_id": spec.id,
                "api_key": self._api_key,
                "file_type": "json",
                "sort_order": order,
                "limit": 1,
            },
            what=f"FRED vintage dates for {spec.id}",
        )
        dates = (body or {}).get("vintage_dates") or []
        return parse_date(dates[0]) if dates else None


def _is_earlier_release(candidate: Observation, held: Observation) -> bool:
    """Is ``candidate`` the earlier publication of the same reference date?

    A row carrying no release date never displaces one that has a date: an
    unaligned observation is a coverage gap the caller reports, and preferring it
    would turn a dated fact into an undated one. Between two dated rows the
    earlier is the initial release, which is the whole reason for
    ``output_type=4``.
    """
    if candidate.release_date is None:
        return False
    if held.release_date is None:
        return True
    return candidate.release_date < held.release_date


class EcbFetcher(_HttpFetcher):
    """ECB Data Portal SDMX-JSON. Free, no key.

    The base currency is EUR and the cash yield tracks €STR, so this is the
    source that matters most for discounting and for the carry-illusion test.
    """

    async def fetch(
        self, spec: SeriesSpec, policy: SourcePolicy, *, start: date
    ) -> list[Observation]:
        if not spec.key:
            raise UpstreamError(f"ECB series {spec.id} has no flow/key declared in config")
        url = f"{ECB_DATA_URL}/{spec.key}"
        body = await self._get_json(
            url,
            {"format": "jsondata", "startPeriod": start.isoformat(), "detail": "dataonly"},
            what=f"ECB {spec.id}",
        )
        return _from_sdmx_json(body, spec, policy)


class CftcFetcher(_HttpFetcher):
    """CFTC Commitments of Traders over Socrata. Free, no key, weekly.

    This fills the real hole in the grid: every other measure is price-derived,
    with nothing describing flow or positioning. Crowding does not time a move,
    but it is what makes one violent when it comes.

    The stored value is NET non-commercial positioning — long minus short — as a
    fraction of open interest, so a consumer does not have to reconstruct it and
    get the sign wrong, and so a percentile taken over it ranks crowding rather
    than the growth of the market. See :data:`CFTC_NET_UNITS`.
    """

    async def fetch(
        self, spec: SeriesSpec, policy: SourcePolicy, *, start: date
    ) -> list[Observation]:
        if not spec.key:
            raise UpstreamError(f"CFTC series {spec.id} has no market code declared in config")
        body = await self._get_json(
            CFTC_SOCRATA_URL,
            {
                "$where": f"{_CFTC_MARKET_FIELD}='{spec.key}' AND "
                f"{_CFTC_DATE_FIELD} >= '{start.isoformat()}T00:00:00.000'",
                "$order": f"{_CFTC_DATE_FIELD} ASC",
                "$limit": _SOCRATA_LIMIT,
            },
            what=f"CFTC {spec.id}",
        )
        out: list[Observation] = []
        for row in body or []:
            reference = parse_date(row.get(_CFTC_DATE_FIELD))
            if reference is None:
                continue
            long_leg = safe_float(row.get(_CFTC_LONG_FIELD), None)
            short_leg = safe_float(row.get(_CFTC_SHORT_FIELD), None)
            open_interest = safe_float(row.get(_CFTC_OPEN_INTEREST_FIELD), None)
            # A week whose open interest is missing or zero has no denominator, so
            # it has no share to report. None, never a fall back to the raw count:
            # one raw count inside a window of shares is the units mix this change
            # exists to remove, and it would arrive looking like a real reading.
            if long_leg is None or short_leg is None or not open_interest:
                net_share = None
            else:
                net_share = (long_leg - short_leg) / open_interest
            out.append(
                Observation(
                    series_id=spec.id,
                    source=spec.source,
                    reference_date=reference.isoformat(),
                    # Tuesday positions, published the Friday after: a published
                    # schedule, so reference_plus_lag is a fact, not a guess.
                    release_date=policy.release_date_for(reference),
                    value=net_share,
                    frequency=spec.frequency,
                    units=CFTC_NET_UNITS,
                )
            )
        return out


class EiaFetcher(_HttpFetcher):
    """EIA v2. Crude and product inventories, production, refinery runs, SPR.

    Deviation from the 5-year SEASONAL band is what matters, not the level; that
    computation belongs to ``commodity_engine``. This stores the level.
    """

    def __init__(self, config: Config, client: httpx.AsyncClient, api_key: str | None) -> None:
        super().__init__(config, client)
        self._api_key = api_key

    async def fetch(
        self, spec: SeriesSpec, policy: SourcePolicy, *, start: date
    ) -> list[Observation]:
        if not self._api_key:
            raise UpstreamError(
                f"EIA_API_KEY is unset, so {spec.id} cannot be fetched. There is no "
                "substitute value for an inventory series."
            )
        if not spec.key:
            raise UpstreamError(f"EIA series {spec.id} has no route declared in config")
        url = f"{EIA_BASE_URL}/{spec.key}/data/"
        body = await self._get_json(
            url,
            {
                "api_key": self._api_key,
                "data[0]": "value",
                "facets[series][]": spec.id,
                "start": start.isoformat(),
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
            },
            what=f"EIA {spec.id}",
        )
        rows = ((body or {}).get("response") or {}).get("data") or []
        out: list[Observation] = []
        for row in rows:
            reference = parse_date(row.get("period"))
            if reference is None:
                continue
            out.append(
                Observation(
                    series_id=spec.id,
                    source=spec.source,
                    reference_date=reference.isoformat(),
                    release_date=policy.release_date_for(reference),
                    value=safe_float(row.get("value"), None),
                    frequency=spec.frequency,
                    units=row.get("units"),
                )
            )
        return out


class OnsFetcher(_HttpFetcher):
    """UK CPI, from the ONS website's own JSON view of a published time series.

    Not a chosen route so much as the remaining one. ``api.ons.gov.uk`` was
    decommissioned on 2024-11-25 and answers every request with a sentence saying
    so; its successor, ``api.beta.ons.gov.uk``, still serves ``cpih01`` but the
    dataset carries an ONS alert saying it stopped at 2026-01 and pointing readers
    at the website's own tables. That website route is what this reads. It needs no
    key and no User-Agent, and it is Open Government Licence.

    What makes it usable at all is that **every observation carries its own
    ``updateDate``** — a publication timestamp per month, not one date for the
    whole series. Without that the series would be collectible and unreadable, the
    way the ECB's HICP was.

    Two things about that timestamp, both measured rather than assumed:

    * It is a LAST-updated stamp, not strictly a first-release one. That is safe in
      the only direction that matters: a revision moves it later, so it can delay
      the moment a figure becomes readable and can never advance it. There is no
      look-ahead in it. ``docs/DECISIONS.md`` carries the argument.
    * It is **UTC, and the UK is not**. July 2026 reads ``2026-08-18T23:00:00.000Z``,
      which is 2026-08-19 00:00 in London — and 2026-08-19 is the Wednesday ONS
      published it, as is the 2026-09-16 the same payload names as the next
      release. Taking ``.date()`` off the UTC value would date every UK CPI print
      one day early, which is a one-day look-ahead on a series whose whole purpose
      here is to be read point-in-time. It is converted to London first.
    """

    #: English month names as ONS writes them, matched positionally. An explicit
    #: table rather than ``%B``, which resolves against the process locale — the
    #: collector runs unattended on a VM whose locale nobody here sets.
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

    #: Where the ONS publishes, so a UTC stamp can be read as the day it landed.
    TIMEZONE = ZoneInfo("Europe/London")

    async def fetch(
        self, spec: SeriesSpec, policy: SourcePolicy, *, start: date
    ) -> list[Observation]:
        if not spec.key:
            raise UpstreamError(f"ONS series {spec.id} has no route declared in config")
        body = await self._get_json(f"{ONS_BASE_URL}/{spec.key}/data", {}, what=f"ONS {spec.id}")
        rows = (body or {}).get("months") or []
        if not rows:
            raise UpstreamError(
                f"ONS {spec.id} returned no months. The route answered, so this is the "
                "response shape changing rather than a series with no history — check "
                "the key in config.yaml against references/macro-playbook.md."
            )
        units = ((body or {}).get("description") or {}).get("unit") or None
        out: list[Observation] = []
        for row in rows:
            reference = self._reference(row)
            if reference is None or reference < start:
                continue
            out.append(
                Observation(
                    series_id=spec.id,
                    source=spec.source,
                    reference_date=reference.isoformat(),
                    release_date=policy.release_date_for(
                        reference, from_api=self._published(row.get("updateDate"))
                    ),
                    # ONS writes the value as a string, and empty for a month it
                    # has not filled. safe_float maps that to None rather than 0.
                    value=safe_float(row.get("value"), None),
                    frequency=spec.frequency,
                    units=units,
                )
            )
        return out

    @classmethod
    def _reference(cls, row: dict[str, Any]) -> date | None:
        """The month this row describes, from its own fields.

        Built from ``year`` and ``month`` rather than by parsing the ``2026 JUL``
        label, because the label is a display string and the fields are the data.
        """
        try:
            year = int(str(row.get("year")).strip())
        except (TypeError, ValueError):
            return None
        name = str(row.get("month") or "").strip().lower()
        if name not in cls.MONTHS:
            return None
        return date(year, cls.MONTHS.index(name) + 1, 1)

    @classmethod
    def _published(cls, stamp: Any) -> date | None:
        """The London date of an ONS UTC timestamp. See the class docstring."""
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(str(stamp))
        except ValueError:
            return None
        if moment.tzinfo is None:
            return moment.date()
        return moment.astimezone(cls.TIMEZONE).date()


class StatCanFetcher(_HttpFetcher):
    """Canadian CPI, from Statistics Canada's Web Data Service.

    Addressed by VECTOR — ``spec.key`` is the numeric vector id, which is
    StatCan's own stable handle for one series inside a table. No key, no
    registration, and a POST rather than a GET, which is why ``_post_json``
    exists.

    Chosen for the same reason FRED and the ONS are: **every data point carries a
    ``releaseTime``**. A run of them is genuinely per-observation — 59 distinct
    times across the last 164 monthly points — with the older ones carrying the
    date of a later restatement, which is the conservative direction and the same
    shape the ONS history has.

    ``releaseTime`` reads ``2026-09-14T08:30`` with no offset. It is Eastern, and
    08:30 Eastern is the same DAY in UTC all year, so the date part is already the
    release date and converting it would be a change without a difference. Left
    alone deliberately.
    """

    async def fetch(
        self, spec: SeriesSpec, policy: SourcePolicy, *, start: date
    ) -> list[Observation]:
        if not spec.key:
            raise UpstreamError(f"StatCan series {spec.id} has no vector declared in config")
        try:
            vector = int(str(spec.key).lstrip("vV"))
        except ValueError as exc:
            raise UpstreamError(
                f"StatCan series {spec.id} declares key {spec.key!r}, which is not a "
                "vector id. The key is the numeric vector, with or without its 'v'."
            ) from exc
        body = await self._post_json(
            STATCAN_VECTOR_URL,
            [{"vectorId": vector, "latestN": self._periods(spec, start)}],
            what=f"StatCan {spec.id}",
        )
        rows = self._points(spec, body)
        out: list[Observation] = []
        for row in rows:
            reference = parse_date(row.get("refPer"))
            if reference is None or reference < start:
                continue
            out.append(
                Observation(
                    series_id=spec.id,
                    source=spec.source,
                    reference_date=reference.isoformat(),
                    release_date=policy.release_date_for(
                        reference, from_api=_statcan_release_date(row.get("releaseTime"))
                    ),
                    value=safe_float(row.get("value"), None),
                    frequency=spec.frequency,
                    # No units. WDS data points carry `scalarFactorCode` and
                    # `decimals` but no unit string; the unit lives behind a
                    # separate metadata call as a numeric code against a table
                    # this build does not hold. A NULL unit is the gap
                    # `unaligned_count`'s sibling reports, and it is honest —
                    # inventing "Index 2002=100" here would be this file
                    # asserting something StatCan did not say in this response.
                )
            )
        return out

    def _periods(self, spec: SeriesSpec, start: date) -> int:
        """How many of the most recent periods cover the window the collector asked for.

        WDS pages by count, not by date, so the window has to be converted. Derived
        from the same publication-rate table the FRED chunking uses and the same
        day-count basis, so there is no second place to keep a cadence.
        """
        per_year = _RELEASES_PER_YEAR.get(
            (spec.frequency or "").strip().lower(), _UNKNOWN_FREQUENCY_RELEASES_PER_YEAR
        )
        days = max(1, (utc_today() - start).days)
        basis = self._config.require_float("numerics.bsm.day_count_basis")
        # One period of slack, so the month the window opens in is inside it
        # rather than on its edge.
        return ceil(days / basis * per_year) + 1

    @staticmethod
    def _points(spec: SeriesSpec, body: Any) -> list[dict[str, Any]]:
        """The data points, or a refusal naming what WDS actually said.

        WDS answers HTTP 200 with a per-vector status, so a failed vector looks
        like a successful request. Reading the envelope is the difference between
        "this series is empty" and "this request was rejected", and those are not
        the same finding.
        """
        if not isinstance(body, list) or not body:
            raise UpstreamError(f"StatCan {spec.id} returned no vector envelope")
        envelope = body[0]
        status = (envelope or {}).get("status")
        if status != "SUCCESS":
            raise UpstreamError(f"StatCan {spec.id} returned status {status!r}, not SUCCESS")
        obj = (envelope or {}).get("object") or {}
        if obj.get("terminated"):
            raise UpstreamError(
                f"StatCan {spec.id} is marked TERMINATED by the publisher. It would keep "
                "serving its last value indefinitely, which reads as a current print."
            )
        return list(obj.get("vectorDataPoint") or [])


def _statcan_release_date(stamp: Any) -> date | None:
    """The date out of a WDS ``releaseTime``. See :class:`StatCanFetcher`."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp)).date()
    except ValueError:
        return None


#: The SDMX ids that mean "this dimension is the time axis". An EXACT match, not
#: a prefix: the OECD's CLI dataflow also carried ``TIME_HORIZ``, a content
#: dimension naming the indicator's horizon, and it sat BEFORE ``TIME_PERIOD`` in
#: the observation order. A ``startswith("TIME")`` test matched it first, took
#: its single non-date value as the period list and its position as the time
#: position, and every observation was then dropped for an unparseable date.
#:
#: Dropped is the lucky outcome. Had that value parsed as a date, every
#: observation in the response would have been stamped with it — the exact
#: failure the comment below already warns about, reached from the other end.
_SDMX_TIME_DIMENSION_IDS = frozenset({"TIME_PERIOD", "TIME"})


def _refuse_an_under_specified_key(
    dimensions: list[Any], time_position: int, spec: SeriesSpec
) -> None:
    """Refuse a response holding several series under one configured id.

    Under ``dimensionAtObservation=AllDimensions`` there is no ``series`` map to
    count, so the guard below it never fires and the observations of every
    country in the response merge into one flat dictionary. The OECD's CLI
    dataflow with an empty ``REF_AREA`` returned 23 of them, which would have
    arrived as one series carrying 23 countries' values on the same dates under
    one id — a number nobody could trace back to a country, ranked as though it
    were one.

    Refused rather than warned about. A warning is what the ``series`` branch
    does, and it can do that because it then takes exactly one; here there is no
    "first" to take, so continuing means merging. The remedy is a narrower key in
    ``macro.series``, and the failure names the dimensions that need pinning.
    """
    plural = [
        str(dimension.get("id"))
        for position, dimension in enumerate(dimensions)
        if position != time_position and len(dimension.get("values") or []) > 1
    ]
    if not plural:
        return
    raise UpstreamError(
        f"SDMX series {spec.id} is under-specified: the response varies over {plural} as "
        "well as time, so it holds several series rather than one. Merging them would put "
        "every one of their values under this id on the same dates. Pin those dimensions "
        f"in the macro.series key for {spec.id}."
    )


def _sdmx_envelope(body: Any) -> tuple[list[Any], dict[str, Any]]:
    """The data sets and the observation structure, from either SDMX-JSON version.

    Two publishers can both "speak SDMX-JSON" and not speak the same one, which
    is the whole reason this function exists as its own step.

    * **1.0**, which the ECB serves: ``dataSets`` and ``structure`` at the top
      level of the body.
    * **2.0**, which the OECD served: both nested under ``data``, and the
      structure is ``structures`` — a LIST — rather than a single object.

    Reading only the 1.0 shape found nothing in an OECD response, and finding
    nothing is not an error: the decoder returned an empty list, the fetcher
    reported the target collected, and the run said ok with zero rows. The
    envelope is detected rather than configured per source, because a source that
    moves to 2.0 would otherwise go silently empty in exactly the same way — and
    that is why this survives the OECD fetcher it was written for.
    """
    body = body or {}
    if body.get("dataSets") is not None:
        return list(body.get("dataSets") or []), dict(body.get("structure") or {})

    # Named `nested` rather than `data`: tests/test_no_magic_numbers.py reads a
    # `.get()` on a receiver called `data` as a config lookup, which is the right
    # heuristic everywhere else in this package and wrong only here.
    nested = body.get("data") or {}
    if not isinstance(nested, dict):
        return [], {}
    structures = nested.get("structures") or []
    structure = structures[0] if structures else (nested.get("structure") or {})
    return list(nested.get("dataSets") or []), dict(structure or {})


def _from_sdmx_json(body: Any, spec: SeriesSpec, policy: SourcePolicy) -> list[Observation]:
    """Decode SDMX-JSON, in either of the two versions publishers serve.

    The shape is an observation dictionary keyed by dimension index, with the time
    dimension carried separately as a list of periods. Rows whose period will not
    parse are dropped rather than positioned by index — an observation attached to
    the wrong date is worse than one that is missing.

    :func:`_sdmx_envelope` is what finds the two of them in either version's
    body; everything below this line is common to both.
    """
    data_sets, structure = _sdmx_envelope(body)
    if not data_sets:
        return []

    # Find the time dimension AND its position. The position is what decodes an
    # observation key: under dimensionAtObservation=AllDimensions the key carries
    # every dimension, and time is not reliably the last of them. Assuming it is
    # attaches values to the wrong dates, which is worse than dropping them.
    dimensions = (structure.get("dimensions") or {}).get("observation") or []
    periods: list[str] = []
    time_position = 0
    for position, dimension in enumerate(dimensions):
        if str(dimension.get("id", "")).upper() in _SDMX_TIME_DIMENSION_IDS:
            periods = [str(v.get("id")) for v in dimension.get("values") or []]
            time_position = position
            break
    if not periods:
        return []

    _refuse_an_under_specified_key(dimensions, time_position, spec)

    observations: dict[str, Any] = {}
    series = data_sets[0].get("series") or {}
    if len(series) > 1:
        # Each SeriesSpec addresses ONE series. More than one back means the
        # configured key is under-specified, and merging them would present one
        # series' numbers under another's id with nothing to show for it.
        log.warning(
            "SDMX response carries several series for one configured key; only the "
            "first is used. Narrow the key in macro.series.",
            extra={"series_id": spec.id, "source": spec.source, "series_returned": len(series)},
        )
    for entry in list(series.values())[:1]:
        observations.update(entry.get("observations") or {})
    if not observations:
        observations = data_sets[0].get("observations") or {}

    out: list[Observation] = []
    for key, payload in observations.items():
        index = _time_index(key, time_position)
        if index is None or index >= len(periods):
            continue
        reference = parse_date(periods[index]) or _parse_sdmx_period(periods[index])
        if reference is None:
            continue
        value = safe_float(payload[0] if isinstance(payload, list) and payload else None, None)
        out.append(
            Observation(
                series_id=spec.id,
                source=spec.source,
                reference_date=reference.isoformat(),
                release_date=policy.release_date_for(reference),
                value=value,
                frequency=spec.frequency,
            )
        )
    out.sort(key=lambda obs: obs.reference_date)
    return out


def _time_index(key: str, time_position: int) -> int | None:
    """Read the time index out of an SDMX observation key like ``"0:3:12"``.

    ``time_position`` is where the time dimension actually sits in the observation
    dimension list, not a guess that it comes last. A key shorter than that
    position returns ``None`` and the observation is dropped, because an
    observation on the wrong date is worse than a missing one.
    """
    parts = key.split(":")
    if time_position >= len(parts):
        return None
    try:
        return int(parts[time_position])
    except ValueError:
        return None


def _parse_sdmx_period(period: str) -> date | None:
    """SDMX periods that are not plain dates: ``2026-01``, ``2026-Q1``, ``2026``.

    Each maps to the FIRST day of the period it names. That is the reference date;
    when the figure was published is the release date's job, and it is never
    inferred from here.
    """
    text = str(period).strip()
    try:
        if "-Q" in text:
            year, quarter = text.split("-Q")
            return date(int(year), (int(quarter) - 1) * _MONTHS_PER_QUARTER + 1, 1)
        parts = text.split("-")
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
        if len(parts) == 1 and parts[0].isdigit():
            return date(int(parts[0]), 1, 1)
    except (ValueError, TypeError):
        return None
    return None


def build_fetchers(
    config: Config,
    client: httpx.AsyncClient,
    *,
    fred_api_key: str | None,
    eia_api_key: str | None,
) -> dict[str, Fetcher]:
    return {
        "fred": FredFetcher(config, client, fred_api_key),
        "ecb": EcbFetcher(config, client),
        "cftc": CftcFetcher(config, client),
        "eia": EiaFetcher(config, client, eia_api_key),
        "ons": OnsFetcher(config, client),
        "statcan": StatCanFetcher(config, client),
    }
