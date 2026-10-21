"""The lot engine: lots opened by purchases, consumed by sales under the policy's lot method, with
the wash-sale rule applied as purchases and sales arrive (MASTER_PLAN §5.16).

Every amount is in the base currency and includes the trade's costs: an acquisition's cost is
what was paid for the units with its commissions and taxes, a sale's proceeds what was received
after them (LIRPF art. 35 counts both).

**The wash-sale rule.** A sale whose result is a loss looks back: homogeneous units bought within
the policy's window before it and still held take the loss onto themselves, unit for unit, and it
is not counted. Units of the loss left unmatched stay open until the window after the sale
closes; a homogeneous purchase inside it takes them, and the loss leaves the year it was counted
in (the ledger refuses this once that year is settled). A deferred loss rides on the units that
took it and is released, in proportion, as they are sold; it is part of that sale's result, so a
loss released into another loss sale inside a window is deferred again. A unit takes at most one
loss.

The hooks a decision asks before it trades — :meth:`LotEngine.tax_cost`,
:meth:`LotEngine.wash_sale_conflict` and :meth:`LotEngine.harvestable_losses` — run the trade on
a copy of the engine, so they answer with the same arithmetic the trade itself would meet.

The simulator uses this engine, and so does the live path (``tax/book.py``), which feeds it the
account's fills and keeps its foreign-currency balances as lots.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from ..domain.tax import (
    IncomeEvent,
    OpenLossWindow,
    ProposedTrade,
    Realisation,
    SecurityIdentity,
    TaxLot,
)
from ..errors import TaxError
from ..jurisdiction.tax import TaxPolicy, WashSaleWindow
from .ledger import TaxLedger

ZERO = Decimal(0)
#: Months in a year: a unit, for moving a date by whole months.
MONTHS_PER_YEAR: Final = 12
#: A lot id is the security's key, this separator and the engine's running number.
LOT_NUMBER_SEPARATOR: Final = "#"


def add_months(day: date, months: int) -> date:
    """``day`` moved by whole calendar months; a day past the target month's end is its last."""
    index = day.year * MONTHS_PER_YEAR + day.month - 1 + months
    year, month_index = divmod(index, MONTHS_PER_YEAR)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(slots=True)
class _Lot:
    lot_id: str
    security: str
    acquired_on: date
    quantity: Decimal
    cost: Decimal
    deferred_loss: Decimal
    replacement_units: Decimal

    @property
    def free_units(self) -> Decimal:
        return self.quantity - self.replacement_units

    def snapshot(self) -> TaxLot:
        return TaxLot(
            lot_id=self.lot_id,
            security=self.security,
            acquired_on=self.acquired_on,
            quantity=self.quantity,
            cost=self.cost,
            deferred_loss=self.deferred_loss,
            replacement_units=self.replacement_units,
        )


@dataclass(slots=True)
class _LossWindow:
    """Loss units of a sale that a homogeneous purchase may still take until ``closes_on``."""

    security: str
    sold_on: date
    closes_on: date
    loss_per_unit: Decimal
    units: Decimal


@dataclass(slots=True)
class _Slice:
    lot: _Lot
    quantity: Decimal
    proceeds: Decimal
    cost: Decimal
    released: Decimal
    deferred: Decimal = ZERO

    @property
    def result(self) -> Decimal:
        return self.proceeds - self.cost - self.released


@dataclass(frozen=True, slots=True)
class TaxCostEstimate:
    """What a proposed trade would add to its year's tax (negative: a saving).

    ``realised`` is what the trade would count in the year's gains; ``deferred`` the loss the
    wash-sale rule would set aside (a buy may defer an earlier sale's loss).
    """

    trade: ProposedTrade
    realised: Decimal
    deferred: Decimal
    tax: Decimal
    tax_year: int
    rate_table_covers_year: bool


