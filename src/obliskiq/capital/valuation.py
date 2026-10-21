"""What a holding and a currency are worth in the base currency, now.

A currency is valued at the ledger's rate to the base currency, else at a configured spot pair's
quote. A unit of an instrument is its quote's reference (the mid, else the last) times its
multiplier, at that rate. Anything that cannot be valued is ``None``, never a guess: a caller that
needs the value stops instead (honest degradation). The rebalancer, the capital reader and the
reservation planner value the book this way.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument
from ..errors import ObliskError
from ..execution.account import AccountReader, CashRead
from ..execution.instruments import InstrumentDirectory


@dataclass(frozen=True, slots=True)
class UnitValue:
    """One unit of a conid in the base currency, with the rate behind it (``None`` parts when it
    cannot be valued)."""

    conid: int
    instrument: TradedInstrument | None
    unit: Decimal | None
    rate: Decimal | None


class Valuer:
    """Values currencies and instrument units in the base currency from fresh reads."""

    def __init__(
        self, settings: ObliskConfig, *, reader: AccountReader, directory: InstrumentDirectory
    ) -> None:
        self._base = settings.account.base_currency
        self._pairs = tuple(settings.execution.currency.pairs)
        self._reader = reader
        self._directory = directory

    @property
    def base(self) -> str:
        return self._base

    async def rate(self, currency: str, cash: CashRead) -> Decimal | None:
        """One unit of ``currency`` in the base currency: the ledger's rate, else a pair's quote."""
        if currency == self._base:
            return Decimal(1)
        line = cash.line(currency)
        if line is not None and line.rate_to_base is not None:
            return line.rate_to_base
        for pair in self._pairs:
            if {pair.base, pair.quote} != {currency, self._base}:
                continue
            try:
                quote = await self._reader.quote(pair.conid, option=False)
            except ObliskError:
                return None
            reference = None if quote is None else quote.evidence.reference()
            if reference is None:
                return None
            return reference if currency == pair.base else 1 / reference
        return None

    async def unit(self, conid: int, cash: CashRead) -> UnitValue:
        """One unit of ``conid`` in the base currency, from its quote."""
        instrument = self._directory.describe(conid)
        if instrument is None:
            return UnitValue(conid, None, None, None)
        rate = await self.rate(instrument.currency, cash)
        try:
            quote = await self._reader.quote(
                conid, option=instrument.asset_class is AssetClass.OPTION
            )
        except ObliskError:
            quote = None
        reference = None if quote is None else quote.evidence.reference()
        if reference is None or rate is None:
            return UnitValue(conid, instrument, None, rate)
        return UnitValue(conid, instrument, reference * instrument.multiplier * rate, rate)

    async def cash_value(self, amounts: dict[str, Decimal], cash: CashRead) -> Decimal | None:
        """``amounts`` by currency, in the base currency; ``None`` when a currency has no rate."""
        total = Decimal(0)
        for currency, amount in sorted(amounts.items()):
            if amount == 0:
                continue
            rate = await self.rate(currency, cash)
            if rate is None:
                return None
            total += amount * rate
        return total
