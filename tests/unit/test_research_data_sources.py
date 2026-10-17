"""Research data from MSCI, the ECB Data Portal and the Bundesbank, on recorded downloads
(tests/fixtures/research/README.md), and the series derived from stored ones: an overnight-rate
accrual and a constant-maturity zero-coupon bond, against hand calculations.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_research import (
    BundesbankDataset,
    ConstantMaturityZeroDataset,
    EcbDataDataset,
    MsciDataset,
    OvernightAccrualDataset,
)
from obliskiq.domain.research import (
    Frequency,
    Observation,
    PointInTime,
    ResearchSeries,
    Vendor,
)
from obliskiq.errors import DataSourceError
from obliskiq.research.data import (
    LoadedSeries,
    Sources,
    derive_dataset,
    is_derived,
    load_dataset,
    parse_bundesbank,
    parse_ecb_data,
    parse_msci,
    sha256,
    store_loaded,
)
from obliskiq.store import Store
from obliskiq.store.research import research_observations, research_series_info

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"
NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
BUND_KEY = "D.I.ZST.ZI.EUR.S1311.B.A604.R05XX.R.A.A._Z._Z.A"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def msci(**overrides: Any) -> MsciDataset:
    fields: dict[str, Any] = {
        "id": "msci_world_eur",
        "vendor": "msci",
        "index_code": "990100",
        "variant": "NETR",
        "currency": "EUR",
        "start": date(1990, 1, 1),
        "bias_notes": ["test"],
    }
    fields.update(overrides)
    return MsciDataset(**fields)


def feed(answers: Mapping[str, bytes]) -> tuple[Sources, list[tuple[str, Any]]]:
    calls: list[tuple[str, Any]] = []

    async def fetch(url: str, params: Mapping[str, str] | None) -> bytes:
        calls.append((url, params))
        return next(content for marker, content in answers.items() if marker in url)

    def yahoo(*_: Any) -> Any:
        raise AssertionError("no Yahoo call expected")

    return Sources(fetch=fetch, yahoo=yahoo, fred_api_key=None), calls


# ── MSCI ──


def test_msci_levels_are_dated_by_calculation_day():
    points = parse_msci(fixture("msci_world_netr_eur.json"), msci())
    assert points[0] == (date(2024, 1, 1), pytest.approx(458.2176605079118))
    assert points[-1][0] == date(2024, 1, 31)
    assert all(a[0] < b[0] for a, b in pairwise(points))


def test_an_msci_error_or_another_index_is_refused():
    with pytest.raises(DataSourceError, match="cannot be earlier than 19970101"):
        parse_msci(fixture("msci_error.json"), msci())
    with pytest.raises(DataSourceError, match="not msci_em_eur's index"):
        parse_msci(fixture("msci_world_netr_eur.json"), msci(id="msci_em_eur", index_code="891800"))
    with pytest.raises(DataSourceError, match="not JSON"):
        parse_msci(b"<html>", msci())
    with pytest.raises(DataSourceError, match="not an object"):
        parse_msci(b"[]", msci())
    echo = {"msci_index_code": "990100", "index_variant_type": "NETR", "ISO_currency_symbol": "EUR"}
    with pytest.raises(DataSourceError, match="has no levels"):
        parse_msci(json.dumps(echo).encode(), msci())
    for rows, message in (
        ([{"calc_date": 20240101}], "malformed"),
        ([{"calc_date": 20241301, "level_eod": 1.0}], "is not a date"),
        ([{"calc_date": 20240101, "level_eod": 0.0}], "is not a level"),
    ):
        answer = {**echo, "indexes": {"INDEX_LEVELS": rows}}
        with pytest.raises(DataSourceError, match=message):
            parse_msci(json.dumps(answer).encode(), msci())


async def test_msci_loads_daily_levels_as_observed(shipped_settings: ObliskConfig, store: Store):
    sources, calls = feed({"msci.com": fixture("msci_world_netr_eur.json")})
    [loaded] = await load_dataset(msci(), shipped_settings, sources, now=NOW)
    assert store_loaded(store, [loaded]) == len(loaded.observations)
    info = research_series_info(store, "msci_world_eur.level")
    assert info is not None
    assert (info.vendor, info.point_in_time, info.currency) == (
        Vendor.MSCI,
        PointInTime.AS_OBSERVED,
        "EUR",
    )
    assert info.content_sha256 == sha256(fixture("msci_world_netr_eur.json"))
    [(url, params)] = calls
    assert url == shipped_settings.research.data.msci.url
    # A start before the endpoint's earliest date is moved up to it.
    assert params["start_date"] == "19970101"
    assert params["end_date"] == "20261017"
    assert (params["index_codes"], params["index_variant"], params["currency_symbol"]) == (
        "990100",
        "NETR",
        "EUR",
    )
    first = research_observations(store, "msci_world_eur.level", as_of=date(2024, 1, 1))
    assert [o.observed_on for o in first] == [date(2024, 1, 1)]


async def test_an_msci_answer_without_levels_fails(shipped_settings: ObliskConfig):
    empty = {
        "msci_index_code": "990100",
        "index_variant_type": "NETR",
        "ISO_currency_symbol": "EUR",
        "indexes": {"INDEX_LEVELS": []},
    }
    sources, _ = feed({"msci.com": json.dumps(empty).encode()})
    with pytest.raises(DataSourceError, match="no levels for msci_world_eur"):
        await load_dataset(msci(), shipped_settings, sources, now=NOW)


# ── the ECB Data Portal ──


def ecb_data(**overrides: Any) -> EcbDataDataset:
    fields: dict[str, Any] = {
        "id": "ecb_eonia",
        "vendor": "ecb_data",
        "flow": "EON",
        "key": "D.EONIA_TO.RATE",
        "units": "percent a year",
        "start": date(1999, 1, 1),
        "bias_notes": ["test"],
    }
    fields.update(overrides)
    return EcbDataDataset(**fields)


def test_ecb_data_portal_rates_by_day():
    eonia = parse_ecb_data(fixture("ecb_eonia.csv"), "EON", "D.EONIA_TO.RATE")
    assert eonia[0] == (date(2021, 12, 20), -0.491)
    assert eonia[-1][0] == date(2021, 12, 31)
    estr = parse_ecb_data(fixture("ecb_estr.csv"), "EST", "B.EU000A2X2A25.WT")
    assert estr[0][0] == date(2022, 1, 3)  # 1 January is not a TARGET day


def test_an_ecb_data_portal_answer_for_another_key_or_shape_is_refused():
    with pytest.raises(DataSourceError, match=r"not EST\.B\.X"):
        parse_ecb_data(fixture("ecb_eonia.csv"), "EST", "B.X")
    with pytest.raises(DataSourceError, match="lacks columns"):
        parse_ecb_data(b"A,B\n1,2\n", "EON", "D.EONIA_TO.RATE")
    blank = b"KEY,TIME_PERIOD,OBS_VALUE\nEON.D.K,2024-01-02,\nEON.D.K,2024-01-03,3.9\n"
    assert parse_ecb_data(blank, "EON", "D.K") == [(date(2024, 1, 3), 3.9)]


async def test_ecb_data_portal_loads_with_its_start(shipped_settings: ObliskConfig):
    sources, calls = feed({"data-api.ecb": fixture("ecb_eonia.csv")})
    [loaded] = await load_dataset(ecb_data(), shipped_settings, sources, now=NOW)
    assert loaded.series.vendor is Vendor.ECB_DATA
    assert loaded.series.series_id == "ecb_eonia.value"
    assert loaded.series.units == "percent a year"
    [(url, params)] = calls
    assert url == "https://data-api.ecb.europa.eu/service/data/EON/D.EONIA_TO.RATE"
    assert params == {"format": "csvdata", "startPeriod": "1999-01-01"}
    nothing = b"KEY,TIME_PERIOD,OBS_VALUE\n"
    sources, _ = feed({"data-api.ecb": nothing})
    with pytest.raises(DataSourceError, match="returned no values for ecb_eonia"):
        await load_dataset(ecb_data(), shipped_settings, sources, now=NOW)


# ── the Bundesbank ──


def bundesbank(**overrides: Any) -> BundesbankDataset:
    fields: dict[str, Any] = {
        "id": "bbk_bund_05y",
        "vendor": "bundesbank",
        "flow": "BBSIS",
        "key": BUND_KEY,
        "units": "percent a year",
        "start": date(1997, 1, 1),
        "bias_notes": ["test"],
    }
    fields.update(overrides)
    return BundesbankDataset(**fields)


def test_bundesbank_yields_skip_days_without_a_value():
    points = parse_bundesbank(fixture("bundesbank_r05.csv"), "BBSIS", BUND_KEY)
    assert points[0] == (date(2024, 1, 2), 1.94)  # 1 January has no value
    assert date(2024, 1, 6) not in {day for day, _ in points}  # a Saturday


def test_a_bundesbank_answer_for_another_series_is_refused():
    with pytest.raises(DataSourceError, match=r"is not BBSIS\.OTHER"):
        parse_bundesbank(fixture("bundesbank_r05.csv"), "BBSIS", "OTHER")
    with pytest.raises(DataSourceError, match="header"):
        parse_bundesbank(b"", "BBSIS", BUND_KEY)
    torn = f'"",BBSIS.{BUND_KEY}\n2024-01-02\n'.encode()
    with pytest.raises(DataSourceError, match="has no value"):
        parse_bundesbank(torn, "BBSIS", BUND_KEY)


async def test_bundesbank_loads_by_flow_and_key(shipped_settings: ObliskConfig):
    sources, calls = feed({"bundesbank": fixture("bundesbank_r05.csv")})
    [loaded] = await load_dataset(bundesbank(), shipped_settings, sources, now=NOW)
    assert (loaded.series.vendor, loaded.series.point_in_time) == (
        Vendor.BUNDESBANK,
        PointInTime.AS_OBSERVED,
    )
    [(url, params)] = calls
    assert url.endswith(f"/BBSIS/{BUND_KEY}")
    assert params == {"format": "csv", "lang": "en", "startPeriod": "1997-01-01"}


# ── derived series ──


def put(store: Store, series_id: str, rows: list[tuple[date, float]], **kind: Any) -> None:
    series = ResearchSeries(
        series_id=series_id,
        dataset_id=series_id.split(".")[0],
        vendor=Vendor.ECB_DATA,
        field="value",
        units="percent a year",
        currency=None,
        frequency=Frequency.DAILY,
        point_in_time=kind.get("point_in_time", PointInTime.AS_OBSERVED),
        source_url="https://example.org",
        downloaded_at=NOW,
        content_sha256=kind.get("sha", "a" * 64),
        bias_notes=(f"note of {series_id}",),
    )
    lag = kind.get("lag", 0)
    observations = tuple(
        Observation(
            observed_on=day, available_on=date.fromordinal(day.toordinal() + lag), value=value
        )
        for day, value in rows
    )
    store_loaded(store, [LoadedSeries(series, observations)])


def accrual(**overrides: Any) -> OvernightAccrualDataset:
    fields: dict[str, Any] = {
        "id": "euro_cash",
        "vendor": "derived",
        "method": "overnight_accrual",
        "segments": [
            {"series": "old.value", "through": date(2024, 1, 8)},
            {"series": "new.value", "through": None},
        ],
        "day_count_basis": 360,
        "currency": "EUR",
        "base_level": "100",
        "bias_notes": ["accrual"],
    }
    fields.update(overrides)
    return OvernightAccrualDataset(**fields)


def test_an_overnight_accrual_splices_its_segments_and_counts_actual_days(
    shipped_settings: ObliskConfig, store: Store
):
    # Friday 5 January at 3.6 %, Monday 8 January at 3.9 %, then the new rate from 9 January
    # (the old series' later days are not used).
    put(
        store,
        "old.value",
        [(date(2024, 1, 5), 3.6), (date(2024, 1, 8), 3.9), (date(2024, 1, 9), 9.0)],
    )
    put(
        store,
        "new.value",
        [(date(2024, 1, 8), 9.0), (date(2024, 1, 9), 4.0), (date(2024, 1, 10), 4.1)],
    )
    [loaded] = derive_dataset(accrual(), shipped_settings, store, now=NOW)
    levels = [(o.observed_on, o.value) for o in loaded.observations]
    friday_to_monday = 100 * (1 + 0.036 * 3 / 360)
    monday_to_tuesday = friday_to_monday * (1 + 0.039 / 360)
    tuesday_to_wednesday = monday_to_tuesday * (1 + 0.040 / 360)
    assert levels == [
        (date(2024, 1, 5), 100.0),
        (date(2024, 1, 8), pytest.approx(friday_to_monday, rel=1e-12)),
        (date(2024, 1, 9), pytest.approx(monday_to_tuesday, rel=1e-12)),
        (date(2024, 1, 10), pytest.approx(tuesday_to_wednesday, rel=1e-12)),
    ]
    series = loaded.series
    assert (series.vendor, series.series_id, series.point_in_time) == (
        Vendor.DERIVED,
        "euro_cash.level",
        PointInTime.AS_OBSERVED,
    )
    assert series.source_url.startswith("derived: overnight accrual of old.value through")
    assert "note of old.value" in series.bias_notes
    assert "accrual" in series.bias_notes
    # The content hash names the inputs' hashes: a changed input changes it.
    put(store, "new.value", [(date(2024, 1, 9), 4.0)], sha="b" * 64)
    [again] = derive_dataset(accrual(), shipped_settings, store, now=NOW)
    assert again.series.content_sha256 != series.content_sha256


def test_a_derived_value_is_public_only_when_its_inputs_were(
    shipped_settings: ObliskConfig, store: Store
):
    put(store, "old.value", [(date(2024, 1, 5), 3.6), (date(2024, 1, 8), 3.9)], lag=3)
    put(store, "new.value", [(date(2024, 1, 9), 4.0)], lag=3)
    [loaded] = derive_dataset(
        accrual(),
        shipped_settings,
        store,
        now=NOW,
    )
    # Monday's level uses Friday's rate, public on Monday (lag 3).
    monday = loaded.observations[1]
    assert (monday.observed_on, monday.available_on) == (date(2024, 1, 8), date(2024, 1, 8))
    tuesday = loaded.observations[2]
    assert tuesday.available_on == date(2024, 1, 11)


def test_a_derived_series_needs_its_inputs_and_one_kind_of_availability(
    shipped_settings: ObliskConfig, store: Store
):
    with pytest.raises(DataSourceError, match=r"needs old\.value: load it first"):
        derive_dataset(accrual(), shipped_settings, store, now=NOW)
    put(store, "old.value", [(date(2024, 1, 5), 3.6)])
    put(store, "new.value", [(date(2024, 1, 9), 4.0)], point_in_time=PointInTime.RELEASE_LAG)
    with pytest.raises(DataSourceError, match="mixes inputs"):
        derive_dataset(accrual(), shipped_settings, store, now=NOW)
    late = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(DataSourceError, match="which has no rows"):
        derive_dataset(accrual(), shipped_settings, store, now=late)
    # Each segment takes only its own days: here neither has a day in its range.
    put(store, "new.value", [(date(2024, 1, 9), 4.0)])
    put(store, "old.value", [(date(2019, 6, 3), 3.6)])
    disjoint = accrual(
        segments=[
            {"series": "new.value", "through": date(2020, 1, 1)},
            {"series": "old.value", "through": None},
        ]
    )
    with pytest.raises(DataSourceError, match="has no rate in its segments"):
        derive_dataset(disjoint, shipped_settings, store, now=NOW)


def test_accrual_segments_must_close_in_order():
    with pytest.raises(ValidationError, match="only the last segment runs open-ended"):
        accrual(
            segments=[
                {"series": "a.value", "through": None},
                {"series": "b.value", "through": None},
            ]
        )
    with pytest.raises(ValidationError, match="strictly rising"):
        accrual(
            segments=[
                {"series": "a.value", "through": date(2024, 2, 1)},
                {"series": "b.value", "through": date(2024, 1, 1)},
                {"series": "c.value", "through": None},
            ]
        )


def zero(**overrides: Any) -> ConstantMaturityZeroDataset:
    fields: dict[str, Any] = {
        "id": "bund_zero_05y",
        "vendor": "derived",
        "method": "constant_maturity_zero",
        "yield_series": "y5.value",
        "maturity_years": 5,
        "shorter_yield_series": "y4.value",
        "shorter_maturity_years": 4,
        "compounding": "continuous",
        "year_basis_days": "365.25",
        "currency": "EUR",
        "base_level": "100",
        "bias_notes": ["zero"],
    }
    fields.update(overrides)
    return ConstantMaturityZeroDataset(**fields)


def test_a_constant_maturity_zero_rolls_down_the_curve_each_day(
    shipped_settings: ObliskConfig, store: Store
):
    monday, tuesday, wednesday = date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)
    put(store, "y5.value", [(monday, 2.0), (tuesday, 2.1), (wednesday, 2.1)])
    # Wednesday's 4-year yield is missing: Wednesday is not a common day.
    put(store, "y4.value", [(monday, 1.8), (tuesday, 1.9)])
    [loaded] = derive_dataset(zero(), shipped_settings, store, now=NOW)
    held = 1 / 365.25
    rate_left = 2.1 - held * (2.1 - 1.9)  # the 5-year yield less one day, interpolated
    log_return = 5 * 0.020 - (5 - held) * rate_left / 100
    assert [o.observed_on for o in loaded.observations] == [monday, tuesday]
    assert loaded.observations[1].value == pytest.approx(100 * math.exp(log_return), rel=1e-12)
    assert (
        loaded.series.source_url
        == "derived: 5-year constant-maturity zero from y5.value and y4.value"
    )
    [annual] = derive_dataset(zero(compounding="annual"), shipped_settings, store, now=NOW)
    annual_return = 5 * math.log1p(0.020) - (5 - held) * math.log1p(rate_left / 100)
    assert annual.observations[1].value == pytest.approx(100 * math.exp(annual_return), rel=1e-12)


def test_a_zero_needs_two_common_days_and_a_shorter_maturity(
    shipped_settings: ObliskConfig, store: Store
):
    put(store, "y5.value", [(date(2024, 1, 8), 2.0)])
    put(store, "y4.value", [(date(2024, 1, 8), 1.8)])
    with pytest.raises(DataSourceError, match="at least 2 common days"):
        derive_dataset(zero(), shipped_settings, store, now=NOW)
    with pytest.raises(ValidationError, match="shorter maturity must be below"):
        zero(shorter_maturity_years=5)


async def test_a_derived_dataset_is_computed_never_downloaded(shipped_settings: ObliskConfig):
    sources, calls = feed({})
    assert is_derived(zero())
    assert not is_derived(msci())
    with pytest.raises(DataSourceError, match="computed, not downloaded"):
        await load_dataset(zero(), shipped_settings, sources, now=NOW)
    assert calls == []


def test_the_shipped_derived_datasets_name_shipped_inputs(shipped_settings: ObliskConfig):
    research = shipped_settings.research
    ids = {d.id for d in research.datasets}
    for dataset in research.datasets:
        if isinstance(dataset, OvernightAccrualDataset):
            names = [segment.series for segment in dataset.segments]
        elif isinstance(dataset, ConstantMaturityZeroDataset):
            names = [dataset.yield_series, dataset.shorter_yield_series]
            assert dataset.base_level == Decimal(100)
        else:
            continue
        # Each input is a shipped dataset that sits before the derived one.
        for name in names:
            source = name.split(".")[0]
            assert source in ids
            order = [d.id for d in research.datasets]
            assert order.index(source) < order.index(dataset.id)
