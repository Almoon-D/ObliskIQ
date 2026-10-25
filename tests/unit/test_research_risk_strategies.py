"""The total-risk strategy (DECISIONS D92) with a stand-in view and book: the risky share of a
target volatility over the forecast, capped at one, its floor and largest change, the declared
twin's constant share, the targets it hands the core's bands and tax-aware execution, and the
builders from the toy's specification.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from research_risk_toy import RISK_TOY

from obliskiq.errors import PreregistrationError, SimulationError
from obliskiq.quant.covariance import Estimate, EstimatorRule
from obliskiq.research.prereg import load_prereg
from obliskiq.research.strategies import (
    Bands,
    CoreStrategy,
    RoleVehicles,
    TaxAware,
    TotalRisk,
    build_strategy,
    declared_share,
    static_weights,
    total_risk,
)

D = Decimal
ON = date(2025, 6, 30)
HUNDRED = Decimal(100)
VEHICLES = {
    "EQ": RoleVehicles("EQ", "EQ_SWAP"),
    "BD": RoleVehicles("BD", None),
    "CASH": RoleVehicles("CASH", "CASH_SWAP"),
}
RULE = EstimatorRule(half_life=10.0, window=None, ewma_share=1.0, shrinkage=False,
                     prior_observations=0.0)  # fmt: skip


@dataclass
class View:
    """A view whose estimate is a yearly volatility given per instrument, uncorrelated."""

    vols: dict[str, float] = field(default_factory=dict)
    as_of: date = ON
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def covariance(self, keys, rule, target):
        self.asked.append(tuple(keys))
        if not self.vols:
            return None
        daily = np.diag([self.vols[k] ** 2 / 261 for k in keys])
        return Estimate(daily, 100.0, None)


@dataclass
class Book:
    """A stand-in book: values in the base currency, a unit price of 1, chosen tax answers."""

    values: dict[str, Decimal]
    investable: Decimal = HUNDRED
    taxes: dict[str, Decimal] = field(default_factory=dict)
    on: date = ON

    @property
    def units(self) -> dict[str, Decimal]:
        return {key: value for key, value in self.values.items() if value > 0}

    def value(self, key: str) -> Decimal:
        return self.values.get(key, D(0))

    def weight(self, key: str) -> Decimal:
        return self.value(key) / self.investable

    def basis(self, key: str) -> Decimal:
        return self.value(key)

    def units_for(self, key: str, value: Decimal) -> Decimal:
        return value

    def sales_tax(self, sales: list[tuple[str, Decimal]]) -> Decimal:
        return sum((self.taxes.get(key, D(0)) * units for key, units in sales), D(0))

    def purchase_defers_loss(self, key: str) -> bool:
        return False


def scaling(**overrides) -> TotalRisk:
    fields = {
        "base": {"EQ": D("1")},
        "target_volatility": 0.10,
        "floor": D("0.2"),
        "max_change": D("0.25"),
        "estimator": RULE,
        "declared": np.array([[0.16**2]]),
        "periods_per_year": 261,
    }
    fields.update(overrides)
    return TotalRisk(**fields)


def strategy(rule: TotalRisk, *, tax: bool = False) -> CoreStrategy:
    weights = {"EQ": D("0.625"), "CASH": D("0.375")}
    return CoreStrategy(
        name="scaled",
        weights=weights,
        vehicles=VEHICLES,
        bands=Bands(D("0.05"), D("0.25")),
        cash_role="CASH",
        tilt=None,
        tax_aware=TaxAware(D("0.05")) if tax else None,
        scaling=rule,
    )


def test_the_share_is_the_target_over_the_forecast_capped_at_one_with_a_floor():
    rule = scaling()
    assert rule.share(0.20, None) == D("0.5")
    assert rule.share(0.05, None) == D(1)
    assert rule.share(1.00, None) == D("0.2")  # the floor, not 0.1
    # The largest change holds the share near what is held.
    assert rule.share(0.05, D("0.5")) == D("0.75")
    assert rule.share(1.00, D("0.9")) == D("0.65")
    assert rule.share(0.125, D("0.79")) == D("0.8")


def test_the_forecast_reads_the_estimate_or_the_declared_matrix():
    rule = scaling()
    view = View({"EQ": 0.20})
    assert rule.forecast(view, ["EQ"]) == pytest.approx(0.20)
    assert view.asked == [("EQ",)]
    twin = scaling(estimator=None)
    assert twin.forecast(View(), ["EQ"]) == pytest.approx(0.16)
    assert twin.declared_volatility() == pytest.approx(0.16)
    with pytest.raises(SimulationError, match="no covariance of EQ"):
        rule.forecast(View(), ["EQ"])


def test_a_mix_is_forecast_on_its_relative_weights():
    rule = scaling(
        base={"EQ": D("0.6"), "BD": D("0.4")},
        declared=np.array([[0.0256, 0.0], [0.0, 0.0016]]),
    )
    expected = math.sqrt(0.36 * 0.04 + 0.16 * 0.0025)
    assert rule.forecast(View({"EQ": 0.2, "BD": 0.05}), ["EQ", "BD"]) == pytest.approx(expected)


def test_targets_hold_the_base_at_the_share_and_the_rest_in_cash():
    built = strategy(scaling())
    first = built.targets(View({"EQ": 0.20}), Book({}))
    assert first == {"EQ": D("0.5"), "CASH": D("0.5")}
    # Volatility doubles: the share falls by at most the largest change, from 0.5 to 0.25.
    held = Book({"EQ": D(50), "CASH": D(50)})
    assert built.targets(View({"EQ": 0.40}), held) == {"EQ": D("0.25"), "CASH": D("0.75")}
    # Inside every band: nothing trades.
    assert built.targets(View({"EQ": 0.198}), held) is None
    targets, scales = built.role_targets(View({"EQ": 0.20}), held)
    assert sum(targets.values()) == 1
    assert scales == {"EQ": D("0.5"), "CASH": D(1)}


def test_targets_need_a_view_that_estimates_covariance():
    @dataclass
    class TrendOnly:
        as_of: date = date(2026, 10, 1)

        def trend(self, key: str, rule: object) -> None:
            return None

    with pytest.raises(SimulationError, match="scales total risk on a view of covariance"):
        strategy(scaling()).role_targets(TrendOnly(), Book({}))


def test_a_reduction_whose_tax_is_above_its_benefit_is_held_when_tax_aware():
    built = strategy(scaling(), tax=True)
    costly = Book({"EQ": D(50), "CASH": D(50)}, taxes={"EQ": D("0.2")})
    assert built.targets(View({"EQ": 0.40}), costly) is None
    cheap = Book({"EQ": D(50), "CASH": D(50)}, taxes={"EQ": D("0.01")})
    assert built.targets(View({"EQ": 0.40}), cheap) == {"EQ": D("0.25"), "CASH": D("0.75")}
    # Tax-aware without a benefit fraction: a reduction is always made, whatever its tax.
    always = replace(built, tax_aware=TaxAware(None))
    assert always.targets(View({"EQ": 0.40}), costly) == {"EQ": D("0.25"), "CASH": D("0.75")}


def test_a_strategy_that_scales_refuses_a_tilt_a_missing_cash_role_or_an_unheld_base():
    from obliskiq.quant.trend import TrendRule

    rule = scaling()
    common = {"name": "x", "weights": {"EQ": D("0.5")}, "bands": Bands(D("0.05"), D("0.25"))}
    tilt = TrendRule(momentum_months=3, average_months=2, hysteresis=0.0, floor=0.5)
    with pytest.raises(ValueError, match="both tilts and scales"):
        CoreStrategy(**common, vehicles=VEHICLES, cash_role="CASH", tilt=tilt, tax_aware=None,
                     scaling=rule)  # fmt: skip
    with pytest.raises(ValueError, match="without a cash role"):
        CoreStrategy(**common, vehicles=VEHICLES, cash_role=None, tilt=None, tax_aware=None,
                     scaling=rule)  # fmt: skip
    with pytest.raises(ValueError, match="cannot hold"):
        CoreStrategy(**common, vehicles=VEHICLES, cash_role="CASH", tilt=None, tax_aware=None,
                     scaling=scaling(base={"GD": D(1)}))  # fmt: skip
    with pytest.raises(ValueError, match="tax-aware execution belongs"):
        CoreStrategy(**common, vehicles=VEHICLES, cash_role="CASH", tilt=None,
                     tax_aware=TaxAware(D("0.05")), scaling=None)  # fmt: skip


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"base": {"EQ": D("0.5")}}, "sum to 1"),
        ({"base": {"EQ": D("1"), "BD": D("0")}}, "sum to 1"),
        ({"declared": np.eye(2)}, "covers the base"),
        ({"target_volatility": 0.0}, "target volatility"),
        ({"floor": D(1)}, "floor"),
        ({"max_change": D(0)}, "largest change"),
        ({"periods_per_year": 0}, "largest change"),
    ],
)
def test_a_total_risk_rule_refuses_what_cannot_be(overrides, message):
    with pytest.raises(ValueError, match=message):
        scaling(**overrides)


@pytest.fixture
def study(tmp_path: Path):
    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    return load_prereg(path).spec.study


def test_the_builders_give_each_portfolio_its_rule_and_the_twin_its_declared_share(
    study, shipped_settings
):
    s1, s2 = study.candidates
    t1, t2 = study.twins
    core = study.core
    assert declared_share(t1, core) == D("0.625")
    assert static_weights(t1, core, shipped_settings) == {"EQ": D("0.625"), "CASH": D("0.375")}
    rule = total_risk(s2, core)
    assert rule.estimator is not None
    assert rule.estimator.shrinkage
    assert total_risk(t2, core).estimator is None
    # 0.6 / 0.4 of a 16 % and a 4 % volatility, uncorrelated.
    assert rule.declared_volatility() == pytest.approx(math.sqrt(0.36 * 0.0256 + 0.16 * 0.0016))
    built = build_strategy(s2, study, shipped_settings)
    assert isinstance(built, CoreStrategy)
    assert built.scaling is not None
    assert built.vehicles["EQ"].partner == "EQ_SWAP"  # tax-aware: swap partners
    assert built.tax_aware == TaxAware(D("0.05"))
    partners = build_strategy(s1, study, shipped_settings)
    assert isinstance(partners, CoreStrategy)
    assert partners.vehicles["EQ"].partner == "EQ_SWAP"
    assert partners.tax_aware == TaxAware(None)
    plain = build_strategy(s1.model_copy(update={"tax_aware": False}), study, shipped_settings)
    assert isinstance(plain, CoreStrategy)
    assert plain.vehicles["EQ"].partner is None
    assert plain.tax_aware is None


def test_the_builders_refuse_a_core_without_its_scaling_or_tax_rule(study):
    from obliskiq.research.strategies import tilt_rule

    s1, s2 = study.candidates
    with pytest.raises(PreregistrationError, match=r"needs study\.core\.scaling"):
        total_risk(s1, study.core.model_copy(update={"scaling": None}))
    bare = study.model_copy(update={"core": study.core.model_copy(update={"tax_aware": None})})
    with pytest.raises(PreregistrationError, match=r"needs study\.core\.tax_aware"):
        build_strategy(s2, bare, None)  # type: ignore[arg-type]
    with pytest.raises(PreregistrationError, match=r"needs study\.core\.tilt"):
        tilt_rule(study.core)