@dataclass(frozen=True, slots=True)
class WashSaleConflict:
    """A trade that would defer a loss: which way, how much and the dates involved."""

    trade: ProposedTrade
    kind: Literal["purchase_defers_earlier_loss", "loss_sale_deferred"]
    deferred: Decimal
    dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class HarvestableLoss:
    """A position whose sale now would count a loss, and how much of it would be deferred."""

    security: str
    quantity: Decimal
    unrealised_loss: Decimal
    harvestable: Decimal
    deferred: Decimal


class LotEngine:
    """Lots, realisations and income under one tax policy, recorded into one ledger."""

    def __init__(
        self,
        policy: TaxPolicy,
        securities: Iterable[SecurityIdentity],
        ledger: TaxLedger | None = None,
    ) -> None:
        self._policy = policy
        self._ledger = ledger if ledger is not None else TaxLedger(policy)
        self._identities: dict[str, SecurityIdentity] = {}
        self._lots: dict[str, list[_Lot]] = {}
        self._windows: list[_LossWindow] = []
        self._sequence = 0
        for identity in securities:
            self.register(identity)

    @property
    def ledger(self) -> TaxLedger:
        return self._ledger

    def register(self, identity: SecurityIdentity) -> None:
        known = self._identities.get(identity.key)
        if known is not None and known != identity:
            raise TaxError(f"security {identity.key} is already registered as another identity")
        self._identities[identity.key] = identity

    def identity(self, security: str) -> SecurityIdentity:
        identity = self._identities.get(security)
        if identity is None:
            raise TaxError(f"security {security} has no registered identity")
        return identity

    # ── reading ──

    def lots(self, security: str | None = None) -> tuple[TaxLot, ...]:
        keys = sorted(self._lots) if security is None else [security]
        return tuple(lot.snapshot() for key in keys for lot in self._lots.get(key, []))

    def quantity(self, security: str) -> Decimal:
        return sum((lot.quantity for lot in self._lots.get(security, [])), ZERO)

    def cost(self, security: str) -> Decimal:
        return sum((lot.cost for lot in self._lots.get(security, [])), ZERO)

    def basis(self, security: str) -> Decimal:
        """What a sale of every unit counts against: the lots' cost and the losses they carry."""
        return sum((lot.cost + lot.deferred_loss for lot in self._lots.get(security, [])), ZERO)

    def purchase_defers_loss(self, security: str, on: date) -> bool:
        """Whether a purchase of ``security`` on ``on`` would take a loss still open under the
        wash-sale rule (the purchase side of :meth:`wash_sale_conflict`, without a trial)."""
        identity = self.identity(security)
        return any(
            window.units > ZERO
            and window.sold_on <= on <= window.closes_on
            and self._policy.homogeneous(self.identity(window.security), identity)
            for window in self._windows
        )

    def held(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, lots in self._lots.items() if lots))

    def open_loss_units(self, on: date) -> Decimal:
        """Loss units still waiting for a homogeneous purchase on ``on``."""
        return sum((w.units for w in self._windows if w.closes_on >= on), ZERO)

    def loss_windows(self) -> tuple[OpenLossWindow, ...]:
        """The loss units still waiting for a homogeneous purchase, oldest sale first."""
        return tuple(
            OpenLossWindow(
                security=w.security,
                sold_on=w.sold_on,
                closes_on=w.closes_on,
                loss_per_unit=w.loss_per_unit,
                units=w.units,
            )
            for w in sorted(self._windows, key=lambda w: (w.sold_on, w.security))
            if w.units > ZERO
        )

    @property
    def sequence(self) -> int:
        """The running number the last lot id carries."""
        return self._sequence

    def restore(
        self,
        lots: Iterable[TaxLot],
        *,
        windows: Iterable[OpenLossWindow] = (),
        sequence: int = 0,
    ) -> None:
        """Load saved lots, open loss windows and the lot numbering into an engine that holds
        none (a restart from the store)."""
        if self._lots or self._windows:
            raise TaxError("lots can be restored only into an empty engine")
        self._sequence = max(self._sequence, sequence)
        for window in windows:
            self.identity(window.security)
            self._windows.append(
                _LossWindow(
                    window.security,
                    window.sold_on,
                    window.closes_on,
                    window.loss_per_unit,
                    window.units,
                )
            )
        for saved in sorted(lots, key=lambda lot: (lot.acquired_on, lot.lot_id)):
            self.identity(saved.security)
            self._lots.setdefault(saved.security, []).append(
                _Lot(
                    saved.lot_id,
                    saved.security,
                    saved.acquired_on,
                    saved.quantity,
                    saved.cost,
                    saved.deferred_loss,
                    saved.replacement_units,
                )
            )
            _, _, number = saved.lot_id.rpartition(LOT_NUMBER_SEPARATOR)
            if number.isdigit():
                self._sequence = max(self._sequence, int(number))

    # ── events ──

    def acquire(self, security: str, *, on: date, quantity: Decimal, cost: Decimal) -> TaxLot:
        """Open a lot of ``quantity`` units costing ``cost`` in all."""
        identity = self.identity(security)
        if not quantity.is_finite() or quantity <= ZERO:
            raise TaxError(f"an acquisition's quantity must be positive, got {quantity}")
        if not cost.is_finite() or cost < ZERO:
            raise TaxError(f"an acquisition's cost must be finite and not negative, got {cost}")
        self._ledger.ensure_open(on.year)
        self._windows = [w for w in self._windows if w.closes_on >= on and w.units > ZERO]
        takes: list[tuple[_LossWindow, Decimal]] = []
        free = quantity
        for window in sorted(self._windows, key=lambda w: w.sold_on):
            if free <= ZERO:
                break
            if window.sold_on > on or not self._policy.homogeneous(
                self.identity(window.security), identity
            ):
                continue
            matched = min(window.units, free)
            takes.append((window, matched))
            free -= matched
        for window, _ in takes:
            self._ledger.ensure_open(window.sold_on.year)
        self._sequence += 1
        lot = _Lot(
            f"{security}{LOT_NUMBER_SEPARATOR}{self._sequence}", security, on, quantity, cost,
            ZERO, ZERO,
        )  # fmt: skip
        self._lots.setdefault(security, []).append(lot)
        for window, matched in takes:
            deferred = matched * window.loss_per_unit
            window.units -= matched
            lot.deferred_loss += deferred
            lot.replacement_units += matched
            self._ledger.record_deferral(window.sold_on.year, deferred)
        return lot.snapshot()

    def dispose(
        self, security: str, *, on: date, quantity: Decimal, proceeds: Decimal
    ) -> tuple[Realisation, ...]:
        """Sell ``quantity`` units for ``proceeds`` in all, consuming lots by the lot method."""
        identity = self.identity(security)
        if not quantity.is_finite() or quantity <= ZERO:
            raise TaxError(f"a sale's quantity must be positive, got {quantity}")
        if not proceeds.is_finite() or proceeds < ZERO:
            raise TaxError(f"a sale's proceeds must be finite and not negative, got {proceeds}")
        held = self.quantity(security)
        if quantity > held:
            raise TaxError(f"a sale of {quantity} {security} exceeds the {held} held")
        self._ledger.ensure_open(on.year)
        slices = self._consume(security, quantity, proceeds)
        window = self._policy.wash_sale_window(identity)
        if window is not None and any(s.result < ZERO for s in slices):
            self._defer_losses(identity, slices, on, window)
        realisations = tuple(
            Realisation(
                security=security,
                lot_id=s.lot.lot_id,
                acquired_on=s.lot.acquired_on,
                disposed_on=on,
                quantity=s.quantity,
                proceeds=s.proceeds,
                cost=s.cost,
                released=s.released,
                deferred=s.deferred,
            )
            for s in slices
        )
        for realisation in realisations:
            self._ledger.record_realisation(realisation)
        return realisations

    def receive_income(
        self, security: str, *, on: date, gross: Decimal, source_country: str | None
    ) -> IncomeEvent:
        """Income paid on ``security``; the source withholds at the rate the policy states."""
        self.identity(security)
        self._ledger.ensure_open(on.year)
        withheld = ZERO
        if source_country is not None:
            rate = self._policy.withholding_rate(source_country)
            if rate is None:
                raise TaxError(
                    f"the profile states no withholding rate for income from {source_country}"
                )
            withheld = gross * rate
        event = IncomeEvent(
            security=security,
            received_on=on,
            gross=gross,
            withheld=withheld,
            source_country=source_country,
        )
        self._ledger.record_income(event)
        return event

    def _consume(self, security: str, quantity: Decimal, proceeds: Decimal) -> list[_Slice]:
        slices: list[_Slice] = []
        remaining = quantity
        lots = self._lots[security]
        while remaining > ZERO:
            lot = lots[0]
            take = min(lot.quantity, remaining)
            share = take / lot.quantity
            piece = _Slice(
                lot=lot,
                quantity=take,
                proceeds=proceeds * take / quantity,
                cost=lot.cost * share,
                released=lot.deferred_loss * share,
            )
            lot.quantity -= take
            lot.cost -= piece.cost
            lot.deferred_loss -= piece.released
            # Kept proportional, and never above the units left: a share rounded at Decimal's
            # precision could otherwise leave a lot more replacement units than units.
            lot.replacement_units = min(
                lot.replacement_units - lot.replacement_units * share, lot.quantity
            )
            if lot.quantity <= ZERO:
                lots.pop(0)
            slices.append(piece)
            remaining -= take
        if not lots:
            del self._lots[security]
        return slices

    def _defer_losses(
        self, identity: SecurityIdentity, slices: list[_Slice], on: date, window: WashSaleWindow
    ) -> None:
        # A window that opens at the sale (before_months zero) looks only forward: units already
        # held never take the loss.
        opens = add_months(on, -window.before_months)
        candidates = (
            sorted(
                (
                    lot
                    for key, lots in self._lots.items()
                    if self._policy.homogeneous(identity, self.identity(key))
                    for lot in lots
                    if opens <= lot.acquired_on <= on and lot.free_units > ZERO
                ),
                key=lambda lot: (lot.acquired_on, lot.lot_id),
            )
            if window.before_months > 0
            else []
        )
        for piece in slices:
            if piece.result >= ZERO:
                continue
            per_unit = -piece.result / piece.quantity
            unmatched = piece.quantity
            for lot in candidates:
                matched = min(unmatched, lot.free_units)
                if matched <= ZERO:
                    continue
                deferred = matched * per_unit
                lot.deferred_loss += deferred
                lot.replacement_units += matched
                piece.deferred += deferred
                unmatched -= matched
                if unmatched <= ZERO:
                    break
            if unmatched > ZERO:
                self._windows.append(
                    _LossWindow(
                        identity.key, on, add_months(on, window.after_months), per_unit, unmatched
                    )
                )

    # ── hooks ──

    def copy(self) -> LotEngine:
        """An independent engine in the same state: a trial that leaves this one untouched."""
        return self._copy()

    def _copy(self) -> LotEngine:
        """An independent engine in the same state: its own lots, windows and ledger."""
        twin = LotEngine(self._policy, (), ledger=self._ledger.copy())
        twin._identities = dict(self._identities)  # identities are immutable
        twin._lots = {key: [replace(lot) for lot in lots] for key, lots in self._lots.items()}
        twin._windows = [replace(window) for window in self._windows]
        twin._sequence = self._sequence
        return twin

    def _make(self, trade: ProposedTrade) -> tuple[Decimal, Decimal]:
        """Make ``trade`` at the price it names: what it counts in the year's gains and the loss
        the wash-sale rule sets aside."""
        notional = trade.quantity * trade.price
        if trade.side == "sell":
            results = self.dispose(
                trade.security, on=trade.on, quantity=trade.quantity, proceeds=notional
            )
            realised = sum((r.counted for r in results), ZERO)
            return realised, sum((r.deferred for r in results), ZERO)
        lot = self.acquire(trade.security, on=trade.on, quantity=trade.quantity, cost=notional)
        return ZERO, lot.deferred_loss

    def tax_cost(self, trade: ProposedTrade) -> TaxCostEstimate:
        """The tax ``trade`` would add to its year, at the price it names, before trading costs."""
        year = trade.on.year
        before = self._ledger.provisional(year)
        trial = self._copy()
        realised, deferred = trial._make(trade)
        after = trial.ledger.provisional(year)
        return TaxCostEstimate(
            trade=trade,
            realised=realised,
            deferred=deferred,
            tax=after.tax_due - before.tax_due,
            tax_year=year,
            rate_table_covers_year=after.rate_table_covers_year,
        )

    def trial_tax(self, trades: Sequence[ProposedTrade]) -> Decimal:
        """The tax ``trades`` would add to their year made together, in order, at the prices they
        name and before trading costs: a loss on one offsets a gain on another as the year would
        (:meth:`tax_cost` prices one trade alone). Nothing here changes."""
        if not trades:
            return ZERO
        years = {trade.on.year for trade in trades}
        if len(years) > 1:
            raise TaxError(f"a trial of several trades needs one tax year, got {sorted(years)}")
        year = trades[0].on.year
        before = self._ledger.provisional(year)
        trial = self._copy()
        for trade in trades:
            trial._make(trade)
        return trial.ledger.provisional(year).tax_due - before.tax_due

    def wash_sale_conflict(self, trade: ProposedTrade) -> WashSaleConflict | None:
        """Whether ``trade`` would defer a loss, and how much."""
        trial = self._copy()
        notional = trade.quantity * trade.price
        if trade.side == "buy":
            identity = self.identity(trade.security)
            dates = tuple(
                sorted(
                    {
                        w.sold_on
                        for w in self._windows
                        if w.units > ZERO
                        and w.sold_on <= trade.on <= w.closes_on
                        and self._policy.homogeneous(self.identity(w.security), identity)
                    }
                )
            )
            lot = trial.acquire(trade.security, on=trade.on, quantity=trade.quantity, cost=notional)
            if lot.deferred_loss <= ZERO:
                return None
            return WashSaleConflict(trade, "purchase_defers_earlier_loss", lot.deferred_loss, dates)
        results = trial.dispose(
            trade.security, on=trade.on, quantity=trade.quantity, proceeds=notional
        )
        deferred = sum((r.deferred for r in results), ZERO)
        if deferred <= ZERO:
            return None
        return WashSaleConflict(trade, "loss_sale_deferred", deferred, (trade.on,))

    def harvestable_losses(
        self, prices: Mapping[str, Decimal], *, on: date
    ) -> tuple[HarvestableLoss, ...]:
        """Positions under water at ``prices`` (per unit, base currency), largest harvest first.

        ``harvestable`` is the loss a sale of the whole position would count now; ``deferred``
        the part the wash-sale rule would set aside because homogeneous units were bought within
        the window. A position without a price is left out.
        """
        found: list[HarvestableLoss] = []
        for security in self.held():
            price = prices.get(security)
            if price is None:
                continue
            quantity = self.quantity(security)
            carried = sum((lot.deferred_loss for lot in self._lots[security]), ZERO)
            unrealised = self.cost(security) + carried - quantity * price
            if unrealised <= ZERO:
                continue
            results = self._copy().dispose(
                security, on=on, quantity=quantity, proceeds=quantity * price
            )
            counted = sum((r.counted for r in results), ZERO)
            deferred = sum((r.deferred for r in results), ZERO)
            found.append(
                HarvestableLoss(
                    security=security,
                    quantity=quantity,
                    unrealised_loss=unrealised,
                    harvestable=max(-counted, ZERO),
                    deferred=deferred,
                )
            )
        return tuple(sorted(found, key=lambda loss: (-loss.harvestable, loss.security)))
