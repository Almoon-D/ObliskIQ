"""The FX engine (lite's ``tests/test_compute_fx.py``, ported): rates into the base currency with
the direction decided once, the four-way decomposition with inflation compounded to the horizon,
ranking on base real only, the flags, and the carry-illusion test whose base rate cancels.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.data.macro.catalog import MacroCatalog
from obliskiq.domain.enums import AssetClass, InstrumentRole
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.engines.fx.decomposition import (
    ILLUSION,
    NOT_MEASURED,
    OPPORTUNITY,
    FxEngine,
    carry_verdict,
    carry_verdicts,
    deflate,
    flags,
    fx_ranking,
    leg_leaf,
    market_leaves,
)
from obliskiq.engines.fx.rates import BaseRates
from obliskiq.grid.frames import MacroReader
from obliskiq.store import Store, measurements, repos

AS_OF = date(2026, 10, 22)
STAMP = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
SPX = 416904


def obs(
    series: str, source: str, units: str, day: date, value: float, released: date | None = None
) -> MacroObservation:
    return MacroObservation(
        series_id=series,
        source=source,
        reference_date=day,
        release_date=released or day,
        value=value,
        frequency="daily",
        units=units,
        collected_at=STAMP,
    )


def eurusd(day: date, value: float, released: date | None = None) -> MacroObservation:
    return obs("EURUSD", "ecb", "USD per EUR", day, value, released)


def month(back: int) -> date:
    """A monthly print's reference date, ``back`` months of 30 days before ``AS_OF``."""
    return AS_OF - timedelta(days=30 * back)


# ── rates ──


