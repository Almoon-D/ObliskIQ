"""``obliskiq preflight --paper-order-test``: one paper order per instrument, end to end.

For each conid in ``ibkr.preflight.paper_order.conids`` (each a described tradable candidate of
the universe): confirm
the contract through IBKR's contract search, read its trading rules and the algos offered, read
a quote, preview the order (whatif), then make an entry intent buying the minimum quantity at a
limit that cannot fill (``limit_offset_fraction`` below the bid, rounded down to the contract's
tick), clear it through the pre-trade gate, place it through the write boundary, read its status
and the live orders (is the cOID listed?), and cancel it. Every reply message id is recorded.

An instrument quoted in a currency the account does not hold is refused by the gate's cash check
(``currency_not_held``): that refusal is the expected, recorded outcome, not a failure. The test
needs the mode ``paper`` (``obliskiq mode set paper``); it writes nothing in any other mode.

The report prints statuses, ids of IBKR's messages and the algos, never an account id, a balance
or a position.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument
from ..domain.orders import (
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderSide,
    OrderTerms,
    Sleeve,
    TimeInForce,
)
from ..errors import ObliskError
from ..ibkr.identity import resolve_identity
from ..ibkr.orders import SMART_ROUTER, PlacementStatus
from ..ibkr.redact import redact
from ..util.timeutil import utc_now
from .intents import new_intent
from .pretrade import PretradeRefusal, Reason
from .pricing import tick_for, to_tick
from .runtime import OrderPath

#: The cash-check refusal expected for an instrument in a currency the account does not hold.
EXPECTED_CASH_REFUSALS = frozenset({Reason.CURRENCY_NOT_HELD.value})

#: The paper order test buys for the core sleeve: a label, the order is cancelled at once.
TEST_SLEEVE = Sleeve.CORE


@dataclass
class PaperStep:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"  [{mark}] {self.name}{': ' + self.detail if self.detail else ''}"


@dataclass
class PaperOrderReport:
    steps: list[PaperStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append(PaperStep(name, ok, detail))
        return ok

    def render(self, known_ids: tuple[str, ...] = ()) -> str:
        lines = ["IBKR paper order test:", ""]
        lines += [redact(step.line(), known_ids) for step in self.steps]
        lines.append("")
        lines.append("Every step passed." if self.ok else "Some steps failed (see above).")
        return "\n".join(lines)


def passive_limit(bid: Decimal, offset: Decimal, tick: Decimal) -> Decimal | None:
    """``offset`` below the bid, rounded down to a whole tick; ``None`` when that is not a price."""
    limit = to_tick(bid * (1 - offset), tick, up=False)
    return limit if limit > 0 else None


class PaperOrderTest:
    """Runs the paper order test over an order path."""

    def __init__(self, path: OrderPath, *, now: Callable[[], datetime] = utc_now) -> None:
        self._path = path
        self._now = now
        config = path.settings.ibkr.preflight.paper_order
        self._conids = config.conids
        self._offset = config.limit_offset_fraction
        self._ttl = timedelta(minutes=config.intent_ttl_minutes)

    async def run(self) -> PaperOrderReport:
        report = PaperOrderReport()
        try:
            mode = await self._path.modes.current()
        except ObliskError as exc:
            report.add("mode", False, f"the mode cannot be read: {exc}")
            return report
        if mode is not ExecutionMode.PAPER:
            report.add(
                "mode", False, f"the mode is {mode.value}; run `obliskiq mode set paper` first"
            )
            return report
        report.add("mode", True, "paper")
        for conid in self._conids:
            instrument = self._path.directory.describe(conid)
            if instrument is None:  # pragma: no cover - the schema ties the conids to the universe
                report.add(f"{conid}", False, "no universe instrument describes it")
                continue
            try:
                await self._one(instrument, report)
            except ObliskError as exc:
                report.add(f"{instrument.symbol} stopped", False, str(exc))
        return report

    async def _terms(
        self, instrument: TradedInstrument, report: PaperOrderReport
    ) -> OrderTerms | None:
        client = self._path.client
        name = instrument.symbol
        identity = await resolve_identity(
            client.secdef,
            instrument.symbol,
            listing_exchange=instrument.listing_exchange,
            currency=instrument.currency,
        )
        if not report.add(
            f"{name} identity",
            identity.conid == instrument.conid,
            f"IBKR resolves {name} on {instrument.listing_exchange} to conid {identity.conid}; "
            f"the allowlist says {instrument.conid}",
        ):
            return None
        rules = await client.contracts.rules(instrument.conid, is_buy=True)
        algos = await client.contracts.algos(instrument.conid)
        report.add(
            f"{name} algos", True, ", ".join(algo.algo_id for algo in algos) or "none offered"
        )
        size = rules.size_increment
        read = await self._path.reader.quote(instrument.conid, option=False)
        bid = None if read is None else read.evidence.bid
        tick = None if bid is None else tick_for(rules, bid)
        limit = None if bid is None or tick is None else passive_limit(bid, self._offset, tick)
        if size is None or size <= 0 or limit is None or not math.isfinite(size):
            report.add(
                f"{name} pricing",
                False,
                f"size increment {size}, bid {bid}, tick {tick}: no passive limit",
            )
            return None
        status = (
            "unreported"
            if read is None or read.evidence.status is None
            else (read.evidence.status.value)
        )
        report.add(f"{name} quote", True, f"{status}; limit {limit} ({self._offset} below bid)")
        return OrderTerms(
            conid=instrument.conid,
            side=OrderSide.BUY,
            quantity=Decimal(repr(size)),
            price=LimitPrice(limit=limit),
            tif=TimeInForce.DAY,
            venue=SMART_ROUTER,
        )

    async def _one(self, instrument: TradedInstrument, report: PaperOrderReport) -> None:
        path = self._path
        name = instrument.symbol
        terms = await self._terms(instrument, report)
        if terms is None:
            return
        whatif = await path.orders.whatif(terms, coid=path.orders.coid_for(_probe_id()))
        report.add(
            f"{name} whatif",
            whatif.error is None,
            f"commission {whatif.commission} {whatif.currency or ''}; initial margin change "
            f"{whatif.initial_margin.change}; warning {whatif.warning_code or 'none'}"
            + (f"; error {whatif.error}" if whatif.error else ""),
        )
        now = self._now()
        intent = new_intent(
            path.settings,
            sleeve=TEST_SLEEVE,
            role=IntentRole.ENTRY,
            terms=terms,
            reason="paper order test",
            mode=ExecutionMode.PAPER,
            approval_required=False,
            created_at=now,
            expires_at=now + self._ttl,
        )
        stored = await path.book.add(intent)
        for step in (IntentState.APPROVED, IntentState.STAGED):
            await path.book.advance(intent.intent_id, step, "paper order test")
        clearance = await path.gate.clear(intent)
        if isinstance(clearance, PretradeRefusal):
            expected = (
                instrument.currency != path.settings.account.base_currency
                and clearance.code in EXPECTED_CASH_REFUSALS
                and instrument.asset_class is AssetClass.STOCK
            )
            report.add(
                f"{name} pre-trade",
                expected,
                ("refused by the cash check, as expected: " if expected else "refused: ")
                + clearance.reason,
            )
            await path.book.advance(intent.intent_id, IntentState.REJECTED, clearance.reason)
            return
        report.add(f"{name} pre-trade", True, f"cleared ({clearance.effect.value})")
        result = await path.boundary.place(intent.intent_id, clearance)
        ids = [", ".join(reply.message_ids) or "no id" for reply in result.replies]
        placed = result.status is PlacementStatus.PLACED and result.order_id is not None
        report.add(
            f"{name} place",
            placed,
            f"{result.status.value}; order status {result.order_status}; replies "
            f"{'; '.join(ids) or 'none'}" + (f"; {result.detail}" if result.detail else ""),
        )
        if not placed or result.order_id is None:
            return
        order_id = result.order_id
        live = await path.orders.live_orders()
        listed = [order for order in live.orders if order.coid == stored.coid]
        report.add(
            f"{name} cOID",
            bool(listed),
            f"listed as order_ref ({len(stored.coid)} characters)"
            if listed
            else "the cOID is not listed in the live orders",
        )
        status = await path.boundary.sync_order(order_id)
        report.add(f"{name} status", True, status.status)
        answer = await path.boundary.cancel(order_id)
        after = await path.orders.status(order_id)
        report.add(
            f"{name} cancel",
            answer.error is None,
            f"{answer.message or answer.error}; then {after.status}",
        )


def _probe_id() -> uuid.UUID:
    """A fresh id for a preview's cOID, so the preview never touches the order's own."""
    return uuid.uuid7()
