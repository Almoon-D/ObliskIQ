"""The service's, capital's, the skeleton's, trading's and reporting's configuration refuses what
cannot work: an anchor venue not declared, a cap without its size, a target outside the
allowlist, a book over 100 %, a minimum trade of nothing, two cycles of one name.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_runtime import CoreConfig, MinTradeConfig, RuntimeConfig
from obliskiq.execution.instruments import UniverseDirectory
from obliskiq.execution.runtime import schedule_references


def _data(settings: ObliskConfig) -> dict[str, Any]:
    return settings.model_dump(mode="json")


def test_the_shipped_service_configuration_loads(shipped_settings: ObliskConfig) -> None:
    runtime = shipped_settings.runtime
    assert [c.name for c in runtime.cycles] == ["asia", "europe", "us", "us_late"]
    assert shipped_settings.capital.mode == "cap"
    assert sum(t.weight for t in shipped_settings.core.skeleton_targets) == Decimal(1)
    assert "rebalance" in shipped_settings.discipline.exit_reasons_allowed


def test_an_anchor_on_an_undeclared_venue_is_refused(shipped_settings: ObliskConfig) -> None:
    data = _data(shipped_settings)
    data["runtime"]["digest"]["venue"] = "XXXX"
    data["runtime"]["cycles"][0]["venue"] = "XYYY"
    with pytest.raises(ValueError, match=r"runtime\.digest\.venue: XXXX is not a venue") as caught:
        ObliskConfig.model_validate(data)
    assert "runtime.cycles.asia.venue: XYYY" in str(caught.value)


def test_a_target_outside_the_universe_is_refused(shipped_settings: ObliskConfig) -> None:
    data = _data(shipped_settings)
    data["core"]["skeleton_targets"][0]["conid"] = 1
    with pytest.raises(ValueError, match="world_equity: 1 is not pinned by a universe instrument"):
        ObliskConfig.model_validate(data)


def test_the_skeleton_names_each_role_once_and_fills_at_most_the_book() -> None:
    target = {"role": "a", "weight": "0.5", "conid": 1}
    with pytest.raises(ValueError, match="each role and each conid is listed once"):
        CoreConfig.model_validate({"skeleton_targets": [target, {**target, "conid": 2}]})
    with pytest.raises(ValueError, match="more than the whole book"):
        CoreConfig.model_validate(
            {"skeleton_targets": [target, {"role": "b", "weight": "0.6", "conid": 2}]}
        )


def test_a_minimum_trade_needs_a_part_and_cycles_their_own_names(
    shipped_settings: ObliskConfig,
) -> None:
    with pytest.raises(ValueError, match="needs absolute_base, fraction_of_managed or both"):
        MinTradeConfig(absolute_base=None, fraction_of_managed=None, combine="max")
    data = shipped_settings.runtime.model_dump(mode="json")
    data["cycles"][1]["name"] = "asia"
    with pytest.raises(ValueError, match="each cycle has its own name"):
        RuntimeConfig.model_validate(data)


def test_the_calendars_answer_a_venues_business_weekday(shipped_settings: ObliskConfig) -> None:
    calendars = Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=("XNYS",)
    )
    assert calendars.is_business_weekday("XNYS", date(2026, 11, 26))  # Thanksgiving: a holiday
    assert not calendars.is_business_weekday("XNYS", date(2026, 11, 28))  # a Saturday


def test_the_schedule_references_skip_an_unmapped_listing(shipped_settings: ObliskConfig) -> None:
    calendars = Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=("XAMS", "XNYS")
    )
    directory = UniverseDirectory.from_settings(shipped_settings)
    references = schedule_references(calendars, directory)
    assert set(references) == {"XAMS", "XNYS"}  # XETR and XLON are not built: skipped
