"""The core engine: every registered variant forms a target portfolio for a small and a large
account; the tilt, the bands and the tax rule move it as the pre-registration says; and what it
cannot form it says so, trading nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal

import pytest
from toy_book import ToyBook, closes

from obliskiq.config import ObliskConfig
from obliskiq.domain.portfolio import RoleStatus, TargetStatus
from obliskiq.engines.core.engine import CoreEngine
from obliskiq.engines.core.mechanism import RoleVehicles
from obliskiq.engines.core.view import MonthEnds
from obliskiq.errors import CoreError

D = Decimal
TODAY = date(2026, 10, 26)
MONTH_END = date(2026, 9, 30)
PRICE = D(100)
FLOOR = D("0.25")


@pytest.fixture(scope="module")
def engine(shipped_settings: ObliskConfig) -> CoreEngine:
    return CoreEngine(shipped_settings)


@pytest.fixture(scope="module")
def roles(shipped_settings: ObliskConfig) -> list[str]:
    return list(shipped_settings.core.roles)


def fill(roles: list[str], *, partners: bool = True, skip: tuple[str, ...] = ()) -> dict:
    """A vehicle for every role but ``skip``: key ``k`` for the k-th, ``k + 10`` its partner."""
    return {
        role: RoleVehicles(str(k), str(k + 10) if partners else None)
        for k, role in enumerate(roles, start=1)
        if role not in skip
    }


def book(investable: str, units: dict[str, str] | None = None, **fields: object) -> ToyBook:
    prices = {str(k): PRICE for k in (*range(1, 30), 99)}
    held = {key: D(count) for key, count in (units or {}).items()}
    return ToyBook(held, prices, D(investable), **fields)  # type: ignore[arg-type]


def view(vehicles: dict, monthly: float, cash_monthly: float = 0.001) -> MonthEnds:
    """Month-end closes of every vehicle growing by ``monthly``, the cash role's by its own."""
    cash = vehicles["cash"].primary
    series = {
        key: closes(MONTH_END, 24, cash_monthly if key == cash else monthly)
        for held in vehicles.values()
        for key in held.keys
    }
    return MonthEnds(MONTH_END, series)


# ── every registered variant ──


@pytest.mark.parametrize("investable", ["1000", "1000000"])
def test_every_registered_variant_forms_a_target_for_a_small_and_a_large_account(
    engine: CoreEngine, roles: list[str], investable: str
) -> None:
    vehicles = fill(roles)
    assert set(engine.variants.names) == {
        "equity_reference", "60/40", "A", "B", "C", "D10", "D20", "D30", "D40", "D50",
    }  # fmt: skip
    for name in engine.variants.names:
        account = book(investable)
        target = engine.portfolio(
            vehicles=vehicles,
            book=account,
            view=view(vehicles, 0.01),
            as_of=TODAY,
            variant=name,
        )
        assert target.variant == name
        assert target.status is TargetStatus.READY
        assert target.rebalance  # nothing is held yet: the first purchase
        total = sum((v.weight for v in target.vehicles), D(0))
        assert D("0.999") < total <= 1
        assert all(v.weight > 0 for v in target.vehicles)
        deltas = engine.deltas(target, account, band=D("0.005"))
        assert not deltas.sells
        spent = sum((PRICE * units for _, units in deltas.buys), D(0))
        assert spent <= D(investable)
        # Whole units: a thousand euros buys at most ten units of a hundred.
        assert all(units == units.to_integral_value() for _, units in deltas.buys)
        if investable == "1000000":
            assert spent > D(investable) * D("0.99")


def test_the_chosen_variant_is_the_default(engine: CoreEngine, roles: list[str]) -> None:
    vehicles = fill(roles)
    target = engine.portfolio(vehicles=vehicles, book=book("1000"), view=None, as_of=TODAY)
    assert target.variant == engine.variants.chosen == "A"
    assert target.trend_as_of is None


def test_variant_a_holds_the_risk_balance_of_the_pre_registration(
    engine: CoreEngine, roles: list[str]
) -> None:
    """The prose: about 9 % world, 7 % EM, 11 % commodities, 34 % intermediate euro bonds, 11 %
    long, 11 % Treasuries, 12 % inflation-linked and 5 % gold."""
    weights = engine.variants.get("A").weights
    assert sum(weights.values()) == 1
    expected = {
        "world_equity": "0.09", "em_equity": "0.07", "broad_commodities": "0.11",
        "euro_govt_intermediate": "0.34", "euro_govt_long": "0.11", "us_treasuries": "0.11",
        "inflation_linked": "0.12", "gold": "0.05",
    }  # fmt: skip
    for role, share in expected.items():
        assert weights[role] == pytest.approx(D(share), abs=D("0.01")), role
    assert "cash" not in weights


