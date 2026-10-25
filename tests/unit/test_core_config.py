"""The core's configuration is the pre-registration's constants and the owner's choice, and refuses
what cannot be held.

``config/defaults/core.yaml`` restates what Session 4c registered
(``research/preregistrations/core-allocation.md``); a change there is a new pre-registration.
These tests hold the two equal, so a constant edited in one place and not the other fails here,
and hold ``core.variant`` to the entry of DECISIONS that records the owner's point on the frontier.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import copy
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_core import CoreConfig
from obliskiq.engines.core.variants import CoreVariants
from obliskiq.research.prereg import (
    BandedWeightsSpec,
    BuyAndHoldSpec,
    EquityOverlaySpec,
    RiskBalanceSpec,
    load_prereg,
)
from obliskiq.research.strategies import static_weights

D = Decimal


@pytest.fixture(scope="module")
def prereg(repo_root: Path) -> Any:
    return load_prereg(repo_root / "research" / "preregistrations" / "core-allocation.md")


@pytest.fixture(scope="module")
def codes(shipped_settings: ObliskConfig) -> dict[str, str]:
    """The pre-registration's code of each role, from the role's name ('... (WORLD)')."""
    found = {
        role: re.search(r"\(([A-Z_]+)\)$", config.name)
        for role, config in shipped_settings.core.roles.items()
    }
    assert all(found.values())
    return {role: match.group(1) for role, match in found.items() if match}


def test_the_chosen_variant_is_the_owners_point_on_the_frontier(
    shipped_settings: ObliskConfig, repo_root: Path
) -> None:
    text = (repo_root / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    entry = text[text.index("### D91 ") : text.index("### D92 ")]
    assert "resolves to **A**" in entry
    assert "`core.variant` defaults to A" in entry
    assert shipped_settings.core.variant == "A"


def test_the_environments_risk_bands_tilt_and_tax_rule_are_the_registered_ones(
    shipped_settings: ObliskConfig, prereg: Any, codes: dict[str, str]
) -> None:
    config = shipped_settings.core
    registered = prereg.spec.study.core
    by_code = {code: role for role, code in codes.items()}
    assert codes[config.cash_role] == registered.cash_role
    assert {
        env: [codes[r] for r in roles] for env, roles in config.environments.items()
    } == registered.environments
    assert {codes[r]: v for r, v in config.risk.volatilities.items()} == dict(
        registered.risk.volatilities
    )
    ours = {frozenset((codes[a], codes[b])): c for a, b, c in config.risk.correlations}
    theirs = {frozenset((a, b)): c for a, b, c in registered.risk.correlations}
    assert ours == theirs
    assert by_code  # every role has a code
    assert (config.bands.absolute, config.bands.relative) == (
        registered.bands.absolute,
        registered.bands.relative,
    )
    for field in ("momentum_months", "average_months", "hysteresis", "floor"):
        assert getattr(config.tilt, field) == getattr(registered.tilt, field), field
    assert config.tax_aware.benefit_fraction == registered.tax_aware.benefit_fraction


def test_every_registered_portfolio_is_a_variant_with_its_constants(
    shipped_settings: ObliskConfig, prereg: Any, codes: dict[str, str]
) -> None:
    study = prereg.spec.study
    ours = {variant.name: variant for variant in shipped_settings.core.variants}
    registered = {"equity_reference": study.reference} | {s.name: s for s in study.candidates}
    assert set(ours) == set(registered)
    for name, spec in registered.items():
        variant = ours[name]
        assert variant.kind == spec.strategy, name
        if isinstance(spec, BuyAndHoldSpec | BandedWeightsSpec):
            assert {codes[r]: w for r, w in variant.weights.items()} == dict(spec.weights), name  # type: ignore[union-attr]
        elif isinstance(spec, RiskBalanceSpec):
            assert dict(variant.budgets) == dict(spec.budgets), name  # type: ignore[union-attr]
            assert variant.tilt == spec.tilt, name  # type: ignore[union-attr]
        else:
            assert isinstance(spec, EquityOverlaySpec)
            assert codes[variant.equity] == spec.equity, name  # type: ignore[union-attr]
            assert (variant.fraction, variant.tilt) == (spec.fraction, spec.tilt), name  # type: ignore[union-attr]


def test_the_engines_static_weights_are_the_harness_weights(
    shipped_settings: ObliskConfig, prereg: Any, codes: dict[str, str]
) -> None:
    """The weights the account holds are the weights the harness computed from the
    pre-registration, role for role, to the last digit."""
    variants = CoreVariants(shipped_settings)
    study = prereg.spec.study
    for spec in study.candidates:
        if isinstance(spec, BandedWeightsSpec):
            continue
        theirs = static_weights(spec, study.core, shipped_settings)
        ours = {codes[role]: w for role, w in variants.get(spec.name).weights.items()}
        assert ours == theirs, spec.name


def test_a_core_refuses_what_cannot_be_held(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.core.model_dump(mode="json")

    def refuse(change: Any, match: str) -> None:
        mutated = copy.deepcopy(data)
        change(mutated)
        with pytest.raises(ValidationError, match=match):
            CoreConfig.model_validate(mutated)

    refuse(lambda d: d.update(variant="Z"), "is not a registered variant")
    refuse(lambda d: d.update(cash_role="nowhere"), "is not one of core.roles")
    refuse(lambda d: d["environments"]["growth_up"].append("cash"), "belongs to no environment")
    refuse(lambda d: d["environments"]["growth_up"].append("nowhere"), "does not declare")
    refuse(lambda d: d["environments"].update(empty=[]), "at least one role")
    refuse(lambda d: d["environments"]["growth_up"].append("em_equity"), "names a role twice")
    refuse(lambda d: d["environments"]["inflation_up"].remove("gold"), "must cover exactly")
    refuse(lambda d: d["risk"]["volatilities"].pop("gold"), "exactly one correlation")
    refuse(lambda d: d["risk"]["correlations"].pop(), "exactly one correlation")
    refuse(lambda d: d["risk"]["correlations"].append(["gold", "gold", "1"]), "two different roles")
    refuse(lambda d: d["variants"].append(dict(d["variants"][0])), "registered once")
    refuse(lambda d: d["variants"][0]["weights"].update(world_equity="0.5"), "add up to 1")
    refuse(lambda d: d["variants"][0]["weights"].update(nowhere="0.5"), "does not declare")
    refuse(lambda d: d["variants"][2]["budgets"].pop("growth_up"), "budgets every environment")
    refuse(lambda d: d["variants"][2]["budgets"].update(growth_up="0.5"), "add up to 1")
    refuse(lambda d: d["variants"][5].update(equity="cash"), "not a risky role")
