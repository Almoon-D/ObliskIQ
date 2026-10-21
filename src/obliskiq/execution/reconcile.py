"""Reconciliation: the broker's orders, trades, positions and cash per currency against the store
(MASTER_PLAN §5.12).

A run reads the broker whole — the live orders (a complete snapshot), the trades IBKR serves, every
position and the ledger — records what it read (the orders and their intents' states, the fills,
a positions and a cash snapshot) and compares:

* **Orders.** Every intent the store holds at the broker (submitted, working, partly filled) is
  listed under its cOID or has fills, unless it moved within the grace period
  (``execution.reconcile.grace_seconds``, IBKR's reporting lag); past it, its order's status is
  read, and one still unaccounted for is drift. A working order the bot did not place (no cOID,
  or one the store does not know) is drift: the cash and the position it moves are unknown.
* **Fills.** A working order that reports more filled than the store holds under its cOID, and an
  execution the bot did not make, are drift.
* **Positions.** Each instrument against the **anchor** — the reads of the latest run that was
  green and settled — plus the fills since it. A difference waits while the instrument had a fill
  or an order move within the grace period (the run is then green but no anchor), and is drift
  after it, until the broker agrees again or the owner accepts the broker's state
  (:meth:`Reconciler.accept`). A holding no sleeve owns is registered: to the sleeve whose entry
  filled it, else ``unmanaged``.
* **Cash.** Each currency against the anchor plus the cash the bot's fills moved (a conversion's
  in both currencies) and the owner's recorded flows the broker has shown since. A difference
  beyond ``cash_tolerance_base`` either way is matched to a recorded flow of that amount (a
  deposit that arrived, a withdrawal that left), which is stamped and explains it; anything else
  is an unexplained cash event (``cash_events``) that freezes new buys until the owner classifies
  it (a dividend, interest, a fee, an unannounced deposit; ``obliskiq cash classify``). A negative
  balance is always drift: no leverage.

A fill in the same second as the anchor's read may or may not be in it: both readings are
accepted. Any drift freezes new risk: the pre-trade gate refuses orders that add risk until a run
is green. A read that fails or is partial makes the run incomplete, never green. A run that is not
green alerts the owner.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ..config.schema import ObliskConfig
from ..domain.capital import CashFlow, Deposit, Withdrawal
from ..domain.orders import (
    UNMANAGED,
    CashBalance,
    ExecutionMode,
    Fill,
    HoldingAssignment,
    HoldingOwner,
    IntentRole,
    OrderSide,
    Position,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
)
from ..errors import ObliskError
from ..ibkr.errors import IbkrError
from ..ibkr.order_decode import LiveOrders
from ..ibkr.orders import is_live
from ..logging_setup import get_logger
from ..store import capital as capital_repo
from ..store import orders as repo
from ..store.orders import AccountRead
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .account import AccountReader, CashRead, PositionsRead
from .boundary import OrderAlert, OrderAlertHook, OrderAlertKind, WriteBoundary, raise_alert
from .fill_cash import fill_cash
from .instruments import InstrumentDirectory
from .killswitch import CONVERSION_SEC_TYPE
from .state import BROKER_STATES

log = get_logger("execution.reconcile")

ZERO = Decimal(0)

#: Who registers a holding reconciliation finds.
RECONCILE_ACTOR = "reconciliation"

#: Findings that keep a green run from becoming the anchor: the broker may not show it all yet.
SETTLING = frozenset({"position_settling", "cash_settling"})
#: A cash change nothing explains, waiting for the owner's classification (``cash_events``).
CASH_UNEXPLAINED = "cash_unexplained"
#: A fill since the anchor whose cash cannot be computed (an instrument nobody describes).
CASH_UNPRICED = "cash_unpriced"

type Reads = tuple[LiveOrders, tuple[Fill, ...], PositionsRead, CashRead]


def _freeze(kind: str, detail: str) -> ReconcileFinding:
    return ReconcileFinding(kind=kind, detail=detail, freezes=True)


def _note(kind: str, detail: str) -> ReconcileFinding:
    return ReconcileFinding(kind=kind, detail=detail, freezes=False)


def _recorded(
    ledger: Sequence[CashFlow], since: datetime, *, same_second: bool
) -> dict[str, Decimal]:
    """The owner's flows the broker's cash showed after a read at ``since``, by currency: a
    deposit, a withdrawal, a classified adjustment. ``same_second``: one stamped in the read's
    own second counts as after it."""
    moved: dict[str, Decimal] = {}
    for flow in ledger:
        stamp = flow.matched_at
        if not flow.live or stamp is None:
            continue
        if stamp > since or (same_second and stamp == since):
            moved[flow.currency] = moved.get(flow.currency, ZERO) + flow.signed
    return moved


def _after(fill: Fill, since: datetime, *, same_second: bool) -> bool:
    """Whether a fill came after a read at ``since``; ``same_second``: a fill stamped in the
    read's own second counts as after it (second-resolution stamps cannot order them)."""
    if fill.simulated:
        return False
    return fill.executed_at >= since if same_second else fill.executed_at > since


