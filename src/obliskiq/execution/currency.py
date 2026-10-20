"""The currency manager (MASTER_PLAN §5.12): a trade is funded in its own currency by a
limit-capped spot conversion shortly before it; foreign cash above what is needed is brought home.

* **Funding.** For the buys about to be placed in a currency other than the base: what they
  commit at their limits, plus the currency's working buffer (``buffer_base``), against its cash
  less what the open orders in it already commit, plus what conversions into it still working
  will bring. A shortfall is converted from the base currency in **one** conversion per currency
  for every buy of the batch (batching amortises IBKR's minimum commission), raised to
  ``min_conversion_base`` where the base cash and the single-foreign-currency cap leave room. A
  book under ``capital.mode: cap`` converts its shortfall alone: the account's cash beyond the cap
  is not the book's to convert (DECISIONS D104).
* **Repatriation.** A foreign currency no pending buy needs, whose cash beyond its buffer and its
  open commitments reaches ``repatriate_above_base``, is converted home.
* **Every conversion** is an intent (role ``conversion``) the pre-trade gate clears on its own
  path — the cash it converts from against every open commitment and the floor, the cap on what
  it buys — and the write boundary places, so a conversion adds risk and the kill switch refuses
  it. Its limit sits ``limit_fraction`` through the pair's reference rate, on the pair's tick
  away from the reference (the collar bounds it); its size is the amount it converts from
  (IBKR's ``fxQty``), enough at the limit to bring in what is needed. The trades it funds wait
  for its fill, at most ``fill_timeout_seconds``, and while it works no second conversion into the
  currency is made; one that is refused, rejected or cancelled leaves them unplaced and alerts.
* **Valuation.** Rates come from IBKR: the ledger's rate to the base currency, else the pair's own
  quote. ECB reference rates serve reporting only.
* In **shadow** mode nothing is converted (a shadow fill of a conversion is not simulated,
  DECISIONS D52): the trades wait.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import UUID

from ..config.schema import ObliskConfig
from ..config.schema_execution import CurrencyPair
from ..domain.orders import (
    TERMINAL_STATES,
    ExecutionMode,
    IntentRole,
    IntentState,
    LimitPrice,
    OrderIntent,
    OrderSide,
    OrderTerms,
    Sleeve,
    TimeInForce,
)
from ..errors import ObliskError
from ..ibkr.orders import SMART_ROUTER, PlacementStatus, is_live
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now
from .account import AccountReader, CashRead
from .boundary import OrderAlert, OrderAlertHook, OrderAlertKind, WriteBoundary, raise_alert
from .fx import ConversionLeg, conversion_leg, leg_for
from .instruments import InstrumentDirectory
from .intents import new_intent
from .modes import Modes
from .pretrade import PretradeGate, PretradeRefusal
from .pricing import tick_for, to_tick
from .protective import ContractRulesReader
from .state import BROKER_STATES, IntentBook

log = get_logger("execution.currency")

ZERO = Decimal(0)

#: States in which an intent may still become an order.
_PENDING = frozenset(IntentState) - TERMINAL_STATES

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ConversionOutcome:
    """One conversion: what it was for, and how it ended (``filled``, ``working`` or why not)."""

    currency: str
    intent_id: UUID | None
    amount: Decimal
    status: str
    detail: str


@dataclass
class FundingReport:
    """Which intents may be placed now (``ready``), which wait and why."""

    ready: set[UUID] = field(default_factory=set)
    waiting: dict[UUID, str] = field(default_factory=dict)
    conversions: list[ConversionOutcome] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Need:
    intent: OrderIntent
    currency: str
    amount: Decimal


class CurrencyManager:
    """Funds foreign-currency trades by conversion and brings excess foreign cash home."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        gate: PretradeGate,
        boundary: WriteBoundary,
        book: IntentBook,
        worker: StoreWorker,
        reader: AccountReader,
        directory: InstrumentDirectory,
        rules: ContractRulesReader,
        modes: Modes,
        alert: OrderAlertHook | None = None,
        now: Callable[[], datetime] = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._modes = modes
        self._gate = gate
        self._boundary = boundary
        self._book = book
        self._worker = worker
        self._reader = reader
        self._directory = directory
        self._rules = rules
        self._alert = alert
        self._now = now
        self._sleep = sleep
        self._base = settings.account.base_currency
        config = settings.execution.currency
        self._pairs = tuple(config.pairs)
        self._buffer = config.buffer_base
        self._minimum = config.min_conversion_base
        self._repatriate = config.repatriate_above_base
        self._limit_fraction = config.limit_fraction
        self._ttl = timedelta(minutes=config.intent_ttl_minutes)
        self._timeout = config.fill_timeout_seconds
        self._poll = config.fill_poll_seconds
        self._floor = settings.execution.pretrade.cash_floor
        self._cap = settings.positions.max_single_foreign_currency_pct
        self._capped_book = settings.capital.mode == "cap"

    # ── what is needed ──

    def _need(self, intent: OrderIntent) -> _Need | None:
        """What a buy commits at its limit, in its instrument's currency; ``None`` for anything
        that spends no foreign cash."""
        terms = intent.terms
        if terms.side is not OrderSide.BUY or terms.conversion:
            return None
        instrument = self._directory.describe(terms.conid)
        if instrument is None or instrument.currency == self._base:
            return None
        if terms.cash_quantity is not None:
            amount = terms.cash_quantity
        else:
            amount = terms.price.limit * (terms.quantity or ZERO) * instrument.multiplier
        return _Need(intent=intent, currency=instrument.currency, amount=amount)

    def _leg(self, source: str, target: str) -> ConversionLeg | None:
        """The leg of a configured pair that converts ``source`` into ``target``."""
        legs = (leg_for(pair, source=source, target=target) for pair in self._pairs)
        return next((leg for leg in legs if leg is not None), None)

    async def _open(self, currency: str, exclude: set[UUID]) -> tuple[Decimal, Decimal]:
        """What open orders commit in ``currency`` (buys, and conversions out of it) and what
        working conversions into it will bring, both by what they have not filled yet."""
        working = await self._worker.run(repo.intents_in_states, BROKER_STATES)
        committed, incoming = ZERO, ZERO
        for stored in working:
            intent = stored.intent
            if intent.intent_id in exclude:
                continue
            fills = await self._worker.run(repo.fills_for_intent, intent.intent_id)
            if intent.terms.conversion:
                leg = conversion_leg(self._pairs, intent.terms)
                if leg is None:
                    continue  # a pair no longer configured converts nothing here
                # A conversion is always sized in cash (OrderTerms).
                left = max(ZERO, (intent.terms.cash_quantity or ZERO) - leg.spent(fills))
                if leg.source == currency:
                    committed += left
                if leg.target == currency:
                    incoming += leg.received(left, intent.terms.price.limit)
                continue
            need = self._need(intent)
            if need is None or need.currency != currency:
                continue
            quantity = intent.terms.quantity
            filled = sum((fill.quantity for fill in fills), ZERO)
            spent = sum((fill.quantity * fill.price for fill in fills), ZERO)
            left = (
                need.amount - spent if quantity is None else need.amount * (1 - filled / quantity)
            )
            committed += max(ZERO, left)
        return committed, incoming

    async def _reference(self, pair: CurrencyPair) -> Decimal | None:
        """The pair's reference rate (quote per base), ``None`` when it cannot be read."""
        try:
            quote = await self._reader.quote(pair.conid, option=False)
        except ObliskError:
            return None
        return None if quote is None else quote.evidence.reference()

    def _rate(
        self, currency: str, cash: CashRead, pair: CurrencyPair, reference: Decimal | None
    ) -> Decimal | None:
        """One unit of ``currency`` in the base currency: the ledger's rate, else the pair's."""
        line = cash.line(currency)
        if line is not None and line.rate_to_base is not None:
            return line.rate_to_base
        if reference is None:
            return None
        return reference if currency == pair.base else 1 / reference

    # ── funding ──

    async def fund(self, intents: Sequence[OrderIntent]) -> FundingReport:
        """Fund the buys of ``intents`` in their currencies; report which may be placed now."""
        report = FundingReport()
        needs: dict[str, list[_Need]] = {}
        for intent in intents:
            need = self._need(intent)
            if need is None:
                report.ready.add(intent.intent_id)
            else:
                needs.setdefault(need.currency, []).append(need)
        if not needs:
            return report
        try:
            cash = await self._reader.cash()
        except ObliskError as exc:
            for group in needs.values():
                self._wait(report, group, f"the cash cannot be read: {exc}")
            return report
        for currency, group in sorted(needs.items()):
            await self._fund_currency(currency, group, cash, report)
        return report

    def _wait(self, report: FundingReport, group: Sequence[_Need], why: str) -> None:
        for need in group:
            report.waiting[need.intent.intent_id] = why

    async def _fund_currency(
        self, currency: str, group: list[_Need], cash: CashRead, report: FundingReport
    ) -> None:
        leg = self._leg(self._base, currency)
        if leg is None:
            self._wait(report, group, f"no currency pair joins {currency} and {self._base}")
            return
        pair = leg.pair
        line = cash.line(currency)
        reference = None
        if line is None or line.rate_to_base is None:
            reference = await self._reference(pair)
        rate = self._rate(currency, cash, pair, reference)
        if rate is None:
            self._wait(report, group, f"no rate for {currency}: no quote for {pair.symbol}")
            return
        committed, incoming = await self._open(currency, {n.intent.intent_id for n in group})
        held = ZERO if line is None else line.cash
        needed = sum((need.amount for need in group), ZERO)
        shortfall = needed + self._buffer / rate - (held - committed)
        if shortfall <= 0:
            report.ready.update(need.intent.intent_id for need in group)
            return
        if reference is None:
            reference = await self._reference(pair)
        if reference is None:
            self._wait(report, group, f"no quote for {pair.symbol}")
            return
        if incoming > 0:
            self._wait(report, group, f"a conversion into {currency} is still working")
            return
        mode = group[0].intent.mode
        if mode is ExecutionMode.SHADOW:
            self._wait(report, group, "shadow mode converts nothing (D52)")
            return
        limit = await self._limit(leg, reference)
        if limit is None:
            self._wait(report, group, f"no limit for {pair.symbol}: the rules give no tick")
            return
        least = leg.needed(shortfall, limit).to_integral_value(rounding=ROUND_CEILING)
        amount = least if self._capped_book else max(least, await self._room(currency, cash, rate))
        outcome = await self._convert(
            leg,
            amount=amount,
            limit=limit,
            sleeve=group[0].intent.sleeve,
            mode=mode,
            reason=f"funds {len(group)} buy(s) in {currency}",
            wait=True,
        )
        report.conversions.append(outcome)
        if outcome.status == "filled":
            report.ready.update(need.intent.intent_id for need in group)
        else:
            self._wait(report, group, f"the conversion into {currency} is {outcome.status}")

    async def _room(self, currency: str, cash: CashRead, rate: Decimal) -> Decimal:
        """The minimum conversion, in whole units of the base currency, as far as the base cash
        left after its commitments and floor and the single-foreign-currency cap allow."""
        base = cash.line(self._base)
        nav = cash.nav
        if base is None or nav is None:
            return ZERO
        committed, _ = await self._open(self._base, set())
        spare = base.cash - committed - self._floor
        line = cash.line(currency)
        held = ZERO if line is None else line.cash
        # The cap's own check also counts the positions quoted in the currency.
        room = min(self._minimum, spare, nav * self._cap - held * rate)
        return max(ZERO, room.to_integral_value(rounding=ROUND_FLOOR))

    # ── repatriation ──

    async def repatriate(self) -> list[ConversionOutcome]:
        """Bring home foreign cash no pending buy needs, beyond its buffer and commitments."""
        try:
            cash = await self._reader.cash()
        except ObliskError as exc:
            log.warning("no repatriation: the cash cannot be read", extra={"error": str(exc)})
            return []
        pending = await self._worker.run(repo.intents_in_states, _PENDING)
        busy: set[str] = set()
        for stored in pending:
            need = self._need(stored.intent)
            if need is not None:
                busy.add(need.currency)
            leg = conversion_leg(self._pairs, stored.intent.terms)
            if stored.intent.terms.conversion and leg is not None:
                busy |= {leg.source, leg.target}
        outcomes = []
        for line in cash.lines:
            currency = line.currency
            leg = self._leg(currency, self._base)
            if currency in busy or leg is None:
                continue  # the base currency has no leg home
            reference = await self._reference(leg.pair)
            rate = self._rate(currency, cash, leg.pair, reference)
            if rate is None or reference is None:
                continue
            committed, _ = await self._open(currency, set())
            excess = line.cash - committed - self._buffer / rate
            if excess * rate < self._repatriate:
                continue
            mode = await self._mode()
            if mode is None or mode is ExecutionMode.SHADOW:
                continue
            limit = await self._limit(leg, reference)
            if limit is None:
                continue
            outcomes.append(
                await self._convert(
                    leg,
                    amount=excess.to_integral_value(rounding=ROUND_FLOOR),
                    limit=limit,
                    sleeve=Sleeve.CORE,
                    mode=mode,
                    reason=f"brings {currency} home above its buffer",
                    wait=False,
                )
            )
        return outcomes

    async def _mode(self) -> ExecutionMode | None:
        try:
            return await self._modes.current()
        except Exception:  # the mode unreadable: nothing is converted
            return None

    # ── one conversion ──

    async def _leg_and_limit(self, source: str, target: str) -> tuple[ConversionLeg, Decimal] | str:
        leg = self._leg(source, target)
        if leg is None:
            return f"no currency pair converts {source} into {target}"
        reference = await self._reference(leg.pair)
        if reference is None:
            return f"no quote for {leg.pair.symbol}"
        limit = await self._limit(leg, reference)
        if limit is None:
            return f"the rules of {leg.pair.symbol} give no tick"
        return leg, limit

    async def conversion_terms(self, source: str, target: str, amount: Decimal) -> OrderTerms | str:
        """The terms a conversion of ``amount`` of ``source`` into ``target`` would carry (for a
        whatif); why there are none."""
        found = await self._leg_and_limit(source, target)
        if isinstance(found, str):
            return found
        leg, limit = found
        return self._terms(leg, amount, limit)

    async def convert(
        self, source: str, target: str, amount: Decimal, *, mode: ExecutionMode, reason: str
    ) -> ConversionOutcome:
        """Convert ``amount`` of ``source`` into ``target`` now, and wait for the fill."""
        found = await self._leg_and_limit(source, target)
        if isinstance(found, str):
            return self._failed(target, None, amount, found)
        leg, limit = found
        return await self._convert(
            leg, amount=amount, limit=limit, sleeve=Sleeve.CORE, mode=mode, reason=reason, wait=True
        )

    @staticmethod
    def _terms(leg: ConversionLeg, amount: Decimal, limit: Decimal) -> OrderTerms:
        return OrderTerms(
            conid=leg.pair.conid,
            side=leg.side,
            cash_quantity=amount,
            price=LimitPrice(limit=limit),
            tif=TimeInForce.DAY,
            venue=SMART_ROUTER,
            conversion=True,
        )

    async def _limit(self, leg: ConversionLeg, reference: Decimal) -> Decimal | None:
        """``limit_fraction`` through the reference against the conversion, on the pair's tick
        away from the reference (the collar bounds it), so it fills at once."""
        buying = leg.side is OrderSide.BUY
        raw = reference * (1 + self._limit_fraction if buying else 1 - self._limit_fraction)
        try:
            rules = await self._rules.rules(leg.pair.conid, is_buy=buying)
        except ObliskError:
            return None
        tick = tick_for(rules, reference)
        return None if tick is None else to_tick(raw, tick, up=buying)

    async def _convert(
        self,
        leg: ConversionLeg,
        *,
        amount: Decimal,
        limit: Decimal,
        sleeve: Sleeve,
        mode: ExecutionMode,
        reason: str,
        wait: bool,
    ) -> ConversionOutcome:
        """Convert ``amount`` of the leg's source at ``limit``: an intent the gate clears and the
        boundary places; with ``wait``, read until it fills or the timeout passes."""
        target_currency, source = leg.target, leg.source
        now = self._now()
        intent = new_intent(
            self._settings,
            sleeve=sleeve,
            role=IntentRole.CONVERSION,
            terms=self._terms(leg, amount, limit),
            reason=reason,
            mode=mode,
            approval_required=False,
            created_at=now,
            expires_at=now + self._ttl,
        )
        await self._book.add(intent)
        for step in (IntentState.APPROVED, IntentState.STAGED):
            await self._book.advance(intent.intent_id, step, reason)
        clearance = await self._gate.clear(intent)
        if isinstance(clearance, PretradeRefusal):
            await self._book.advance(intent.intent_id, IntentState.REJECTED, clearance.reason)
            return self._failed(target_currency, intent.intent_id, amount, clearance.reason)
        try:
            placement = await self._boundary.place(intent.intent_id, clearance)
        except ObliskError as exc:
            return self._failed(target_currency, intent.intent_id, amount, f"not placed: {exc}")
        if placement.status not in (PlacementStatus.PLACED, PlacementStatus.ALREADY_PLACED):
            return self._failed(
                target_currency,
                intent.intent_id,
                amount,
                f"{placement.status.value}: {placement.detail}",
            )
        if not wait or placement.order_id is None:
            return ConversionOutcome(
                target_currency, intent.intent_id, amount, "working", f"{source} {amount} sent"
            )
        return await self._await_fill(target_currency, intent.intent_id, placement.order_id, amount)

    async def _await_fill(
        self, currency: str, intent_id: UUID, order_id: str, amount: Decimal
    ) -> ConversionOutcome:
        waited = 0.0
        while True:
            try:
                order = await self._boundary.sync_order(order_id)
            except ObliskError as exc:
                order = None
                log.warning("a conversion's status cannot be read", extra={"error": str(exc)})
            if order is not None and not is_live(order):
                state = (await self._book.get(intent_id)).intent.state
                if state is IntentState.FILLED:
                    try:
                        await self._worker.run(repo.upsert_fills, list(await self._reader.trades()))
                    except ObliskError as exc:
                        log.warning(
                            "the conversion's trades cannot be read", extra={"error": str(exc)}
                        )
                    return ConversionOutcome(currency, intent_id, amount, "filled", order.status)
                return self._failed(currency, intent_id, amount, f"the order ended {order.status}")
            if waited >= self._timeout:
                return ConversionOutcome(
                    currency, intent_id, amount, "working", f"not filled after {waited:.0f} s"
                )
            await self._sleep(self._poll)
            waited += self._poll

    def _failed(
        self, currency: str, intent_id: UUID | None, amount: Decimal, detail: str
    ) -> ConversionOutcome:
        raise_alert(
            self._alert,
            OrderAlert(
                kind=OrderAlertKind.CONVERSION_FAILED,
                intent_id=intent_id,
                detail=f"a conversion into {currency} failed: {detail}",
                at=self._now(),
            ),
        )
        return ConversionOutcome(currency, intent_id, amount, "failed", detail)
