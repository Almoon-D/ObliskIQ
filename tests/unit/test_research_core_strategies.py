"""The core's strategies with a stand-in market view and book: static weights inside wide bands,
the trend tilt and its floor, tax-aware execution (a costly reduction held, a cheap one made,
the cash role giving up what a held reduction keeps), swap partners on a purchase that would
defer a loss, and the builders from a specification.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from research_core_toy import CORE_TOY

from obliskiq.config import ObliskConfig
from obliskiq.engines.core.mechanism import (
    Bands,
    BuyAndHold,
    CoreStrategy,
    RoleVehicles,
    TaxAware,
)
from obliskiq.engines.core.weights import environment_budgets
from obliskiq.errors import PreregistrationError, SimulationError
from obliskiq.quant.trend import TrendRule, TrendState
from obliskiq.research.prereg import load_prereg
from obliskiq.research.strategies import FixedWeights, build_strategy, static_weights

D = Decimal
HUNDRED = Decimal(100)
ON = date(2025, 6, 30)
RULE = TrendRule(momentum_months=12, average_months=10, hysteresis=0.02, floor=0.25)
VEHICLES = {
    "EQ": RoleVehicles("EQ", "EQ_SWAP"),
    "BD": RoleVehicles("BD", None),
    "CASH": RoleVehicles("CASH", "CASH_SWAP"),
}


def state(scale: float) -> TrendState:
    return TrendState(scale >= 1, scale > RULE.floor, 0.0, 0.0, scale)


@dataclass
class View:
    scales: dict[str, float | None] = field(default_factory=dict)
    as_of: date = ON

    def trend(self, key: str, cash: str, rule: TrendRule) -> TrendState | None:
        assert (cash, rule) == ("CASH", RULE)
        scale = self.scales.get(key, 1.0)
        return None if scale is None else state(scale)


@dataclass
class Book:
    """A stand-in book: values in the base currency, a unit price of 1, chosen tax answers."""

    values: dict[str, Decimal]
    investable: Decimal = HUNDRED
    bases: dict[str, Decimal] = field(default_factory=dict)
    taxes: dict[str, Decimal] = field(default_factory=dict)
    defers: set[str] = field(default_factory=set)
    on: date = ON

    @property
    def units(self) -> dict[str, Decimal]:
        return {key: value for key, value in self.values.items() if value > 0}

    def price(self, key: str) -> Decimal:
        return D(1)

    def value(self, key: str) -> Decimal:
        return self.values.get(key, D(0))

    def weight(self, key: str) -> Decimal:
        return self.value(key) / self.investable if self.investable > 0 else D(0)

    def basis(self, key: str) -> Decimal:
        return self.bases.get(key, self.value(key))

    def units_for(self, key: str, value: Decimal) -> Decimal:
        return value

    def sales_tax(self, sales: list[tuple[str, Decimal]]) -> Decimal:
        return sum((self.taxes.get(key, D(0)) * units for key, units in sales), D(0))

    def purchase_defers_loss(self, key: str) -> bool:
        return key in self.defers


def core(weights: dict[str, Decimal], *, tilt: bool = False, tax: bool = False) -> CoreStrategy:
    return CoreStrategy(
        name="core",
        weights=weights,
        vehicles=VEHICLES,
        bands=Bands(D("0.05"), D("0.25")),
        cash_role="CASH",
        tilt=RULE if tilt else None,
        tax_aware=TaxAware(D("0.05")) if tax else None,
    )


SIXTY = {"EQ": D("0.6"), "BD": D("0.4")}


def test_the_band_is_the_smaller_of_its_two_widths():
    bands = Bands(D("0.05"), D("0.25"))
    assert bands.width(D("0.6")) == D("0.05")
    assert bands.width(D("0.08")) == D("0.02")


def test_static_weights_are_bought_first_then_held_inside_their_bands():
    strategy = core(SIXTY)
    assert strategy.targets(View(), Book({})) == SIXTY
    assert strategy.targets(View(), Book({"EQ": D(62), "BD": D(38)})) is None
    assert strategy.targets(View(), Book({"EQ": D(66), "BD": D(34)})) == {
        "EQ": D("0.6"),
        "BD": D("0.4"),
    }
    # A role held without a target (cash left over) is outside its band: it is sold.
    assert strategy.targets(View(), Book({"EQ": D(60), "BD": D(39), "CASH": D(1)})) == SIXTY


def test_the_tilt_scales_a_falling_role_down_to_its_floor_and_cash_takes_the_rest():
    strategy = core(SIXTY, tilt=True)
    targets, scales = strategy.role_targets(View({"EQ": 0.25}))
    assert targets == {"EQ": D("0.15"), "BD": D("0.4"), "CASH": D("0.45")}
    assert scales == {"EQ": D("0.25"), "BD": D(1), "CASH": D(1)}
    with pytest.raises(SimulationError, match="EQ lacks the 13 month ends"):
        strategy.role_targets(View({"EQ": None}))


def test_a_held_reduction_on_unround_values_never_sums_above_one():
    # Without the weight quantum these values summed 1E-27 above 1, which the simulator refuses
    # as leverage (every candidate would then be not evaluable).
    strategy = core({"EQ": D("0.5"), "BD": D("0.5")}, tilt=True, tax=True)
    values = {"EQ": D("48.248772095"), "BD": D("42.7460397"), "CASH": D("0.41")}
    investable = sum(values.values(), D(0))
    book = Book(values, investable=investable, taxes={"EQ": D("0.2")})
    weights = strategy.targets(View({"EQ": 0.4}), book)
    assert weights is not None
    assert sum(weights.values(), D(0)) <= 1
    # EQ keeps its weight: rounded down to the quantum, a residue below its value.
    assert D(0) <= values["EQ"] - weights["EQ"] * investable < D("1e-15")


def test_a_costly_trend_reduction_is_held_and_alone_trades_nothing():
    strategy = core(SIXTY, tilt=True, tax=True)
    # EQ wants to fall from 60 to 15: tax of 0.10 per unit sold is above the 5 % benefit.
    book = Book({"EQ": D(60), "BD": D(40)}, taxes={"EQ": D("0.10")})
    assert strategy.targets(View({"EQ": 0.25}), book) is None
    # With a band breach elsewhere the rest trades and EQ keeps its weight.
    drifted = Book({"EQ": D(60), "BD": D(30), "CASH": D(10)}, taxes={"EQ": D("0.10")})
    assert strategy.targets(View({"EQ": 0.25}), drifted) == {"EQ": D("0.6"), "BD": D("0.4")}


def test_a_cheap_reduction_or_a_loss_is_made():
    strategy = core(SIXTY, tilt=True, tax=True)
    cheap = Book({"EQ": D(60), "BD": D(40)}, taxes={"EQ": D("0.04")})
    weights = strategy.targets(View({"EQ": 0.25}), cheap)
    assert weights == {"EQ": D("0.15"), "BD": D("0.4"), "CASH": D("0.45")}
    loss = Book({"EQ": D(60), "BD": D(40)}, taxes={"EQ": D("-0.02")})
    assert strategy.targets(View({"EQ": 0.25}), loss) == weights


def test_a_held_reduction_takes_from_cash_then_from_the_roles_still_trading():
    strategy = core({"EQ": D("0.5"), "BD": D("0.5")}, tilt=True, tax=True)
    # Both fall to the floor; EQ's reduction is costly and held. Cash would receive 0.75 and
    # gives up EQ's kept 0.375: EQ 0.5, BD 0.125, CASH 0.375.
    book = Book({"EQ": D(50), "BD": D(50)}, taxes={"EQ": D("0.10")})
    weights = strategy.targets(View({"EQ": 0.25, "BD": 0.25}), book)
    assert weights == {"EQ": D("0.5"), "BD": D("0.125"), "CASH": D("0.375")}
    # Tax owed leaves 90 to invest against 100 held: holding EQ (weight 50/90) and cash giving
    # up all it has still exceeds 1, so BD, still trading, gives up the rest.
    owed = Book({"EQ": D(50), "BD": D(50)}, investable=D(90), taxes={"EQ": D("0.10")})
    weights = strategy.targets(View({"EQ": 0.25}), owed)
    assert weights is not None
    assert sum(weights.values()) == pytest.approx(D(1))
    assert weights["EQ"] == pytest.approx(D(50) / D(90))
    # With both reductions held there is nothing still trading: every role scales down.
    both = Book(
        {"EQ": D(50), "BD": D(50)}, investable=D(90), taxes={"EQ": D("0.10"), "BD": D("0.10")}
    )
    weights = strategy.targets(View({"EQ": 0.25, "BD": 0.25}), both)
    assert weights is not None
    assert sum(weights.values()) == pytest.approx(D(1))
    assert weights["EQ"] == pytest.approx(weights["BD"])


def test_a_purchase_that_would_defer_a_loss_goes_to_the_swap_partner():
    strategy = core(SIXTY, tilt=True, tax=True)
    # Cash held, EQ back at full scale: the buy of EQ would defer the loss of its sale.
    book = Book({"BD": D(40), "CASH": D(60)}, defers={"EQ"})
    assert strategy.targets(View(), book) == {"EQ_SWAP": D("0.6"), "BD": D("0.4")}
    # A vehicle already held is bought first, unless it would defer a loss.
    held = Book({"EQ_SWAP": D(10), "BD": D(40), "CASH": D(50)})
    assert strategy.targets(View(), held) == {"EQ_SWAP": D("0.6"), "BD": D("0.4")}
    # When both would defer, the first in order takes it.
    both = Book({"BD": D(40), "CASH": D(60)}, defers={"EQ", "EQ_SWAP"})
    assert strategy.targets(View(), both) == {"EQ": D("0.6"), "BD": D("0.4")}
    # Without tax-aware execution the primary is bought.
    plain = core(SIXTY, tilt=True)
    assert plain.targets(View(), book) == {"EQ": D("0.6"), "BD": D("0.4")}


def test_a_sale_comes_first_from_the_vehicle_with_the_smaller_gain():
    strategy = core(SIXTY, tilt=True, tax=True)
    # EQ is held in both vehicles; the partner carries a loss (basis above value).
    book = Book(
        {"EQ": D(40), "EQ_SWAP": D(30), "BD": D(30)},
        bases={"EQ": D(20), "EQ_SWAP": D(35)},
    )
    weights = strategy.targets(View(), book)
    assert weights == {"EQ": D("0.4"), "EQ_SWAP": D("0.2"), "BD": D("0.4")}


def test_without_anything_to_invest_the_targets_go_to_the_primaries():
    strategy = core(SIXTY)
    assert strategy.targets(View(), Book({}, investable=D(0))) == SIXTY


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"weights": {"XX": D(1)}}, "without vehicles"),
        ({"weights": {"EQ": D("0.7"), "BD": D("0.4")}}, "sum to 1 at most"),
        ({"cash_role": None}, "without a cash role"),
        ({"cash_role": "XX"}, "cash role without vehicles"),
        ({"tilt": None}, "belongs to the tilt"),
    ],
)
def test_an_incoherent_core_strategy_is_refused(fields, message):
    values = {
        "name": "core",
        "weights": SIXTY,
        "vehicles": VEHICLES,
        "bands": Bands(D("0.05"), D("0.25")),
        "cash_role": "CASH",
        "tilt": RULE,
        "tax_aware": TaxAware(D("0.05")),
    }
    with pytest.raises(ValueError, match=message):
        CoreStrategy(**{**values, **fields})


# ── the builders ──


def test_environment_budgets_split_each_environment_among_its_roles():
    shares = environment_budgets(
        {"up": ["EQ", "CM"], "down": ["BD", "CM"]}, {"up": D("0.6"), "down": D("0.4")}
    )
    assert shares == {"EQ": D("0.3"), "CM": D("0.5"), "BD": D("0.2")}


@pytest.fixture
def toy(tmp_path: Path):
    path = tmp_path / "core_toy.md"
    path.write_text(CORE_TOY, encoding="utf-8")
    return load_prereg(path)


def test_every_kind_of_strategy_builds_from_the_core_toy(toy, shipped_settings: ObliskConfig):
    study = toy.spec.study
    built = {spec.name: build_strategy(spec, study, shipped_settings) for spec in study.portfolios}
    assert isinstance(built["equity"], BuyAndHold)
    assert isinstance(built["fixed"], FixedWeights)
    sixty = built["60/40"]
    assert isinstance(sixty, CoreStrategy)
    assert (sixty.tilt, sixty.tax_aware, sixty.vehicles["EQ"].partner) == (None, None, None)
    overlay = built["overlay"]
    assert isinstance(overlay, CoreStrategy)
    assert overlay.weights == {"EQ": D(1), "CASH": D(0)}
    assert overlay.tax_aware == TaxAware(D("0.05"))
    assert overlay.vehicles["EQ"] == RoleVehicles("EQ", "EQ_SWAP")
    balance = built["balance"]
    assert isinstance(balance, CoreStrategy)
    # Two uncorrelated roles at equal risk: inverse volatility, 5 / 21 and 16 / 21.
    assert balance.weights["EQ"] == pytest.approx(D(5) / D(21), abs=D("0.000001"))
    assert sum(balance.weights.values()) == D(1)


def test_a_risk_budget_that_does_not_converge_is_refused(toy, shipped_settings: ObliskConfig):
    numerics = shipped_settings.numerics.model_copy(
        update={
            "risk_budget": shipped_settings.numerics.risk_budget.model_copy(
                update={"max_iterations": 1}
            )
        }
    )
    settings = shipped_settings.model_copy(update={"numerics": numerics})
    study = toy.spec.study
    balance = next(spec for spec in study.candidates if spec.name == "balance")
    assert study.core is not None
    # Two uncorrelated assets converge in one sweep; a correlated three-asset core does not.
    correlated = study.core.model_copy(
        update={
            "risk": study.core.risk.model_copy(update={"correlations": [("EQ", "BD", D("0.5"))]})
        }
    )
    with pytest.raises(PreregistrationError, match="did not converge"):
        static_weights(balance, correlated, settings)


def test_a_held_reduction_larger_than_what_trades_takes_every_free_role_to_zero():
    strategy = core({"EQ": D("0.9"), "BD": D("0.1")}, tilt=True, tax=True)
    # Tax owed leaves 80 to invest against 100 held. EQ's costly reduction is held at 90/80;
    # cash gives up all it would get and BD, the one role still trading, all it has: what is
    # held is then scaled to the investable value.
    book = Book({"EQ": D(90), "BD": D(10)}, investable=D(80), taxes={"EQ": D("0.10")})
    weights = strategy.targets(View({"EQ": 0.25}), book)
    assert weights == {"EQ": D(1)}