def _signed(fill: Fill, since: datetime, *, same_second: bool) -> Decimal:
    """A fill's effect on a position read at ``since``."""
    if not _after(fill, since, same_second=same_second):
        return ZERO
    return fill.quantity if fill.side is OrderSide.BUY else -fill.quantity


class Reconciler:
    """Compares the broker with the store, records the run and freezes new risk on drift."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        boundary: WriteBoundary,
        worker: StoreWorker,
        reader: AccountReader,
        directory: InstrumentDirectory,
        alert: OrderAlertHook | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._boundary = boundary
        self._worker = worker
        self._reader = reader
        self._directory = directory
        self._alert = alert
        self._now = now
        config = settings.execution.reconcile
        self._grace = timedelta(seconds=config.grace_seconds)
        self._tolerance = config.cash_tolerance_base
        self._pairs = tuple(settings.execution.currency.pairs)

    # ── reading ──

    async def _read(self) -> Reads | str:
        try:
            live = await self._reader.live_orders()
            trades = await self._reader.trades()
            positions = await self._reader.positions()
            cash = await self._reader.cash()
        except (IbkrError, ObliskError) as exc:
            return f"the broker could not be read: {exc}"
        if not live.complete:
            return "the live orders are not a complete snapshot"
        return live, trades, positions, cash

    async def _snapshot(self, positions: PositionsRead, cash: CashRead) -> tuple[int, int]:
        positions_read = await self._worker.run(
            repo.record_positions, positions.positions, observed_at=positions.observed_at
        )
        balances = [
            CashBalance(
                currency=line.currency,
                cash=line.cash,
                settled_cash=None,
                observed_at=cash.observed_at,
            )
            for line in cash.lines
        ]
        cash_read = await self._worker.run(repo.record_cash, balances, observed_at=cash.observed_at)
        return positions_read, cash_read

    # ── a run ──

    async def run(self) -> ReconcileRun:
        """One reconciliation, recorded; it alerts unless green."""
        reads = await self._read()
        if isinstance(reads, str):
            return await self._record(
                ReconcileStatus.INCOMPLETE, [_freeze("broker_unreadable", reads)]
            )
        live, trades, positions, cash = reads
        now = self._now()
        anchor = await self._worker.run(repo.reconciliation_anchor)
        await self._worker.run(repo.upsert_fills, list(trades))
        await self._boundary.record_orders(list(live.orders))
        findings = await self._orders(live, now)
        findings += await self._fills(live)
        for position in positions.positions:
            if position.quantity != 0:
                findings += await self.register(position.conid)
        findings += [
            _freeze(
                "negative_cash", f"{line.currency} is {line.cash}: a negative balance is a loan"
            )
            for line in cash.lines
            if line.cash < 0
        ]
        if anchor is not None:
            since = anchor[0].observed_at
            fills = await self._worker.run(repo.fills_between, since=since, as_of=now)
            findings += await self._foreign(fills, since)
            findings += await self._positions(positions, anchor[0], fills, now)
            findings += await self._cash(cash, anchor[1], fills, now)
        drift = any(finding.freezes for finding in findings)
        settled = not any(finding.kind in SETTLING for finding in findings)
        reads_taken = await self._snapshot(positions, cash)
        return await self._record(
            ReconcileStatus.DRIFT if drift else ReconcileStatus.GREEN,
            findings,
            reads=reads_taken,
            anchor=not drift and settled,
        )

    async def accept(self, *, actor: str, reason: str) -> ReconcileRun:
        """The owner accepts the broker's state as it is now: a green run whose reads are the new
        anchor. An incomplete run when the broker cannot be read whole."""
        reads = await self._read()
        if isinstance(reads, str):
            return await self._record(
                ReconcileStatus.INCOMPLETE, [_freeze("broker_unreadable", reads)]
            )
        live, trades, positions, cash = reads
        await self._worker.run(repo.upsert_fills, list(trades))
        await self._boundary.record_orders(list(live.orders))
        reads_taken = await self._snapshot(positions, cash)
        return await self._record(
            ReconcileStatus.GREEN,
            [_note("accepted", f"the broker's state accepted by {actor}: {reason}")],
            reads=reads_taken,
            anchor=True,
        )

    async def _record(
        self,
        status: ReconcileStatus,
        findings: Sequence[ReconcileFinding],
        *,
        reads: tuple[int, int] | None = None,
        anchor: bool = False,
    ) -> ReconcileRun:
        run = ReconcileRun(at=self._now(), status=status, findings=tuple(findings))
        await self._worker.run(
            repo.record_reconciliation,
            run,
            positions_read=None if reads is None else reads[0],
            cash_read=None if reads is None else reads[1],
            anchor=anchor,
        )
        if status is not ReconcileStatus.GREEN:
            detail = "; ".join(f"{f.kind}: {f.detail}" for f in findings if f.freezes)
            raise_alert(
                self._alert,
                OrderAlert(
                    kind=OrderAlertKind.RECONCILIATION_DRIFT,
                    intent_id=None,
                    detail=f"reconciliation {status.value}, new risk frozen: {detail}",
                    at=run.at,
                ),
            )
        return run

    async def _recent(self, intent_id: UUID, now: datetime) -> bool:
        """Whether the intent moved within the grace period."""
        history = await self._worker.run(repo.intent_history, intent_id)
        return bool(history) and now - history[-1].at <= self._grace

    # ── orders and fills ──

    async def _orders(self, live: LiveOrders, now: datetime) -> list[ReconcileFinding]:
        findings: list[ReconcileFinding] = []
        listed = {order.coid for order in live.orders if order.coid is not None}
        stored = await self._worker.run(repo.intents_in_states, BROKER_STATES)
        filled = await self._worker.run(repo.fills_by_coid, [s.coid for s in stored])
        for entry in stored:
            intent = entry.intent
            if intent.mode is ExecutionMode.SHADOW or entry.coid in listed:
                continue
            if entry.coid in filled or await self._recent(intent.intent_id, now):
                continue
            if await self._settled_by_status(intent.intent_id):
                continue
            findings.append(
                _freeze(
                    "order_missing",
                    f"intent {intent.intent_id} ({intent.state.value}) is not listed at the "
                    "broker, has no fills and no final status",
                )
            )
        for order in live.orders:
            if not is_live(order):
                continue
            known = (
                None
                if order.coid is None
                else await self._worker.run(repo.intent_by_coid, order.coid)
            )
            if known is None:
                findings.append(
                    _freeze("foreign_order", f"order {order.order_id} was not placed by the bot")
                )
        return findings

    async def _settled_by_status(self, intent_id: UUID) -> bool:
        """An unlisted intent's order, read by its id: a final status settles the intent."""
        orders = await self._worker.run(repo.broker_orders_for_intent, intent_id)
        if not orders:
            return False
        try:
            order = await self._boundary.sync_order(orders[-1].order_id)
        except ObliskError as exc:
            log.warning("an order's status cannot be read", extra={"error": str(exc)})
            return False
        return not is_live(order)

    async def _fills(self, live: LiveOrders) -> list[ReconcileFinding]:
        coids = [order.coid for order in live.orders if order.coid is not None]
        recorded = await self._worker.run(repo.fills_by_coid, coids)
        findings = []
        for order in live.orders:
            if order.coid is None or (order.sec_type or "").upper() == CONVERSION_SEC_TYPE:
                continue  # a conversion's fills are in the pair's base currency, not its size
            have = sum((fill.quantity for fill in recorded.get(order.coid, [])), ZERO)
            if (order.filled_quantity or ZERO) > have:
                findings.append(
                    _freeze(
                        "fills_unreconciled",
                        f"order {order.order_id} reports {order.filled_quantity} filled, the "
                        f"store {have}",
                    )
                )
        return findings

    async def _foreign(self, fills: Sequence[Fill], since: datetime) -> list[ReconcileFinding]:
        findings = []
        for fill in fills:
            if not _after(fill, since, same_second=True):
                continue
            known = (
                None
                if fill.coid is None
                else await self._worker.run(repo.intent_by_coid, fill.coid)
            )
            if known is None:
                findings.append(
                    _freeze(
                        "foreign_fill",
                        f"execution {fill.execution_id} on conid {fill.conid} was not the bot's",
                    )
                )
        return findings

    # ── positions ──

    async def _positions(
        self,
        positions: PositionsRead,
        anchor: AccountRead[Position],
        fills: Sequence[Fill],
        now: datetime,
    ) -> list[ReconcileFinding]:
        findings: list[ReconcileFinding] = []
        since = anchor.observed_at
        conids = {p.conid for p in positions.positions} | {row.conid for row in anchor.rows}
        for conid in sorted(conids):
            before = sum((row.quantity for row in anchor.rows if row.conid == conid), ZERO)
            own = [fill for fill in fills if fill.conid == conid]
            expected = {
                before + sum((_signed(f, since, same_second=same) for f in own), ZERO)
                for same in (False, True)
            }
            held = positions.quantity(conid)
            if held in expected:
                continue
            shown = " or ".join(str(value) for value in sorted(expected))
            if await self._settling(conid, own, now):
                findings.append(
                    _note("position_settling", f"conid {conid}: {held} held, {shown} expected")
                )
                continue
            findings.append(
                _freeze(
                    "position_mismatch",
                    f"conid {conid}: the broker holds {held}, the store expects {shown}",
                )
            )
        return findings

    async def _settling(self, conid: int, fills: Sequence[Fill], now: datetime) -> bool:
        """Whether a difference may still be IBKR's reporting lag: a fill or an order of the
        instrument moved within the grace period."""
        if any(now - fill.executed_at <= self._grace for fill in fills):
            return True
        intents = await self._worker.run(repo.intents_for_conid, conid)
        for stored in intents:
            if await self._recent(stored.intent.intent_id, now):
                return True
        return False

    async def register(self, conid: int) -> list[ReconcileFinding]:
        """A held conid no sleeve owns: assigned to the sleeve whose entry filled it (a new
        holding, on its first fill), else ``unmanaged`` (the bot did not open it)."""
        if await self._worker.run(repo.holding_assignment, conid) is not None:
            return []
        intents = await self._worker.run(repo.intents_for_conid, conid)
        owner: HoldingOwner = UNMANAGED
        for stored in reversed(intents):
            intent = stored.intent
            fills = await self._worker.run(repo.fills_for_intent, intent.intent_id)
            if intent.role is IntentRole.ENTRY and fills:
                owner = intent.sleeve
                break
        await self._worker.run(
            repo.assign_holding,
            HoldingAssignment(
                conid=conid,
                owner=owner,
                assigned_by=RECONCILE_ACTOR,
                assigned_at=self._now(),
                reason="found held with no owner",
            ),
        )
        return [_note("holding_registered", f"conid {conid} assigned to {owner}")]

    # ── cash ──

    async def _cash(
        self,
        cash: CashRead,
        anchor: AccountRead[CashBalance],
        fills: Sequence[Fill],
        now: datetime,
    ) -> list[ReconcileFinding]:
        since = anchor.observed_at
        flows = [self._flows(fills, since, same_second=same) for same in (False, True)]
        known = [flow for flow in flows if flow is not None]
        if len(known) != len(flows):
            return [_freeze(CASH_UNPRICED, "a fill since the anchor cannot be priced in cash")]
        ledger = await self._worker.run(capital_repo.cash_flows)
        # A flow stamped in the anchor's own second may or may not be in it: both are accepted.
        recorded = [_recorded(ledger, since, same_second=same) for same in (False, True)]
        before = {row.currency: row.cash for row in anchor.rows}
        currencies = set(before) | {line.currency for line in cash.lines}
        currencies |= {currency for flow in known for currency in flow}
        currencies |= {currency for moved in recorded for currency in moved}
        recent = any(now - fill.executed_at <= self._grace for fill in fills)
        findings: list[ReconcileFinding] = []
        for currency in sorted(currencies):
            line = cash.line(currency)
            held = ZERO if line is None else line.cash
            expected = [
                before.get(currency, ZERO) + moved.get(currency, ZERO) + flow.get(currency, ZERO)
                for moved in recorded
                for flow in known
            ]
            rate = None if line is None else line.rate_to_base
            tolerance = ZERO if rate is None or rate <= 0 else self._tolerance / rate
            if any(abs(held - value) <= tolerance for value in expected):
                continue
            detail = (
                f"{currency}: {held} held, {min(expected):.2f} to {max(expected):.2f} expected "
                "from the anchor, the bot's fills and the recorded flows"
            )
            if recent:
                findings.append(_note("cash_settling", detail))
                continue
            gap = held - (max(expected) if held > max(expected) else min(expected))
            matched = await self._match(ledger, currency, gap, tolerance, cash.observed_at)
            if matched is not None:
                findings.append(_note("cash_flow_arrived", f"{detail}: {matched}"))
                continue
            event = await self._worker.run(
                capital_repo.note_unexplained, currency, gap, at=cash.observed_at
            )
            findings.append(
                _freeze(
                    CASH_UNEXPLAINED,
                    f"{detail}: E{event.event_id}, {gap:+.2f} {currency} nothing explains; new "
                    f"buys wait until it is classified (`obliskiq cash classify "
                    f"E{event.event_id} <kind> --confirm`)",
                )
            )
        return findings

    async def _match(
        self,
        ledger: Sequence[CashFlow],
        currency: str,
        gap: Decimal,
        tolerance: Decimal,
        at: datetime,
    ) -> str | None:
        """Match an unexplained difference to the owner's recorded flow of that amount (a
        deposit that arrived, a withdrawal that left), stamping it; what was matched."""
        kind = Deposit if gap > ZERO else Withdrawal
        for flow in ledger:
            if (
                isinstance(flow, kind)
                and flow.live
                and flow.matched_at is None
                and flow.currency == currency
                and flow.flow_id is not None
                and abs(flow.signed - gap) <= tolerance
            ):
                await self._worker.run(capital_repo.match_flow, flow.flow_id, at=at)
                return f"{flow.kind.value} F{flow.flow_id} of {flow.amount} {currency} matched"
        return None

    def _flows(
        self, fills: Sequence[Fill], since: datetime, *, same_second: bool
    ) -> dict[str, Decimal] | None:
        """The cash the fills after ``since`` moved, per currency; ``None`` when a fill cannot be
        priced (an instrument nobody describes)."""
        flows: dict[str, Decimal] = {}
        for fill in fills:
            if not _after(fill, since, same_second=same_second):
                continue
            moved = fill_cash(fill, directory=self._directory, pairs=self._pairs)
            if moved is None:
                return None
            for currency, amount in moved.items():
                flows[currency] = flows.get(currency, ZERO) + amount
        return flows
