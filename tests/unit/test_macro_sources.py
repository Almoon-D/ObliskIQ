"""Macro sources: release dates by rule, the backtest guard, the SDMX decoder, each fetcher on its
recorded answer, and the collector's guards (units, refetch window, a dead source, an empty answer).

Ported in intent from lite's ``tests/test_macro_pull.py`` and ``test_macro_series.py``. The
release date is the load-bearing detail: aligning a print to its reference month lets every number
derived from it know the figure weeks before the market did.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.data.macro import catalog as catalog_module
from obliskiq.data.macro.catalog import (
    MacroCatalog,
    NotPointInTimeError,
    SeriesSpec,
    backtest_series,
)
from obliskiq.data.macro.fetchers import (
    BisFetcher,
    BojFetcher,
    EcbFetcher,
    EiaFetcher,
    Fetched,
    FrbFetcher,
    FredFetcher,
    OnsFetcher,
    StatcanFetcher,
    Window,
    build_fetchers,
)
from obliskiq.data.macro.pull import MacroPull, UnitsChangedError
from obliskiq.data.macro.sdmx import decode, envelope, parse_period
from obliskiq.data.market.plumbing import Pacer
from obliskiq.domain.macro import MacroObservation
from obliskiq.errors import UpstreamError
from obliskiq.store import Store, repos
from obliskiq.store import sources as source_repo
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "macro"
NOW = datetime(2026, 10, 23, 7, 0, tzinfo=UTC)
TODAY = NOW.date()
FRED = "https://api.stlouisfed.org/fred/series"
WIDE = ("1900-01-01", "2100-01-01")


def fixture(name: str) -> Any:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def spec(**overrides: Any) -> SeriesSpec:
    fields: dict[str, Any] = {
        "id": "CPIAUCSL",
        "source": "fred",
        "frequency": "monthly",
        "units": "Index 1982-1984=100",
        "group": "cpi",
        "key": None,
        "value_vintage": "initial",
        "policy": "from_api",
        "lag_days": 0,
        "consumers": ("view:macro_board",),
    }
    fields.update(overrides)
    return SeriesSpec(**fields)


def no_retries(settings: ObliskConfig) -> RetryPolicy:
    return RetryPolicy.from_settings(settings.http.model_copy(update={"retries": 0}))


async def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


def monthly_history(settings: ObliskConfig) -> Any:
    return settings.sources.macro.commodity_history.model_copy(update={"frequency": "monthly"})


def fred_routes(
    router: respx.MockRouter, observations: Any, *, vintages: tuple[str, str] = WIDE
) -> respx.Route:
    first, last = vintages

    def vintage(request: httpx.Request) -> httpx.Response:
        ascending = request.url.params.get("sort_order") == "asc"
        return httpx.Response(200, json={"vintage_dates": [first if ascending else last]})

    router.get(f"{FRED}/vintagedates").mock(side_effect=vintage)
    router.get(FRED).mock(
        return_value=httpx.Response(200, json={"seriess": [{"units_short": "%"}]})
    )
    return router.get(f"{FRED}/observations").mock(side_effect=observations)


# ── release dates and the backtest guard ──


def test_a_release_date_is_never_silently_the_reference_date() -> None:
    reference = date(2026, 1, 1)
    assert spec(policy="unknown").release_date_for(reference) is None
    assert spec(policy="reference_plus_lag", lag_days=3).release_date_for(reference) == date(
        2026, 1, 4
    )
    assert spec(policy="same_as_reference").release_date_for(reference) == reference
    stated = date(2026, 2, 12)
    assert spec(policy="from_api").release_date_for(reference, from_api=stated) == stated
    assert spec(policy="from_api").release_date_for(reference) is None
    assert spec(policy="as_collected").release_date_for(reference, collected_on=TODAY) == TODAY


def test_the_catalog_takes_the_source_rule_unless_a_series_states_its_own(
    shipped_settings: ObliskConfig,
) -> None:
    catalog = MacroCatalog.from_settings(shipped_settings)
    assert catalog.spec("CPIAUCSL").policy == "from_api"  # type: ignore[union-attr]
    estr = catalog.spec("ESTR")
    assert estr is not None
    assert (estr.policy, estr.lag_days) == ("reference_plus_lag", 1)
    eia = catalog.spec("WCESTUS1")
    assert eia is not None
    assert (eia.policy, eia.lag_days) == ("reference_plus_lag", 10)
    assert catalog.spec("NOT_DECLARED") is None
    assert {s.id for s in catalog.of_source("frb")} == {"EBP", "GZ_SPREAD"}
    assert catalog.spec("DCOILWTICO").is_commodity_history  # type: ignore[union-attr]


def test_every_declared_series_has_a_release_rule_a_backtest_can_use(
    shipped_settings: ObliskConfig,
) -> None:
    """No declared series is left on its source's ``unknown`` rule."""
    catalog = MacroCatalog.from_settings(shipped_settings)
    assert [s.id for s in catalog.specs.values() if not s.backtest_usable] == []


