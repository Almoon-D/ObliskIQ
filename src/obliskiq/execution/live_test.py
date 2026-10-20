"""``obliskiq preflight --execution-test``: Session 5's live steps on the paper account
(SESSION_PROMPTS Session 5 item 6), a command the owner runs on the deployment machine (§A.5).

**Phase one** (``--execution-test``):

(a) a conversion from the base currency into the test instrument's currency: a whatif of it, then
    one conversion of ``execution_test.conversion_base``, its fill and how both cash balances moved;
(b) a buy of the contract's size increment at the touch (inside the collar), its fill, and its
    protective stop-limit placed and listed at the broker, resting GTC;
(d) how long after the fill IBKR showed it in the live orders, the trades and the positions.

**Phase two** (``--execution-test --finish``, a new process: the stop must survive the restart):
the stop still listed, then the position sold at the touch through the exit coordinator, which
cancels the stop; the position and the stop gone.

(c) IBKR's attached orders (a child stop-limit linked by ``parentId``) are not probed: placing one
needs a clearance for a package the gate does not issue (DECISIONS D99).

Every write goes through the pre-trade gate and the write boundary, in mode ``paper`` only. The
report prints statuses, message ids and the test's own amounts (what moved), never an account id,
a balance or a position held before the test.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal

from ..domain.instrument import TradedInstrument
from ..domain.orders import (
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderSide,
    OrderTerms,
    ProtectiveSpec,
    QuoteEvidence,
)
from ..errors import ObliskError
from ..ibkr.identity import resolve_identity
from ..ibkr.models import ContractRules
from ..ibkr.orders import SMART_ROUTER, PlacementStatus, is_live
from ..store import orders as repo
from ..store.orders import StoredIntent
from ..util.timeutil import utc_now
from .account import QuoteRead
from .intents import new_intent
from .paper_test import TEST_SLEEVE, PaperOrderReport, _probe_id
from .pretrade import PretradeRefusal
from .pricing import tick_for, to_tick
from .protective import limit_offset
from .quotes import QuoteCheck, QuotePolicy
from .runtime import OrderPath
from .state import BROKER_STATES

type Sleep = Callable[[float], Awaitable[None]]

#: The exit reason the test's sale gives (lite's `exit_reasons_allowed`): the owner's command.
FINISH_REASON = "override"

#: Where a fill shows, in the order item (d) measures IBKR's reporting lag.
LAG_READS = ("live orders", "trades", "positions")


class ExecutionTest:
    """Runs Session 5's live steps over an order path."""

    def __init__(
        self,
        path: OrderPath,
        *,
        now: Callable[[], datetime] = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._path = path
        self._now = now
        self._sleep = sleep
        config = path.settings.ibkr.preflight.execution_test
        self._conid = config.conid
        self._conversion = config.conversion_base
        self._stop_fraction = config.stop_fraction
        self._timeout = config.fill_timeout_seconds
        self._poll = config.poll_seconds
        self._ttl = timedelta(minutes=config.intent_ttl_minutes)
        self._base = path.settings.account.base_currency
        self._quotes = QuotePolicy(path.settings)
        # The annual volatility over the root of the sessions in a year is the daily one.
        self._sessions = Decimal(path.settings.execution.collar.sessions_per_year)

    async def _ready(self, report: PaperOrderReport) -> TradedInstrument | None:
        try:
            mode = await self._path.modes.current()
        except ObliskError as exc:
            report.add("mode", False, f"the mode cannot be read: {exc}")
            return None
        if not report.add(
            "mode", mode is ExecutionMode.PAPER, f"{mode.value} (the test needs paper)"
        ):
            return None
        instrument = self._path.directory.describe(self._conid)
        if instrument is None:  # pragma: no cover - the schema ties the conid to the allowlist
            report.add("instrument", False, f"{self._conid} is not described")
        return instrument

    # ── phase one ──

    async def run(self) -> PaperOrderReport:
        report = PaperOrderReport()
        instrument = await self._ready(report)
        if instrument is None:
            return report
        try:
            await self._phase_one(instrument, report)
        except ObliskError as exc:
            report.add("stopped", False, str(exc))
        return report

    async def _phase_one(self, instrument: TradedInstrument, report: PaperOrderReport) -> None:
        path = self._path
        identity = await resolve_identity(
            path.client.secdef,
            instrument.symbol,
            listing_exchange=instrument.listing_exchange,
            currency=instrument.currency,
        )
        if not report.add(
            f"{instrument.symbol} identity",
            identity.conid == instrument.conid,
            f"IBKR resolves conid {identity.conid}; the allowlist says {instrument.conid}",
        ):
            return
        if instrument.currency != self._base and not await self._convert(instrument, report):
            return
        report.add("(c) attached orders", True, "not probed: no package clearance (D99)")
        await self._buy(instrument, report)

    async def _convert(self, instrument: TradedInstrument, report: PaperOrderReport) -> bool:
        path = self._path
        target = instrument.currency
        terms = await path.currency.conversion_terms(self._base, target, self._conversion)
        if isinstance(terms, str):
            return report.add("(a) conversion", False, terms)
        whatif = await path.orders.whatif(terms, coid=path.orders.coid_for(_probe_id()))
        report.add(
            "(a) conversion whatif",
            whatif.error is None,
            f"{terms.side.value} conid {terms.conid} fxQty {terms.cash_quantity} at most "
            f"{terms.price.limit}; amount {whatif.amount} {whatif.currency or ''}, commission "
            f"{whatif.commission}; warning {whatif.warning_code or 'none'}"
            + (f"; error {whatif.error}" if whatif.error else ""),
        )
        before = await path.reader.cash()
        outcome = await path.currency.convert(
            self._base, target, self._conversion, mode=ExecutionMode.PAPER, reason="execution test"
        )
        if not report.add(
            "(a) conversion", outcome.status == "filled", f"{outcome.status}: {outcome.detail}"
        ):
            return False
        after = await path.reader.cash()
        moved = []
        for currency in (self._base, target):
            old, new = before.line(currency), after.line(currency)
            change = (Decimal(0) if new is None else new.cash) - (
                Decimal(0) if old is None else old.cash
            )
            moved.append(f"{currency} {change:+.2f}")
        trades = await path.reader.trades()
        stored = None if outcome.intent_id is None else await path.book.get(outcome.intent_id)
        fills = [f for f in trades if stored is not None and f.coid == stored.coid]
        report.add(
            "(a) both balances",
            True,
            f"{', '.join(moved)}; fills {[(str(f.quantity), str(f.price)) for f in fills]}, "
            f"commission {[str(f.commission) for f in fills]}",
        )
        return True

    async def _quote(
        self, instrument: TradedInstrument
    ) -> tuple[QuoteRead, ContractRules, Decimal] | str:
        path = self._path
        read = await path.reader.quote(instrument.conid, option=False)
        rules = await path.client.contracts.rules(instrument.conid, is_buy=True)
        if read is None or read.evidence.reference() is None:
            return "no quote"
        tick = tick_for(rules, read.evidence.reference() or Decimal(0))
        if tick is None:
            return "the contract's rules give no tick"
        return read, rules, tick

    def _touch(
        self, side: OrderSide, quote: QuoteEvidence, sigma: float | None, tick: Decimal
    ) -> Decimal | None:
        """The touch on a whole tick, never past the collar."""
        band = self._quotes.band(quote, sigma=sigma)
        if isinstance(band, QuoteCheck):
            return None
        if side is OrderSide.BUY:
            touch = quote.ask or band.reference
            return min(to_tick(touch, tick, up=True), to_tick(band.upper, tick, up=False))
        touch = quote.bid or band.reference
        return max(to_tick(touch, tick, up=False), to_tick(band.lower, tick, up=True))

    async def _buy(self, instrument: TradedInstrument, report: PaperOrderReport) -> None:
        path = self._path
        found = await self._quote(instrument)
        if isinstance(found, str):
            report.add("(b) buy", False, found)
            return
        read, rules, tick = found
        size = rules.size_increment
        limit = self._touch(OrderSide.BUY, read.evidence, read.sigma, tick)
        if limit is None or size is None or size <= 0 or not math.isfinite(size):
            report.add("(b) buy", False, f"no limit inside the collar or no size ({size})")
            return
        daily = None if read.sigma is None else Decimal(repr(read.sigma)) / self._sessions.sqrt()
        offset, basis = limit_offset(path.settings, daily_sigma=daily, gap_quantile=None)
        stop = to_tick(limit * (1 - self._stop_fraction), tick, up=False)
        spec = ProtectiveSpec(stop=stop, limit_offset_fraction=offset, basis=basis)
        terms = OrderTerms(
            conid=instrument.conid,
            side=OrderSide.BUY,
            quantity=Decimal(repr(size)),
            price=LimitPrice(limit=limit),
            tif=path.settings.execution.flatten.tif,
            venue=SMART_ROUTER,
        )
        placed = await self._place(terms, spec, report)
        if placed is None:
            return
        stored, order_id = placed
        if not await self._await_fill(stored.coid, order_id, report, "(b)"):
            return
        await path.reconciler.register(instrument.conid)
        outcome = await path.protective.protect(instrument.conid)
        report.add(
            "(b) protective stop", outcome.action == "placed", f"{outcome.action}: {outcome.detail}"
        )
        live = await path.orders.live_orders()
        stops = [
            o
            for o in live.orders
            if o.conid == instrument.conid and o.side is OrderSide.SELL and is_live(o)
        ]
        report.add(
            "(b) stop at the broker",
            bool(stops),
            "; ".join(
                f"order {o.order_id} {o.order_type} {o.remaining_quantity} {o.tif}" for o in stops
            )
            or "no working sell listed",
        )
        report.add(
            "phase one done",
            True,
            "stop this process, then run `obliskiq preflight --execution-test --finish`",
        )

    async def _place(
        self, terms: OrderTerms, spec: ProtectiveSpec, report: PaperOrderReport
    ) -> tuple[StoredIntent, str] | None:
        """The test's entry, protected by ``spec``: cleared, placed; its order id."""
        path = self._path
        now = self._now()
        intent = new_intent(
            path.settings,
            sleeve=TEST_SLEEVE,
            role=IntentRole.ENTRY,
            terms=terms,
            reason="execution test",
            mode=ExecutionMode.PAPER,
            approval_required=False,
            created_at=now,
            expires_at=now + self._ttl,
            protective=spec,
        )
        stored = await path.book.add(intent)
        for state in (IntentState.APPROVED, IntentState.STAGED):
            await path.book.advance(intent.intent_id, state, "execution test")
        clearance = await path.gate.clear(intent)
        if isinstance(clearance, PretradeRefusal):
            await path.book.advance(intent.intent_id, IntentState.REJECTED, clearance.reason)
            report.add("(b) buy", False, f"refused: {clearance.reason}")
            return None
        result = await path.boundary.place(intent.intent_id, clearance)
        ids = [", ".join(reply.message_ids) or "no id" for reply in result.replies]
        placed = result.status is PlacementStatus.PLACED and result.order_id is not None
        report.add(
            "(b) buy",
            placed,
            f"{result.status.value} {terms.quantity} at {terms.price.limit}, stop {spec.stop} "
            f"({spec.basis}); replies {'; '.join(ids) or 'none'}"
            + (f"; {result.detail}" if result.detail else ""),
        )
        if not placed or result.order_id is None:
            return None
        return stored, result.order_id

    async def _await_fill(
        self, coid: str, order_id: str, report: PaperOrderReport, step: str
    ) -> bool:
        """Wait for the fill; report when the live orders, the trades and the positions showed it
        (item (d): IBKR's reporting lag)."""
        path = self._path
        started = self._now()
        seen: dict[str, float] = {}
        filled_at: datetime | None = None
        held_before = (await path.reader.positions()).quantity(self._conid)
        while True:
            order = await path.boundary.sync_order(order_id)
            now = self._now()
            if not is_live(order) and filled_at is None:
                filled_at = now
                seen[LAG_READS[0]] = (now - started).total_seconds()
            if filled_at is not None:
                trades = await path.reader.trades()
                if LAG_READS[1] not in seen and any(f.coid == coid for f in trades):
                    seen[LAG_READS[1]] = (now - filled_at).total_seconds()
                    await path.worker.run(repo.upsert_fills, list(trades))
                positions = await path.reader.positions()
                if LAG_READS[2] not in seen and positions.quantity(self._conid) != held_before:
                    seen[LAG_READS[2]] = (now - filled_at).total_seconds()
            if len(seen) == len(LAG_READS):
                break
            if (now - started).total_seconds() >= self._timeout:
                break
            await self._sleep(self._poll)
        report.add(
            f"{step} fill",
            filled_at is not None and order.status.strip().lower() == "filled",
            f"order {order_id} {order.status}",
        )
        report.add(
            f"(d) reporting lag {step}",
            len(seen) == len(LAG_READS),
            ", ".join(f"{what} after {seconds:.0f} s" for what, seconds in seen.items())
            or "nothing seen",
        )
        return filled_at is not None and order.status.strip().lower() == "filled"

    # ── phase two ──

    async def finish(self) -> PaperOrderReport:
        report = PaperOrderReport()
        instrument = await self._ready(report)
        if instrument is None:
            return report
        try:
            await self._phase_two(instrument, report)
        except ObliskError as exc:
            report.add("stopped", False, str(exc))
        return report

    async def _phase_two(self, instrument: TradedInstrument, report: PaperOrderReport) -> None:
        path = self._path
        working = await path.worker.run(repo.intents_in_states, BROKER_STATES)
        stops = [
            s
            for s in working
            if s.intent.role is IntentRole.PROTECTIVE and s.intent.conid == instrument.conid
        ]
        live = await path.orders.live_orders()
        listed = {o.coid for o in live.orders if is_live(o)}
        survived = [s for s in stops if s.coid in listed]
        if not report.add(
            "(b) the stop after a restart",
            bool(survived),
            f"{len(survived)} of {len(stops)} protective stops still listed",
        ):
            return
        found = await self._quote(instrument)
        if isinstance(found, str):
            report.add("(b) sale", False, found)
            return
        read, _, tick = found
        held = (await path.reader.positions()).quantity(instrument.conid)
        limit = self._touch(OrderSide.SELL, read.evidence, read.sigma, tick)
        if held <= 0 or limit is None:
            report.add("(b) sale", False, f"nothing to sell ({held}) or no limit")
            return
        now = self._now()
        intent = new_intent(
            path.settings,
            sleeve=TEST_SLEEVE,
            role=IntentRole.EXIT,
            terms=OrderTerms(
                conid=instrument.conid,
                side=OrderSide.SELL,
                quantity=held,
                price=LimitPrice(limit=limit),
                tif=path.settings.execution.flatten.tif,
                venue=SMART_ROUTER,
            ),
            reason=FINISH_REASON,
            mode=ExecutionMode.PAPER,
            approval_required=False,
            created_at=now,
            expires_at=now + self._ttl,
        )
        stored = await path.book.add(intent)
        for state in (IntentState.APPROVED, IntentState.STAGED):
            await path.book.advance(intent.intent_id, state, "execution test")
        result = await path.coordinator.place_exit(intent.intent_id)
        cancelled = [r for r in result.resized if r.resized_quantity == 0]
        order_id = None if result.placement is None else result.placement.order_id
        if (
            not report.add(
                "(b) sale",
                result.placed and order_id is not None,
                f"{result.detail}; {len(cancelled)} stop(s) cancelled beside it"
                + (f"; {result.refusal.reason}" if result.refusal else ""),
            )
            or order_id is None
        ):
            return
        await self._await_fill(stored.coid, order_id, report, "(b) sale")
        live = await path.orders.live_orders()
        left = [o for o in live.orders if o.conid == instrument.conid and is_live(o)]
        report.add(
            "(b) nothing left working",
            not left,
            "no working order on the instrument" if not left else f"{len(left)} still working",
        )
