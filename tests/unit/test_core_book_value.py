"""The live core book answers the engine from the account's units, prices and tax lots, and refuses
to realise a gain whose tax it cannot price; the core's latest target feeds the reservation
planner.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Any

from obliskiq.capital.strategy import DefaultStrategyValue, PositionFacts
from obliskiq.decision.core_book import UNPRICED_TAX, LiveBook
from obliskiq.decision.core_value import CoreStrategyValue, LatestTarget
from obliskiq.domain.orders import Sleeve
from obliskiq.domain.portfolio import TargetPortfolio, TargetStatus, VehicleTarget

D = Decimal
ON = date(2026, 10, 26)


class FakeHooks:
    """The lots' answers: which securities they account for, a tax per unit, a basis."""

    def __init__(
        self, known: set[int], tax_per_unit: int = 2, defers: set[int] | None = None
    ) -> None:
        self.known, self.tax_per_unit, self.defers = known, D(tax_per_unit), defers or set()

    def complete(self, conid: int, units: Decimal) -> bool:
        return conid in self.known

    def basis(self, conid: int) -> Decimal:
        return D(700)

    def trial_tax(self, sales: list[tuple[int, Decimal, Decimal]], *, on: date) -> Decimal:
        assert on == ON
        return sum((units * self.tax_per_unit for _, units, _ in sales), D(0))

    def purchase_defers_loss(self, conid: int, on: date) -> bool:
        return conid in self.defers


def book(hooks: Any = None, **units: str) -> LiveBook:
    return LiveBook(
        {key.removeprefix("k"): D(count) for key, count in units.items()},
        {"1": D(100), "2": D(50)},
        D(10000),
        hooks=hooks,
        on=ON,
    )


def test_the_book_values_what_it_holds_at_the_prices_of_the_day() -> None:
    account = book(k1="30", k2="0")
    assert dict(account.units) == {"1": D(30)}  # a zero holding is no holding
    assert account.investable == D(10000)
    assert account.price("2") == D(50)
    assert account.value("1") == D(3000)
    assert account.value("2") == D(0)
    assert account.weight("1") == D("0.3")
    assert account.units_for("1", D(250)) == D(2)
    assert LiveBook({}, {"1": D(1)}, D(0), hooks=None, on=ON).weight("1") == D(0)


def test_the_tax_of_a_sale_is_the_lots_when_they_account_for_every_unit() -> None:
    account = book(FakeHooks({1}), k1="30")
    assert account.sales_tax([("1", D(10)), ("1", D(0))]) == D(20)
    assert account.basis("1") == D(700)
    assert account.sales_tax([]) == D(0)


def test_a_sale_the_lots_cannot_price_is_priced_at_no_benefit() -> None:
    held = book(FakeHooks(set()), k1="30")  # the lots do not account for the units
    assert held.sales_tax([("1", D(10))]) == UNPRICED_TAX
    assert held.basis("1") == D(3000)  # no gain is known: the value
    none = book(None, k1="30")
    assert none.sales_tax([("1", D(10))]) == UNPRICED_TAX
    assert none.basis("1") == D(3000)
    assert none.sales_tax([("1", D(0))]) == D(0)


def test_a_purchase_defers_a_loss_only_where_the_lots_say_so() -> None:
    assert book(FakeHooks({1}, defers={1}), k1="30").purchase_defers_loss("1")
    assert not book(FakeHooks({1}), k1="30").purchase_defers_loss("1")
    assert not book(None).purchase_defers_loss("1")


# ── the latest target ──


def target(status: TargetStatus, weights: dict[str, str]) -> TargetPortfolio:
    ready = status is TargetStatus.READY
    return TargetPortfolio(
        variant="A",
        as_of=ON,
        trend_as_of=None,
        status=status,
        rebalance=False,
        roles=(),
        vehicles=tuple(VehicleTarget(key=k, role="r", weight=D(w)) for k, w in weights.items())
        if ready
        else (),
        reasons=() if ready else ("why",),
    )


def facts(conid: int, sleeve: Sleeve = Sleeve.CORE) -> PositionFacts:
    return PositionFacts(conid=conid, sleeve=sleeve, units=D(1), opened_on=None)


def test_the_latest_target_gives_the_planner_the_vehicles_weights() -> None:
    latest = LatestTarget()
    assert latest.weights() is None  # no target formed yet
    latest.portfolio = target(TargetStatus.NOT_EVALUABLE, {})
    assert latest.weights() is None
    latest.portfolio = target(TargetStatus.READY, {"11": "0.6", "12": "0.4"})
    assert latest.weights() == {11: D("0.6"), 12: D("0.4")}


def test_the_core_would_sell_a_holding_its_target_no_longer_names(
    shipped_settings: Any,
) -> None:
    latest = LatestTarget()
    value = CoreStrategyValue(latest, DefaultStrategyValue(shipped_settings.capital))
    assert not value.would_exit(facts(11), today=ON)  # before a target: it claims nothing
    latest.portfolio = target(TargetStatus.READY, {"11": "1"})
    assert not value.would_exit(facts(11), today=ON)
    assert value.would_exit(facts(99), today=ON)
    # Another sleeve's positions are the default's: no exit signal.
    assert not value.would_exit(facts(99, Sleeve.SATELLITE), today=ON)


def test_the_cores_positions_are_unprotected_and_promise_no_return(shipped_settings: Any) -> None:
    value = CoreStrategyValue(LatestTarget(), DefaultStrategyValue(shipped_settings.capital))
    assert value.protection(facts(11), today=ON) is None
    assert value.expected_return(facts(11), today=ON) is None
    opened = PositionFacts(conid=5, sleeve=Sleeve.SATELLITE, units=D(1), opened_on=ON)
    assert value.protection(opened, today=ON) is not None  # the default's entry window


def test_a_target_that_is_not_evaluable_is_refused_when_it_names_a_vehicle_or_says_nothing() -> (
    None
):
    import pytest
    from pydantic import ValidationError

    ok = target(TargetStatus.READY, {"11": "0.6", "12": "0.4"})
    assert ok.weight_of("11") == D("0.6")
    assert ok.weight_of("99") == D(0)
    base = ok.model_dump()
    with pytest.raises(ValidationError, match="no leverage"):
        TargetPortfolio.model_validate(
            {**base, "vehicles": [{"key": "1", "role": "r", "weight": "1.5"}]}
        )
    with pytest.raises(ValidationError, match="no leverage"):
        TargetPortfolio.model_validate(
            {**base, "vehicles": [{"key": "1", "role": "r", "weight": "-0.1"}]}
        )
    gone = {**base, "status": "not_evaluable", "rebalance": False, "reasons": ["why"]}
    with pytest.raises(ValidationError, match="holds no vehicle and trades nothing"):
        TargetPortfolio.model_validate(gone)
    with pytest.raises(ValidationError, match="says why"):
        TargetPortfolio.model_validate({**gone, "vehicles": [], "reasons": []})
