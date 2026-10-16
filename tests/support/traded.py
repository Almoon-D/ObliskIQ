"""Traded instruments for tests of the eligibility policy and the prohibited categories.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Any

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.instrument import TradedInstrument

EXPIRY = date(2027, 1, 15)


def instrument(conid: int, asset: AssetClass = AssetClass.STOCK, **kw: Any) -> TradedInstrument:
    return TradedInstrument(
        conid=conid,
        symbol=kw.pop("symbol", f"I{conid}"),
        listing_exchange="NYSE",
        currency="USD",
        asset_class=asset,
        description=kw.pop("description", "a test instrument"),
        multiplier=Decimal(kw.pop("multiplier", 1)),
        underlying_conid=kw.pop("underlying", None),
        expiry=kw.pop("expiry", None),
    )


def option(conid: int, underlying: TradedInstrument) -> TradedInstrument:
    return instrument(
        conid, AssetClass.OPTION, multiplier=100, underlying=underlying.conid, expiry=EXPIRY
    )


STOCK = instrument(1)
ETF = instrument(2, AssetClass.ETF)
OTHER_STOCK = instrument(3)
