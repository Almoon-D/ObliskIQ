"""The cash a fill moves, per currency.

A fill of a security moves its currency by its value (units x price x multiplier) and its
commission: out for a buy, in for a sale. A fill of one of IBKR's spot pairs (a conversion) moves
both currencies: buying the pair receives its base currency and pays its quote currency at the
fill's price. Reconciliation (the cash it expects), the managed book (its cash in mode ``cap``) and
the tax book (the currency lots) all read a fill's cash this way.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from decimal import Decimal

from ..config.schema_execution import CurrencyPair
from ..domain.orders import Fill, OrderSide
from .instruments import InstrumentDirectory

ZERO = Decimal(0)


def pair_of(conid: int, pairs: Sequence[CurrencyPair]) -> CurrencyPair | None:
    """The configured spot pair ``conid`` is, if any."""
    return next((pair for pair in pairs if pair.conid == conid), None)


def fill_cash(
    fill: Fill, *, directory: InstrumentDirectory, pairs: Sequence[CurrencyPair]
) -> dict[str, Decimal] | None:
    """The cash ``fill`` moved, by currency (money in positive); ``None`` when the instrument is
    one nobody describes, so its cash cannot be known."""
    sign = Decimal(1) if fill.side is OrderSide.BUY else Decimal(-1)
    pair = pair_of(fill.conid, pairs)
    if pair is not None:
        return {pair.base: sign * fill.quantity, pair.quote: -sign * fill.quantity * fill.price}
    instrument = directory.describe(fill.conid)
    if instrument is None:
        return None
    value = fill.quantity * fill.price * instrument.multiplier
    return {instrument.currency: -sign * value - (fill.commission or ZERO)}