def test_no_source_without_a_release_date_policy_can_feed_a_backtest(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    """The acceptance test: an undeclared series and one whose rule is unknown are refused."""
    catalog = MacroCatalog.from_settings(shipped_settings)
    repos.upsert_macro(
        store,
        [
            MacroObservation(
                series_id="ECB_SOMETHING",
                source="ecb",
                reference_date=date(2026, 9, 1),
                release_date=None,
                value=1.0,
                frequency="monthly",
                units="%",
                collected_at=NOW,
            )
        ],
    )
    with pytest.raises(NotPointInTimeError, match="not declared"):
        backtest_series(store, catalog, "ECB_SOMETHING", as_of=TODAY)
    undated = MacroCatalog(
        {**catalog.specs, "ECB_SOMETHING": spec(id="ECB_SOMETHING", source="ecb", policy="unknown")}
    )
    with pytest.raises(NotPointInTimeError, match="never a backtest"):
        backtest_series(store, undated, "ECB_SOMETHING", as_of=TODAY)
    assert "unknown" not in catalog_module.BACKTEST_POLICIES


def test_a_dated_series_feeds_a_backtest_aligned_on_its_release(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    catalog = MacroCatalog.from_settings(shipped_settings)
    row = MacroObservation(
        series_id="CPIAUCSL",
        source="fred",
        reference_date=date(2026, 9, 1),
        release_date=date(2026, 10, 15),
        value=330.0,
        frequency="monthly",
        units="Index 1982-1984=100",
        collected_at=NOW,
    )
    repos.upsert_macro(store, [row])
    assert backtest_series(store, catalog, "CPIAUCSL", as_of=date(2026, 10, 14)).observations == ()
    assert backtest_series(store, catalog, "CPIAUCSL", as_of=date(2026, 10, 15)).latest == row


def test_a_flash_release_before_its_period_ends_is_stored(store: Store) -> None:
    """Migration 010 dropped 001's check that refused a release before the reference date."""
    flash = MacroObservation(
        series_id="HICP_FLASH",
        source="ecb",
        reference_date=date(2026, 10, 31),
        release_date=date(2026, 10, 30),
        value=2.1,
        frequency="monthly",
        units="%",
        collected_at=NOW,
    )
    repos.upsert_macro(store, [flash])
    assert repos.macro_series(store, "HICP_FLASH", as_of=date(2026, 10, 30)).latest == flash


# ── SDMX ──


def test_sdmx_periods_map_to_the_first_day_of_the_period_they_name() -> None:
    assert parse_period("2026-10-22") == date(2026, 10, 22)
    assert parse_period("2026-09") == date(2026, 9, 1)
    assert parse_period("2026-Q3") == date(2026, 7, 1)
    assert parse_period("2026-S2") == date(2026, 7, 1)
    assert parse_period("2026") == date(2026, 1, 1)
    for bad in ("2026-Q9", "next week", "2026-13", "", "2026-W40-1"):
        assert parse_period(bad) is None, bad


def test_the_ecb_answer_decodes_oldest_first() -> None:
    points = decode(fixture("ecb_dfr.json"), "ECB_DFR")
    assert points[0][0] == date(2026, 10, 15)
    assert all(value == 2.5 for _, value in points)
    assert [day for day, _ in points] == sorted(day for day, _ in points)


def sdmx2(dimensions: list[dict[str, Any]], observations: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {
            "structures": [{"dimensions": {"observation": dimensions}}],
            "dataSets": [{"observations": observations}],
        }
    }


def test_sdmx_json_2_is_a_different_envelope_and_the_decoder_reads_it() -> None:
    body = sdmx2(
        [
            {"id": "REF_AREA", "values": [{"id": "DE"}]},
            {"id": "TIME_HORIZ", "values": [{"id": "H"}]},
            {"id": "TIME_PERIOD", "values": [{"id": "2026-01"}, {"id": "2026-02"}]},
        ],
        {"0:0:1": [100.5], "0:0:0": [100.1], "0:0:7": [9.9], "0:0:x": [1.0], "0": [1.0]},
    )
    assert decode(body, "CLI") == [(date(2026, 1, 1), 100.1), (date(2026, 2, 1), 100.5)]


def test_a_response_holding_several_series_is_refused_not_merged() -> None:
    body = sdmx2(
        [
            {"id": "REF_AREA", "values": [{"id": "DE"}, {"id": "FR"}]},
            {"id": "TIME_PERIOD", "values": [{"id": "2026-01"}]},
        ],
        {"0:0": [1.0], "1:0": [2.0]},
    )
    with pytest.raises(UpstreamError, match="under-specified"):
        decode(body, "CLI")


def test_an_envelope_without_data_or_time_is_empty_not_invented() -> None:
    assert envelope(["not", "a", "mapping"]) == ([], {})
    assert envelope({"data": "nope"}) == ([], {})
    assert decode({"data": {"structure": {}, "dataSets": []}}, "X") == []
    no_time = {"dataSets": [{"series": {}}], "structure": {"dimensions": {"observation": []}}}
    assert decode(no_time, "X") == []
    bad_period = sdmx2([{"id": "TIME", "values": [{"id": "soon"}]}], {"0": [1.0]})
    assert decode(bad_period, "X") == []


def test_several_series_under_one_key_keep_only_the_first(caplog: pytest.LogCaptureFixture) -> None:
    body = {
        "dataSets": [
            {"series": {"0:0": {"observations": {"0": [1.0]}}, "0:1": {"observations": {"0": [2]}}}}
        ],
        "structure": {
            "dimensions": {"observation": [{"id": "TIME_PERIOD", "values": [{"id": "2026"}]}]}
        },
    }
    assert decode(body, "X") == [(date(2026, 1, 1), 1.0)]
    assert "several series" in caplog.text


# ── FRED ──


async def test_fred_asks_for_the_initial_release_so_the_date_is_a_fact(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    seen: dict[str, str] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2025-12-01", "realtime_start": "2026-01-13", "value": "319.775"},
                    {"date": "2025-11-01", "realtime_start": "2025-12-10", "value": "318.400"},
                    {"date": "never", "realtime_start": "2025-12-10", "value": "1"},
                ]
            },
        )

    with respx.mock as router:
        fred_routes(router, answer)
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        rows = await fetcher.fetch(spec(), Window(date(2016, 1, 1), TODAY))
    assert seen["output_type"] == "4"
    assert seen["realtime_start"] == seen["observation_start"]
    assert seen["realtime_end"] == seen["observation_end"]
    assert "frequency" not in seen
    assert [(r.reference_date, r.release_date, r.units) for r in rows] == [
        (date(2025, 11, 1), date(2025, 12, 10), "%"),
        (date(2025, 12, 1), date(2026, 1, 13), "%"),
    ]


