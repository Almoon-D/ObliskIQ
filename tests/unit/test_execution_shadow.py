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
from obliskiq.quant.costs import OrderTerms as CostTerms

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


def test_a_ucits_etf_on_xetra_is_priced_end_to_end_and_filled_in_shadow(
    filler: ShadowFiller, shipped_settings: ObliskConfig
) -> None:
    """DECISIONS D69: a UCITS order is priced on its listing venue's schedule, in shadow too."""
    book = CostBook.from_config(shipped_settings)
    order = terms(quantity="30", limit="101.5", venue="XETR")
    outcome = _simulate(
        filler,
        order,
        asset_class=AssetClass.ETF,
        venue="XETR",
        currency="EUR",
        market_cap_usd=None,
        issuer_country="IE",
    )
    assert outcome.fill is not None, outcome.reason
    cost = outcome.cost
    assert cost is not None
    # 30 units at 100: 0.05 % of 3,000 is 1.50, under the EUR 3.00 SmartRouting minimum.
    assert (cost.commission.schedule_id, cost.commission.amount) == ("europe_eur", Decimal("3.00"))
    # An ETF has no market capitalisation, so the illiquid tier is assumed: 0.5 % spread and
    # 0.5 % slippage, 15.00 each, one unit of price per unit traded.
    assert (cost.spread_cost, cost.slippage_cost) == (Decimal("15.000"), Decimal("15.000"))
    assert outcome.fill.price == Decimal("101.0")
    assert outcome.fill.commission == Decimal("3.00")
    assert cost.total == Decimal("33.000")
    assert "commission:europe_eur" in cost.unverified
    assert "tax_not_modelled" in cost.unverified
    # Above the minimum the rate applies: 0.05 % of 10,000.
    large = book.commission(CostTerms("XETR", AssetClass.ETF, "EUR", Decimal(100), Decimal(100)))
    assert large.amount == Decimal("5.0000")
    # The same ETF in USD on the LSE has its own minimum.
    london = book.commission(CostTerms("XLON", AssetClass.ETF, "USD", Decimal(10), Decimal(50)))
    assert (london.schedule_id, london.amount) == ("europe_usd", Decimal("4.00"))
    # A schedule on value needs the price.
    with pytest.raises(ValueError, match="needs the order's price"):
        book.commission(CostTerms("XETR", AssetClass.ETF, "EUR", Decimal(1)))


@pytest.mark.parametrize(
    ("venue", "currency", "schedule"),
    [
        ("XETR", "EUR", "europe_eur"),
        ("XLON", "GBP", "lse_gbp"),
        ("XLON", "USD", "europe_usd"),
        ("XAMS", "EUR", "europe_eur"),
        ("XAMS", "USD", "europe_usd"),
        ("XPAR", "EUR", "europe_eur"),
        ("XMIL", "EUR", "europe_eur"),
        ("XSWX", "CHF", "six_chf"),
        ("XSWX", "EUR", "europe_eur"),
        ("XSWX", "USD", "europe_usd"),
        ("XSWX", "GBP", "six_gbp"),
    ],
)
def test_every_core_venue_has_a_schedule(
    shipped_settings: ObliskConfig, venue: str, currency: str, schedule: str
) -> None:
    book = CostBook.from_config(shipped_settings)
    for asset_class in (AssetClass.STOCK, AssetClass.ETF, AssetClass.ETC):
        estimate = book.commission(
            CostTerms(venue, asset_class, currency, Decimal(1000), Decimal(100))
        )
        assert (estimate.schedule_id, estimate.amount, estimate.gap) == (
            schedule,
            Decimal("50.0000"),
            None,
        )
