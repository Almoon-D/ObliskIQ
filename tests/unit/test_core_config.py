"""The core's configuration: every constant is Session 4c's pre-registration's, the variant is the
owner's point (DECISIONS D91: A), the static weights are the ones Session 4c's report printed, and
the schema refuses a core that cannot be built.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_core import CoreConfig
from obliskiq.engines.core.rules import RoleVehicles
from obliskiq.engines.core.variants import build_variant
from obliskiq.errors import CoreError
from obliskiq.research.prereg import (
    BandedWeightsSpec,
    BuyAndHoldSpec,
    CoreSpec,
    EquityOverlaySpec,
    RiskBalanceSpec,
    StudySpec,
    load_prereg,
)

REPO = Path(__file__).resolve().parents[2]
PREREG = REPO / "research" / "preregistrations" / "core-allocation.md"

#: Session 4c's report's static weights by role code (research/reports/core-allocation.md).
REPORTED = {
    "A": {
        "WORLD": "9.03",
        "EGB_INT": "33.80",
        "EM": "6.52",
        "COMMOD": "10.82",
        "EGB_LONG": "11.27",
        "UST": "10.90",
        "IL": "12.30",
        "GOLD": "5.36",
    },
    "C": {
        "WORLD": "16.39",
        "EGB_INT": "27.53",
        "EM": "11.81",
        "COMMOD": "17.05",
        "EGB_LONG": "9.18",
        "UST": "7.58",
        "IL": "7.41",
        "GOLD": "3.05",
    },
}


@pytest.fixture(scope="module")
def study() -> StudySpec:
    return load_prereg(PREREG).spec.study


def codes(study: StudySpec, settings: ObliskConfig) -> dict[str, str]:
    """Each of the pre-registration's role codes as a role id: the role whose exposure series is
    the series the code's primary vehicle was priced on."""
    assert study.roles is not None
    series = {i.key: i.price_series for i in study.instruments}
    by_series = {role.exposure_series: name for name, role in settings.core.roles.items()}
    return {code: by_series[series[vehicles.primary]] for code, vehicles in study.roles.items()}


def test_every_core_constant_is_the_pre_registrations(
    study: StudySpec, shipped_settings: ObliskConfig
) -> None:
    core = shipped_settings.core
    spec: CoreSpec | None = study.core
    assert spec is not None
    role = codes(study, shipped_settings)
    assert len(set(role.values())) == len(role) == len(core.roles)
    assert role[spec.cash_role] == core.cash_role
    assert {
        name: [role[code] for code in basket] for name, basket in spec.environments.items()
    } == core.environments
    assert list(core.environments) == list(spec.environments)  # the solver's order
    assert {role[code]: vol for code, vol in spec.risk.volatilities.items()} == dict(
        core.risk.volatilities
    )
    declared = {frozenset((role[a], role[b])): c for a, b, c in spec.risk.correlations}
    assert {frozenset((a, b)): c for a, b, c in core.risk.correlations} == declared
    assert (core.bands.absolute, core.bands.relative) == (spec.bands.absolute, spec.bands.relative)
    assert spec.tilt is not None
    assert spec.tax_aware is not None
    assert core.tilt.model_dump() == spec.tilt.model_dump()
    assert core.tax_aware.benefit_fraction == spec.tax_aware.benefit_fraction


def test_every_registered_portfolio_is_a_variant_with_its_constants(
    study: StudySpec, shipped_settings: ObliskConfig
) -> None:
    role = codes(study, shipped_settings)
    variants = shipped_settings.core.variants
    assert set(variants) == {p.name for p in study.portfolios}
    for registered in study.portfolios:
        variant: Any = variants[registered.name]
        assert variant.strategy == registered.strategy
        if isinstance(registered, BuyAndHoldSpec | BandedWeightsSpec):
            assert variant.weights == {role[k]: w for k, w in registered.weights.items()}
        elif isinstance(registered, RiskBalanceSpec):
            assert (variant.budgets, variant.tilt) == (registered.budgets, registered.tilt)
        elif isinstance(registered, EquityOverlaySpec):
            assert (variant.equity, variant.fraction, variant.tilt) == (
                role[registered.equity],
                registered.fraction,
                registered.tilt,
            )


def test_the_core_runs_the_owners_point(shipped_settings: ObliskConfig) -> None:
    # DECISIONS D91: 30 % reached on at most 5 % of 20-year paths resolves to A; D95 keeps it.
    assert shipped_settings.core.variant == "A"
    variant = build_variant(shipped_settings)
    assert (variant.name, variant.tilts, variant.buy_and_hold) == ("A", False, False)


