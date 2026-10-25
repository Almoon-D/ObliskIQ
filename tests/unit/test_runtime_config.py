"""The service's, capital's, trading's and reporting's configuration refuses what cannot work: an
anchor venue not declared, a cap without its size, a minimum trade of nothing, two cycles of one
name.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from typing import Any

import pytest

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_runtime import MinTradeConfig, RuntimeConfig
from obliskiq.execution.instruments import UniverseDirectory
from obliskiq.execution.runtime import schedule_references


def _data(settings: ObliskConfig) -> dict[str, Any]:
    return settings.model_dump(mode="json")


def test_the_shipped_service_configuration_loads(shipped_settings: ObliskConfig) -> None:
    runtime = shipped_settings.runtime
    assert [c.name for c in runtime.cycles] == ["asia", "europe", "us", "us_late"]
    assert shipped_settings.capital.mode == "cap"
    assert "rebalance" in shipped_settings.discipline.exit_reasons_allowed


def test_an_anchor_on_an_undeclared_venue_is_refused(shipped_settings: ObliskConfig) -> None:
    data = _data(shipped_settings)
    data["runtime"]["digest"]["venue"] = "XXXX"
    data["runtime"]["cycles"][0]["venue"] = "XYYY"
    with pytest.raises(ValueError, match=r"runtime\.digest\.venue: XXXX is not a venue") as caught:
        ObliskConfig.model_validate(data)
    assert "runtime.cycles.asia.venue: XYYY" in str(caught.value)


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
