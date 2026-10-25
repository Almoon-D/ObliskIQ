"""The vehicles that fill the core's roles: the selector's best, a held vehicle kept, a swap
partner for the tilting variants, and a role nobody can fill says why.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
from vehicle_rig import (
    AS_OF,
    REPO,
    inputs,
    selector,
    with_checks,
    with_vehicles,
)

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_universe import UniverseInstrument
from obliskiq.decision.core_vehicles import RoleChoice, VehicleChooser
from obliskiq.decision.swap_partners import SwapPartners
from obliskiq.decision.vehicle_selector import Exposure, SelectorInputs, resolve_exposure
from obliskiq.domain.enums import AssetClass
from obliskiq.jurisdiction import TAX_POLICIES, load_profile
from obliskiq.quant.tracking import CorrelationReading

SPAN = (date(2021, 10, 29), date(2026, 10, 22))


class FakeInputs:
    """The selector's inputs without a store: the rig's rates, partners that track at 0.999."""

    def __init__(self) -> None:
        self.selected: list[str] = []
        self.paired: list[Sequence[tuple[str, str]]] = []

    async def selector_inputs(
        self, exposure: Exposure, candidates: Sequence[UniverseInstrument], today: date
    ) -> SelectorInputs:
        self.selected.append(exposure.key)
        return inputs(as_of=today)

    async def partner_readings(
        self, pairs: Sequence[tuple[str, str]], today: date
    ) -> dict[tuple[str, str], CorrelationReading]:
        self.paired.append(pairs)
        return {pair: CorrelationReading(0.999, 60, *SPAN, None) for pair in pairs}


@pytest.fixture
def world(shipped_settings: ObliskConfig) -> ObliskConfig:
    settings = with_checks(shipped_settings, (AssetClass.ETF, "XMIL"), (AssetClass.ETF, "XLON"))
    return with_vehicles(settings, hedged_share_classes="allowed")


def chooser(settings: ObliskConfig, fake: FakeInputs) -> VehicleChooser:
    tax = TAX_POLICIES.resolve(load_profile(REPO / "config", "es"), settings)
    return VehicleChooser(settings, selector(settings), SwapPartners(settings, tax), fake)


def ranked(settings: ObliskConfig, role: str) -> list[Any]:
    return list(selector(settings).select(resolve_exposure(settings, role), inputs()).ranked)


async def test_the_best_ranked_vehicle_fills_a_role(world: ObliskConfig) -> None:
    best = ranked(world, "world_equity")[0]
    choice = await chooser(world, FakeInputs()).choose(
        ["world_equity"], held=set(), aware=False, today=AS_OF
    )
    found = choice.roles["world_equity"]
    assert found.primary is not None
    assert found.primary.symbol == best.symbol
    assert found.partner is None
    assert choice.vehicles == {"world_equity": found.vehicles}
    assert found.vehicles is not None
    assert found.vehicles.primary == str(best.conid)
    assert choice.conids == {str(best.conid): best.conid}
    assert choice.unfilled == {}


async def test_a_vehicle_the_core_holds_stays_while_it_is_still_proposed(
    world: ObliskConfig,
) -> None:
    second = ranked(world, "world_equity")[1]
    choice = await chooser(world, FakeInputs()).choose(
        ["world_equity"], held={second.conid}, aware=False, today=AS_OF
    )
    assert choice.roles["world_equity"].primary is not None
    assert choice.roles["world_equity"].primary.conid == second.conid


async def test_a_held_vehicle_the_selector_no_longer_proposes_gives_way(
    world: ObliskConfig,
) -> None:
    best = ranked(world, "world_equity")[0]
    choice = await chooser(world, FakeInputs()).choose(
        ["world_equity"], held={123456789}, aware=False, today=AS_OF
    )
    assert choice.roles["world_equity"].primary is not None
    assert choice.roles["world_equity"].primary.conid == best.conid


async def test_a_tilting_variant_takes_a_swap_partner_and_keeps_the_one_it_holds(
    world: ObliskConfig,
) -> None:
    fake = FakeInputs()
    both = chooser(world, fake)
    choice = await both.choose(["world_equity"], held=set(), aware=True, today=AS_OF)
    found = choice.roles["world_equity"]
    assert found.primary is not None
    assert found.partner is not None
    assert found.partner.symbol != found.primary.symbol
    assert found.vehicles is not None
    assert found.vehicles.partner == str(found.partner.conid)
    assert set(choice.conids) == {str(found.primary.conid), str(found.partner.conid)}
    later = await both.choose(
        ["world_equity"], held={found.primary.conid, found.partner.conid}, aware=True, today=AS_OF
    )
    assert later.roles["world_equity"].partner is not None
    assert later.roles["world_equity"].partner.conid == found.partner.conid


async def test_a_role_nobody_can_fill_says_why(shipped_settings: ObliskConfig) -> None:
    choice = await chooser(shipped_settings, FakeInputs()).choose(
        ["gold"], held=set(), aware=True, today=AS_OF
    )
    found = choice.roles["gold"]
    assert found.primary is None
    assert found.vehicles is None
    assert "no vehicle of gold is proposable" in found.why
    assert choice.vehicles == {}
    assert choice.unfilled == {"gold": found.why}


async def test_a_role_without_a_candidate_says_so(world: ObliskConfig) -> None:
    settings = world.model_copy(
        update={
            "universe": world.universe.model_copy(
                update={
                    "instruments": [
                        i
                        for i in world.universe.instruments
                        if "gold" not in i.exposures.core_roles
                    ]
                }
            )
        }
    )
    choice = await chooser(settings, FakeInputs()).choose(
        ["gold"], held=set(), aware=False, today=AS_OF
    )
    assert choice.roles["gold"].why == "gold has no candidate in the universe"


async def test_a_selection_and_a_partner_search_are_made_once_a_day(world: ObliskConfig) -> None:
    fake = FakeInputs()
    both = chooser(world, fake)
    for _ in range(3):
        await both.choose(["world_equity"], held=set(), aware=True, today=AS_OF)
    assert fake.selected == ["world_equity"]
    assert len(fake.paired) == 1
    await both.choose(["world_equity"], held=set(), aware=True, today=date(2026, 10, 23))
    assert fake.selected == ["world_equity", "world_equity"]


def test_a_role_choice_without_a_vehicle_has_none() -> None:
    assert RoleChoice("gold", None, None, "why").vehicles is None
