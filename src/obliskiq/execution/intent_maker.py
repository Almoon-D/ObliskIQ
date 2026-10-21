"""Making an intent for a planned trade: priced now, refused with why when it cannot be.

The rebalancer and the reservation planner decide what to trade; this turns one decision into a
proposed intent the executor clears, funds, places and reprices: the instrument's listing venue,
a fresh quote, the contract's rules and algos, and ``Pricer.initial`` (a patient limit inside the
mid, or an enabled algo with a limit cap), in whole units on IBKR's smart router, for the day.
An entry buys; a sale is an exit with an allowed exit reason (``discipline``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.orders import (
    ExecutionMode,
    IntentRole,
    OrderIntent,
    OrderSide,
    OrderTerms,
    Sleeve,
    TimeInForce,
)
from ..errors import CalendarError, ObliskError
from ..ibkr.orders import SMART_ROUTER
from ..util.timeutil import utc_now
from .account import AccountReader
from .executor import ContractReader
from .instruments import InstrumentDirectory
from .intents import new_intent
from .pricing import Pricer
from .state import IntentBook


class IntentMaker:
    """Prices a planned trade and adds it to the intent book."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        reader: AccountReader,
        contracts: ContractReader,
        directory: InstrumentDirectory,
        calendars: Calendars,
        book: IntentBook,
        pricer: Pricer,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._contracts = contracts
        self._directory = directory
        self._calendars = calendars
        self._book = book
        self._pricer = pricer
        self._now = now
        self._ttl = timedelta(minutes=settings.trading.intent_ttl_minutes)

    async def make(
        self,
        *,
        conid: int,
        side: OrderSide,
        quantity: Decimal,
        sleeve: Sleeve,
        reason: str,
        mode: ExecutionMode,
    ) -> OrderIntent | str:
        """The intent, added to the book; why, when it cannot be priced now."""
        instrument = self._directory.describe(conid)
        if instrument is None:
            return "no instrument describes the conid"
        try:
            venue = self._calendars.venue_for_ibkr(instrument.listing_exchange)
            quote = await self._reader.quote(
                conid, option=instrument.asset_class is AssetClass.OPTION
            )
            rules = await self._contracts.rules(conid, is_buy=side is OrderSide.BUY)
            algos = await self._contracts.algos(conid)
        except (CalendarError, ObliskError) as exc:
            return f"it cannot be priced: {exc}"
        if quote is None:
            return "it has no quote"
        priced = self._pricer.initial(
            instrument,
            side,
            quote.evidence,
            sigma=quote.sigma,
            rules=rules,
            algos=algos,
            venue=venue,
        )
        if priced.price is None:
            return f"it is not priced: {priced.detail}"
        now = self._now()
        intent = new_intent(
            self._settings,
            sleeve=sleeve,
            role=IntentRole.ENTRY if side is OrderSide.BUY else IntentRole.EXIT,
            terms=OrderTerms(
                conid=conid,
                side=side,
                quantity=quantity,
                price=priced.price,
                tif=TimeInForce.DAY,
                venue=SMART_ROUTER,
            ),
            reason=reason,
            mode=mode,
            approval_required=False,
            created_at=now,
            expires_at=now + self._ttl,
        )
        await self._book.add(intent)
        return intent