async def test_a_daily_series_is_split_and_a_monthly_one_is_not(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    windows: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        windows.append((request.url.params["realtime_start"], request.url.params["realtime_end"]))
        return httpx.Response(200, json={"observations": []})

    with respx.mock as router:
        fred_routes(router, answer)
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        await fetcher.fetch(spec(), Window(date(2016, 1, 1), TODAY))
        assert len(windows) == 1
        windows.clear()
        await fetcher.fetch(spec(id="DGS10", frequency="daily"), Window(date(2016, 1, 1), TODAY))
    assert len(windows) > 1
    for (_, first_end), (second_start, _) in pairwise(windows):
        assert date.fromisoformat(second_start) == date.fromisoformat(first_end) + timedelta(days=1)
    assert windows[-1][1] == TODAY.isoformat()


async def test_the_earliest_release_wins_and_an_undated_row_never_displaces_a_dated_one(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    answers = iter(
        [
            [{"date": "2020-06-01", "realtime_start": "2020-08-15", "value": "2.0"}],
            [{"date": "2020-06-01", "realtime_start": "2020-07-01", "value": "2.0"}],
            [{"date": "2020-06-01", "realtime_start": "", "value": "2.0"}],
            [{"date": "2020-07-01", "realtime_start": "", "value": "2.0"}],
            [{"date": "2020-07-01", "realtime_start": "2020-08-01", "value": "2.0"}],
        ]
    )

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"observations": next(answers, [])})

    with respx.mock as router:
        fred_routes(router, answer)
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        # Thirty-six years of a daily series: five windows, one answer each.
        rows = await fetcher.fetch(
            spec(id="DGS10", frequency="daily"), Window(date(1990, 1, 1), TODAY)
        )
    assert [(r.reference_date, r.release_date) for r in rows] == [
        (date(2020, 6, 1), date(2020, 7, 1)),
        (date(2020, 7, 1), date(2020, 8, 1)),
    ]


async def test_a_missing_fred_value_is_absent_not_zero(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2025-12-01", "realtime_start": "2026-01-13", "value": "."}
                ]
            },
        )

    with respx.mock as router:
        fred_routes(router, answer)
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        rows = await fetcher.fetch(spec(), Window(date(2025, 1, 1), TODAY))
    assert rows[0].value is None