def test_the_ecb_convention_is_inverted_exactly_once(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_macro(store, [eurusd(AS_OF, 1.25)])
    rates = BaseRates(store, shipped_settings, as_of=AS_OF)
    assert rates.to_base("USD") == Decimal(1) / Decimal("1.25")
    assert "USD per EUR" in rates.reading("USD").reason


def test_the_base_currency_converts_by_one_without_any_series(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    rates = BaseRates(store, shipped_settings, as_of=AS_OF)
    assert rates.base_currency == "EUR"
    assert rates.to_base("EUR") == 1


def test_an_unconfigured_currency_is_none_rather_than_parity(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    reading = BaseRates(store, shipped_settings, as_of=AS_OF).reading("XAU")
    assert reading.value is None
    assert "declares an FX series" in reading.reason


def test_a_configured_series_with_no_observation_is_none(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    reading = BaseRates(store, shipped_settings, as_of=AS_OF).reading("USD")
    assert reading.value is None
    assert "EURUSD" in reading.reason


def test_a_non_positive_rate_is_refused(store: Store, shipped_settings: ObliskConfig) -> None:
    repos.upsert_macro(store, [eurusd(AS_OF, 0.0)])
    assert BaseRates(store, shipped_settings, as_of=AS_OF).to_base("USD") is None


def test_as_of_uses_the_rate_known_on_the_day_not_the_newest(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_macro(
        store,
        [eurusd(AS_OF - timedelta(days=1), 1.10), eurusd(AS_OF, 1.30, AS_OF + timedelta(days=1))],
    )
    assert BaseRates(store, shipped_settings, as_of=AS_OF).to_base("USD") == Decimal(1) / Decimal(
        "1.1"
    )


def test_a_series_declared_the_other_way_up_is_refused(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    series = [
        entry.model_copy(update={"units": "EUR per USD"}) if entry.id == "EURUSD" else entry
        for entry in shipped_settings.sources.macro.series
    ]
    macro = shipped_settings.sources.macro.model_copy(update={"series": series})
    sources = shipped_settings.sources.model_copy(update={"macro": macro})
    settings = shipped_settings.model_copy(update={"sources": sources})
    repos.upsert_macro(store, [eurusd(AS_OF, 1.25)])
    reading = BaseRates(store, settings, as_of=AS_OF).reading("USD")
    assert reading.value is None
    assert "invert" in reading.reason


# ── the decomposition ──


def test_inflation_is_compounded_to_the_horizon_not_subtracted_whole() -> None:
    real = deflate(0.01, 0.10, 5, 365.0)
    assert real is not None
    assert real == pytest.approx(1.01 / 1.10 ** (5 / 365) - 1)
    assert real > 0, "a year of inflation off a five-day return would make it absurdly negative"
    assert deflate(None, 0.1, 5, 365.0) is None
    assert deflate(0.1, None, 5, 365.0) is None
    assert deflate(0.1, -1.5, 365, 365.0) is None


@pytest.fixture
def seeded_us(store: Store, shipped_settings: ObliskConfig) -> Store:
    """SPX up 10 % over the year, the dollar 20 % weaker against the euro, US CPI 3 %, euro CPI
    2 %, a 4 % ten-year yield against a 5 % Fed funds rate and a 2 % ECB deposit rate."""
    repos.upsert_instrument(
        store,
        Instrument(
            conid=SPX,
            symbol="SPX",
            listing_exchange="CBOE",
            currency="USD",
            asset_class=AssetClass.INDEX,
            domicile=None,
            ucits=None,
            kid_status=None,
            share_class=None,
            lot_size=None,
            option_exchange=None,
            history_exchange=None,
            roles=frozenset({InstrumentRole.DATA}),
            superseded_by=None,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            superseded_at=None,
        ),
    )
    days = [AS_OF - timedelta(days=back) for back in range(800, -1, -1)]
    start = AS_OF - timedelta(days=round(12 * 365.0 / 12))
    repos.upsert_bars(
        store,
        [
            Bar(
                conid=SPX,
                session_date=day,
                open=None,
                high=None,
                low=None,
                close=1000.0 if day <= start else 1100.0,
                volume=None,
                source="t",
            )
            for day in days
        ],
    )
    repos.upsert_macro(
        store,
        [
            obs("CPIAUCSL", "fred", "Index 1982-1984=100", month(b), 100.0 * 1.03 ** (-(b) / 12))
            for b in range(0, 30)
        ]
        + [
            obs(
                "CP0000EZCCM086NEST",
                "fred",
                "Index 2025=100",
                month(b),
                100.0 * 1.02 ** (-(b) / 12),
            )
            for b in range(0, 30)
        ]
        + [eurusd(day, 1.0 if day <= start else 1.25) for day in [*days[::7], AS_OF]]
        + [
            obs("DFF", "fred", "%", AS_OF, 5.0),
            obs("ECB_DFR", "ecb", "%", AS_OF, 2.0),
            obs("DGS10", "fred", "%", AS_OF, 4.0),
        ],
    )
    return store


def us_leaves(store: Store, settings: ObliskConfig) -> dict[str, Measurement]:
    engine = FxEngine(store, settings)
    rows = engine.build(as_of=AS_OF, computed_at=STAMP)
    engine.write(rows)
    return {row.leaf: row for row in rows if row.entity_id == "us"}


def test_all_four_legs_are_computed_and_a_local_gain_is_a_base_loss(
    seeded_us: Store, shipped_settings: ObliskConfig
) -> None:
    leaves = us_leaves(seeded_us, shipped_settings)
    local = leaves[leg_leaf("local_nominal", 12)]
    base = leaves[leg_leaf("base_nominal", 12)]
    real = leaves[leg_leaf("base_real", 12)]
    assert local.value == pytest.approx(0.10)
    assert base.value == pytest.approx(1.10 * (1.0 / 1.25) - 1)
    assert base.value is not None
    assert base.value < 0 < local.value
    assert real.status is MeasurementStatus.OK
    assert real.value is not None
    assert real.value < base.value
    assert leaves[leg_leaf("fx_return", 12)].value == pytest.approx(1 / 1.25 - 1)
    # The newest print against the newest released a year or more before (13 months back here).
    assert leaves["cpi_yoy"].value == pytest.approx(1.03 ** (13 / 12) - 1)
    assert all(row.entity_kind is EntityKind.MARKET for row in leaves.values())


def test_the_base_currencys_market_needs_no_fx_series(
    seeded_us: Store, shipped_settings: ObliskConfig
) -> None:
    engine = FxEngine(seeded_us, shipped_settings)
    rows = [
        row for row in engine.build(as_of=AS_OF, computed_at=STAMP) if row.entity_id == "eurozone"
    ]
    fx = {row.leaf: row for row in rows}[leg_leaf("fx_return", 3)]
    assert fx.status is MeasurementStatus.OK
    assert fx.value == 0


def test_an_unseeded_native_index_is_absent_with_why(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    leaves = us_leaves(store, shipped_settings)
    local = leaves[leg_leaf("local_nominal", 3)]
    assert local.status is MeasurementStatus.NA
    assert "not seeded" in (local.reason or "")
    assert leaves[leg_leaf("base_real", 3)].reason == local.reason


def test_a_region_without_an_index_or_a_window_without_a_start_is_absent_with_why(
    seeded_us: Store, shipped_settings: ObliskConfig
) -> None:
    engine = FxEngine(seeded_us, shipped_settings)
    reader = MacroReader(seeded_us, MacroCatalog.from_settings(shipped_settings), AS_OF)
    world = {
        row.leaf: row
        for row in engine.for_market("world", reader=reader, as_of=AS_OF, computed_at=STAMP)
    }
    assert "declares no native index" in (world[leg_leaf("local_nominal", 3)].reason or "")
    seeded_us.execute(
        "DELETE FROM bars WHERE conid = ? AND session_date < ?",
        (SPX, (AS_OF - timedelta(days=30)).isoformat()),
    )
    short = us_leaves(seeded_us, shipped_settings)[leg_leaf("local_nominal", 3)]
    assert short.status is MeasurementStatus.NA
    assert "holds no close on or before" in (short.reason or "")


def test_a_base_currency_no_region_declares_has_no_base_cpi(
    seeded_us: Store, shipped_settings: ObliskConfig
) -> None:
    account = shipped_settings.account.model_copy(update={"base_currency": "NOK"})
    settings = shipped_settings.model_copy(update={"account": account})
    cpi = us_leaves(seeded_us, settings)["base_cpi_yoy"]
    assert cpi.status is MeasurementStatus.NA
    assert "declares the base CPI" in (cpi.reason or "")


def test_the_carry_test_and_its_cancelling_base_rate(
    seeded_us: Store, shipped_settings: ObliskConfig
) -> None:
    leaves = us_leaves(seeded_us, shipped_settings)
    differential = leaves["carry_rate_differential"].value
    hedged = leaves["carry_hedged_equivalent"].value
    excess = leaves["carry_excess_over_base"].value
    assert differential == pytest.approx(0.05 - 0.02)
    assert hedged == pytest.approx(0.04 - 0.03)
    assert excess == pytest.approx(0.04 - 0.05), "the pickup is the bond's spread over its own rate"
    assert "overnight" in (leaves["carry_excess_over_base"].reason or "")
    (verdict,) = [
        v for v in carry_verdicts(seeded_us, shipped_settings, as_of=AS_OF) if v.market == "us"
    ]
    assert verdict.verdict == ILLUSION


def test_a_missing_policy_rate_gives_na_not_the_headline(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    repos.upsert_macro(store, [obs("DGS10", "fred", "%", AS_OF, 9.0)])
    leaves = us_leaves(store, shipped_settings)
    assert leaves["carry_hedged_equivalent"].status is MeasurementStatus.NA
    assert "local policy rate" in (leaves["carry_hedged_equivalent"].reason or "")


def test_the_build_is_point_in_time(seeded_us: Store, shipped_settings: ObliskConfig) -> None:
    first = us_leaves(seeded_us, shipped_settings)
    later = AS_OF + timedelta(days=1)
    repos.upsert_macro(seeded_us, [eurusd(later, 2.0), obs("DFF", "fred", "%", later, 9.0)])
    repos.upsert_bars(
        seeded_us,
        [
            Bar(
                conid=SPX,
                session_date=later,
                open=None,
                high=None,
                low=None,
                close=5.0,
                volume=None,
                source="t",
            )
        ],
    )
    again = us_leaves(seeded_us, shipped_settings)
    assert {k: (v.value, v.inputs_hash) for k, v in first.items()} == {
        k: (v.value, v.inputs_hash) for k, v in again.items()
    }


# ── the views ──


def leaf(market: str, name: str, value: float | None) -> Measurement:
    return Measurement(
        entity_kind=EntityKind.MARKET,
        entity_id=market,
        indicator="fx_real",
        leaf=name,
        as_of=AS_OF,
        value=value,
        status=MeasurementStatus.OK if value is not None else MeasurementStatus.NA,
        reason=None if value is not None else "not measured",
        reason_code=None if value is not None else "x",
        n_obs=None,
        window=None,
        units="decimal",
        source="t",
        market_data_status=None,
        inputs_hash="0" * 64,
        computed_at=STAMP,
    )


def four_way(market: str, base_real: float | None, local: float = 0.6) -> list[Measurement]:
    return [
        leaf(market, leg_leaf("local_nominal", 3), local),
        leaf(market, leg_leaf("local_real", 3), 0.1),
        leaf(market, leg_leaf("base_nominal", 3), 0.0),
        leaf(market, leg_leaf("base_real", 3), base_real),
    ]


def test_ranking_reads_base_real_only_and_excludes_the_unmeasured(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    measurements.upsert_measurements(
        store,
        four_way("hot", -0.05, local=0.60)
        + four_way("calm", 0.03, local=0.02)
        + four_way("dark", None),
    )
    ranked = fx_ranking(store, shipped_settings, as_of=AS_OF, months=3)
    assert [row.market for row in ranked] == ["calm", "hot"], (
        "a 60 % local gain ranks below a real 3 %"
    )
    assert "dark" not in [row.market for row in ranked]


def test_a_missing_leg_unranks_only_under_the_four_way_rule(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    rows = four_way("partial", 0.02)
    rows[1] = leaf("partial", leg_leaf("local_real", 3), None)
    measurements.upsert_measurements(store, rows)
    assert fx_ranking(store, shipped_settings, as_of=AS_OF, months=3) == []
    fx = shipped_settings.fx_inflation.model_copy(update={"require_four_way_decomposition": False})
    lax = shipped_settings.model_copy(update={"fx_inflation": fx})
    assert [row.market for row in fx_ranking(store, lax, as_of=AS_OF, months=3)] == ["partial"]


def test_a_high_inflation_or_depreciating_market_is_flagged(shipped_settings: ObliskConfig) -> None:
    fx = shipped_settings.fx_inflation
    hot = {
        "cpi_yoy": leaf("x", "cpi_yoy", fx.high_inflation_cpi_yoy),
        "depreciation_annualised": leaf(
            "x", "depreciation_annualised", -fx.high_depreciation_3y_annualised
        ),
    }
    assert len(flags(shipped_settings, hot)) == 2
    calm = {"cpi_yoy": leaf("x", "cpi_yoy", 0.02)}
    assert flags(shipped_settings, calm) == ()


def carry(excess: float | None, real: float | None) -> dict[str, Measurement]:
    return {
        "carry_excess_over_base": leaf("x", "carry_excess_over_base", excess),
        "carry_base_real_yield": leaf("x", "carry_base_real_yield", real),
    }


def test_the_carry_verdicts(shipped_settings: ObliskConfig) -> None:
    tolerance = shipped_settings.fx_inflation.carry_illusion_tolerance
    assert carry_verdict(shipped_settings, "x", carry(tolerance, 0.02)).verdict == ILLUSION
    assert (
        carry_verdict(shipped_settings, "x", carry(tolerance + 0.02, 0.02)).verdict == OPPORTUNITY
    )
    assert carry_verdict(shipped_settings, "x", carry(tolerance + 0.02, -0.01)).verdict == ILLUSION
    assert carry_verdict(shipped_settings, "x", carry(None, None)).verdict == NOT_MEASURED
    assert carry_verdict(shipped_settings, "x", {}).reason == "not built"


def test_a_moved_base_rate_alone_does_not_flip_the_verdict(
    store: Store, shipped_settings: ObliskConfig, make_observation: Callable[..., MacroObservation]
) -> None:
    """hedged - base = yield - local: the base rate cancels out of the excess."""
    found = []
    for base in (1.0, 3.0):
        store.execute("DELETE FROM macro")
        repos.upsert_macro(
            store,
            [
                obs("DGS10", "fred", "%", AS_OF, 4.0),
                obs("DFF", "fred", "%", AS_OF, 3.5),
                obs("ECB_DFR", "ecb", "%", AS_OF, base),
            ],
        )
        found.append(us_leaves(store, shipped_settings)["carry_excess_over_base"].value)
    assert found[0] == pytest.approx(found[1]) == pytest.approx(0.005)


def test_the_view_is_empty_before_a_build(store: Store) -> None:
    assert market_leaves(store, as_of=AS_OF) == {}
