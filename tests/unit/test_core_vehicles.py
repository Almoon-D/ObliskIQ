"""Each core role's vehicles through the vehicle selector: the best-ranked primary, its swap
partner when the variant trades tax-aware, held vehicles kept (and closed when no longer
proposable), refused identities never bought, a role with nothing proposable, a held vehicle no
role names, and correlations measured or not.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal

from capital_rig import REPO
from vehicle_rig import inputs, selector

from obliskiq.config import ObliskConfig
from obliskiq.decision.core_vehicles import (
    UNLISTED,
    CoreVehicleChooser,
    MeasuredCorrelations,
    Unmeasured,
)
from obliskiq.decision.swap_partners import SwapPartners
from obliskiq.decision.vehicle_selector import resolve_exposure
from obliskiq.jurisdiction import TAX_POLICIES, load_profile
from obliskiq.quant.tracking import CorrelationReading

SPPW, XDWD, IWDA, EXV1 = 355720826, 163606916, 78999785, 89005163
UNMEASURED = Unmeasured("no research history in this test")


def chooser(settings: ObliskConfig) -> CoreVehicleChooser:
    policy = TAX_POLICIES.resolve(load_profile(REPO / "config", "es"), settings)
    return CoreVehicleChooser(settings, selector(settings), SwapPartners(settings, policy))


def reading(value: float) -> CorrelationReading:
    return CorrelationReading(value, 60, date(2021, 10, 31), date(2026, 9, 30), None)


def test_the_primary_is_the_best_ranked_vehicle_and_a_partner_needs_its_correlation(
    shipped_settings: ObliskConfig,
) -> None:
    pick = chooser(shipped_settings)
    found = pick.choose(["world_equity"], {}, inputs(), UNMEASURED, partners=True, refused={})
    world = found.choices["world_equity"]
    assert world.primary == SPPW
    assert world.partner is None
    assert "no swap partner (XDWD: correlation with SPPW unmeasured" in world.reason
    assert found.selections["world_equity"].best is not None
    measured = MeasuredCorrelations({}, {("SPPW", "XDWD"): reading(1.0)}, "not read")
    world = pick.choose(
        ["world_equity"], {}, inputs(), measured, partners=True, refused={}
    ).choices["world_equity"]
    assert (world.primary, world.partner) == (SPPW, XDWD)
    assert "swap partner XDWD" in world.reason
    # Without tax-aware execution no partner is chosen.
    plain = pick.choose(["world_equity"], {}, inputs(), measured, partners=False, refused={})
    assert plain.choices["world_equity"].partner is None


def test_a_held_vehicle_stays_with_its_role_and_is_closed_when_no_longer_proposable(
    shipped_settings: ObliskConfig,
) -> None:
    pick = chooser(shipped_settings)
    held = {IWDA: Decimal(500), XDWD: Decimal(900)}
    world = pick.choose(
        ["world_equity"], held, inputs(), UNMEASURED, partners=False, refused={}
    ).choices["world_equity"]
    # XDWD, the larger, first; IWDA's London line is not proposable: sold, never bought.
    assert world.held == (XDWD, IWDA)
    assert world.closed == frozenset({IWDA})
    assert world.primary == SPPW
    assert f"held and no longer bought: {IWDA}" in world.reason


def test_a_refused_identity_is_never_the_primary(shipped_settings: ObliskConfig) -> None:
    pick = chooser(shipped_settings)
    one = pick.choose(
        ["world_equity"], {}, inputs(), UNMEASURED, partners=False, refused={SPPW: "not found"}
    )
    assert one.choices["world_equity"].primary == XDWD
    both = pick.choose(
        ["world_equity"],
        {},
        inputs(),
        UNMEASURED,
        partners=False,
        refused={SPPW: "not found", XDWD: "another conid"},
    ).choices["world_equity"]
    assert both.primary is None
    assert "IBKR did not confirm SPPW (not found), XDWD (another conid)" in both.reason


def test_a_role_without_a_proposable_vehicle_says_why(shipped_settings: ObliskConfig) -> None:
    gold = (
        chooser(shipped_settings)
        .choose(["gold"], {}, inputs(), UNMEASURED, partners=True, refused={})
        .choices["gold"]
    )
    assert gold.primary is None
    assert "none of its 3 candidates passes the gates (first, IGLN [capability_unverified]" in (
        gold.reason
    )


def test_a_held_vehicle_no_role_names_is_reported_to_be_sold(
    shipped_settings: ObliskConfig,
) -> None:
    found = chooser(shipped_settings).choose(
        ["world_equity"], {EXV1: Decimal(100)}, inputs(), UNMEASURED, partners=False, refused={}
    )
    unlisted = found.choices[f"{UNLISTED}{EXV1}"]
    assert (unlisted.primary, unlisted.held, unlisted.closed) == (None, (EXV1,), {EXV1})
    assert "fills no core role" in unlisted.reason


def test_a_held_vehicle_counts_toward_its_first_role(shipped_settings: ObliskConfig) -> None:
    pick = chooser(shipped_settings)
    declared = shipped_settings.universe.pinned(SPPW)
    assert declared is not None
    both = declared.model_copy(
        update={
            "exposures": declared.exposures.model_copy(
                update={"core_roles": ["em_equity", "world_equity"]}
            )
        }
    )
    assert pick.role_of(both, ["world_equity", "em_equity"]) == "world_equity"
    assert pick.role_of(both, ["cash"]) == "em_equity"
    # A held SPPW joins the role it declares although the variant names another; held and the
    # best-ranked, it is that role's primary.
    held = pick.choose(
        ["em_equity"], {SPPW: Decimal(10)}, inputs(), UNMEASURED, partners=False, refused={}
    )
    assert list(held.choices) == ["em_equity", "world_equity"]
    world = held.choices["world_equity"]
    assert (world.primary, world.held, world.closed) == (SPPW, (), frozenset())


def test_measured_correlations_fill_in_what_was_not_read(shipped_settings: ObliskConfig) -> None:
    exposure = resolve_exposure(shipped_settings, "world_equity")
    measured = MeasuredCorrelations(
        {"world_equity": {"SPPW": reading(0.99)}}, {("SPPW", "XDWD"): reading(1.0)}, "not read"
    )
    tracking = measured.tracking(exposure, ["SPPW", "XDWD"])
    assert tracking["SPPW"].value == 0.99
    assert tracking["XDWD"].reason == "not read"
    pairs = measured.pairs([("SPPW", "XDWD"), ("XDWD", "SPPW")])
    assert pairs[("SPPW", "XDWD")].value == 1.0
    assert pairs[("XDWD", "SPPW")].reason == "not read"
    assert UNMEASURED.tracking(exposure, ["SPPW"])["SPPW"].reason == (
        "no research history in this test"
    )
    assert UNMEASURED.pairs([("SPPW", "XDWD")])[("SPPW", "XDWD")].value is None
