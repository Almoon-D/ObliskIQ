"""The quote policy per asset class and the collar, as Session 4a's pre-trade check will use them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from order_rig import quote, terms

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import OrderSide
from obliskiq.execution.quotes import QuotePolicy

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
REALTIME = quote(at=NOW)
DELAYED = quote(status=MarketDataStatus.DELAYED, delay=1200, at=NOW)


@pytest.fixture
def quotes(shipped_settings: ObliskConfig) -> QuotePolicy:
    return QuotePolicy(shipped_settings)


def test_an_option_needs_real_time_quotes_on_itself_and_its_underlying(quotes: QuotePolicy) -> None:
    option = AssetClass.OPTION
    assert quotes.check(option, REALTIME, now=NOW, underlying=REALTIME).ok
    assert not quotes.check(option, DELAYED, now=NOW, underlying=REALTIME).ok
    assert not quotes.check(option, REALTIME, now=NOW, underlying=DELAYED).ok
    assert not quotes.check(option, REALTIME, now=NOW, underlying=None).ok
    stale_underlying = quote(at=NOW - timedelta(seconds=61))
    assert not quotes.check(option, REALTIME, now=NOW, underlying=stale_underlying).ok


def test_a_stock_takes_a_real_time_or_a_fresh_delayed_quote(quotes: QuotePolicy) -> None:
    stock = AssetClass.STOCK
    assert quotes.check(stock, REALTIME, now=NOW).ok
    assert quotes.check(stock, DELAYED, now=NOW).ok
    assert not quotes.check(stock, REALTIME, now=NOW, require_delayed=True).ok
    late = quote(status=MarketDataStatus.DELAYED, delay=1201, at=NOW)
    assert not quotes.check(stock, late, now=NOW).ok
    assert not quotes.check(stock, quote(at=NOW - timedelta(seconds=61)), now=NOW).ok
    assert not quotes.check(stock, None, now=NOW).ok


def test_funds_and_unlisted_classes_have_no_quote_policy(quotes: QuotePolicy) -> None:
    assert "NAV" in quotes.check(AssetClass.FUND, REALTIME, now=NOW).reason
    assert not quotes.check(AssetClass.FUTURE, REALTIME, now=NOW).ok
    assert quotes.rule_for(AssetClass.INDEX) is None


def test_the_collar_is_the_base_for_a_real_time_quote_and_widens_with_the_delay(
    quotes: QuotePolicy, shipped_settings: ObliskConfig
) -> None:
    collar = shipped_settings.execution.collar
    assert quotes.collar_fraction(sigma=None, delay_seconds=0) == Decimal(
        repr(collar.base_fraction)
    )
    year = collar.sessions_per_year * collar.session_hours * 3600
    expected = collar.base_fraction + collar.sigma_multiplier * 0.3 * math.sqrt(1200 / year)
    widened = quotes.collar_fraction(sigma=0.3, delay_seconds=1200)
    assert widened is not None
    assert float(widened) == pytest.approx(expected)
    assert widened > quotes.collar_fraction(sigma=0.3, delay_seconds=60)  # type: ignore[operator]
    assert quotes.collar_fraction(sigma=None, delay_seconds=1200) is None


def test_the_collar_bounds_how_far_a_limit_reaches_through_the_reference(
    quotes: QuotePolicy,
) -> None:
    # Reference mid 100, real-time: the collar is 0.5 %.
    assert quotes.within_collar(terms(limit="100.5"), REALTIME, sigma=None).ok
    assert not quotes.within_collar(terms(limit="100.6"), REALTIME, sigma=None).ok
    assert quotes.within_collar(terms(limit="50"), REALTIME, sigma=None).ok  # passive
    sell = OrderSide.SELL
    assert quotes.within_collar(terms(side=sell, limit="99.5"), REALTIME, sigma=None).ok
    assert not quotes.within_collar(terms(side=sell, limit="99.4"), REALTIME, sigma=None).ok
    # A delayed quote widens it.
    assert quotes.within_collar(terms(limit="101"), DELAYED, sigma=0.3).ok


def test_the_collar_for_stop_limits(quotes: QuotePolicy) -> None:
    sell = OrderSide.SELL
    assert quotes.within_collar(terms(side=sell, stop="99.5", limit="99"), REALTIME, sigma=None).ok
    assert not quotes.within_collar(
        terms(side=sell, stop="99.6", limit="99"), REALTIME, sigma=None
    ).ok
    assert not quotes.within_collar(terms(stop="101", limit="102"), REALTIME, sigma=None).ok


def test_the_collar_refuses_what_it_cannot_measure(quotes: QuotePolicy) -> None:
    unknown_delay = quote(status=MarketDataStatus.DELAYED, delay=None, at=NOW)
    assert not quotes.within_collar(terms(), unknown_delay, sigma=0.3).ok
    assert not quotes.within_collar(terms(), DELAYED, sigma=None).ok
    crossed = quote(bid="101", ask="100", at=NOW)  # crossed: no mid; no last either
    no_last = crossed.model_copy(update={"last": None})
    assert not quotes.within_collar(terms(), no_last, sigma=None).ok
    with_last = crossed.model_copy(update={"last": Decimal("100")})
    assert quotes.within_collar(terms(limit="100.5"), with_last, sigma=None).ok


def test_a_real_time_requirement_for_another_class(shipped_settings: ObliskConfig) -> None:
    from obliskiq.config.schema_execution import NavPriced, RealtimeRequired

    execution = shipped_settings.execution.model_copy(
        update={
            "quote_policy": [
                RealtimeRequired(
                    policy="realtime_required",
                    asset_classes=[AssetClass.STOCK],
                    max_quote_age_seconds=60,
                ),
                NavPriced(policy="nav_priced", asset_classes=[AssetClass.FUND]),
            ]
        }
    )
    strict = QuotePolicy(shipped_settings.model_copy(update={"execution": execution}))
    assert strict.check(AssetClass.STOCK, REALTIME, now=NOW).ok
    assert not strict.check(AssetClass.STOCK, DELAYED, now=NOW).ok
