"""Swap-partner groups: another issuer under the tax policy, another index unless the owner
allows the same one, and a correlation measured above the threshold (Session 9).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from typing import Any

import pytest
from vehicle_rig import REPO, attributes, select, with_checks, with_vehicles

from obliskiq.config import ObliskConfig
from obliskiq.decision.swap_partners import Partner, SwapPartners
from obliskiq.domain.enums import AssetClass
from obliskiq.jurisdiction import TAX_POLICIES, load_profile
from obliskiq.quant.tracking import CorrelationReading

SPAN = (date(2021, 10, 29), date(2026, 10, 22))


def reading(value: float) -> CorrelationReading:
    return CorrelationReading(value, 60, *SPAN, None)


def group(settings: ObliskConfig, role: str, primary: str, correlations: dict[Any, Any]) -> Any:
    tax = TAX_POLICIES.resolve(load_profile(REPO / "config", "es"), settings)
    selection = select(settings, role)
    return SwapPartners(settings, tax).group(selection, primary, correlations)


@pytest.fixture
def world(shipped_settings: ObliskConfig) -> ObliskConfig:
    """MSCI World on Xetra (XDWD, SPPW), FTSE Developed on Borsa Italiana (VHVE), the iShares
    fund on the LSE (IWDA) and its EUR-hedged class (IWLE), every venue verified."""
    settings = with_checks(shipped_settings, (AssetClass.ETF, "XMIL"), (AssetClass.ETF, "XLON"))
    return with_vehicles(settings, hedged_share_classes="allowed")


def test_a_same_index_partner_is_refused_while_the_setting_is_off(world: ObliskConfig) -> None:
    correlations = {("XDWD", other): reading(0.999) for other in ("SPPW", "VHVE", "IWDA", "IWLE")}
    found = group(world, "world_equity", "XDWD", correlations)
    refused = {r.symbol: r.reason for r in found.refused}
    assert "same index as XDWD (MSCI World)" in refused["SPPW"]
    assert "vehicles.swap_partners.same_index" in refused["SPPW"]
    assert [p.symbol for p in found.partners] == ["VHVE"]
    # The owner's switch admits a same-index partner from another issuer.
    on = world.model_copy(
        update={
            "vehicles": world.vehicles.model_copy(
                update={
                    "swap_partners": world.vehicles.swap_partners.model_copy(
                        update={"same_index": True}
                    )
                }
            )
        }
    )
    partners = {p.symbol for p in group(on, "world_equity", "XDWD", correlations).partners}
    assert {"SPPW", "VHVE"} <= partners


def test_another_issuer_on_another_index_correlated_enough_is_a_partner(
    world: ObliskConfig,
) -> None:
    found = group(world, "world_equity", "XDWD", {("XDWD", "VHVE"): reading(0.996)})
    partner = next(p for p in found.partners if p.symbol == "VHVE")
    assert isinstance(partner, Partner)
    assert partner.correlation == 0.996
    assert "issuer Vanguard, index FTSE Developed" in partner.reason


def test_a_class_of_the_same_fund_is_homogeneous(world: ObliskConfig) -> None:
    found = group(world, "world_equity", "IWDA", {("IWDA", "IWLE"): reading(0.99)})
    refused = {r.symbol: r.reason for r in found.refused}
    assert "homogeneous with IWDA under es_irpf" in refused["IWLE"]


def test_the_issuer_rule_refuses_one_issuers_other_index(
    shipped_settings: ObliskConfig,
) -> None:
    """DBXQ and X57E are both Xtrackers: another index is not enough under es."""
    found = group(shipped_settings, "euro_govt_intermediate", "DBXQ", {})
    refused = {r.symbol: r.reason for r in found.refused}
    assert "swap rule refuses it beside DBXQ" in refused["X57E"]


def test_an_uncorrelated_or_unmeasured_partner_is_refused(world: ObliskConfig) -> None:
    floor = world.vehicles.swap_partners.min_correlation
    low = group(world, "world_equity", "XDWD", {("XDWD", "VHVE"): reading(0.9)})
    assert f"below {floor}" in {r.symbol: r.reason for r in low.refused}["VHVE"]
    unread = group(world, "world_equity", "XDWD", {})
    assert "no reading was taken" in {r.symbol: r.reason for r in unread.refused}["VHVE"]
    unmeasured = group(
        world,
        "world_equity",
        "XDWD",
        {("XDWD", "VHVE"): CorrelationReading.unmeasured("not loaded")},
    )
    assert "unmeasured: not loaded" in {r.symbol: r.reason for r in unmeasured.refused}["VHVE"]


def test_an_unknown_index_cannot_be_told_apart(world: ObliskConfig) -> None:
    unknown = attributes(world, "VHVE", tracked_index=None)
    found = group(unknown, "world_equity", "XDWD", {("XDWD", "VHVE"): reading(0.999)})
    reason = {r.symbol: r.reason for r in found.refused}["VHVE"]
    assert "tax identity" in reason


def test_a_primary_the_selector_does_not_propose_is_refused(
    shipped_settings: ObliskConfig,
) -> None:
    with pytest.raises(ValueError, match="IWDA is not a vehicle the selector proposes"):
        group(shipped_settings, "world_equity", "IWDA", {})


def test_an_unknown_index_with_the_switch_off_is_refused(world: ObliskConfig) -> None:
    """Gold ETCs are debt securities: their identity needs no index, so an unknown one reaches the
    index rule and is refused there."""
    gold = with_checks(world, (AssetClass.ETC, "XLON"), (AssetClass.ETC, "XMIL"))
    gold = attributes(gold, "SGLD", tracked_index=None)
    found = group(gold, "gold", "IGLN", {("IGLN", "SGLD"): reading(0.99)})
    reason = {r.symbol: r.reason for r in found.refused}["SGLD"]
    assert "tracked index is unknown" in reason