@pytest.mark.parametrize("name", ["A", "C"])
def test_the_static_weights_are_the_ones_session_4c_reported(
    name: str, study: StudySpec, shipped_settings: ObliskConfig
) -> None:
    role = codes(study, shipped_settings)
    weights = build_variant(shipped_settings, name).weights
    assert sum(weights.values()) == Decimal(1)
    for code, percent in REPORTED[name].items():
        shown = (weights[role[code]] * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert shown == Decimal(percent), code


def test_the_research_and_the_live_builds_give_the_same_weights(
    study: StudySpec, shipped_settings: ObliskConfig
) -> None:
    from obliskiq.research.strategies import static_weights

    role = codes(study, shipped_settings)
    assert study.core is not None
    for registered in study.candidates:
        if isinstance(registered, BuyAndHoldSpec):
            continue
        research = static_weights(registered, study.core, shipped_settings)
        live = build_variant(shipped_settings, registered.name).weights
        assert {role[k]: w for k, w in research.items()} == live, registered.name


def test_every_variant_builds_and_an_overlay_holds_the_rest_in_cash(
    shipped_settings: ObliskConfig,
) -> None:
    for name in shipped_settings.core.variants:
        variant = build_variant(shipped_settings, name)
        assert sum(variant.weights.values()) <= 1
    d20 = build_variant(shipped_settings, "D20")
    assert d20.weights == {"world_equity": Decimal("0.4"), "cash": Decimal("0.6")}
    assert d20.tilts
    assert "cash" in d20.roles
    reference = build_variant(shipped_settings, "equity reference")
    assert reference.buy_and_hold
    assert reference.rules({"world_equity": RoleVehicles("1", None)}).bands is None


def test_an_unknown_variant_or_an_unsolvable_risk_is_refused(
    shipped_settings: ObliskConfig,
) -> None:
    with pytest.raises(CoreError, match="is not a variant"):
        build_variant(shipped_settings, "Z")
    numerics = shipped_settings.numerics.model_copy(
        update={
            "risk_budget": shipped_settings.numerics.risk_budget.model_copy(
                update={"max_iterations": 1}
            )
        }
    )
    with pytest.raises(CoreError, match="did not converge"):
        build_variant(shipped_settings.model_copy(update={"numerics": numerics}), "A")
    # Eight roles all at -0.5 to one another: 1.5 I - 0.5 J has an eigenvalue of -2.5, so the
    # declared matrix is no covariance.
    risk = shipped_settings.core.risk
    negative = risk.model_copy(
        update={"correlations": [(a, b, Decimal("-0.5")) for a, b, _ in risk.correlations]}
    )
    core = shipped_settings.core.model_copy(update={"risk": negative})
    with pytest.raises(CoreError, match="variant A: the correlation matrix is not positive"):
        build_variant(shipped_settings.model_copy(update={"core": core}), "A")


def _core(shipped_settings: ObliskConfig, **changes: Any) -> dict[str, Any]:
    data = shipped_settings.core.model_dump(mode="json")
    data.update(changes)
    return data


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"variant": "Z"}, "is not one of core.variants"),
        ({"cash_role": "nowhere"}, "nowhere is not a role of core.roles"),
        ({"cash_role": "gold"}, "the cash role belongs to no environment"),
        (
            {"environments": {"growth_up": [], "growth_down": ["gold"]}},
            "every environment needs at least one role",
        ),
    ],
)
def test_a_core_that_cannot_be_built_is_refused(
    shipped_settings: ObliskConfig, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CoreConfig.model_validate(_core(shipped_settings, **changes))


def test_a_variant_must_fit_the_book_and_its_environments(shipped_settings: ObliskConfig) -> None:
    data = _core(shipped_settings)
    data["variants"]["60/40"]["weights"]["world_equity"] = "0.7"
    with pytest.raises(ValueError, match="more than the whole book"):
        CoreConfig.model_validate(data)
    data = _core(shipped_settings)
    data["variants"]["A"]["budgets"] = {"growth_up": "1"}
    with pytest.raises(ValueError, match="one budget per environment"):
        CoreConfig.model_validate(data)
    data = _core(shipped_settings)
    data["variants"]["A"]["budgets"]["growth_up"] = "0.5"
    with pytest.raises(ValueError, match="positive and summing to 1"):
        CoreConfig.model_validate(data)
    data = _core(shipped_settings)
    data["variants"]["D10"]["equity"] = "cash"
    with pytest.raises(ValueError, match="not the cash role"):
        CoreConfig.model_validate(data)
    data = _core(shipped_settings)
    data["risk"]["volatilities"].pop("gold")
    with pytest.raises(ValueError, match=r"exactly one correlation|covers exactly"):
        CoreConfig.model_validate(data)
    data = _core(shipped_settings)
    data["risk"]["correlations"] = data["risk"]["correlations"][1:]
    with pytest.raises(ValueError, match="exactly one correlation"):
        CoreConfig.model_validate(data)
    data = _core(shipped_settings)
    first = data["risk"]["correlations"][0]
    data["risk"]["correlations"][0] = [first[0], first[0], first[2]]
    with pytest.raises(ValueError, match="pairs two different roles"):
        CoreConfig.model_validate(data)
