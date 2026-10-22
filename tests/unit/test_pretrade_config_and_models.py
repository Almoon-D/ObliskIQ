"""The configuration, domain models and decoding the pre-trade rails add: each validator's
refusal, the ledger's exchange rate, the whatif's currency and the quote mapping.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from order_rig import terms

from obliskiq.config.schema_execution import ApprovalConfig, CapsConfig
from obliskiq.config.schema_ibkr import PaperOrderConfig
from obliskiq.domain.orders import OrderSide, Sleeve, StopResize, StopResizeState
from obliskiq.execution.account import quote_read
from obliskiq.ibkr.decode import decode_ledger
from obliskiq.ibkr.models import LastKind, Quote
from obliskiq.ibkr.order_decode import decode_whatif

NOW = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)


def test_the_shipped_configuration_holds_the_rails(shipped_settings: Any) -> None:
    pretrade = shipped_settings.execution.pretrade
    assert pretrade.caps.max_order_fraction_of_nav == Decimal("0.25")
    assert pretrade.cash_floor == Decimal(25)
    assert shipped_settings.tenor.mandatory_close_trading_days == 10
    assert shipped_settings.instruments.allow_futures_options is False
    assert shipped_settings.execution.flatten.tif.value == "DAY"


def test_the_caps_and_lists_refuse_what_does_not_fit() -> None:
    with pytest.raises(ValueError, match="cannot exceed the daily cap"):
        CapsConfig(
            max_order_fraction_of_nav=Decimal("0.6"),
            max_daily_fraction_of_nav=Decimal("0.5"),
            max_orders_per_day=1,
        )
    with pytest.raises(ValueError, match="each sleeve is listed once"):
        ApprovalConfig(sleeves=[Sleeve.CORE, Sleeve.CORE])
    with pytest.raises(ValueError, match="each conid is listed once"):
        PaperOrderConfig(conids=[1, 1], limit_offset_fraction=Decimal("0.05"), intent_ttl_minutes=1)


def test_the_paper_order_conids_must_be_tradable_universe_instruments(config_dir: Any) -> None:
    from obliskiq.config import ConfigInvalid, load_config

    owner = config_dir / "owner.yaml"
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + "\nibkr:\n  preflight:\n    paper_order:\n      conids: [756733]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigInvalid, match=r"756733 \(SPY\) is not a tradable candidate"):
        load_config(config_dir)


def test_a_stop_resize_cuts_a_sell_stop() -> None:
    fields: dict[str, Any] = {
        "exit_intent_id": "01a14500-0000-7000-8000-000000000001",
        "stop_intent_id": "01a14500-0000-7000-8000-000000000002",
        "stop_order_id": "1",
        "original_terms": terms(side=OrderSide.SELL, quantity="10", stop="95", limit="94"),
        "resized_quantity": Decimal(6),
        "state": StopResizeState.RESIZED,
        "created_at": NOW,
        "updated_at": NOW,
        "detail": "cut",
    }
    assert StopResize(**fields).resized_quantity == Decimal(6)
    with pytest.raises(ValueError, match="cuts the stop"):
        StopResize(**{**fields, "resized_quantity": Decimal(10)})
    with pytest.raises(ValueError, match="a sell sized in units"):
        StopResize(**{**fields, "original_terms": terms(quantity="10")})


def test_the_ledger_reads_each_currencys_rate_to_base() -> None:
    ledger = decode_ledger(
        {
            "BASE": {"netliquidationvalue": 100, "cashbalance": 10},
            "EUR": {"cashbalance": "10", "exchangerate": 1},
            "USD": {"cashbalance": "5", "exchangerate": "0.86"},
            "GBP": {"cashbalance": "1", "exchangerate": 0},
        },
        base_currency="EUR",
    )
    rates = {b.currency: b.exchange_rate for b in ledger.balances}
    assert rates == {"EUR": Decimal(1), "GBP": None, "USD": Decimal("0.86")}


def test_the_whatif_names_its_currency_when_its_text_does() -> None:
    def amounts(total: str) -> Any:
        return decode_whatif({"amount": {"amount": "1,977.60 USD (10 Shares)", "total": total}})

    assert amounts("1,978.60 USD").currency == "USD"
    assert amounts("1,978.60").currency == "USD"  # from the amount line
    bare = decode_whatif({"amount": {"amount": "1977.60", "total": "1978.60 dollars"}})
    assert bare.currency is None


def quote(**changes: Any) -> Quote:
    fields: dict[str, Any] = {
        "conid": 1,
        "last": 100.0,
        "last_kind": LastKind.TRADE,
        "bid": 99.9,
        "ask": 100.1,
        "crossed": False,
        "volume": None,
        "open_interest": None,
        "implied_vol": 0.25,
        "historical_vol_30d": 0.2,
        "implied_vol_percentile": None,
        "dividend_yield": None,
        "delta": None,
        "gamma": None,
        "vega": None,
        "theta": None,
        "market_data_status": None,
        "status_flag": None,
        "delay_seconds": None,
        "delay_basis": None,
        "updated_at": None,
    }
    fields.update(changes)
    return Quote(**fields)


def test_a_quote_becomes_evidence_stamped_no_later_than_its_update() -> None:
    read = quote_read(quote(updated_at=NOW - timedelta(seconds=5)), option=False, read_at=NOW)
    assert read.evidence.observed_at == NOW - timedelta(seconds=5)
    assert read.sigma == pytest.approx(0.2)  # the underlying's historical volatility first
    assert quote_read(quote(), option=True, read_at=NOW).sigma == pytest.approx(0.25)
    assert quote_read(quote(historical_vol_30d=None), option=False, read_at=NOW).sigma == 0.25
    none = quote_read(quote(implied_vol=None, historical_vol_30d=0.0), option=False, read_at=NOW)
    assert none.sigma is None


def test_a_previous_close_is_no_price_and_a_halt_is_flagged() -> None:
    closed = quote_read(
        quote(last_kind=LastKind.PRIOR_CLOSE, bid=None, ask=0.0), option=False, read_at=NOW
    )
    assert (closed.evidence.last, closed.evidence.bid, closed.evidence.ask) == (None, None, None)
    assert quote_read(quote(last_kind=LastKind.HALTED), option=False, read_at=NOW).halted


def test_the_execution_test_conid_must_be_a_tradable_universe_instrument(config_dir: Any) -> None:
    from obliskiq.config import ConfigInvalid, load_config

    owner = config_dir / "owner.yaml"
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + "\nibkr:\n  preflight:\n    execution_test:\n      conid: 756733\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigInvalid, match=r"execution_test\.conid: 756733 \(SPY\) is not a"):
        load_config(config_dir)
