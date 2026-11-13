"""What a book may spend now, and what already works on its instruments.

The core's rebalancer and the thesis book's read the same cash: the smaller of the account's cash
and the managed book's own, less the open withdrawal reservations, the part of a deposit not yet
released for deployment (``capital/deploy.py``), every open buy's commitment and
``execution.pretrade.cash_floor``. Each then keeps to its own budget.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

from ..capital.deploy import held_back
from ..capital.managed import CapitalReader, CapitalView
from ..config.schema import ObliskConfig
from ..domain.orders import TERMINAL_STATES, ExecutionMode, IntentState, OrderIntent, OrderSide
from ..execution.account import CashRead
from ..execution.instruments import InstrumentDirectory
from ..store import capital as capital_repo
from ..store import orders as repo
from ..store.worker import StoreWorker

ZERO = Decimal(0)


class BookCash:
    """Reads the working intents and the cash a book may spend."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        capital: CapitalReader,
        directory: InstrumentDirectory,
        worker: StoreWorker,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._capital = capital
        self._directory = directory
        self._worker = worker
        self._now = now
        self._base = settings.account.base_currency
        self._floor = settings.execution.pretrade.cash_floor

    async def pending(self, mode: ExecutionMode) -> tuple[list[OrderIntent], list[OrderIntent]]:
        """The intents of this mode that still work on an instrument (a cancelled one waiting to
        be retried at the next window included), and every open buy of this mode."""
        now = self._now()
        busy: list[OrderIntent] = []
        buys: list[OrderIntent] = []
        open_states = [state for state in IntentState if state not in TERMINAL_STATES]
        for stored in await self._worker.run(repo.intents_in_states, open_states):
            intent = stored.intent
            if intent.mode is not mode:
                continue
            busy.append(intent)
            if intent.side is OrderSide.BUY and not intent.terms.conversion:
                buys.append(intent)
        for stored in await self._worker.run(repo.intents_in_states, [IntentState.CANCELLED]):
            intent = stored.intent
            if intent.mode is mode and intent.expires_at > now and not intent.terms.conversion:
                busy.append(intent)
        return busy, buys

    async def _committed(self, buys: Sequence[OrderIntent], cash: CashRead) -> Decimal | None:
        """What the open buys still commit, in the base currency (``None``: one is unvalued)."""
        total = ZERO
        for intent in buys:
            instrument = self._directory.describe(intent.conid)
            rate = (
                None
                if instrument is None
                else await self._capital.valuer.rate(instrument.currency, cash)
            )
            quantity = intent.terms.quantity
            if instrument is None or rate is None or quantity is None:
                return None
            fills = await self._worker.run(repo.fills_for_intent, intent.intent_id)
            left = max(ZERO, quantity - sum((fill.quantity for fill in fills), ZERO))
            total += left * intent.terms.price.limit * instrument.multiplier * rate
        return total

    async def _held_back(self, cash: CashRead) -> Decimal | None:
        """The part of the deposits not released for deployment yet, in the base currency."""
        flows = await self._worker.run(capital_repo.cash_flows)
        held = held_back(flows, self._settings.capital, today=self._now().date())
        return await self._capital.valuer.cash_value(held, cash)

    async def spendable(
        self, mode: ExecutionMode, view: CapitalView, cash: CashRead
    ) -> Decimal | str:
        """What the book may spend now, or why it cannot be said."""
        _, buys = await self.pending(mode)
        committed = await self._committed(buys, cash)
        if committed is None:
            return "an open buy cannot be valued in the base currency"
        account_cash = ZERO
        for line in cash.lines:
            rate = await self._capital.valuer.rate(line.currency, cash)
            if rate is None:
                return f"the {line.currency} cash cannot be valued in {self._base}"
            account_cash += line.cash * rate
        held_back_now = await self._held_back(cash)
        if held_back_now is None:
            return f"a deposit cannot be valued in {self._base}"
        book_cash = view.book_cash - view.reserved - held_back_now
        return max(ZERO, min(account_cash, book_cash) - committed - self._floor)
