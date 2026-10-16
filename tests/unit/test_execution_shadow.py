"""Shadow fills: priced with the cost model, filled only inside the limit, never invented.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from order_rig import quote, terms

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import OrderSide, QuoteEvidence
from obliskiq.execution.shadow import ShadowFiller
from obliskiq.quant.costs import CostBook

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
MID_100 = quote(bid="99.9", ask="100.1", at=NOW)
LIQUID = {"market_cap_usd": Decimal("3000000000000"), "issuer_country": "US"}


@pytest.fixture
def filler(shipped_settings: ObliskConfig) -> ShadowFiller:
    return ShadowFiller(CostBook.from_config(shipped_settings))


def _simulate(
    filler: ShadowFiller, order: Any, evidence: QuoteEvidence = MID_100, **kw: Any
) -> Any:
    fields: dict[str, Any] = {
        "coid": "c1",
        "asset_class": AssetClass.STOCK,
        "venue": "XNYS",
        "currency": "USD",
        "at": NOW,
        **LIQUID,
    }
    fields.update(kw)
    return filler.simulate(order, evidence, **fields)


def test_a_buy_fills_at_the_reference_plus_spread_and_slippage(filler: ShadowFiller) -> None:
    outcome = _simulate(filler, terms(limit="100.5"))
    assert outcome.fill is not None, outcome.reason
    fill = outcome.fill
    assert fill.simulated
    assert fill.side is OrderSide.BUY
    assert fill.quantity == Decimal(10)
    assert fill.price == Decimal("100.2")  # 0.1 % spread + 0.1 % slippage on 1,000
    assert fill.commission == outcome.cost.commission.amount
    assert fill.execution_id.startswith("shadow-c1-")


def test_a_fill_outside_the_limit_rests(filler: ShadowFiller) -> None:
    outcome = _simulate(filler, terms(limit="100.1"))
    assert outcome.fill is None
    assert outcome.cost is not None
    assert "outside the limit" in outcome.reason


def test_a_sell_fills_below_the_reference(filler: ShadowFiller) -> None:
    outcome = _simulate(filler, terms(side=OrderSide.SELL, limit="99.5"))
    assert outcome.fill is not None
    assert outcome.fill.price == Decimal("99.8")
    assert _simulate(filler, terms(side=OrderSide.SELL, limit="99.9")).fill is None


def test_a_sell_stop_limit_waits_for_its_stop(filler: ShadowFiller) -> None:
    stop = terms(side=OrderSide.SELL, stop="95", limit="93")
    assert "not triggered" in _simulate(filler, stop).reason
    falling = quote(bid="93.9", ask="94.1", at=NOW)
    outcome = _simulate(filler, stop, falling)
    assert outcome.fill is not None
    assert outcome.fill.price < Decimal(94)
    assert "buy stop-limit" in _simulate(filler, terms(stop="101", limit="102")).reason


def test_money_sizing_rounds_down_to_whole_units(filler: ShadowFiller) -> None:
    outcome = _simulate(filler, terms(cash="1050", limit="100.5"))
    assert outcome.fill is not None
    assert outcome.fill.quantity == Decimal(10)
    assert "less than one unit" in _simulate(filler, terms(cash="50")).reason


def test_nothing_is_invented(filler: ShadowFiller) -> None:
    bare = QuoteEvidence(
        conid=265598, status=MarketDataStatus.REALTIME, delay_seconds=0, observed_at=NOW
    )
    assert "no reference price" in _simulate(filler, terms(), bare).reason
    assert "conversions" in _simulate(filler, terms(cash="100", conversion=True)).reason
    gap = _simulate(filler, terms(limit="100.5"), venue="XTKS", currency="JPY")
    assert gap.fill is None
    assert "cannot price" in gap.reason


def test_an_option_is_costed_per_contract(filler: ShadowFiller) -> None:
    premium = quote(bid="2.00", ask="2.10", at=NOW)
    outcome = _simulate(
        filler,
        terms(quantity="1", limit="2.5"),
        premium,
        asset_class=AssetClass.OPTION,
        multiplier=Decimal(100),
    )
    assert outcome.fill is not None, outcome.reason
    assert Decimal("2.05") < outcome.fill.price <= Decimal("2.5")
