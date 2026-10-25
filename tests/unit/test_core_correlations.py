"""The core's research correlations for the service and its StrategyValue for the planner: every
role's tracking and partner correlations read from the research store once a day (unmeasured
without one), and what the core's last target portfolio says of a core position.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from obliskiq.capital.core_value import CoreStrategyValue
from obliskiq.capital.strategy import DefaultStrategyValue, PositionFacts
from obliskiq.config import ObliskConfig
from obliskiq.decision.core_vehicles import Unmeasured
from obliskiq.decision.vehicle_selector import resolve_exposure
from obliskiq.domain.core import TargetPortfolio, Trigger, VehicleTarget
from obliskiq.domain.orders import Sleeve
from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.orchestration.correlations import (
    NO_RESEARCH_STORE,
    ResearchCorrelationSource,
    measure,
)
from obliskiq.research.data import universe_series_id
from obliskiq.store import open_research_store
from obliskiq.store.research import replace_research_series

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
TODAY = date(2026, 10, 22)
SPPW, XDWD = 355720826, 163606916
D = Decimal


def _series(series_id: str, points: list[tuple[date, float]]) -> Any:
    dataset, _, field = series_id.partition(".")
    series = ResearchSeries(
        series_id=series_id, dataset_id=dataset, vendor=Vendor.YAHOO, field=field, units="price",
        currency="EUR", frequency=Frequency.DAILY, point_in_time=PointInTime.AS_OBSERVED,
        source_url="x", downloaded_at=NOW, content_sha256="a" * 64, bias_notes=("x",),
    )  # fmt: skip
    return series, [Observation(observed_on=d, available_on=d, value=v) for d, v in points]


def seed_research(settings: ObliskConfig, data_dir: Path) -> None:
    rng = np.random.default_rng(3)
    months = [TODAY.replace(day=1) - timedelta(days=30 * k) for k in range(60, 0, -1)]
    common = np.cumsum(rng.normal(0, 0.03, len(months)))
    with open_research_store(settings, data_dir) as store:
        for series_id, noise in (
            ("msci_world_eur.level", 0.0),
            (universe_series_id("XDWD"), 0.002),
            (universe_series_id("SPPW"), 0.002),
        ):
            values = np.exp(common + rng.normal(0, noise, len(months))) * 100
            replace_research_series(
                store, *_series(series_id, list(zip(months, values.tolist(), strict=True)))
            )


def test_every_role_is_measured_on_the_research_store(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    seed_research(shipped_settings, tmp_path)
    with open_research_store(shipped_settings, tmp_path) as store:
        found = measure(store, shipped_settings, as_of=TODAY)
    world = resolve_exposure(shipped_settings, "world_equity")
    tracking = found.tracking(world, ["SPPW", "XDWD", "IWDA"])
    assert tracking["SPPW"].value is not None
    assert tracking["SPPW"].value > 0.99
    assert tracking["IWDA"].value is None  # never loaded: unmeasured, with why
    pair = found.pairs([("SPPW", "XDWD")])[("SPPW", "XDWD")]
    assert pair.value is not None
    assert pair.value > 0.99
    assert set(found.by_exposure) == set(shipped_settings.core.roles)


async def test_the_source_reads_once_a_day_and_is_unmeasured_without_a_store(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    empty = ResearchCorrelationSource(shipped_settings, tmp_path)
    found = await empty.read(TODAY)
    assert isinstance(found, Unmeasured)
    assert found.reason == NO_RESEARCH_STORE
    await empty.aclose()
    seed_research(shipped_settings, tmp_path)
    source = ResearchCorrelationSource(shipped_settings, tmp_path)
    first = await source.read(TODAY)
    assert await source.read(TODAY) is first
    assert await source.read(TODAY + timedelta(days=1)) is not first
    await source.aclose()


# ── the core's StrategyValue ──


def portfolio(weights: dict[int, str]) -> TargetPortfolio:
    return TargetPortfolio(
        variant="A",
        as_of=TODAY,
        trend_as_of=None,
        budget=D(950),
        trigger=Trigger.HOLD,
        roles=(),
        vehicles=tuple(
            VehicleTarget(conid=c, role="world_equity", weight=D(w), held_units=D(1), price=D(1))
            for c, w in weights.items()
        ),
        trades=(),
        withheld=(),
        notes=(),
    )


def test_the_core_states_its_last_targets_and_the_default_answers_the_rest(
    shipped_settings: ObliskConfig,
) -> None:
    value = CoreStrategyValue(DefaultStrategyValue(shipped_settings.capital))
    core = PositionFacts(conid=SPPW, sleeve=Sleeve.CORE, units=D(10), opened_on=TODAY)
    other = PositionFacts(conid=XDWD, sleeve=Sleeve.CORE, units=D(10), opened_on=TODAY)
    thesis = PositionFacts(conid=1, sleeve=Sleeve.SATELLITE, units=D(10), opened_on=TODAY)
    # Before a portfolio is noted the core states no target and exits nothing.
    assert value.portfolio is None
    assert value.target_weight(core, today=TODAY) is None
    assert not value.would_exit(core, today=TODAY)
    value.note(portfolio({SPPW: "0.09", XDWD: "0"}))
    assert value.target_weight(core, today=TODAY) == D("0.09")
    assert not value.would_exit(core, today=TODAY)
    assert value.would_exit(other, today=TODAY)  # held at no target: sold anyway
    assert value.protection(core, today=TODAY) is None
    assert value.expected_return(core, today=TODAY) is None
    # A satellite position is the default's: protected in its entry window, no target.
    assert value.protection(thesis, today=TODAY) is not None
    assert value.target_weight(thesis, today=TODAY) is None
    assert not value.would_exit(thesis, today=TODAY)
    assert value.expected_return(thesis, today=TODAY) is None
