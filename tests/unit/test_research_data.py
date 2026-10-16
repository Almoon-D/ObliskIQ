"""Research data: the loaders on recorded downloads (tests/fixtures/research/README.md), the store
round trip point in time, conversion to the base currency, coverage and survivorship.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import csv
import io
import json
import math
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_research import (
    AqrDataset,
    EcbDataset,
    FredDataset,
    KenFrenchDataset,
    YahooDataset,
)
from obliskiq.domain.research import PointInTime, Vendor
from obliskiq.errors import DataSourceError
from obliskiq.research.data import (
    FxRates,
    Sources,
    coverage_by_year,
    http_fetch,
    load_dataset,
    parse_alfred,
    parse_aqr,
    parse_ecb,
    parse_fred_csv,
    parse_ken_french,
    sha256,
    store_loaded,
    survivorship,
    yahoo_bars,
)
from obliskiq.store import Store
from obliskiq.store.research import research_observations, research_series_info
from obliskiq.util.http import RetryPolicy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"
NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
MISSING = ("-99.99", "-999")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ken_french(**overrides: Any) -> KenFrenchDataset:
    fields: dict[str, Any] = {
        "id": "ff3_monthly",
        "vendor": "ken_french",
        "file": "F-F_Research_Data_Factors_CSV.zip",
        "section": 0,
        "frequency": "monthly",
        "columns": None,
        "release_lag_days": 45,
        "bias_notes": ["test"],
    }
    fields.update(overrides)
    return KenFrenchDataset(**fields)


def aqr(**overrides: Any) -> AqrDataset:
    fields: dict[str, Any] = {
        "id": "aqr_tsmom_monthly",
        "vendor": "aqr",
        "file": "Time-Series-Momentum-Factors-Monthly.xlsx",
        "sheet": "TSMOM Factors",
        "header_row": 18,
        "columns": ["TSMOM", "TSMOM^EQ"],
        "units": "decimal",
        "release_lag_days": 45,
        "bias_notes": ["test"],
    }
    fields.update(overrides)
    return AqrDataset(**fields)


def zipped(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


# ── Ken French ──


def test_the_ken_french_monthly_table_becomes_decimal_month_end_returns():
    columns = parse_ken_french(fixture("ken_french_ff3_factors.zip"), ken_french(), MISSING)
    assert list(columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    first_day, first = columns["Mkt-RF"][0]
    assert first_day == date(1926, 7, 31)
    assert first == pytest.approx(0.0289)
    assert columns["RF"][0] == (date(1926, 7, 31), pytest.approx(0.0022))
    assert columns["Mkt-RF"][-1][0] == date(2026, 8, 31)
    assert len({len(points) for points in columns.values()}) == 1


def test_the_ken_french_annual_table_is_its_second_section():
    dataset = ken_french(section=1, frequency="annual", columns=["Mkt-RF"])
    columns = parse_ken_french(fixture("ken_french_ff3_factors.zip"), dataset, MISSING)
    assert columns["Mkt-RF"][0] == (date(1927, 12, 31), pytest.approx(0.2944))


@pytest.mark.parametrize(
    ("dataset", "message"),
    [
        (ken_french(section=5), "section 5 is missing"),
        (ken_french(columns=["UMD"]), r"no column\(s\) \['UMD'\]"),
        (ken_french(frequency="annual"), "is not a annual stamp"),
    ],
)
def test_a_ken_french_table_that_is_not_there_fails(dataset, message):
    with pytest.raises(DataSourceError, match=message):
        parse_ken_french(fixture("ken_french_ff3_factors.zip"), dataset, MISSING)


def test_ken_french_missing_markers_are_left_out_and_bad_rows_refused():
    text = "title\n\n,A,B\n202401,   1.00, -99.99\n202402,  -999,   2.00\n\n"
    columns = parse_ken_french(zipped("x.csv", text), ken_french(), MISSING)
    assert columns == {"A": [(date(2024, 1, 31), 0.01)], "B": [(date(2024, 2, 29), 0.02)]}
    with pytest.raises(DataSourceError, match="has 1 values"):
        parse_ken_french(zipped("x.csv", ",A,B\n202401, 1.0\n"), ken_french(), MISSING)
    with pytest.raises(DataSourceError, match="'x' is not a number"):
        parse_ken_french(zipped("x.csv", ",A\n202401, x\n"), ken_french(), MISSING)
    with pytest.raises(DataSourceError, match="not a finite number"):
        parse_ken_french(zipped("x.csv", ",A\n202401, inf\n"), ken_french(), MISSING)
    with pytest.raises(DataSourceError, match="not a zip archive"):
        parse_ken_french(b"plain text", ken_french(), MISSING)
    two = io.BytesIO()
    with zipfile.ZipFile(two, "w") as archive:
        archive.writestr("a.csv", "")
        archive.writestr("b.csv", "")
    with pytest.raises(DataSourceError, match="expected one file"):
        parse_ken_french(two.getvalue(), ken_french(), MISSING)


# ── AQR ──


def test_the_aqr_workbook_gives_dated_factor_returns():
    columns = parse_aqr(fixture("aqr_tsmom_factors_monthly.xlsx"), aqr())
    assert columns["TSMOM"][0] == (date(1985, 1, 31), pytest.approx(0.043456226781221075))
    assert columns["TSMOM^EQ"][0] == (date(1985, 1, 31), pytest.approx(0.15337570278299956))
    assert len(columns["TSMOM"]) == 24
    assert columns["TSMOM"][-1][0] == date(1986, 12, 31)


def test_aqr_percent_units_are_scaled_and_missing_columns_refused():
    percent = parse_aqr(fixture("aqr_tsmom_factors_monthly.xlsx"), aqr(units="percent"))
    assert percent["TSMOM"][0][1] == pytest.approx(0.00043456226781221075)
    with pytest.raises(DataSourceError, match=r"no column\(s\) \['TSMOM\^XX'\]"):
        parse_aqr(fixture("aqr_tsmom_factors_monthly.xlsx"), aqr(columns=["TSMOM^XX"]))
    with pytest.raises(DataSourceError, match="no sheet 'Missing'"):
        parse_aqr(fixture("aqr_tsmom_factors_monthly.xlsx"), aqr(sheet="Missing"))


# ── FRED and ALFRED ──


def test_fred_csv_gives_the_latest_revision():
    points = parse_fred_csv(fixture("fred_unrate.csv"), "UNRATE")
    assert points[0] == (date(2023, 10, 1), 3.9)
    assert len(points) == 6
    assert parse_fred_csv(b"observation_date,X\n2024-01-01,.\n2024-02-01,1.5\n\n", "X") == [
        (date(2024, 2, 1), 1.5)
    ]
    with pytest.raises(DataSourceError, match="unexpected header"):
        parse_fred_csv(b"date,Y\n", "X")
    with pytest.raises(DataSourceError, match="not a YYYY-MM-DD date"):
        parse_fred_csv(b"observation_date,X\n2024/01/01,1\n", "X")


def test_alfred_keeps_every_vintage_with_its_publication_date():
    observations = parse_alfred(fixture("alfred_unrate_vintages.json"), "UNRATE")
    october = [
        (o.available_on, o.value) for o in observations if o.observed_on == date(2023, 10, 1)
    ]
    # October 2023 was first published at 3.9 on 3 November, revised to 3.8 on 5 January.
    assert october == [(date(2023, 11, 3), 3.9), (date(2024, 1, 5), 3.8)]
    assert len(observations) == 7


def test_alfred_malformed_answers_are_refused():
    with pytest.raises(DataSourceError, match="has no observations"):
        parse_alfred(b"not json", "X")
    with pytest.raises(DataSourceError, match="lacks 'realtime_start'"):
        parse_alfred(
            json.dumps({"observations": [{"date": "2024-01-01", "value": "1"}]}).encode(), "X"
        )
    missing = {
        "observations": [{"date": "2024-01-01", "value": ".", "realtime_start": "2024-02-01"}]
    }
    assert parse_alfred(json.dumps(missing).encode(), "X") == ()


# ── ECB ──


def test_ecb_rates_are_per_euro_by_day_and_skip_unpublished_ones():
    rates = parse_ecb(fixture("ecb_eurofxref_hist.zip"), ["USD", "GBP", "CYP"])
    assert rates["USD"][0] == (date(2023, 12, 20), 1.0944)
    assert dict(rates["USD"])[date(2024, 1, 2)] == 1.0956
    assert date(2024, 3, 29) not in dict(rates["USD"])  # Good Friday: no fixing
    assert rates["CYP"] == []  # N/A throughout
    days = [day for day, _ in rates["GBP"]]
    assert days == sorted(days)
    with pytest.raises(DataSourceError, match=r"lacks Date or currencies \['XXX'\]"):
        parse_ecb(fixture("ecb_eurofxref_hist.zip"), ["XXX"])


# ── Yahoo ──


@dataclass
class Stamp:
    text: str

    def date(self) -> date:
        return date.fromisoformat(self.text[:10])


class Frame:
    """The rows of a recorded yfinance frame, read back with the csv module."""

    def __init__(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self._rows = rows

    @classmethod
    def from_fixture(cls, name: str) -> Frame:
        with (FIXTURES / name).open(encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return cls([(row.pop("Date"), dict(row)) for row in reader])

    def iterrows(self) -> Iterable[tuple[Stamp, Mapping[str, Any]]]:
        return [(Stamp(stamp), row) for stamp, row in self._rows]


def test_yahoo_bars_keep_the_close_the_adjusted_close_and_the_dividend():
    bars = yahoo_bars(Frame.from_fixture("yahoo_spy.csv"), "SPY")
    assert len(bars) == 61
    assert bars[0].session == date(2024, 1, 2)
    assert bars[0].close == pytest.approx(472.6499938964844)
    paid = [bar for bar in bars if bar.dividend > 0]
    assert [(bar.session, bar.dividend) for bar in paid] == [(date(2024, 3, 15), 1.595)]
    eunl = yahoo_bars(Frame.from_fixture("yahoo_eunl_de.csv"), "EUNL.DE")
    assert all(bar.close == bar.adj_close for bar in eunl)  # accumulating: nothing paid out


def test_a_yahoo_frame_without_prices_or_columns_is_refused():
    row = {"Close": "1", "Adj Close": "1", "Dividends": "0"}
    gap = Frame([("2024-01-02", {**row, "Close": math.nan}), ("2024-01-03", row)])
    assert [bar.session for bar in yahoo_bars(gap, "X")] == [date(2024, 1, 3)]
    with pytest.raises(DataSourceError, match="lacks a column"):
        yahoo_bars(Frame([("2024-01-02", {"Close": "1"})]), "X")
    with pytest.raises(DataSourceError, match="is not a price"):
        yahoo_bars(Frame([("2024-01-02", {**row, "Close": "0"})]), "X")
    with pytest.raises(DataSourceError, match="no prices"):
        yahoo_bars(Frame([]), "X")


# ── loading into the store ──


def sources(fetched: dict[str, bytes], *, key: str | None = None) -> Sources:
    calls: list[tuple[str, Mapping[str, str] | None]] = []

    async def fetch(url: str, params: Mapping[str, str] | None) -> bytes:
        calls.append((url, params))
        name = next(name for marker, name in fetched.items() if marker in url)
        return fixture(name) if isinstance(name, str) else name

    def yahoo(symbol: str, start: date, end: date) -> Frame:
        assert (start, end) == (date(2009, 1, 1), NOW.date())
        return Frame.from_fixture("yahoo_spy.csv")

    fetch.calls = calls  # type: ignore[attr-defined]
    return Sources(fetch=fetch, yahoo=yahoo, fred_api_key=key)


async def test_every_shipped_vendor_loads_into_the_store_point_in_time(
    shipped_settings: ObliskConfig, store: Store
):
    fetched = {
        "ken.french": "ken_french_ff3_factors.zip",
        "aqr.com": "aqr_tsmom_factors_monthly.xlsx",
        "ecb.europa": "ecb_eurofxref_hist.zip",
        "api.stlouisfed": "alfred_unrate_vintages.json",
    }
    feed = sources(fetched, key="KEY")
    research = shipped_settings.research
    loaded = []
    for dataset_id in ("ff3_monthly", "aqr_tsmom_monthly", "ecb_fx", "alfred_cpi", "eunl"):
        dataset = research.dataset(dataset_id)
        assert dataset is not None
        loaded.extend(await load_dataset(dataset, shipped_settings, feed, now=NOW))
    assert store_loaded(store, loaded) > 0
    ff3 = research_series_info(store, "ff3_monthly.Mkt-RF")
    assert ff3 is not None
    assert (ff3.vendor, ff3.point_in_time) == (Vendor.KEN_FRENCH, PointInTime.RELEASE_LAG)
    assert ff3.content_sha256 == sha256(fixture("ken_french_ff3_factors.zip"))
    assert any("revised without vintages" in note for note in ff3.bias_notes)
    # A month is public 45 days after its end: July 1926 on 14 September.
    assert research_observations(store, "ff3_monthly.Mkt-RF", as_of=date(1926, 9, 13)) == []
    [july] = research_observations(store, "ff3_monthly.Mkt-RF", as_of=date(1926, 9, 14))
    assert july.value == pytest.approx(0.0289)
    usd = research_series_info(store, "ecb_fx.USD")
    assert usd is not None
    assert usd.units == "USD per EUR"
    cpi = research_observations(store, "alfred_cpi.value", as_of=date(2023, 12, 31))
    assert [o.value for o in cpi] == [3.9, 3.7]  # the fixture is UNRATE's vintages
    close = research_observations(store, "eunl.close", as_of=date(2024, 1, 2))
    assert [o.observed_on for o in close] == [date(2024, 1, 2)]
    assert research_series_info(store, "eunl.dividend") is not None
    assert research_series_info(store, "eunl.adj_close") is not None
    calls = feed.fetch.calls  # type: ignore[attr-defined]
    alfred_call = next(params for url, params in calls if "stlouisfed" in url)
    assert alfred_call is not None
    assert alfred_call["realtime_start"] == "1776-07-04"


async def test_fred_without_vintages_uses_the_declared_lag(shipped_settings: ObliskConfig):
    dataset = FredDataset(
        id="fred_unrate", vendor="fred", series="UNRATE", vintages=False, frequency="monthly",
        units="percent", release_lag_days=10, start=date(2023, 10, 1), bias_notes=["test"],
    )  # fmt: skip
    [series] = await load_dataset(
        dataset, shipped_settings, sources({"fredgraph": "fred_unrate.csv"}), now=NOW
    )
    assert series.series.point_in_time is PointInTime.RELEASE_LAG
    assert series.observations[0].available_on == date(2023, 10, 11)
    assert any("10 days" in note for note in series.series.bias_notes)


async def test_alfred_needs_a_key_and_empty_answers_fail(shipped_settings: ObliskConfig):
    vintages = shipped_settings.research.dataset("alfred_cpi")
    assert isinstance(vintages, FredDataset)
    with pytest.raises(DataSourceError, match="needs FRED_API_KEY"):
        await load_dataset(vintages, shipped_settings, sources({}), now=NOW)
    empty = sources({"stlouisfed": json.dumps({"observations": []}).encode()}, key="K")
    with pytest.raises(DataSourceError, match="no observations for CPIAUCSL"):
        await load_dataset(vintages, shipped_settings, empty, now=NOW)
    csv_only = vintages.model_copy(update={"vintages": False})
    nothing = sources({"fredgraph": b"observation_date,CPIAUCSL\n"})
    with pytest.raises(DataSourceError, match="no observations for CPIAUCSL"):
        await load_dataset(csv_only, shipped_settings, nothing, now=NOW)


async def test_an_empty_column_or_currency_fails(shipped_settings: ObliskConfig):
    with pytest.raises(DataSourceError, match="has no CYP rates"):
        await load_dataset(
            EcbDataset(id="fx", vendor="ecb", currencies=["CYP"], bias_notes=["x"]),
            shipped_settings, sources({"ecb.europa": "ecb_eurofxref_hist.zip"}), now=NOW,
        )  # fmt: skip
    empty = zipped("x.csv", ",A\n\n")
    with pytest.raises(DataSourceError, match="column A has no values"):
        await load_dataset(ken_french(), shipped_settings, sources({"ken.french": empty}), now=NOW)


async def test_a_dividendless_yahoo_history_has_no_dividend_series(shipped_settings):
    dataset = YahooDataset(
        id="eunl", vendor="yahoo", symbol="EUNL.DE", currency="EUR", start=date(2009, 1, 1),
        bias_notes=["x"],
    )  # fmt: skip
    feed = Sources(
        fetch=sources({}).fetch,
        yahoo=lambda *_: Frame.from_fixture("yahoo_eunl_de.csv"),
        fred_api_key=None,
    )
    loaded = await load_dataset(dataset, shipped_settings, feed, now=NOW)
    assert [item.series.field for item in loaded] == ["close", "adj_close"]


# ── the HTTP fetch ──


@pytest.fixture
def policy(shipped_settings: ObliskConfig) -> RetryPolicy:
    return RetryPolicy.from_settings(shipped_settings.http).__class__(
        retries=0, base_delay_seconds=0, max_delay_seconds=0, jitter_seconds=0,
        timeout_seconds=1, retry_status_codes=frozenset(), retry_after_max_seconds=1,
    )  # fmt: skip


@respx.mock
async def test_the_http_fetch_returns_bytes_and_fails_on_anything_but_200(policy):
    respx.get("https://example.org/ok").mock(return_value=httpx.Response(200, content=b"data"))
    respx.get("https://example.org/missing").mock(return_value=httpx.Response(404))
    respx.get("https://example.org/down").mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as client:
        fetch = http_fetch(client, policy)
        assert await fetch("https://example.org/ok", None) == b"data"
        with pytest.raises(DataSourceError, match="answered HTTP 404"):
            await fetch("https://example.org/missing", None)
        with pytest.raises(DataSourceError, match="could not be reached") as raised:
            await fetch("https://example.org/down", {"api_key": "SECRET"})
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None


# ── the base currency ──


def test_ecb_rates_convert_to_the_base_currency_and_go_stale():
    rates = parse_ecb(fixture("ecb_eurofxref_hist.zip"), ["USD", "GBP"])
    fx = FxRates(rates, max_age_days=4)
    assert fx.per_euro("EUR", date(2024, 1, 2)) == 1.0
    assert fx.per_euro("USD", date(2024, 1, 2)) == 1.0956
    # 100 USD on 2 January 2024 at 1.0956 USD per EUR.
    assert fx.to_base(100.0, "USD", "EUR", date(2024, 1, 2)) == pytest.approx(100 / 1.0956)
    # Across Easter the Thursday rate stands for Friday to Monday (four days old on Monday).
    assert fx.per_euro("USD", date(2024, 4, 1)) == 1.0811
    assert fx.per_euro("USD", date(2023, 12, 1)) is None  # before the first rate
    assert FxRates(rates, max_age_days=2).per_euro("USD", date(2024, 4, 1)) is None
    assert fx.to_base(1.0, "JPY", "EUR", date(2024, 1, 2)) is None
    assert fx.to_base(5.0, "USD", "USD", date(2024, 1, 2)) == 5.0
    gbp_in_usd = fx.to_base(1.0, "GBP", "USD", date(2024, 1, 2))
    assert gbp_in_usd == pytest.approx(1.0956 / 0.86645)
    converted = fx.convert([(date(2023, 12, 1), 1.0), (date(2024, 1, 2), 1.0956)], "USD", "EUR")
    assert converted == [(date(2024, 1, 2), pytest.approx(1.0))]


def test_fx_rates_read_from_the_store_as_of_a_date(store: Store):
    rates = parse_ecb(fixture("ecb_eurofxref_hist.zip"), ["USD"])
    from obliskiq.domain.research import Frequency, Observation, ResearchSeries

    series = ResearchSeries(
        series_id="ecb_fx.USD", dataset_id="ecb_fx", vendor=Vendor.ECB, field="USD",
        units="USD per EUR", currency="USD", frequency=Frequency.DAILY,
        point_in_time=PointInTime.AS_OBSERVED, source_url="https://www.ecb.europa.eu",
        downloaded_at=NOW, content_sha256="a" * 64, bias_notes=("x",),
    )  # fmt: skip
    rows = [Observation(observed_on=d, available_on=d, value=v) for d, v in rates["USD"]]
    store_loaded(store, [type("L", (), {"series": series, "observations": tuple(rows)})()])
    fx = FxRates.from_store(store, "ecb_fx", ["USD", "EUR"], as_of=date(2024, 1, 2), max_age_days=4)
    assert fx.per_euro("USD", date(2024, 1, 2)) == 1.0956
    assert fx.per_euro("USD", date(2024, 1, 5)) == 1.0956  # later rates were not public yet


# ── coverage and survivorship ──


def test_coverage_and_survivorship_report_what_the_universe_lacks(store: Store):
    from obliskiq.domain.research import Frequency, Observation, ResearchSeries

    def put(series_id: str, days: list[date]) -> None:
        series = ResearchSeries(
            series_id=series_id, dataset_id="u", vendor=Vendor.YAHOO, field="close",
            units="price", currency="EUR", frequency=Frequency.DAILY,
            point_in_time=PointInTime.AS_OBSERVED, source_url="x", downloaded_at=NOW,
            content_sha256="a" * 64, bias_notes=("x",),
        )  # fmt: skip
        rows = tuple(Observation(observed_on=d, available_on=d, value=1.0) for d in days)
        store_loaded(store, [type("L", (), {"series": series, "observations": rows})()])

    put("u.a", [date(2020, 6, 1), date(2021, 6, 1), date(2022, 6, 1)])
    put("u.b", [date(2021, 6, 1), date(2022, 6, 1)])  # listed in 2021
    put("u.c", [date(2020, 6, 1)])  # gone after 2020
    members = ["u.a", "u.b", "u.c", "u.d"]
    years = coverage_by_year(
        store, members, first_year=2020, last_year=2022, as_of=date(2026, 1, 1)
    )
    assert [(y.year, y.missing) for y in years] == [
        (2020, ("u.b", "u.d")),
        (2021, ("u.c", "u.d")),
        (2022, ("u.c", "u.d")),
    ]
    assert years[0].missing_share == 0.5
    # As of mid-2021, the 2021 rows of June are public but 2022 has nothing yet.
    early = coverage_by_year(
        store, members, first_year=2022, last_year=2022, as_of=date(2021, 7, 1)
    )
    assert early[0].missing_share == 1.0
    report = survivorship(
        store, members, start=date(2020, 6, 1), end=date(2022, 6, 1), as_of=date(2026, 1, 1)
    )
    assert report.without_data == ("u.d",)
    assert report.late_entries == ("u.b",)
    assert report.early_exits == ("u.c",)
    from obliskiq.research.data import YearCoverage

    assert YearCoverage(2020, 0, ()).missing_share == 1.0
