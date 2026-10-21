"""The live tax book: the account's fills become lots, under the active jurisdiction's policy
(MASTER_PLAN §5.16, DECISIONS D74, D110).

Session 4b's :class:`~obliskiq.tax.lots.LotEngine` is the engine; this module feeds it the fills
the broker reported, in the order they were made, and saves its whole state after each one
(``store/lots.py``): the open lots, the loss windows a purchase may still take, the ledger and the
lot numbering, together with the fill and the realisations it made. A restart reloads that state;
a fill is taken once.

* **A security's fill** opens lots (a buy) or consumes them first in, first out (a sale). Its cost
  or proceeds include the commission (LIRPF art. 35) and are valued in the base currency at the
  rate of the trade date: a conversion's own price, else the rate the ledger gave that day
  (``fx_rates``).
* **A foreign currency** is kept as lots when the policy says so (``currency_lots``): it is
  acquired when it comes in (a conversion into it, a sale's proceeds) at its base value that day,
  and disposed of when it goes out (a conversion back, a payment for a purchase) at its base value
  that day; the difference is a gain or a loss. Currency paid out of a balance the book holds no
  lots for (cash the account had before the bot) is noted, never invented.
* **What the book cannot account for** — an instrument without tax facts, a day without a rate, a
  sale of units it holds no lots for (an unmanaged holding) — is recorded ``incomplete`` with why,
  and the security's lots are then incomplete: the hooks report it rather than answer from them.

The decision hooks (``tax_cost``, ``wash_sale_conflict``, ``harvestable_losses`` and the trial of
several sales) are served from these lots through :class:`TaxHooks`. Years settle once the year is
over and no loss window opened in it can still move a loss out of it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..config.schema_instruments import TaxFacts
from ..domain.enums import AssetClass
from ..domain.orders import Fill, OrderSide
from ..domain.tax import (
    ProposedTrade,
    Realisation,
    SecurityIdentity,
    SecurityKind,
    TaxLot,
    TaxYearSummary,
    currency_identity,
)
from ..errors import TaxError
from ..execution.account import CashRead
from ..execution.fill_cash import pair_of
from ..execution.instruments import InstrumentDirectory
from ..jurisdiction.tax import TaxPolicy
from ..logging_setup import get_logger
from ..store import fx as fx_repo
from ..store import lots as lots_repo
from ..store import orders as orders_repo
from ..store.db import Store
from ..store.fx import FxRate
from ..store.lots import TakenFill, TaxState
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .lots import HarvestableLoss, LotEngine, TaxCostEstimate, WashSaleConflict

log = get_logger("tax.book")

ZERO = Decimal(0)

#: The tax kind of each traded asset class; anything else has no tax rule here.
_KINDS = {
    AssetClass.STOCK: SecurityKind.SHARE,
    AssetClass.ETF: SecurityKind.ETF,
    AssetClass.FUND: SecurityKind.FUND,
    AssetClass.BOND: SecurityKind.BOND,
    AssetClass.OPTION: SecurityKind.OPTION,
}

#: The earliest stamp: every fill is after it.
_EPOCH = datetime.min.replace(tzinfo=UTC)


def security_key(conid: int) -> str:
    """How the live book names a contract as a security."""
    return str(conid)


def identity_of(
    conid: int, directory: InstrumentDirectory, facts: Mapping[int, TaxFacts]
) -> SecurityIdentity | None:
    """What the tax rules need to know of ``conid``; ``None`` when it is not known."""
    instrument = directory.describe(conid)
    known = facts.get(conid)
    kind = None if instrument is None else _KINDS.get(instrument.asset_class)
    if instrument is None or known is None or kind is None:
        return None
    try:
        return SecurityIdentity(
            key=security_key(conid),
            kind=kind,
            isin=known.isin,
            issuer=known.issuer,
            tracked_index=known.tracked_index,
            listed=True,
        )
    except ValueError:
        return None


@dataclass
class CatchUp:
    """What one pass over the new fills did."""

    applied: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    waiting: str | None = None
    settled: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class TaxHooks:
    """The decision hooks over a copy of the live lots: nothing asked here changes them."""

    def __init__(self, engine: LotEngine, incomplete: frozenset[str]) -> None:
        self._engine = engine
        self._incomplete = incomplete

    def complete(self, conid: int, units: Decimal) -> bool:
        """Whether the lots account for ``units`` of ``conid`` (and none of its fills was
        incomplete): only then do the hooks answer for it."""
        key = security_key(conid)
        if key in self._incomplete:
            return False
        try:
            self._engine.identity(key)
        except TaxError:
            return False
        return self._engine.quantity(key) == units

    def lots(self, conid: int) -> tuple[TaxLot, ...]:
        return self._engine.lots(security_key(conid))

    def tax_cost(self, conid: int, *, units: Decimal, price: Decimal, on: date) -> TaxCostEstimate:
        """The tax selling ``units`` at ``price`` (base currency, before costs) adds to the year."""
        trade = ProposedTrade(
            security=security_key(conid), side="sell", quantity=units, price=price, on=on
        )
        return self._engine.tax_cost(trade)

    def trial_tax(self, sales: Sequence[tuple[int, Decimal, Decimal]], *, on: date) -> Decimal:
        """The tax ``sales`` (conid, units, price) add to the year made together, in order."""
        return self._engine.trial_tax(
            [
                ProposedTrade(
                    security=security_key(conid), side="sell", quantity=units, price=price, on=on
                )
                for conid, units, price in sales
            ]
        )

    def purchase_defers_loss(self, conid: int, on: date) -> bool:
        """Whether buying ``conid`` on ``on`` would take a loss the wash-sale rule still holds
        open (the two-month rule)."""
        key = security_key(conid)
        try:
            return self._engine.purchase_defers_loss(key, on)
        except TaxError:
            return False

    def wash_sale_conflict(
        self, conid: int, *, side: OrderSide, units: Decimal, price: Decimal, on: date
    ) -> WashSaleConflict | None:
        trade = ProposedTrade(
            security=security_key(conid),
            side="buy" if side is OrderSide.BUY else "sell",
            quantity=units,
            price=price,
            on=on,
        )
        return self._engine.wash_sale_conflict(trade)

    def harvestable_losses(
        self, prices: Mapping[int, Decimal], *, on: date
    ) -> tuple[HarvestableLoss, ...]:
        return self._engine.harvestable_losses(
            {security_key(conid): price for conid, price in prices.items()}, on=on
        )

    def provisional(self, tax_year: int) -> TaxYearSummary:
        return self._engine.ledger.provisional(tax_year)


class TaxBook:
    """The account's lots on the live path, kept in step with its fills."""

    def __init__(
        self,
        settings: ObliskConfig,
        policy: TaxPolicy,
        *,
        worker: StoreWorker,
        directory: InstrumentDirectory,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._policy = policy
        self._worker = worker
        self._directory = directory
        self._now = now
        self._base = settings.account.base_currency
        self._pairs = tuple(settings.execution.currency.pairs)
        self._facts = {entry.conid: entry for entry in settings.eligibility.skeleton_tax_facts}

    @property
    def policy(self) -> TaxPolicy:
        return self._policy

    # ── the engine ──

    def _identity(self, key: str) -> SecurityIdentity | None:
        if key.startswith("CCY:"):
            return currency_identity(key.removeprefix("CCY:"))
        if not key.isdigit():
            return None
        return identity_of(int(key), self._directory, self._facts)

    def _engine_from(self, state: TaxState) -> LotEngine:
        keys = {lot.security for lot in state.lots} | {w.security for w in state.windows}
        identities = []
        for key in sorted(keys):
            identity = self._identity(key)
            if identity is None:
                raise TaxError(f"the saved lots hold {key}, which no tax facts describe any more")
            identities.append(identity)
        engine = LotEngine(self._policy, identities)
        engine.ledger.restore(state.ledger)
        engine.restore(state.lots, windows=state.windows, sequence=state.sequence)
        return engine

    async def engine(self) -> LotEngine:
        """The engine as saved."""
        return self._engine_from(await self._worker.run(lots_repo.load_tax_state))

    async def hooks(self) -> TaxHooks:
        """The decision hooks over the saved lots."""
        engine = await self.engine()
        taken = await self._worker.run(lots_repo.taken_fills)
        fills = {f.execution_id: f for f in await self._worker.run(_all_fills)}
        incomplete = frozenset(
            security_key(fills[e].conid)
            for e, t in taken.items()
            if t.outcome == "incomplete" and e in fills
        )
        return TaxHooks(engine, incomplete)

    def _state(self, engine: LotEngine) -> TaxState:
        return TaxState(
            lots=engine.lots(),
            windows=engine.loss_windows(),
            ledger=engine.ledger.snapshot(),
            sequence=engine.sequence,
        )

    # ── fills ──

    async def note_rates(self, cash: CashRead) -> int:
        """Record the day's rates to the base currency from a ledger read: the rates a fill of
        that day is valued at."""
        rates = [
            FxRate(
                currency=line.currency,
                on=cash.observed_at.date(),
                rate_to_base=line.rate_to_base,
                source="ibkr ledger",
                observed_at=cash.observed_at,
            )
            for line in cash.lines
            if line.currency != self._base
            and line.rate_to_base is not None
            and line.rate_to_base > ZERO
        ]
        return await self._worker.run(fx_repo.record_rates, rates) if rates else 0

    async def catch_up(self) -> CatchUp:
        """Take every fill not taken yet, oldest first; settle the years that are over."""
        outcome = CatchUp()
        engine = await self.engine()
        taken = await self._worker.run(lots_repo.taken_fills)
        fills = [
            fill
            for fill in await self._worker.run(_all_fills)
            if not fill.simulated and fill.execution_id not in taken
        ]
        for fill in fills:
            rate_or_why = await self._rate(fill)
            if isinstance(rate_or_why, str) and not rate_or_why.startswith("incomplete:"):
                outcome.waiting = f"fill {fill.execution_id}: {rate_or_why}"
                break
            trial = engine.copy()  # a fill that fails leaves the engine as it was
            try:
                if isinstance(rate_or_why, str):
                    raise TaxError(rate_or_why.removeprefix("incomplete: "))
                realisations, notes = self._apply(trial, fill, rate_or_why)
            except TaxError as exc:
                taken_fill = TakenFill(fill.execution_id, "incomplete", str(exc), self._now())
                await self._worker.run(lots_repo.record_taken_fill, taken_fill, state=None)
                outcome.incomplete.append(f"{fill.execution_id}: {exc}")
                continue
            detail = "; ".join(notes) or "applied"
            taken_fill = TakenFill(fill.execution_id, "applied", detail, self._now())
            await self._worker.run(
                lots_repo.record_taken_fill,
                taken_fill,
                state=self._state(trial),
                realisations=realisations,
            )
            engine = trial
            outcome.applied.append(fill.execution_id)
            outcome.notes += notes
        outcome.settled = await self._settle_due(engine)
        return outcome

    async def _rate(self, fill: Fill) -> Decimal | str:
        """The rate of the fill's currency to the base on its trade date; why when it has none
        (``incomplete:`` when it never will)."""
        if pair_of(fill.conid, self._pairs) is not None:
            return Decimal(1)  # a conversion's own price values it
        instrument = self._directory.describe(fill.conid)
        if instrument is None:
            return f"incomplete: conid {fill.conid} is described by no instrument"
        if instrument.currency == self._base:
            return Decimal(1)
        day = fill.executed_at.date()
        found = await self._worker.run(fx_repo.rate_on, instrument.currency, day)
        if found is not None:
            return found.rate_to_base
        if day < self._now().date():
            return (
                f"incomplete: no {instrument.currency} rate was recorded on {day.isoformat()}, "
                "the fill's trade date"
            )
        return f"no {instrument.currency} rate recorded yet today"

    def _apply(
        self, engine: LotEngine, fill: Fill, rate: Decimal
    ) -> tuple[list[Realisation], list[str]]:
        on = fill.executed_at.date()
        commission = fill.commission or ZERO
        pair = pair_of(fill.conid, self._pairs)
        realisations: list[Realisation] = []
        notes: list[str] = []
        if pair is not None:
            buying = fill.side is OrderSide.BUY
            moves = {pair.base: fill.quantity, pair.quote: -fill.quantity * fill.price}
            if not buying:
                moves = {currency: -amount for currency, amount in moves.items()}
            base_paid = -moves[self._base] if self._base in moves else None
            if base_paid is None:
                raise TaxError(f"the pair {pair.symbol} does not name the base currency")
            for currency, amount in moves.items():
                if currency == self._base:
                    continue
                if amount > ZERO:
                    self._take_in(engine, currency, amount, base_paid + commission, on)
                else:
                    realisations += self._pay_out(
                        engine, currency, -amount, -base_paid - commission, on, notes
                    )
            return realisations, notes
        instrument = self._directory.describe(fill.conid)
        assert instrument is not None  # _rate refused an undescribed conid already
        identity = identity_of(fill.conid, self._directory, self._facts)
        if identity is None:
            raise TaxError(f"conid {fill.conid} has no tax facts (eligibility.skeleton_tax_facts)")
        engine.register(identity)
        key = identity.key
        value = fill.quantity * fill.price * instrument.multiplier
        foreign = instrument.currency != self._base
        if fill.side is OrderSide.BUY:
            paid = value + commission
            cost = paid * rate
            if foreign:
                realisations += self._pay_out(engine, instrument.currency, paid, cost, on, notes)
            engine.acquire(key, on=on, quantity=fill.quantity, cost=cost)
            return realisations, notes
        held = engine.quantity(key)
        if fill.quantity > held:
            raise TaxError(
                f"a sale of {fill.quantity} of conid {fill.conid} exceeds the {held} the lots "
                "hold (units the bot did not buy)"
            )
        received = value - commission
        proceeds = max(received * rate, ZERO)
        realisations += engine.dispose(key, on=on, quantity=fill.quantity, proceeds=proceeds)
        if foreign and received > ZERO:
            self._take_in(engine, instrument.currency, received, proceeds, on)
        return realisations, notes

    def _take_in(
        self, engine: LotEngine, currency: str, amount: Decimal, cost: Decimal, on: date
    ) -> None:
        """Foreign currency coming in: a currency lot, when the policy keeps them."""
        if not self._policy.currency_lots:
            return
        identity = currency_identity(currency)
        engine.register(identity)
        engine.acquire(identity.key, on=on, quantity=amount, cost=max(cost, ZERO))

    def _pay_out(
        self,
        engine: LotEngine,
        currency: str,
        amount: Decimal,
        proceeds: Decimal,
        on: date,
        notes: list[str],
    ) -> list[Realisation]:
        """Foreign currency going out: its lots consumed first in, first out at ``proceeds``."""
        if not self._policy.currency_lots:
            return []
        identity = currency_identity(currency)
        engine.register(identity)
        held = engine.quantity(identity.key)
        used = min(held, amount)
        if used < amount:
            notes.append(
                f"{amount - used} {currency} paid from a balance with no lots: its gain or loss "
                "is not computed"
            )
        if used <= ZERO:
            return []
        share = max(proceeds, ZERO) * used / amount
        return list(engine.dispose(identity.key, on=on, quantity=used, proceeds=share))

    async def _settle_due(self, engine: LotEngine) -> list[int]:
        """Settle every year that is over and that no open loss window can still change."""
        today = self._now().date()
        settled: list[int] = []
        for year in engine.ledger.years_with_activity():
            if year >= today.year:
                break
            if any(w.sold_on.year == year and w.closes_on >= today for w in engine.loss_windows()):
                break
            engine.ledger.settle(year)
            settled.append(year)
        if settled:
            await self._worker.run(
                lots_repo.save_tax_state, self._state(engine), saved_at=self._now()
            )
        return settled


def _all_fills(store: Store) -> list[Fill]:
    """Every recorded fill, oldest first."""
    return orders_repo.fills_between(store, since=_EPOCH, as_of=datetime.max.replace(tzinfo=UTC))
