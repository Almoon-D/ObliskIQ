"""What the live core reads of the stores to choose its vehicles: the market store's prices and
traded value, the research store's tracking, partner correlations and ECB rates; and, with no
research store, readings that say so.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from service_rig import SERVICE_VENUES

from obliskiq.calendars.service import Calendars
from obliskiq.config.loader import load_config
from obliskiq.decision.vehicle_selector import resolve_exposure
from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.orchestration.vehicle_inputs import (
    NO_RESEARCH,
    EcbRates,
    StoredRoleInputs,
    base_rates,
    market_inputs,
)
from obliskiq.research.data import FxRates, universe_series_id
from obliskiq.store import open_research_store, open_store, repos
from obliskiq.store.research import replace_research_series
from obliskiq.store.worker import StoreWorker

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
D = Decimal


def _series(series_id: str, currency: str, points: list[tuple[date, float]]) -> Any:
    dataset, _, field = series_id.partition(".")
    series = ResearchSeries(
        series_id=series_id, dataset_id=dataset, vendor=Vendor.YAHOO, field=field, units="price",
        currency=currency, frequency=Frequency.DAILY, point_in_time=PointInTime.AS_OBSERVED,
        source_url="x", downloaded_at=NOW, content_sha256="a" * 64, bias_notes=("x",),
    )  # fmt: skip
    rows = [Observation(observed_on=d, available_on=d, value=v) for d, v in points]
    return series, rows


@pytest.fixture
async def world(
    tmp_path: Path, config_dir: Path, make_instrument: Any, make_bar: Any
) -> AsyncIterator[Any]:
    """A market store with bars of XDWD and, optionally, a research store (see ``research``)."""
    settings = load_config(config_dir).settings
    data = tmp_path / "data"
    data.mkdir()
    today = date(2026, 10, 22)
    xdwd = settings.universe.instrument("XDWD")
    assert xdwd is not None
    with open_store(settings, data) as store:
        repos.upsert_instrument(
            store,
            make_instrument(
                conid=xdwd.conid, symbol="XDWD", listing_exchange="IBIS2", currency="EUR",
                option_exchange=None, domicile="IE", ucits=True,
            ),
        )  # fmt: skip
        repos.upsert_bars(
            store,
            [
                make_bar(conid=xdwd.conid, session_date=today - timedelta(days=d), close=120.0,
                         volume=50_000.0)
                for d in range(1, 30)
            ],
        )  # fmt: skip
    calendars = Calendars.from_settings(settings, reference=today, venues=SERVICE_VENUES)
    worker = StoreWorker(lambda: open_store(settings, data))
    yield settings, data, today, calendars, worker, xdwd
    await worker.aclose()


def research(settings: Any, data: Path, today: date) -> None:
    rng = np.random.default_rng(3)
    months = [today.replace(day=1) - timedelta(days=30 * k) for k in range(60, 0, -1)]
    common = np.cumsum(rng.normal(0, 0.03, len(months)))
    with open_research_store(settings, data) as store:
        for series_id, noise in (
            ("msci_world_eur.level", 0.0),
            (universe_series_id("XDWD"), 0.002),
            (universe_series_id("SPPW"), 0.002),
        ):
            values = np.exp(common + rng.normal(0, noise, len(months))) * 100
            replace_research_series(
                store, *_series(series_id, "EUR", list(zip(months, values.tolist(), strict=True)))
            )
        replace_research_series(
            store, *_series("ecb_fx.USD", "USD", [(today - timedelta(days=3), 1.25), (today, 1.1)])
        )


async def test_without_a_research_store_tracking_and_rates_are_unmeasured(world: Any) -> None:
    settings, data, today, calendars, worker, xdwd = world
    inputs = StoredRoleInputs(settings, worker=worker, calendars=calendars, data_dir=data)
    exposure = resolve_exposure(settings, "world_equity")
    candidates = [i for i in settings.universe.instruments if exposure.held_by(i)]
    got = await inputs.selector_inputs(exposure, candidates, today)
    assert got.rates == {}
    assert all(r.value is None and NO_RESEARCH in str(r.reason) for r in got.tracking.values())
    assert got.market[xdwd.conid].sessions == 29
    assert got.market[xdwd.conid].average_daily_value == D(6000000)
    assert "XETR" in got.windows
    pairs = [("XDWD", "SPPW")]
    readings = await inputs.partner_readings(pairs, today)
    assert NO_RESEARCH in str(readings[("XDWD", "SPPW")].reason)


async def test_with_a_research_store_tracking_partners_and_rates_are_measured(world: Any) -> None:
    settings, data, today, calendars, worker, _xdwd = world
    research(settings, data, today)
    inputs = StoredRoleInputs(settings, worker=worker, calendars=calendars, data_dir=data)
    exposure = resolve_exposure(settings, "world_equity")
    candidates = [i for i in settings.universe.instruments if exposure.held_by(i)]
    got = await inputs.selector_inputs(exposure, candidates, today)
    assert got.rates["USD"] == D(repr(1 / 1.1))
    measured = got.tracking["XDWD"]
    assert measured.value is not None
    assert measured.value > 0.9
    readings = await inputs.partner_readings([("XDWD", "SPPW")], today)
    assert readings[("XDWD", "SPPW")].value is not None


async def test_ecb_rates_convert_a_past_close_or_say_they_cannot(world: Any) -> None:
    settings, data, today, _calendars, _worker, _xdwd = world
    rates = EcbRates(settings, data_dir=data)
    assert rates.to_base(D(100), "USD", today) is None  # nothing loaded
    await rates.load(today)
    assert rates.to_base(D(100), "USD", today) is None  # no research store
    research(settings, data, today)
    await rates.load(today)  # loaded once for a day: the empty read stands
    assert rates.to_base(D(100), "USD", today) is None
    fresh = EcbRates(settings, data_dir=data)
    await fresh.load(today)
    assert fresh.to_base(D(110), "USD", today) == D(110) * D(1) / D("1.1")
    assert fresh.to_base(D(100), "EUR", today) == D(100)
    assert fresh.to_base(D(100), "USD", today - timedelta(days=400)) is None  # no fresh rate
    assert fresh.to_base(D(100), "JPY", today) is None  # no such series


def test_the_helpers_read_what_the_stores_hold(world: Any) -> None:
    settings, data, today, _calendars, _worker, xdwd = world
    with open_store(settings, data) as store:
        assert (
            market_inputs(store, [xdwd.conid, 1], as_of=today, sessions=5)[xdwd.conid].sessions == 5
        )
        assert 1 not in market_inputs(store, [1], as_of=today, sessions=5)
    fx = FxRates({"USD": [(today, 1.25)]}, max_age_days=7)
    assert base_rates(fx, ["USD", "EUR", "JPY"], "EUR", today) == {
        "USD": D(1) / D("1.25"),
        "EUR": D(1),
    }