# ── the tilt ──


def test_a_downtrend_cuts_each_risky_role_to_the_floor_and_cash_takes_the_rest(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    target = engine.portfolio(
        vehicles=vehicles,
        book=book("1000000"),
        view=view(vehicles, -0.03),
        as_of=TODAY,
        variant="B",
    )
    by_role = {r.role: r for r in target.roles}
    static = engine.variants.get("B").weights
    for role, weight in static.items():
        assert by_role[role].scale == FLOOR
        assert by_role[role].weight == weight * FLOOR
        assert by_role[role].trend is not None
        assert not by_role[role].trend.momentum_positive
    cash = by_role["cash"]
    assert cash.weight == sum(static.values()) * (1 - FLOOR)
    assert target.trend_as_of == MONTH_END
    assert "momentum negative" in by_role["world_equity"].reason


def test_an_uptrend_leaves_the_static_weights(engine: CoreEngine, roles: list[str]) -> None:
    vehicles = fill(roles)
    target = engine.portfolio(
        vehicles=vehicles,
        book=book("1000000"),
        view=view(vehicles, 0.02),
        as_of=TODAY,
        variant="B",
    )
    static = engine.variants.get("B").weights
    assert {r.role: r.weight for r in target.roles if r.role in static} == dict(static)
    assert all(r.scale == 1 for r in target.roles)


def test_a_tilting_variant_without_closes_is_not_evaluable(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    target = engine.portfolio(
        vehicles=vehicles, book=book("1000"), view=None, as_of=TODAY, variant="B"
    )
    assert target.status is TargetStatus.NOT_EVALUABLE
    assert not target.rebalance
    assert not target.vehicles
    assert "no month-end closes were read" in target.reasons[0]


def test_a_role_short_of_history_says_why_and_trades_nothing(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    full = view(vehicles, 0.01)
    short = MonthEnds(
        MONTH_END,
        {**full.closes, "1": closes(MONTH_END, 6, 0.01)},
        {"1": "X (1) has no month-end close on 2026-03-31 within the allowed age"},
    )
    target = engine.portfolio(
        vehicles=vehicles, book=book("1000"), view=short, as_of=TODAY, variant="B"
    )
    assert target.status is TargetStatus.NOT_EVALUABLE
    assert "world_equity lacks the 13 month ends the tilt needs" in target.reasons[0]
    assert target.reasons[1].startswith("X (1) has no month-end close")


# ── the bands ──


def test_inside_the_outer_bands_the_target_is_kept_for_new_cash(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    weights = engine.variants.get("A").weights
    units = {vehicles[role].primary: str(int(w * 10000)) for role, w in weights.items()}
    account = book("1000000", units)
    target = engine.portfolio(vehicles=vehicles, book=account, view=None, as_of=TODAY)
    assert not target.rebalance
    assert target.vehicles  # where new cash goes
    assert not engine.deltas(target, account, band=D("0.005")).sells


def test_a_role_beyond_its_outer_band_sends_every_role_back_to_target(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    weights = engine.variants.get("A").weights
    units = {vehicles[role].primary: str(int(w * 10000)) for role, w in weights.items()}
    units["1"] = str(int(units["1"]) + 2000)  # world equity 20 points over
    account = book("1000000", units)
    target = engine.portfolio(vehicles=vehicles, book=account, view=None, as_of=TODAY)
    assert target.rebalance
    sells = dict(engine.deltas(target, account, band=D("0.005")).sells)
    assert "1" in sells


def test_a_holding_no_role_names_is_sold(engine: CoreEngine, roles: list[str]) -> None:
    vehicles = fill(roles)
    account = book("1000000", {"99": "100"})
    target = engine.portfolio(vehicles=vehicles, book=account, view=None, as_of=TODAY)
    assert target.rebalance
    assert any("99 is held and no role names it" in reason for reason in target.reasons)
    assert dict(engine.deltas(target, account, band=D("0.005")).sells)["99"] == 100


# ── what it cannot fill ──


def test_a_role_without_a_vehicle_is_held_in_cash(engine: CoreEngine, roles: list[str]) -> None:
    vehicles = fill(roles, skip=("gold", "broad_commodities"))
    target = engine.portfolio(vehicles=vehicles, book=book("1000000"), view=None, as_of=TODAY)
    assert target.status is TargetStatus.READY
    by_role = {r.role: r for r in target.roles}
    static = engine.variants.get("A").weights
    for role in ("gold", "broad_commodities"):
        assert by_role[role].status is RoleStatus.UNFILLED
        assert by_role[role].weight == 0
        assert by_role[role].vehicle is None
        assert "held in the cash role" in by_role[role].reason
    assert by_role["cash"].weight == static["gold"] + static["broad_commodities"]
    assert any("gold has no vehicle" in reason for reason in target.reasons)


def test_a_book_that_needs_cash_and_has_no_cash_vehicle_is_not_evaluable(
    engine: CoreEngine, roles: list[str]
) -> None:
    target = engine.portfolio(
        vehicles=fill(roles, skip=("gold", "cash")), book=book("1000"), view=None, as_of=TODAY
    )
    assert target.status is TargetStatus.NOT_EVALUABLE
    assert target.reasons == ("the cash role cash has no vehicle, to hold the weight of gold",)


def test_a_variant_that_is_not_registered_is_refused(engine: CoreEngine, roles: list[str]) -> None:
    with pytest.raises(CoreError, match="core variant Z is not registered"):
        engine.portfolio(
            vehicles=fill(roles), book=book("1000"), view=None, as_of=TODAY, variant="Z"
        )


# ── tax-aware execution ──


def test_a_reduction_whose_tax_exceeds_its_benefit_is_held(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    # D50 holds world equity whole; a downtrend asks for 25 % of it. The account holds it all
    # and selling would cost 100 a unit in tax, far above 5 % of the 100 a unit sold.
    account = book("1000000", {"1": "10000"}, taxes={"1": D(100)})
    target = engine.portfolio(
        vehicles=vehicles,
        book=account,
        view=view(vehicles, -0.03),
        as_of=TODAY,
        variant="D50",
    )
    world = next(r for r in target.roles if r.role == "world_equity")
    assert world.reduction_held
    assert world.weight == 1
    assert not target.rebalance  # a held reduction alone trades nothing
    assert "the reduction is held" in world.reason


def test_a_reduction_that_costs_little_tax_is_made(engine: CoreEngine, roles: list[str]) -> None:
    vehicles = fill(roles)
    account = book("1000000", {"1": "10000"}, taxes={"1": D(1)})
    target = engine.portfolio(
        vehicles=vehicles,
        book=account,
        view=view(vehicles, -0.03),
        as_of=TODAY,
        variant="D50",
    )
    world = next(r for r in target.roles if r.role == "world_equity")
    assert not world.reduction_held
    assert world.weight == FLOOR
    assert target.rebalance
    assert dict(engine.deltas(target, account, band=D("0.005")).sells)["1"] == 7500


def test_a_purchase_that_would_defer_a_loss_goes_to_the_swap_partner(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    account = book("1000000", defers={"1"})
    target = engine.portfolio(
        vehicles=vehicles,
        book=account,
        view=view(vehicles, 0.01),
        as_of=TODAY,
        variant="D50",
    )
    keys = {v.key for v in target.vehicles}
    assert "11" in keys
    assert "1" not in keys


def test_a_static_variant_never_takes_a_swap_partner(engine: CoreEngine, roles: list[str]) -> None:
    vehicles = fill(roles)
    account = book("1000000", defers={"1"})
    target = engine.portfolio(vehicles=vehicles, book=account, view=None, as_of=TODAY)
    keys = {v.key for v in target.vehicles}
    assert "1" in keys
    assert not any(int(key) > 10 for key in keys)


# ── the references ──


def test_the_equity_reference_is_bought_once_and_never_sold_for(
    engine: CoreEngine, roles: list[str]
) -> None:
    vehicles = fill(roles)
    empty = engine.portfolio(
        vehicles=vehicles, book=book("1000"), view=None, as_of=TODAY, variant="equity_reference"
    )
    assert empty.rebalance
    assert [(v.key, v.weight) for v in empty.vehicles] == [("1", D(1))]
    held = engine.portfolio(
        vehicles=vehicles,
        book=book("1000", {"1": "5"}),
        view=None,
        as_of=TODAY,
        variant="equity_reference",
    )
    assert not held.rebalance
    assert [v.key for v in held.vehicles] == ["1"]  # where new cash goes


def test_a_sixty_forty_book_keeps_its_wide_bands(engine: CoreEngine, roles: list[str]) -> None:
    vehicles = fill(roles)
    inside = engine.portfolio(
        vehicles=vehicles,
        book=book("1000000", {"1": "6000", "3": "4000"}),
        view=None,
        as_of=TODAY,
        variant="60/40",
    )
    assert not inside.rebalance
    outside = engine.portfolio(
        vehicles=vehicles,
        book=book("1000000", {"1": "7000", "3": "3000"}),
        view=None,
        as_of=TODAY,
        variant="60/40",
    )
    assert outside.rebalance
