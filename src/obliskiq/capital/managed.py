"""Managed and investable capital (MASTER_PLAN §5.15).

``capital.mode`` says how much of the account the bot manages:

* ``whole_account``: the account's net liquidation value. The owner's flows reach the account,
  so they reach managed capital through the NAV.
* ``cap``: a book that starts at ``capital.cap_base`` and is kept as its own account: its cash is
  the start, plus the owner's recorded flows that count toward the book, plus the cash the book's
  own fills moved; its value is that cash plus its holdings. It follows flows and returns, never
  beyond what the account holds outside unmanaged holdings. A paper account's balance never moves
  with an owner's flow, so in paper the flows adjust this virtual book only (DECISIONS D106).

**Investable capital** is ``min(managed, NAV - unmanaged) - reserved``, never below zero: the
holdings no sleeve owns are counted but never traded, and every open withdrawal reservation comes
off at once, so targets are recomputed on the reduced capital the moment a reservation exists.

Anything that cannot be read or valued — the NAV, a holding, a currency, an open reservation —
manages nothing (honest degradation): the reader returns why instead of a number.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..domain.capital import CashFlow, ReservationState, WithdrawalReservation
from ..domain.orders import UNMANAGED, ExecutionMode, OrderSide, Sleeve
from ..errors import ObliskError
from ..execution.account import AccountReader, CashRead, PositionsRead
from ..execution.fill_cash import fill_cash
from ..execution.instruments import InstrumentDirectory
from ..store import capital as capital_repo
from ..store import orders as repo
from ..store.orders import IntentFill
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .valuation import UnitValue, Valuer

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class BookHolding:
    """Units a sleeve holds and one unit's value (``unit.unit`` ``None``: not valued)."""

    conid: int
    sleeve: Sleeve
    units: Decimal
    unit: UnitValue

    @property
    def value(self) -> Decimal | None:
        return None if self.unit.unit is None else self.units * self.unit.unit


@dataclass(frozen=True, slots=True)
class CapitalView:
    """What the bot manages and may invest now, in the base currency, and how it was decided.

    ``book_cash`` is the cash the book holds: in mode ``cap`` its own cash; in mode
    ``whole_account`` the account's NAV less the book's holdings and the unmanaged ones.
    """

    mode: str
    nav: Decimal
    managed: Decimal
    unmanaged: Decimal
    reserved: Decimal
    investable: Decimal
    book_value: Decimal
    book_cash: Decimal
    holdings: tuple[BookHolding, ...]
    detail: str

    @property
    def unfunded(self) -> Decimal:
        """How much of the open reservations the book's cash does not cover yet."""
        return max(ZERO, self.reserved - max(self.book_cash, ZERO))

    def holding(self, conid: int) -> BookHolding | None:
        return next((h for h in self.holdings if h.conid == conid), None)


def compose(
    settings: ObliskConfig,
    *,
    nav: Decimal,
    book_value: Decimal,
    book_cash: Decimal | None,
    unmanaged: Decimal,
    reserved: Decimal,
    holdings: Sequence[BookHolding] = (),
) -> CapitalView:
    """Managed and investable capital from the readings. ``book_cash`` is the book's own cash in
    mode ``cap`` (``None`` in mode ``whole_account``, where the account's cash is the book's)."""
    capital = settings.capital
    outside_unmanaged = nav - unmanaged
    if capital.mode == "whole_account" or book_cash is None:
        managed = nav
        cash = nav - book_value - unmanaged
        detail = "the whole account"
    else:
        book = book_value + book_cash
        managed = min(book, outside_unmanaged)
        cash = managed - book_value
        detail = (
            f"a book of {book:.2f} (started at {capital.cap_base})"
            if book <= outside_unmanaged
            else f"the account outside unmanaged holdings, below the book's {book:.2f}"
        )
    investable = max(ZERO, min(managed, outside_unmanaged) - reserved)
    return CapitalView(
        mode=capital.mode,
        nav=nav,
        managed=managed,
        unmanaged=unmanaged,
        reserved=reserved,
        investable=investable,
        book_value=book_value,
        book_cash=cash,
        holdings=tuple(holdings),
        detail=detail,
    )


def is_shadow_family(mode: ExecutionMode) -> bool:
    return mode is ExecutionMode.SHADOW


def book_fills(fills: Sequence[IntentFill], mode: ExecutionMode) -> list[IntentFill]:
    """The fills of the book in ``mode``: shadow's simulated fills, or the broker's real ones."""
    shadow = is_shadow_family(mode)
    return [f for f in fills if is_shadow_family(f.mode) == shadow and f.fill.simulated == shadow]


