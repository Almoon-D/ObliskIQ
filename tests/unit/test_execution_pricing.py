"""Pricing: an algo with a limit cap where enabled and offered, else a patient limit repriced
toward the touch, never past the collar; the cost model is given the listing venue.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from order_rig import INSTRUMENTS, STOCK

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import MarketDataStatus
from obliskiq.domain.orders import AlgoLimitPrice, LimitPrice, OrderSide, QuoteEvidence
from obliskiq.execution.pricing import Pricer, cost_venue, tick_for, to_tick
from obliskiq.ibkr.models import Algo, ContractRules, IncrementRule

AT = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)


def rules(*steps: tuple[float, float]) -> ContractRules:
    return ContractRules(
        conid=STOCK,
        is_buy=True,
        order_types=("limit", "stop_limit"),
        order_types_outside_rth=(),
        cash_quantity_order_types=(),
        tif_types=("DAY", "GTC"),
        algo_eligible=True,
        size_increment=1.0,
        increment_rules=tuple(IncrementRule(lower_edge=e, increment=i) for e, i in steps),
        price_magnifier=None,
        cash_currency=None,
        error=None,
    )


CENTS = rules((0.0, 0.01))
ADAPTIVE = (Algo(algo_id="Adaptive", name="Adaptive", parameters=()),)


def quote(
    bid: str = "99.90",
    ask: str = "100.10",
    *,
    status: MarketDataStatus = MarketDataStatus.REALTIME,
    delay: int | None = 0,
) -> QuoteEvidence:
    return QuoteEvidence(
        conid=STOCK,
        status=status,
        delay_seconds=delay,
        observed_at=AT,
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


def with_algos(settings: ObliskConfig, *, enabled: bool) -> ObliskConfig:
    pricing = settings.execution.pricing
    algos = [rule.model_copy(update={"enabled": enabled}) for rule in pricing.algos]
    execution = settings.execution.model_copy(
        update={"pricing": pricing.model_copy(update={"algos": algos})}
    )
    return settings.model_copy(update={"execution": execution})


@pytest.fixture
def pricer(shipped_settings: ObliskConfig) -> Pricer:
    return Pricer(shipped_settings)


def test_every_shipped_algo_is_off_until_verified(shipped_settings: ObliskConfig) -> None:
    assert shipped_settings.execution.pricing.algos
    assert not any(rule.enabled for rule in shipped_settings.execution.pricing.algos)


def test_an_algo_not_offered_falls_back_to_a_patient_limit(shipped_settings: ObliskConfig) -> None:
    pricer = Pricer(with_algos(shipped_settings, enabled=True))
    stock = INSTRUMENTS[STOCK]
    priced = pricer.initial(
        stock, OrderSide.BUY, quote(), sigma=None, rules=CENTS, algos=(), venue="XNYS"
    )
    assert priced.price == LimitPrice(limit=Decimal("99.99"))  # one tick inside the mid
    assert "patient limit" in priced.detail


def test_an_enabled_algo_the_contract_offers_is_capped_by_the_collar(
    shipped_settings: ObliskConfig,
) -> None:
    pricer = Pricer(with_algos(shipped_settings, enabled=True))
    stock = INSTRUMENTS[STOCK]
    buy = pricer.initial(
        stock, OrderSide.BUY, quote(), sigma=None, rules=CENTS, algos=ADAPTIVE, venue="XNYS"
    )
    assert isinstance(buy.price, AlgoLimitPrice)
    assert buy.price.strategy == "Adaptive"
    assert buy.price.limit == Decimal("100.50")  # 0.5 % above the mid, rounded down
    assert [(p.name, p.value) for p in buy.price.parameters] == [("adaptivePriority", "Patient")]
    sell = pricer.initial(
        stock, OrderSide.SELL, quote(), sigma=None, rules=CENTS, algos=ADAPTIVE, venue="XNYS"
    )
    assert isinstance(sell.price, AlgoLimitPrice)
    assert sell.price.limit == Decimal("99.50")  # 0.5 % below, rounded up
    # The same algo on a venue no rule names: a patient limit.
    elsewhere = pricer.initial(
        stock, OrderSide.BUY, quote(), sigma=None, rules=CENTS, algos=ADAPTIVE, venue="XETR"
    )
    assert isinstance(elsewhere.price, LimitPrice)


def test_an_algo_off_in_configuration_is_never_used(pricer: Pricer) -> None:
    priced = pricer.initial(
        INSTRUMENTS[STOCK],
        OrderSide.BUY,
        quote(),
        sigma=None,
        rules=CENTS,
        algos=ADAPTIVE,
        venue="XNYS",
    )
    assert isinstance(priced.price, LimitPrice)


def test_a_patient_sell_starts_above_the_mid_on_a_whole_tick(pricer: Pricer) -> None:
    priced = pricer.initial(
        INSTRUMENTS[STOCK],
        OrderSide.SELL,
        quote("99.905", "100.105"),
        sigma=None,
        rules=CENTS,
        algos=(),
        venue="XNYS",
    )
    assert priced.price == LimitPrice(limit=Decimal("100.02"))  # 100.005 + 0.01, rounded up


def test_no_price_without_a_tick_a_reference_or_a_volatility_for_a_delayed_quote(
    pricer: Pricer,
) -> None:
    stock = INSTRUMENTS[STOCK]
    no_tick = pricer.initial(
        stock, OrderSide.BUY, quote(), sigma=None, rules=rules((500.0, 0.01)), algos=(), venue="X"
    )
    assert no_tick.price is None
    assert "no tick" in no_tick.detail
    delayed = quote(status=MarketDataStatus.DELAYED, delay=900)
    blind = pricer.initial(
        stock, OrderSide.BUY, delayed, sigma=None, rules=CENTS, algos=(), venue="X"
    )
    assert blind.price is None
    assert "volatility" in blind.detail
    penny = pricer.initial(
        stock, OrderSide.BUY, quote("0.01", "0.01"), sigma=None, rules=CENTS, algos=(), venue="X"
    )
    assert penny.price is None
    assert "not a price" in penny.detail


def test_the_reprices_climb_to_the_touch_in_equal_shares(pricer: Pricer) -> None:
    assert pricer.max_reprices == 3
    limit = Decimal("99.97")
    seen = []
    for done in range(3):
        step = pricer.reprice(
            OrderSide.BUY, limit, quote("99.90", "100.06"), sigma=None, rules=CENTS, done=done
        )
        assert isinstance(step.price, LimitPrice), step.detail
        limit = step.price.limit
        seen.append(limit)
    assert seen == [Decimal("100.00"), Decimal("100.03"), Decimal("100.06")]
    spent = pricer.reprice(OrderSide.BUY, limit, quote(), sigma=None, rules=CENTS, done=3)
    assert spent.price is None
    assert "spent" in spent.detail


def test_a_sell_reprices_down_toward_the_bid(pricer: Pricer) -> None:
    step = pricer.reprice(
        OrderSide.SELL, Decimal("100.10"), quote("99.94", "100.20"), sigma=None, rules=CENTS, done=2
    )
    assert step.price == LimitPrice(limit=Decimal("99.94"))  # the last reprice reaches the bid


def test_a_reprice_never_passes_the_collar(pricer: Pricer) -> None:
    # The ask ran away: the reprice stops at the collar's edge, 0.5 % above the new mid.
    step = pricer.reprice(
        OrderSide.BUY, Decimal("99"), quote("100", "102"), sigma=None, rules=CENTS, done=2
    )
    assert step.price == LimitPrice(limit=Decimal("101.50"))
    sell = pricer.reprice(
        OrderSide.SELL, Decimal("101"), quote("98", "100"), sigma=None, rules=CENTS, done=2
    )
    assert sell.price == LimitPrice(limit=Decimal("98.51"))


def test_a_delayed_quote_widens_how_far_a_reprice_may_reach(pricer: Pricer) -> None:
    delayed = quote("100", "102", status=MarketDataStatus.DELAYED, delay=900)
    step = pricer.reprice(OrderSide.BUY, Decimal("99"), delayed, sigma=0.4, rules=CENTS, done=2)
    assert isinstance(step.price, LimitPrice)
    assert step.price.limit > Decimal("101.50")
    assert step.price.limit <= Decimal("102")
    blind = pricer.reprice(OrderSide.BUY, Decimal("99"), delayed, sigma=None, rules=CENTS, done=0)
    assert blind.price is None


def test_a_reprice_that_would_not_move_toward_the_touch_is_none(pricer: Pricer) -> None:
    fell = pricer.reprice(
        OrderSide.BUY, Decimal("100.10"), quote("99.80", "99.90"), sigma=None, rules=CENTS, done=0
    )
    assert fell.price is None
    assert "no better limit" in fell.detail
    no_tick = pricer.reprice(
        OrderSide.BUY, Decimal("99"), quote(), sigma=None, rules=rules((500.0, 0.01)), done=0
    )
    assert no_tick.price is None


def test_ticks_round_both_ways_and_follow_the_graded_rules() -> None:
    graded = rules((0.0, 0.001), (1.0, 0.01))
    assert tick_for(graded, Decimal("0.5")) == Decimal("0.001")
    assert tick_for(graded, Decimal("12")) == Decimal("0.01")
    assert to_tick(Decimal("12.345"), Decimal("0.01"), up=True) == Decimal("12.35")
    assert to_tick(Decimal("12.345"), Decimal("0.01"), up=False) == Decimal("12.34")


def test_the_cost_model_is_given_the_listing_venue_never_smart(
    build_calendars: object,
) -> None:
    calendars = build_calendars(("XNYS", "XETR"))  # type: ignore[operator]
    assert isinstance(calendars, Calendars)
    assert cost_venue(INSTRUMENTS[STOCK], calendars) == "XNYS"