async def test_the_window_is_clamped_to_the_vintages_alfred_holds(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    windows: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        windows.append((request.url.params["realtime_start"], request.url.params["realtime_end"]))
        return httpx.Response(200, json={"observations": []})

    with respx.mock as router:
        fred_routes(router, answer, vintages=("2014-12-10", "2026-09-02"))
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        await fetcher.fetch(
            spec(id="DHHNGSP", frequency="daily", group="commodity_history"),
            Window(date(2006, 9, 9), TODAY),
        )
    assert windows[0][0] == "2014-12-10"
    assert windows[-1][1] == "2026-09-02"


async def test_a_window_holding_no_vintage_and_a_series_without_any_are_refused(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    def never(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("no observations call is made")

    fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
    with respx.mock as router:
        fred_routes(router, never, vintages=("1998-01-01", "1999-01-01"))
        with pytest.raises(UpstreamError, match="1999-01-01"):
            await fetcher.fetch(spec(), Window(date(2016, 1, 1), TODAY))
    with respx.mock as router:
        router.get(f"{FRED}/vintagedates").mock(
            return_value=httpx.Response(200, json={"vintage_dates": []})
        )
        with pytest.raises(UpstreamError, match="no vintage"):
            await fetcher.fetch(spec(), Window(date(2016, 1, 1), TODAY))


async def test_a_window_of_one_day_is_one_call(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
    one = date(2026, 1, 1)
    assert fetcher.windows(spec(), Window(one, TODAY), (one, one)) == [(one, one)]


async def test_fred_without_a_key_refuses_and_asks_nothing(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), None)
    with respx.mock(assert_all_called=False), pytest.raises(UpstreamError, match="FRED_API_KEY"):
        await fetcher.fetch(spec(), Window(date(2016, 1, 1), TODAY))


async def test_a_refused_or_garbled_fred_answer_is_an_upstream_error(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
    with respx.mock as router:
        router.get(f"{FRED}/vintagedates").mock(
            return_value=httpx.Response(400, text="Bad Request")
        )
        with pytest.raises(UpstreamError, match="HTTP 400"):
            await fetcher.fetch(spec(), Window(date(2016, 1, 1), TODAY))
    with respx.mock as router:
        router.get(f"{FRED}/vintagedates").mock(return_value=httpx.Response(200, text="<html>"))
        with pytest.raises(UpstreamError, match="not JSON"):
            await fetcher.fetch(spec(), Window(date(2016, 1, 1), TODAY))


async def test_a_rebased_index_is_valued_from_one_vintage_and_dated_from_the_first(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    """Eurostat rebased HICP to 2025=100 during 2026: the initial releases change base between
    2025-12 and 2026-01, so a year-on-year ratio across them reads as a 22 % deflation."""
    initial = fixture("fred_hicp_de_initial_release.json")
    latest = fixture("fred_hicp_de_latest_vintage.json")
    first_rows = {row["date"]: row for row in initial["observations"]}
    assert float(first_rows["2025-12-01"]["value"]) > float(first_rows["2026-01-01"]["value"]) * 1.2

    def answer(request: httpx.Request) -> httpx.Response:
        if "output_type" in request.url.params:
            assert "realtime_start" in request.url.params
            return httpx.Response(200, json=initial)
        assert "realtime_start" not in request.url.params
        return httpx.Response(200, json=latest)

    with respx.mock as router:
        fred_routes(router, answer, vintages=("2012-06-15", "2026-09-15"))
        router.get(FRED).mock(
            return_value=httpx.Response(200, json=fixture("fred_hicp_de_series.json"))
        )
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        rows = await fetcher.fetch(
            spec(id="CP0000DEM086NEST", value_vintage="latest"),
            Window(date(2024, 11, 1), date(2026, 9, 15)),
        )
    by_date = {row.reference_date: row for row in rows}
    dec, jan = by_date[date(2025, 12, 1)], by_date[date(2026, 1, 1)]
    assert dec.value is not None
    assert jan.value is not None
    assert abs(jan.value / dec.value - 1) < 0.05  # one base, no -22 %
    assert dec.release_date == date.fromisoformat(first_rows["2025-12-01"]["realtime_start"])
    assert {row.units for row in rows} == {"Index 2025=100"}


async def test_a_period_the_latest_vintage_withdrew_is_dropped_and_one_alfred_lacks_is_undated(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if "output_type" in request.url.params:
            return httpx.Response(
                200,
                json={
                    "observations": [
                        {"date": "2026-01-01", "realtime_start": "2026-02-15", "value": "1"},
                        {"date": "2026-02-01", "realtime_start": "2026-03-15", "value": "1"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2026-01-01", "value": "1.5"},
                    {"date": "2026-03-01", "value": "1.7"},
                    {"date": "garbage", "value": "1.7"},
                ]
            },
        )

    with respx.mock as router:
        fred_routes(router, answer)
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        rows = await fetcher.fetch(spec(value_vintage="latest"), Window(date(2026, 1, 1), TODAY))
    assert [(r.reference_date, r.release_date, r.value) for r in rows] == [
        (date(2026, 1, 1), date(2026, 2, 15), 1.5),
        (date(2026, 3, 1), None, 1.7),
    ]


async def test_commodity_history_is_native_by_default_and_monthly_only_when_asked(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    seen: list[dict[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"observations": []})

    wti = spec(id="DCOILWTICO", frequency="daily", group="commodity_history")
    with respx.mock as router:
        fred_routes(router, answer)
        await FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k").fetch(
            wti, Window(TODAY - timedelta(days=30), TODAY)
        )
        assert "frequency" not in seen[-1]
        monthly = shipped_settings.model_copy(
            update={
                "sources": shipped_settings.sources.model_copy(
                    update={
                        "macro": shipped_settings.sources.macro.model_copy(
                            update={"commodity_history": monthly_history(shipped_settings)}
                        )
                    }
                )
            }
        )
        await FredFetcher(monthly, client, no_retries(monthly), "k").fetch(
            wti, Window(TODAY - timedelta(days=30), TODAY)
        )
    assert (seen[-1]["frequency"], seen[-1]["aggregation_method"]) == ("m", "avg")


async def test_a_series_fred_describes_without_metadata_has_no_units(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        fred_routes(
            router,
            lambda _request: httpx.Response(
                200,
                json={
                    "observations": [
                        {"date": "2026-01-01", "realtime_start": "2026-02-01", "value": "1"}
                    ]
                },
            ),
        )
        router.get(FRED).mock(return_value=httpx.Response(200, json={"seriess": []}))
        fetcher = FredFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        rows = await fetcher.fetch(spec(), Window(date(2026, 1, 1), TODAY))
    assert rows[0].units is None


# ── ECB, EIA, ONS, StatCan ──


async def test_ecb_decodes_its_sdmx_json_and_states_no_release_date(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        route = router.get(
            "https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV"
        ).mock(return_value=httpx.Response(200, json=fixture("ecb_dfr.json")))
        rows = await EcbFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(id="ECB_DFR", source="ecb", key="FM/D.U2.EUR.4F.KR.DFR.LEV"),
            Window(date(2026, 10, 15), TODAY),
        )
    assert route.calls.last.request.url.params["detail"] == "dataonly"
    assert rows
    assert all(r.release_date is None and r.units is None for r in rows)


async def test_eia_decodes_the_v2_envelope_with_its_units(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    body = fixture("eia_wcestus1.json")
    body["response"]["data"].append({"period": None, "value": "1"})
    with respx.mock as router:
        router.get("https://api.eia.gov/v2/petroleum/stoc/wstk/data/").mock(
            return_value=httpx.Response(200, json=body)
        )
        fetcher = EiaFetcher(shipped_settings, client, no_retries(shipped_settings), "k")
        rows = await fetcher.fetch(
            spec(id="WCESTUS1", source="eia", frequency="weekly", key="petroleum/stoc/wstk"),
            Window(date(2026, 9, 25), TODAY),
        )
    assert rows[0].reference_date == date(2026, 9, 25)
    assert {r.units for r in rows} == {"MBBL"}
    assert all(r.value and r.value > 0 for r in rows)
    with pytest.raises(UpstreamError, match="EIA_API_KEY"):
        await EiaFetcher(shipped_settings, client, no_retries(shipped_settings), None).fetch(
            spec(id="WCESTUS1", source="eia"), Window(TODAY, TODAY)
        )


ONS_URL = "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/d7g7/mm23/data"
ONS_KEY = "economy/inflationandpriceindices/timeseries/d7g7/mm23"


async def test_an_ons_release_date_is_the_london_day_not_the_utc_one(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        router.get(ONS_URL).mock(
            return_value=httpx.Response(200, json=fixture("ons_d7g7_mm23.json"))
        )
        rows = await OnsFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(id="UK_CPI_YOY", source="ons", key=ONS_KEY), Window(date(2026, 1, 1), TODAY)
        )
    july = next(r for r in rows if r.reference_date == date(2026, 7, 1))
    assert july.release_date == date(2026, 8, 19)  # 2026-08-18T23:00Z is London's 19th
    assert july.value == 2.9
    assert july.units == "%"
    assert min(r.reference_date for r in rows) == date(2026, 1, 1)


def test_an_ons_month_is_built_from_its_fields_and_a_bad_stamp_is_no_date(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    fetcher = OnsFetcher(shipped_settings, client, no_retries(shipped_settings))
    assert OnsFetcher.reference({"year": "2026", "month": "July", "label": "nonsense"}) == date(
        2026, 7, 1
    )
    assert OnsFetcher.reference({"year": "x", "month": "July"}) is None
    assert OnsFetcher.reference({"year": "2026", "month": "Juillet"}) is None
    assert fetcher.published(None) is None
    assert fetcher.published("not a stamp") is None
    assert fetcher.published("2026-08-19T10:00:00") == date(2026, 8, 19)


async def test_an_ons_body_with_no_months_is_refused(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        router.get(ONS_URL).mock(return_value=httpx.Response(200, json={"months": []}))
        with pytest.raises(UpstreamError, match="no months"):
            await OnsFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
                spec(id="UK_CPI_YOY", source="ons", key=ONS_KEY), Window(date(2024, 1, 1), TODAY)
            )


STATCAN = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods"


async def test_the_statcan_vector_is_read_with_its_release_time(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        route = router.post(STATCAN).mock(
            return_value=httpx.Response(200, json=fixture("statcan_v41690973.json"))
        )
        fetcher = StatcanFetcher(shipped_settings, client, no_retries(shipped_settings))
        rows = await fetcher.fetch(
            spec(id="CA_CPI", source="statcan", key="v41690973"), Window(date(2025, 1, 1), TODAY)
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent[0]["vectorId"] == 41690973
    assert sent[0]["latestN"] > 12
    august = next(r for r in rows if r.reference_date == date(2026, 8, 1))
    assert august.release_date == date(2026, 9, 14)
    assert august.units is None
    assert all(r.reference_date >= date(2025, 1, 1) for r in rows)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ([], "no vector envelope"),
        ([{"status": "FAILED"}], "status 'FAILED'"),
        ([{"status": "SUCCESS", "object": {"terminated": 1}}], "terminated"),
    ],
)
async def test_a_statcan_envelope_that_is_not_a_success_is_refused(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient, body: Any, match: str
) -> None:
    with respx.mock as router:
        router.post(STATCAN).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(UpstreamError, match=match):
            await StatcanFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
                spec(id="CA_CPI", source="statcan", key="41690973"), Window(date(2025, 1, 1), TODAY)
            )


async def test_statcan_refuses_a_key_that_is_no_vector_and_a_bad_post(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    fetcher = StatcanFetcher(shipped_settings, client, no_retries(shipped_settings))
    with pytest.raises(UpstreamError, match="not a vector"):
        await fetcher.fetch(spec(source="statcan", key="cpi"), Window(TODAY, TODAY))
    with respx.mock as router:
        router.post(STATCAN).mock(return_value=httpx.Response(503, text="down"))
        with pytest.raises(UpstreamError, match="HTTP 503"):
            await fetcher.fetch(spec(source="statcan", key="1"), Window(TODAY, TODAY))
    with respx.mock as router:
        router.post(STATCAN).mock(return_value=httpx.Response(200, text="nope"))
        with pytest.raises(UpstreamError, match="not JSON"):
            await fetcher.fetch(spec(source="statcan", key="1"), Window(TODAY, TODAY))
    with respx.mock as router:
        router.post(STATCAN).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "status": "SUCCESS",
                        "object": {
                            "vectorDataPoint": [
                                {"refPer": "2026-09-01", "value": 1, "releaseTime": "soon"},
                                {"refPer": "2026-10-01", "value": 2},
                                {"refPer": "x", "value": 1},
                            ]
                        },
                    }
                ],
            )
        )
        rows = await fetcher.fetch(spec(source="statcan", key="1"), Window(date(2026, 1, 1), TODAY))
    assert [(r.reference_date, r.release_date) for r in rows] == [
        (date(2026, 9, 1), None),
        (date(2026, 10, 1), None),
    ]


# ── Bank of Japan, BIS, Federal Reserve Board ──

BOJ = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"


async def test_the_boj_answer_is_read_with_its_unit_and_dates(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        route = router.get(BOJ).mock(
            return_value=httpx.Response(200, json=fixture("boj_fxerd01.json"))
        )
        rows = await BojFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(id="BOJ_USDJPY", source="boj", frequency="daily", key="FM08/FXERD01"),
            Window(date(2026, 10, 1), TODAY),
        )
    params = route.calls.last.request.url.params
    assert (params["db"], params["code"], params["startDate"]) == ("FM08", "FXERD01", "202610")
    assert rows[-1].reference_date == date(2026, 10, 21)
    assert {r.units for r in rows} == {"Yen per U.S. Dollar"}
    with respx.mock as router:
        router.get(BOJ).mock(return_value=httpx.Response(200, json=fixture("boj_mabs1an11.json")))
        monthly = await BojFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(id="BOJ_MONETARY_BASE", source="boj", key="MD01/MABS1AN11"),
            Window(date(2026, 6, 15), TODAY),
        )
    assert monthly[0].reference_date == date(2026, 6, 1)  # the month the window starts in


async def test_the_boj_answer_continues_from_its_next_position(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    pages = [
        {
            "STATUS": 200,
            "NEXTPOSITION": 2,
            "RESULTSET": [
                {
                    "SERIES_CODE": "X",
                    "UNIT": "u",
                    "VALUES": {"SURVEY_DATES": [20261001], "VALUES": [1.0]},
                },
                {"SERIES_CODE": "OTHER", "VALUES": {"SURVEY_DATES": [20261001], "VALUES": [9.0]}},
            ],
        },
        {
            "STATUS": 200,
            "NEXTPOSITION": None,
            "RESULTSET": [
                {
                    "SERIES_CODE": "X",
                    "UNIT": "u",
                    "VALUES": {"SURVEY_DATES": [20261002, 2026, "20261399"], "VALUES": [2.0, 3, 4]},
                }
            ],
        },
    ]
    answers = iter(pages)
    with respx.mock as router:
        route = router.get(BOJ).mock(side_effect=lambda _r: httpx.Response(200, json=next(answers)))
        rows = await BojFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(id="X", source="boj", key="DB/X"), Window(date(2026, 10, 1), TODAY)
        )
    assert route.calls.last.request.url.params["startPosition"] == "2"
    assert [(r.reference_date, r.value) for r in rows] == [
        (date(2026, 10, 1), 1.0),
        (date(2026, 10, 2), 2.0),
    ]


async def test_the_boj_refuses_a_bad_key_and_an_error_status(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    fetcher = BojFetcher(shipped_settings, client, no_retries(shipped_settings))
    with pytest.raises(UpstreamError, match="not <database>/<code>"):
        await fetcher.fetch(spec(source="boj", key="FXERD01"), Window(TODAY, TODAY))
    with respx.mock as router:
        router.get(BOJ).mock(
            return_value=httpx.Response(200, json={"STATUS": 400, "MESSAGE": "Nonexistent"})
        )
        with pytest.raises(UpstreamError, match="Nonexistent"):
            await fetcher.fetch(spec(source="boj", key="FM08/NOPE"), Window(TODAY, TODAY))


BIS = "https://stats.bis.org/api/v1/data"


async def test_the_bis_csv_is_read_with_its_unit_code(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        route = router.get(f"{BIS}/WS_CREDIT_GAP/Q.US.P.A.C").mock(
            return_value=httpx.Response(200, text=fixture("bis_credit_gap_us.csv"))
        )
        rows = await BisFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(
                id="BIS_CREDIT_GAP_US",
                source="bis",
                frequency="quarterly",
                key="WS_CREDIT_GAP/Q.US.P.A.C",
            ),
            Window(date(2025, 1, 1), TODAY),
        )
    assert route.calls.last.request.url.params["format"] == "csv"
    assert rows[-1] == Fetched(date(2026, 1, 1), -11.3204, None, "770")
    with respx.mock as router:
        router.get(f"{BIS}/WS_CBPOL/D.JP").mock(
            return_value=httpx.Response(200, text=fixture("bis_cbpol_jp.csv"))
        )
        policy = await BisFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(id="BIS_CBPOL_JP", source="bis", frequency="daily", key="WS_CBPOL/D.JP"),
            Window(date(2026, 10, 14), TODAY),
        )
    assert {(r.value, r.units) for r in policy} == {(1.25, "368")}


async def test_a_bis_answer_that_is_not_its_csv_is_refused(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    with respx.mock as router:
        router.get(f"{BIS}/WS_EER/M.N.B.US").mock(
            return_value=httpx.Response(200, text="<xml>no</xml>\n")
        )
        with pytest.raises(UpstreamError, match="without TIME_PERIOD"):
            await BisFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
                spec(source="bis", key="WS_EER/M.N.B.US"), Window(TODAY, TODAY)
            )
    with respx.mock as router:
        router.get(f"{BIS}/WS_EER/M.N.B.US").mock(
            return_value=httpx.Response(
                200, text="TIME_PERIOD,OBS_VALUE,UNIT_MEASURE\n2026-W40,1,882\n2026-09,,\n"
            )
        )
        rows = await BisFetcher(shipped_settings, client, no_retries(shipped_settings)).fetch(
            spec(source="bis", key="WS_EER/M.N.B.US"), Window(TODAY, TODAY)
        )
    assert rows == [Fetched(date(2026, 9, 1), None, None, None)]


EBP_URL = "https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv"


async def test_the_excess_bond_premium_is_read_by_its_column(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    text = fixture("frb_ebp.csv") + "not a date,1,2,3\n"
    with respx.mock as router:
        router.get(EBP_URL).mock(return_value=httpx.Response(200, text=text))
        fetcher = FrbFetcher(shipped_settings, client, no_retries(shipped_settings))
        ebp = await fetcher.fetch(
            spec(id="EBP", source="frb", key="ebp"), Window(date(2026, 7, 20), TODAY)
        )
        gz = await fetcher.fetch(
            spec(id="GZ_SPREAD", source="frb", key="gz_spread"), Window(date(2026, 1, 1), TODAY)
        )
        with pytest.raises(UpstreamError, match="no column"):
            await fetcher.fetch(spec(id="X", source="frb", key="nope"), Window(TODAY, TODAY))
    assert [r.reference_date for r in ebp] == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
    assert ebp[-1].value == pytest.approx(-0.3919245963723856)
    assert len(gz) == 4
    assert gz[0].value == pytest.approx(0.8622082734357994)


def test_every_built_macro_source_has_a_fetcher(
    shipped_settings: ObliskConfig, client: httpx.AsyncClient
) -> None:
    fetchers = build_fetchers(
        shipped_settings, client, no_retries(shipped_settings), fred_api_key=None, eia_api_key=None
    )
    built = {
        name
        for name, provider in shipped_settings.sources.built().items()
        if provider.area == "macro"
    }
    assert set(fetchers) == built


# ── the collector ──


class StubFetcher:
    """Returns fixed rows and counts how often it was asked."""

    def __init__(self, rows: list[Fetched] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls = 0
        self.windows: list[Window] = []

    async def fetch(self, spec: SeriesSpec, window: Window) -> list[Fetched]:
        self.calls += 1
        self.windows.append(window)
        if self.error is not None:
            raise self.error
        return list(self.rows)


@pytest.fixture
def worker(store_worker: StoreWorker) -> StoreWorker:
    return store_worker


def quick(settings: ObliskConfig) -> ObliskConfig:
    retry = settings.collectors.retry.model_copy(
        update={"target_retries": 0, "request_spacing_seconds": 0.0}
    )
    return settings.model_copy(
        update={"collectors": settings.collectors.model_copy(update={"retry": retry})}
    )


def pull(settings: ObliskConfig, worker: StoreWorker, fetchers: dict[str, Any]) -> MacroPull:
    fast = quick(settings)
    return MacroPull(
        fast,
        fetchers=fetchers,
        worker=worker,
        pacer=Pacer(fast.collectors, no_sleep),
        now=lambda: NOW,
    )


def row(
    day: date, value: float | None, *, release: date | None = None, units: str | None = "%"
) -> Fetched:
    return Fetched(day, value, release, units)


async def test_both_dates_reach_the_store_and_a_missing_value_is_not_written(
    shipped_settings: ObliskConfig, worker: StoreWorker, store: Store
) -> None:
    fetcher = StubFetcher(
        [
            row(date(2026, 9, 1), 4.1, release=date(2026, 10, 2)),
            row(date(2026, 10, 1), None, release=date(2026, 10, 22)),
        ]
    )
    collection = await pull(shipped_settings, worker, {"fred": fetcher}).run("fred", only={"DGS2"})
    assert collection.status == "ok"
    stored = repos.macro_series(store, "DGS2", as_of=TODAY, source="fred")
    assert [
        (o.reference_date, o.release_date, o.units, o.frequency) for o in stored.observations
    ] == [(date(2026, 9, 1), date(2026, 10, 2), "%", "daily")]
    assert source_repo.target_succeeded_at(store, "fred", "DGS2") == NOW


async def test_a_series_dated_by_its_own_rule_ignores_what_the_source_states(
    shipped_settings: ObliskConfig, worker: StoreWorker, store: Store
) -> None:
    fetcher = StubFetcher(
        [
            row(date(2026, 10, 9), 412517.0, units="MBBL"),
            row(date(2026, 10, 16), 410000.0, units="MBBL"),
        ]
    )
    await pull(shipped_settings, worker, {"eia": fetcher}).run("eia")
    eia = repos.macro_series(store, "WCESTUS1", as_of=TODAY, source="eia")
    assert eia.latest is not None
    assert eia.latest.release_date == date(2026, 10, 19)  # the week of 16 October is not known yet


async def test_a_changed_unit_fails_the_series_without_retrying_it(
    shipped_settings: ObliskConfig, worker: StoreWorker, store: Store
) -> None:
    fetcher = StubFetcher([row(date(2026, 9, 1), 1.0, units="Index 2015=100")])
    collection = await pull(shipped_settings, worker, {"fred": fetcher}).run(
        "fred", only={"CP0000DEM086NEST"}
    )
    assert collection.status == "error"
    assert "Index 2015=100" in collection.outcomes[0].reason
    assert fetcher.calls == 1
    assert repos.macro_series(store, "CP0000DEM086NEST", as_of=TODAY, source="fred").latest is None
    assert source_repo.target_succeeded_at(store, "fred", "CP0000DEM086NEST") is None
    with pytest.raises(UnitsChangedError):
        pull(shipped_settings, worker, {}).observations(
            MacroCatalog.from_settings(shipped_settings).specs["DGS2"],
            [row(date(2026, 9, 1), 1.0, units="bp")],
            NOW,
        )


async def test_a_repeat_inside_the_window_asks_nothing_and_force_asks_again(
    shipped_settings: ObliskConfig, worker: StoreWorker
) -> None:
    fetcher = StubFetcher([row(date(2026, 9, 1), 4.1, release=date(2026, 10, 2))])
    collector = pull(shipped_settings, worker, {"fred": fetcher})
    await collector.run("fred", only={"DGS2"})
    again = await collector.run("fred", only={"DGS2"})
    assert fetcher.calls == 1
    assert again.outcomes[0].detail == {"cached": True}
    assert again.status == "ok"
    await collector.run("fred", only={"DGS2"}, force=True)
    assert fetcher.calls == 2


async def test_a_failed_fetch_is_not_cached_and_a_dry_run_writes_nothing(
    shipped_settings: ObliskConfig, worker: StoreWorker, store: Store
) -> None:
    broken = StubFetcher(error=UpstreamError("down"))
    collector = pull(shipped_settings, worker, {"fred": broken})
    await collector.run("fred", only={"DGS2"})
    await collector.run("fred", only={"DGS2"})
    assert broken.calls == 2
    fine = StubFetcher([row(date(2026, 9, 1), 4.1, release=date(2026, 10, 2))])
    dry = await pull(shipped_settings, worker, {"fred": fine}).run(
        "fred", only={"DGS2"}, dry_run=True
    )
    assert dry.rows == 1
    failed_dry = await pull(shipped_settings, worker, {"fred": broken}).run(
        "fred", only={"DGS10"}, dry_run=True
    )
    assert failed_dry.status == "error"
    assert source_repo.target_succeeded_at(store, "fred", "DGS10") is None
    assert repos.macro_series(store, "DGS2", as_of=TODAY, source="fred").latest is None


async def test_a_dead_source_is_abandoned_with_one_finding(
    shipped_settings: ObliskConfig, worker: StoreWorker
) -> None:
    broken = StubFetcher(error=UpstreamError("down"))
    collection = await pull(shipped_settings, worker, {"fred": broken}).run("fred")
    stop = shipped_settings.sources.macro.collection.stop_after_consecutive_failures
    assert broken.calls == stop
    assert collection.outcomes[-1].target == "source:fred"
    assert collection.status == "error"


async def test_a_live_fetch_that_returns_nothing_is_degraded_not_ok(
    shipped_settings: ObliskConfig, worker: StoreWorker
) -> None:
    collection = await pull(shipped_settings, worker, {"fred": StubFetcher([])}).run(
        "fred", only={"DGS2"}
    )
    assert collection.status == "degraded"
    assert "no observation survived" in collection.outcomes[0].reason


async def test_windows_follow_the_group_and_unknown_sources_say_so(
    shipped_settings: ObliskConfig, worker: StoreWorker
) -> None:
    fetcher = StubFetcher([])
    collector = pull(shipped_settings, worker, {"fred": fetcher})
    await collector.run("fred", only={"DGS2", "DCOILWTICO"}, force=True)
    starts = sorted(window.start for window in fetcher.windows)
    assert (TODAY - starts[0]).days > (TODAY - starts[1]).days  # twenty years against ten
    nothing = await collector.run("fred", only={"NOPE"})
    assert nothing.status == "skipped"
    assert "NOPE" in nothing.empty_reason
    unbuilt = await pull(shipped_settings, worker, {}).run("ecb", only={"ECB_DFR"})
    assert unbuilt.status == "error"
    assert "no fetcher" in unbuilt.outcomes[0].reason
    assert collector.catalog.spec("DGS2") is not None


async def test_the_newest_rows_are_kept_above_the_cap_and_aggregation_is_labelled(
    shipped_settings: ObliskConfig, worker: StoreWorker
) -> None:
    capped = shipped_settings.model_copy(
        update={
            "sources": shipped_settings.sources.model_copy(
                update={
                    "macro": shipped_settings.sources.macro.model_copy(
                        update={
                            "collection": shipped_settings.sources.macro.collection.model_copy(
                                update={"max_observations_per_series": 2}
                            ),
                            "commodity_history": monthly_history(shipped_settings),
                        }
                    )
                }
            )
        }
    )
    collector = pull(capped, worker, {})
    catalog = MacroCatalog.from_settings(capped)
    rows = collector.observations(
        catalog.specs["DGS2"],
        [row(date(2026, 1, d), float(d), release=date(2026, 1, d + 1)) for d in (1, 2, 3)],
        NOW,
    )
    assert [o.value for o in rows] == [2.0, 3.0]
    assert collector.frequency_of(catalog.specs["DCOILWTICO"]) == "monthly"
    assert collector.frequency_of(catalog.specs["DGS2"]) == "daily"