class CapitalReader:
    """The one reader of managed and investable capital: the rebalancer, the reservation planner
    and, later, the options sleeve (Session 12) and the decision layer (Session 17) use it."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        reader: AccountReader,
        directory: InstrumentDirectory,
        worker: StoreWorker,
        valuer: Valuer,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._directory = directory
        self._worker = worker
        self._valuer = valuer
        self._now = now
        self._pairs = tuple(settings.execution.currency.pairs)

    @property
    def valuer(self) -> Valuer:
        return self._valuer

    async def cash(self) -> CashRead:
        """A fresh read of the ledger: cash per currency, the rates and the NAV."""
        return await self._reader.cash()

    async def holdings(self, mode: ExecutionMode) -> dict[int, tuple[Sleeve, Decimal]]:
        """Units each sleeve holds by conid: in shadow mode, its simulated fills; otherwise the
        broker's positions the registry assigns to a sleeve."""
        if is_shadow_family(mode):
            fills = book_fills(await self._worker.run(repo.intent_fills), mode)
            held: dict[int, tuple[Sleeve, Decimal]] = {}
            for entry in fills:
                if entry.fill.conid in {pair.conid for pair in self._pairs}:
                    continue
                quantity = entry.fill.quantity
                signed = quantity if entry.fill.side is OrderSide.BUY else -quantity
                sleeve, units = held.get(entry.fill.conid, (entry.sleeve, ZERO))
                held[entry.fill.conid] = (sleeve, units + signed)
            return {conid: pair for conid, pair in held.items() if pair[1] != ZERO}
        owners = await self._worker.run(repo.holding_assignments)
        positions = await self._reader.positions()
        found: dict[int, tuple[Sleeve, Decimal]] = {}
        for assignment in owners:
            if assignment.owner == UNMANAGED:
                continue
            units = positions.quantity(assignment.conid)
            if units != ZERO:
                found[assignment.conid] = (Sleeve(assignment.owner), units)
        return found

    async def unmanaged(self, positions: PositionsRead, cash: CashRead) -> Decimal | str:
        """The value of the holdings no sleeve owns (``unmanaged``, or never registered), at
        IBKR's mark; why, when one cannot be valued."""
        owners = {a.conid: a.owner for a in await self._worker.run(repo.holding_assignments)}
        total = ZERO
        for position in positions.positions:
            if position.quantity == ZERO or owners.get(position.conid, UNMANAGED) != UNMANAGED:
                continue
            if position.market_value is None or position.currency is None:
                return f"the unmanaged holding conid {position.conid} carries no market value"
            rate = await self._valuer.rate(position.currency, cash)
            if rate is None:
                return (
                    f"the unmanaged holding conid {position.conid}'s {position.currency} has no "
                    "rate"
                )
            total += position.market_value * rate
        return total

    async def reserved(self, cash: CashRead) -> Decimal | str:
        """The open reservations in the base currency."""
        total = ZERO
        for reservation in await self.open_reservations():
            rate = await self._valuer.rate(reservation.currency, cash)
            if rate is None:
                return (
                    f"reservation {reservation.reservation_id}'s {reservation.currency} has no rate"
                )
            total += reservation.amount * rate
        return total

    async def open_reservations(self) -> list[WithdrawalReservation]:
        return await self._worker.run(capital_repo.reservations, [ReservationState.OPEN])

    async def virtual_cash(self, mode: ExecutionMode) -> dict[str, Decimal] | str:
        """The book's own cash by currency in mode ``cap``: the start, the in-book flows and the
        cash the book's fills moved."""
        cap = self._settings.capital.cap_base
        if cap is None:  # the schema refuses it; kept so the type is narrowed honestly
            return "capital.mode cap names no cap_base"
        cash: dict[str, Decimal] = {self._valuer.base: cap}

        def add(currency: str, amount: Decimal) -> None:
            cash[currency] = cash.get(currency, ZERO) + amount

        flows: list[CashFlow] = await self._worker.run(
            capital_repo.cash_flows, as_of=self._now().date()
        )
        for flow in flows:
            if flow.live and flow.in_book:
                add(flow.currency, flow.signed)
        for entry in book_fills(await self._worker.run(repo.intent_fills), mode):
            moved = fill_cash(entry.fill, directory=self._directory, pairs=self._pairs)
            if moved is None:
                fill = entry.fill
                return f"fill {fill.execution_id} on conid {fill.conid} cannot be priced in cash"
            for currency, amount in moved.items():
                add(currency, amount)
        return cash

    async def read(self, mode: ExecutionMode, *, cash: CashRead | None = None) -> CapitalView | str:
        """Managed and investable capital now; why, when it cannot be read."""
        try:
            cash = cash if cash is not None else await self._reader.cash()
            positions = await self._reader.positions()
            held = await self.holdings(mode)
        except ObliskError as exc:
            return f"the account cannot be read: {exc}"
        nav = cash.nav
        if nav is None or not nav.is_finite() or nav <= ZERO:
            return "the account's net liquidation value cannot be read, so nothing is managed"
        holdings = [
            BookHolding(conid, sleeve, units, await self._valuer.unit(conid, cash))
            for conid, (sleeve, units) in sorted(held.items())
        ]
        unvalued = [h.conid for h in holdings if h.value is None]
        if unvalued:
            return f"a held position has no price: conid {', '.join(map(str, unvalued))}"
        book_value = sum((h.value for h in holdings if h.value is not None), ZERO)
        unmanaged = await self.unmanaged(positions, cash)
        if isinstance(unmanaged, str):
            return unmanaged
        reserved = await self.reserved(cash)
        if isinstance(reserved, str):
            return reserved
        book_cash: Decimal | None = None
        if self._settings.capital.mode == "cap":
            amounts = await self.virtual_cash(mode)
            if isinstance(amounts, str):
                return amounts
            book_cash = await self._valuer.cash_value(amounts, cash)
            if book_cash is None:
                return "the book's cash in a foreign currency cannot be valued"
        return compose(
            self._settings,
            nav=nav,
            book_value=book_value,
            book_cash=book_cash,
            unmanaged=unmanaged,
            reserved=reserved,
            holdings=holdings,
        )
